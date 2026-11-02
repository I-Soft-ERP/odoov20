from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class OvertimeReview(models.Model):
    _name = 'sb.attendance.overtime.review'
    _description = 'Weekly Management Overtime Review'
    _order = 'date_from desc, id desc'

    name = fields.Char(default='New', required=True, readonly=True)
    date_from = fields.Date(string='Week Starting', required=True,
                            default=lambda self: fields.Date.context_today(self) -
                            timedelta(days=fields.Date.context_today(self).weekday()))
    date_to = fields.Date(compute='_compute_end', string='Week Ending')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    team_id = fields.Many2one('sb.attendance.team', string='Team (Optional)')
    line_ids = fields.One2many('sb.attendance.overtime.line', 'review_id', readonly=True)
    team_approval_ids = fields.One2many('sb.attendance.overtime.team.approval', 'review_id', readonly=True)
    pending_count = fields.Integer(string='Unfinalised Registers', readonly=True)
    review_pending_count = fields.Integer(string='Awaiting Decision', compute='_compute_review_pending')
    candidate_hours = fields.Float(readonly=True, digits=(12, 2))
    approved_hours = fields.Float(compute='_compute_totals', digits=(12, 2))
    note = fields.Text(readonly=True, default='Only finalised attendance can be approved. Public holidays require statutory pay assessment; multiplier 0 means not calculated. This review does not create payroll entries.')

    @api.depends('date_from')
    def _compute_end(self):
        for rec in self:
            rec.date_to = rec.date_from + timedelta(days=6) if rec.date_from else False

    @api.depends('team_approval_ids.decision')
    def _compute_review_pending(self):
        for rec in self:
            rec.review_pending_count = len(rec.team_approval_ids.filtered(lambda line: line.decision == 'pending'))

    @api.depends('line_ids.decision', 'line_ids.approved_hours')
    def _compute_totals(self):
        for rec in self:
            rec.approved_hours = sum(l.approved_hours for l in rec.line_ids if l.decision == 'approved')

    def _check_admin(self):
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only attendance administrators may review overtime.'))

    def action_refresh(self):
        self.ensure_one()
        self._check_admin()
        if not self.date_from:
            raise UserError(_('Select the week starting date.'))
        end = self.date_from + timedelta(days=6)
        domain = [('date', '>=', self.date_from), ('date', '<=', end),
                  ('company_id', '=', self.company_id.id)]
        if self.team_id:
            domain.append(('team_id', '=', self.team_id.id))
        registers = self.env['sb.attendance.register'].search(domain)
        if self.team_approval_ids.filtered(lambda line: line.decision != 'pending') or self.line_ids.filtered(lambda line: line.decision != 'pending'):
            raise UserError(_('This review already contains management decisions. Create a new review instead of refreshing it.'))
        # Keep decisions only when the attendance snapshot is unchanged.
        existing = {(l.employee_id.id, l.work_date): l for l in self.line_ids}
        data = {}
        for reg in registers:
            for att in reg.line_ids.filtered(lambda x: x.employee_id and x.status == 'present'):
                key = (att.employee_id.id, reg.date)
                row = data.setdefault(key, {'employee_id': att.employee_id.id,
                                            'team_id': reg.team_id.id,
                                            'work_date': reg.date, 'hours': 0.0,
                                            'registers': [], 'final': True})
                row['hours'] += att.net_hours
                row['registers'].append(reg.name)
                if reg.state != 'final':
                    row['final'] = False
        commands = [fields.Command.clear()]
        groups = {}
        total = 0.0
        for key, row in sorted(data.items(), key=lambda item: (item[0][1], item[0][0])):
            date = row['work_date']
            prior = existing.get(key)
            holiday = self.env['sb.attendance.public.holiday']._is_holiday(date)
            category = 'holiday' if holiday else ('saturday' if date.weekday() == 5 else 'sunday' if date.weekday() == 6 else 'weekday')
            hours = round(row['hours'], 2)
            rules = self.env['sb.attendance.overtime.rules'].search([('company_id', '=', self.company_id.id)], limit=1)
            if not rules:
                raise UserError(_('Configure an Overtime Rules profile for this company first.'))
            if category == 'weekday':
                # Attendance time values are decimal local hours. Net overtime
                # excludes the portion of unpaid breaks attributable to after-hours.
                candidate = 0.0
                for reg in registers.filtered(lambda r: r.date == date):
                    for att in reg.line_ids.filtered(lambda l: l.employee_id.id == row['employee_id'] and l.status == 'present'):
                        gross_after = max(0.0, att.finish_time - max(att.start_time, rules.weekday_start))
                        gross_before = max(0.0, min(att.finish_time, rules.weekday_start) - att.start_time)
                        # Standard Sambros lunch is taken before the overtime
                        # threshold unless actual break times are captured.
                        # Allocate unpaid lunch to ordinary time first. Any lunch
                        # exceeding the available pre-threshold time reduces
                        # overtime, so an unusually late start cannot create
                        # more paid hours than the employee actually worked.
                        unpaid_lunch = max(0.0, att.lunch_hours or 0.0)
                        lunch_after = max(0.0, unpaid_lunch - gross_before)
                        candidate += max(0.0, gross_after - lunch_after)
                candidate = round(candidate, 2)
            else:
                candidate = hours
            if candidate <= 0.0:
                continue
            total += candidate
            group_key = (row['team_id'], date)
            group = groups.setdefault(group_key, {'team_id': row['team_id'], 'work_date': date,
                'employee_count': 0, 'candidate_hours': 0.0, 'is_finalised': True})
            group['employee_count'] += 1
            group['candidate_hours'] += candidate
            group['is_finalised'] = group['is_finalised'] and row['final']
            # Only retain a decision when its source and classification are identical.
            same = (prior and abs(prior.net_hours - hours) < 0.001 and
                    prior.category == category and prior.register_refs == ', '.join(row['registers']) and
                    prior.is_finalised == row['final'])
            decision = prior.decision if same else 'pending'
            if not row['final']:
                decision = 'pending'
            # Stage A: illustrative statutory public-holiday calculation only.
            # Use the employee calendar when present; otherwise the Sambros M-F fallback.
            holiday_vals = {}
            if holiday:
                employee = self.env['hr.employee'].browse(row['employee_id'])
                calendar = employee.resource_calendar_id
                if calendar:
                    # Odoo 20 calendars may omit date_from/date_to on
                    # resource.calendar.attendance. Check optional fields
                    # before accessing them, while respecting date bounds
                    # on versions where they are present.
                    def _applies_on_date(attendance):
                        if attendance.dayofweek != str(date.weekday()):
                            return False
                        if 'date_from' in attendance._fields and attendance.date_from and attendance.date_from > date:
                            return False
                        if 'date_to' in attendance._fields and attendance.date_to and attendance.date_to < date:
                            return False
                        return True

                    scheduled = calendar.attendance_ids.filtered(_applies_on_date)
                    day_hours = sum(max(0.0, a.hour_to - a.hour_from) for a in scheduled)
                    calendar_source = 'employee_calendar'
                else:
                    day_hours = 8.0 if date.weekday() < 5 else 0.0
                    calendar_source = 'standard_fallback'
                ordinary_day = day_hours > 0
                rate = rules.illustrative_hourly_rate
                daily_wage = day_hours * rate if ordinary_day else 8.0 * rate
                worked_pay = hours * rate
                # BCEA s18: normal day => greater of twice daily wage or
                # daily wage plus pay for time worked; otherwise daily wage
                # plus pay for time worked. This is a simulation, not payroll.
                statutory_total = (max(2 * daily_wage, daily_wage + worked_pay)
                                   if ordinary_day else daily_wage + worked_pay)
                already_in_salary = daily_wage if ordinary_day else 0.0
                holiday_vals = {
                    'ordinary_workday': ordinary_day,
                    'calendar_source': calendar_source,
                    'scheduled_day_hours': day_hours,
                    'test_hourly_rate': rate,
                    'holiday_total_pay': round(statutory_total, 2),
                    'holiday_additional_pay': round(max(0.0, statutory_total - already_in_salary), 2),
                }
            commands.append(fields.Command.create({
                **holiday_vals,
                'employee_id': row['employee_id'], 'team_id': row['team_id'],
                'work_date': date, 'net_hours': hours,
                'category': category, 'is_public_holiday': holiday,
                'candidate_hours': candidate, 'rate_multiplier': (0.0 if category == 'holiday' else rules.saturday_rate if category == 'saturday' else rules.sunday_rate if category == 'sunday' else rules.weekday_rate), 'is_finalised': row['final'],
                'register_refs': ', '.join(row['registers']),
                'decision': decision,
                'approved_hours': prior.approved_hours if same and decision == 'approved' else 0.0,
                'reason': prior.reason if same else False,
            }))
        self.write({
            'name': 'Overtime Review — %s to %s' % (self.date_from, end),
            'line_ids': commands,
            'team_approval_ids': [fields.Command.clear()] + [fields.Command.create(v) for v in groups.values()],
            'candidate_hours': round(total, 2),
            'pending_count': len(registers.filtered(lambda r: r.state != 'final')),
        })
        return {'type': 'ir.actions.act_window', 'res_model': self._name,
                'res_id': self.id, 'view_mode': 'form', 'target': 'current'}

    def write(self, vals):
        if 'line_ids' in vals or 'team_approval_ids' in vals:
            self._check_admin()
        return super().write(vals)


class OvertimeLine(models.Model):
    _name = 'sb.attendance.overtime.line'
    _description = 'Daily Overtime Management Decision'
    _order = 'work_date, team_id, employee_id'

    review_id = fields.Many2one('sb.attendance.overtime.review', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='review_id.company_id', store=True)
    employee_id = fields.Many2one('hr.employee', readonly=True, required=True)
    team_id = fields.Many2one('sb.attendance.team', readonly=True)
    work_date = fields.Date(readonly=True)
    net_hours = fields.Float(readonly=True, digits=(12, 2))
    category = fields.Selection([('weekday', 'Weekday'), ('saturday', 'Saturday'),
                                 ('sunday', 'Sunday'), ('holiday', 'Public Holiday')], readonly=True)
    is_public_holiday = fields.Boolean(string='Public Holiday', readonly=True)
    ordinary_workday = fields.Boolean(string='Ordinary Working Day', readonly=True)
    calendar_source = fields.Selection([('employee_calendar', 'Employee Calendar'), ('standard_fallback', 'Sambros Default')], readonly=True)
    scheduled_day_hours = fields.Float(string='Scheduled Day Hours', readonly=True, digits=(12, 2))
    test_hourly_rate = fields.Float(string='Illustrative Rate (R/h)', readonly=True, digits=(12, 2))
    holiday_total_pay = fields.Float(string='Illustrative Statutory Total (R)', readonly=True, digits=(12, 2))
    holiday_additional_pay = fields.Float(string='Illustrative Additional Due (R)', readonly=True, digits=(12, 2))

    candidate_hours = fields.Float(readonly=True, digits=(12, 2))
    rate_multiplier = fields.Float(readonly=True, digits=(4, 2), string='Indicative Multiplier')
    is_finalised = fields.Boolean(string='Attendance Finalised', readonly=True)
    register_refs = fields.Char(string='Source Registers', readonly=True)
    decision = fields.Selection([('pending', 'Pending'), ('approved', 'Approved'),
                                 ('rejected', 'Rejected')], default='pending', required=True)
    approved_hours = fields.Float(digits=(12, 2))
    reason = fields.Text(string='Decision Reason')
    reviewed_by = fields.Many2one('res.users', readonly=True)
    reviewed_on = fields.Datetime(readonly=True)

    def _check_admin(self):
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only attendance administrators may decide overtime.'))

    def write(self, vals):
        self._check_admin()
        if ('decision' in vals or 'approved_hours' in vals) and not self.env.context.get('sb_team_overtime_decision'):
            raise UserError(_('Overtime is approved by team and date. Open the team approval instead.'))
        if 'is_public_holiday' in vals:
            raise UserError(_('Public holidays are synchronised automatically; manual changes are disabled.'))
        if 'decision' in vals:
            if len(self) != 1:
                raise UserError(_('Review one overtime line at a time.'))
            if vals['decision'] == 'approved':
                if not self.is_finalised:
                    raise UserError(_('Finalise attendance before approving overtime.'))
                # Recheck the live attendance before accepting a decision.
                regs = self.env['sb.attendance.register'].search([
                    ('date', '=', self.work_date),
                    ('company_id', '=', self.company_id.id),
                    ('line_ids.employee_id', '=', self.employee_id.id)])
                actual = sum(l.net_hours for r in regs for l in r.line_ids
                             if l.employee_id == self.employee_id and l.status == 'present')
                if any(r.state != 'final' for r in regs) or abs(actual - self.net_hours) > 0.001:
                    raise UserError(_('Attendance has changed or is not finalised. Refresh the review before approval.'))
                if not self.candidate_hours:
                    raise UserError(_('No potential overtime exists for this day.'))
                if not vals.get('reason', self.reason):
                    raise UserError(_('Provide a reason for approval.'))
                approved = vals.get('approved_hours', self.approved_hours)
                if approved <= 0 or approved > self.candidate_hours:
                    raise UserError(_('Approved hours must be greater than zero and no more than potential overtime.'))
            if vals['decision'] == 'rejected' and not vals.get('reason', self.reason):
                raise UserError(_('Provide a reason for rejection.'))
            vals['reviewed_by'] = self.env.user.id
            vals['reviewed_on'] = fields.Datetime.now()
        return super().write(vals)

    @api.constrains('approved_hours', 'candidate_hours', 'decision')
    def _validate_approval(self):
        for rec in self:
            if rec.decision == 'approved' and (rec.approved_hours <= 0 or rec.approved_hours > rec.candidate_hours + 0.0001):
                raise ValidationError(_('Approved hours must be within the potential overtime limit.'))


class OvertimeTeamApproval(models.Model):
    _name = 'sb.attendance.overtime.team.approval'
    _description = 'Team Daily Overtime Approval'
    _order = 'work_date, team_id'

    review_id = fields.Many2one('sb.attendance.overtime.review', required=True, ondelete='cascade')
    team_id = fields.Many2one('sb.attendance.team', required=True, readonly=True)
    work_date = fields.Date(required=True, readonly=True)
    employee_count = fields.Integer(readonly=True)
    candidate_hours = fields.Float(readonly=True, digits=(12, 2))
    rate_multiplier = fields.Float(readonly=True, digits=(4, 2), string='Indicative Multiplier')
    is_finalised = fields.Boolean(string='Attendance Finalised', readonly=True)
    decision = fields.Selection([('pending', 'Pending'), ('approved', 'Approved'),
                                 ('rejected', 'Rejected')], default='pending', readonly=True)
    approved_hours = fields.Float(readonly=True, digits=(12, 2))
    reviewed_by = fields.Many2one('res.users', readonly=True)
    reviewed_on = fields.Datetime(readonly=True)
    reason = fields.Char(string='Management Note')

    def _decide(self, decision):
        self.ensure_one()
        self.review_id._check_admin()
        if self.decision != 'pending':
            raise UserError(_('This team/day has already been decided.'))
        lines = self.review_id.line_ids.filtered(
            lambda l: l.team_id == self.team_id and l.work_date == self.work_date)
        if not lines:
            raise UserError(_('No overtime candidates exist for this team and date.'))
        # Compare against the current underlying attendance, not just a stored checkbox.
        regs = self.env['sb.attendance.register'].search([
            ('date', '=', self.work_date), ('team_id', '=', self.team_id.id),
            ('company_id', '=', self.review_id.company_id.id)])
        if not regs or any(r.state != 'final' for r in regs):
            raise UserError(_('All team attendance registers for this date must be finalised.'))
        current = {}
        for reg in regs:
            for att in reg.line_ids.filtered(lambda a: a.employee_id and a.status == 'present'):
                current[att.employee_id.id] = current.get(att.employee_id.id, 0.0) + att.net_hours
        for line in lines:
            if not line.is_finalised or abs(current.get(line.employee_id.id, 0.0) - line.net_hours) > 0.001:
                raise UserError(_('Attendance has changed. Generate a fresh review before approving.'))
        if decision == 'approved' and not self.is_finalised:
            raise UserError(_('The overtime review must reflect finalised attendance. Refresh first.'))
        if not self.reason:
            raise UserError(_('Enter one management note for the team decision.'))
        for line in lines:
            line.with_context(sb_team_overtime_decision=True).write({
                'decision': decision,
                'approved_hours': line.candidate_hours if decision == 'approved' else 0.0,
                'reason': self.reason})
        self.write({'decision': decision,
                    'approved_hours': sum(lines.mapped('candidate_hours')) if decision == 'approved' else 0.0,
                    'reviewed_by': self.env.user.id, 'reviewed_on': fields.Datetime.now()})
        return {'type': 'ir.actions.act_window', 'res_model': self.review_id._name,
                'res_id': self.review_id.id, 'view_mode': 'form', 'target': 'current'}

    def action_approve_team(self):
        return self._decide('approved')

    def action_reject_team(self):
        return self._decide('rejected')
