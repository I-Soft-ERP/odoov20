from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

class Team(models.Model):
    _name='sb.attendance.team'
    _description='Attendance Team'
    name=fields.Char(required=True)
    foreman_id=fields.Many2one('hr.employee',required=True)
    member_ids=fields.Many2many('hr.employee',string='Legacy Team Members')
    membership_ids=fields.One2many('sb.attendance.team.member','team_id',string='Team Members')
    company_id=fields.Many2one('res.company',default=lambda s:s.env.company,required=True)
    active=fields.Boolean(default=True)
    dashboard_date = fields.Date(string='Attendance Date', compute='_compute_dashboard')
    dashboard_present = fields.Integer(string='Present', compute='_compute_dashboard')
    dashboard_absent = fields.Integer(string='Absent', compute='_compute_dashboard')
    dashboard_prevented = fields.Integer(string='Prevented', compute='_compute_dashboard')
    dashboard_status = fields.Selection([
        ('missing', 'Outstanding'), ('not_scheduled', 'Not Scheduled'), ('draft', 'Open'), ('closing', 'Closing'),
        ('submitted', 'Submitted'), ('final', 'Finalised')],
        string='Register Status', compute='_compute_dashboard')
    dashboard_register_id = fields.Many2one('sb.attendance.register', compute='_compute_dashboard')
    dashboard_team_size = fields.Integer(string='Team Size', compute='_compute_dashboard')
    dashboard_approved_ot = fields.Float(string='Approved Overtime', compute='_compute_dashboard', digits=(12, 2))
    dashboard_pending_ot = fields.Float(string='Pending Overtime', compute='_compute_dashboard', digits=(12, 2))
    dashboard_ot_state = fields.Selection([
        ('none', 'No Overtime'), ('pending', 'Pending Review'),
        ('approved', 'Approved'), ('mixed', 'Approved + Pending')],
        compute='_compute_dashboard', string='Overtime Status')

    def _is_normal_workday(self, day):
        self.ensure_one()
        # Public holidays override the ordinary working calendar. Cached dates
        # are reused; absence of an entire year is not proof of a non-holiday.
        Holiday = self.env['sb.attendance.public.holiday'].sudo()
        if not Holiday.search_count([('year', '=', day.year)]):
            # Fall back to weekday rules until holiday synchronisation is complete.
            # Back-office must verify special holidays in the holiday calendar.
            pass
        elif Holiday.search_count([('holiday_date', '=', day)]):
            return False
        calendar = self.company_id.resource_calendar_id
        if calendar:
            return any(int(a.dayofweek) == day.weekday() for a in calendar.attendance_ids)
        return day.weekday() < 5

    def _expects_register(self, day):
        self.ensure_one()
        if self._is_normal_workday(day):
            return True
        return bool(self.env['sb.attendance.work.expectation'].sudo().search_count([
            ('team_id', '=', self.id), ('date', '=', day)]))

    def _dashboard_selected_date(self):
        raw = self.env.context.get('sb_dashboard_date')
        try:
            return fields.Date.to_date(raw) if raw else fields.Date.context_today(self)
        except (TypeError, ValueError):
            return fields.Date.context_today(self)

    def _compute_dashboard(self):
        """Selected-date team snapshot; approved OT comes exclusively from management decisions.

        The overtime review models are admin-only. Only aggregate hour counts are
        exposed here, never individual approval records or employee details.
        """
        day = self._dashboard_selected_date()
        registers = self.env['sb.attendance.register'].search([
            ('date', '=', day), ('team_id', 'in', self.ids),
            ('company_id', 'in', self.env.companies.ids)])
        by_team = {reg.team_id.id: reg for reg in registers}
        # Approval snapshots may be refreshed by management. Take the latest
        # review's decision for each employee/day; never sum duplicate reviews.
        ot_lines = self.env['sb.attendance.overtime.line'].sudo().search([
            ('work_date', '=', day), ('team_id', 'in', self.ids),
            ('company_id', 'in', self.env.companies.ids)],
            order='review_id desc, id desc')
        ot_by_team = {}
        seen = set()
        for line in ot_lines:
            key = (line.team_id.id, line.employee_id.id, line.work_date)
            if key in seen:
                continue
            seen.add(key)
            bucket = ot_by_team.setdefault(line.team_id.id, {'approved': 0.0, 'pending': 0.0})
            if line.decision == 'approved' and line.is_finalised:
                bucket['approved'] += line.approved_hours
            elif line.decision == 'pending':
                bucket['pending'] += line.candidate_hours
        for team in self:
            reg = by_team.get(team.id)
            team.dashboard_date = day
            team.dashboard_register_id = reg or False
            team.dashboard_status = reg.state if reg else ('missing' if team._expects_register(day) else 'not_scheduled')
            team.dashboard_present = len(reg.line_ids.filtered(lambda l: l.status == 'present')) if reg else 0
            team.dashboard_absent = len(reg.line_ids.filtered(lambda l: l.status == 'absent')) if reg else 0
            team.dashboard_prevented = len(reg.line_ids.filtered(lambda l: l.status == 'prevented')) if reg else 0
            team.dashboard_team_size = len(team._members_on(day))
            bucket = ot_by_team.get(team.id, {})
            # Do not display approvals on registers that have since been reopened.
            team.dashboard_approved_ot = round(bucket.get('approved', 0.0), 2) if reg and reg.state == 'final' else 0.0
            team.dashboard_pending_ot = round(bucket.get('pending', 0.0), 2)
            if team.dashboard_approved_ot and team.dashboard_pending_ot:
                team.dashboard_ot_state = 'mixed'
            elif team.dashboard_approved_ot:
                team.dashboard_ot_state = 'approved'
            elif team.dashboard_pending_ot:
                team.dashboard_ot_state = 'pending'
            else:
                team.dashboard_ot_state = 'none'

    def action_open_dashboard_overtime(self):
        """Open management review for selected-date team without granting review access."""
        self.ensure_one()
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only attendance managers may access overtime approvals.'))
        day = self._dashboard_selected_date()
        week_start = day - timedelta(days=day.weekday())
        return {
            'type': 'ir.actions.act_window',
            'name': _('Overtime Review — %s') % self.name,
            'res_model': 'sb.attendance.overtime.review',
            'view_mode': 'list,form',
            'domain': [('company_id', '=', self.company_id.id),
                       '|', ('team_id', '=', self.id), ('team_id', '=', False),
                       ('date_from', '=', week_start)],
            'context': {'default_team_id': self.id, 'default_date_from': week_start,
                        'default_company_id': self.company_id.id},
            'target': 'current',
        }

    def action_open_dashboard_register(self):
        self.ensure_one()
        day = self._dashboard_selected_date()
        reg = self.env['sb.attendance.register'].search([
            ('team_id', '=', self.id), ('date', '=', day),
            ('company_id', '=', self.company_id.id)], limit=1)
        if reg:
            return {'type': 'ir.actions.act_window', 'name': 'Team Attendance',
                    'res_model': 'sb.attendance.register', 'view_mode': 'form',
                    'res_id': reg.id, 'target': 'current'}
        return {'type': 'ir.actions.act_window', 'name': 'Team Attendance',
                'res_model': 'sb.attendance.register', 'view_mode': 'list,form',
                'domain': [('team_id', '=', self.id), ('date', '=', day)],
                'context': {'default_team_id': self.id, 'default_date': day},
                'target': 'current'}

    def _members_on(self, day):
        self.ensure_one()
        # Attendance needs only membership IDs and active status. Restricted foremen
        # must not trigger Odoo's hr.employee -> hr.employee.public fallback,
        # which can attempt to read private fields added by EPW payroll.
        # Sudo is limited to the server-side membership lookup; the caller's
        # team access is checked separately before exposing any attendance.
        team = self.sudo()
        members = team.membership_ids.filtered(
            lambda m: m.employee_id.active and m.date_from <= day
            and (not m.date_to or m.date_to >= day)
        ).mapped('employee_id')
        # The assigned foreman is an operational team member too.  Use recordset
        # union to avoid counting the foreman twice when already assigned via
        # effective-dated or legacy membership.
        members |= team.member_ids.filtered('active')
        if team.foreman_id.active:
            members |= team.foreman_id
        return members

class TeamMember(models.Model):
    _name='sb.attendance.team.member'
    _description='Attendance Team Membership'
    _order='date_from desc, id desc'
    team_id=fields.Many2one('sb.attendance.team',required=True,ondelete='cascade')
    employee_id=fields.Many2one('hr.employee',required=True)
    date_from=fields.Date(string='From',required=True,default=fields.Date.context_today)
    date_to=fields.Date(string='To')

    @api.constrains('date_from','date_to')
    def _check_dates(self):
        for rec in self:
            if rec.date_to and rec.date_to < rec.date_from:
                raise ValidationError(_('Membership end date cannot precede its start date.'))


class Register(models.Model):
    _name='sb.attendance.register'
    _description='Daily Attendance Register'
    _inherit=['mail.thread','mail.activity.mixin']
    _order='date desc,id desc'
    name=fields.Char(default='New',readonly=True,copy=False)
    date=fields.Date(required=True,default=fields.Date.context_today)
    company_id=fields.Many2one('res.company',required=True,default=lambda s:s.env.company)
    team_id=fields.Many2one('sb.attendance.team',required=True)
    foreman_id=fields.Many2one('hr.employee',related='team_id.foreman_id',store=True)
    start_time=fields.Float(string='Header Start',required=True,default=lambda s:s._default_hour('sb_ops_standard_start',7.5))
    finish_time=fields.Float(string='Header Finish',default=lambda s:s._default_hour('sb_ops_standard_finish',16.5))
    job_description=fields.Char(string='Job / Site (Optional)')
    job_reference=fields.Char(string='Existing Job Reference (Optional)')
    disruption=fields.Selection([('none','None'),('protest','Protest / Civil Unrest'),('access','Site Access Restricted'),('client','Client Stoppage'),('weather','Weather'),('other','Other')],default='none')
    disruption_notes=fields.Text()
    state=fields.Selection([('draft','Open'),('closing','Closing'),('submitted','Submitted'),('final','Finalised')],default='draft',required=True,tracking=True)
    line_ids=fields.One2many('sb.attendance.line','register_id',string='Employees',copy=True)
    submitted_by=fields.Many2one('res.users',readonly=True)
    submitted_at=fields.Datetime(readonly=True)
    finalised_by=fields.Many2one('res.users',readonly=True)
    finalised_at=fields.Datetime(readonly=True)
    _unique_team_date = models.Constraint('UNIQUE(team_id, date, company_id)', 'A register already exists for this team and date.')

    @api.model
    def _default_hour(self,key,fallback):
        company=self.env.company
        return company[key] if key in company._fields else fallback

    @api.onchange('team_id','date')
    def _onchange_team_date(self):
        for r in self:
            if r.state != 'draft' or not r.team_id:
                continue
            day=r.date or fields.Date.context_today(r)
            # Warn immediately, before auto-populating employee lines.
            conflict = r._find_team_date_conflict(r.team_id.id, day, r.company_id.id, r.id)
            if conflict:
                r.line_ids = [fields.Command.clear()]
                return {'warning': {
                    'title': _('Attendance register already exists'),
                    'message': _(
                        'A daily attendance register already exists for team %s on %s: %s. '
                        'Please open the existing register instead.'
                    ) % (r.team_id.name, day, conflict.name),
                    'type': 'notification',
                }}
            if not r._is_attendance_admin() and r.team_id.sudo().foreman_id.user_id.id != r.env.uid:
                raise UserError(_('You may only load employees for your assigned team.'))
            members=r.team_id._members_on(day)
            existing={l.employee_id.id: l for l in r.line_ids if l.employee_id}
            # Retain manual additions and existing employee edits. Only add missing members.
            commands=[fields.Command.create({'employee_id': e.id, 'start_time': r.start_time, 'status': 'present'}) for e in members if e.id not in existing]
            if commands:
                r.line_ids = commands if not r.line_ids else [fields.Command.link(l.id) for l in r.line_ids if l.id] + [fields.Command.create({'employee_id':l.employee_id.id,'start_time':l.start_time,'finish_time':l.finish_time,'status':l.status,'notes':l.notes}) for l in r.line_ids if not l.id and l.employee_id] + commands

    def _is_attendance_admin(self):
        return self.env.su or self.env.user.has_group('sb_daily_attendance.group_attendance_admin')

    def _check_foreman_access(self):
        if self._is_attendance_admin():
            return
        for register in self:
            if register.team_id.sudo().foreman_id.user_id.id != self.env.uid:
                raise UserError(_('You may only manage attendance for your assigned teams.'))

    def _find_team_date_conflict(self, team_id, day, company_id, exclude_id=False):
        domain = [
            ('team_id', '=', team_id),
            ('date', '=', day),
            ('company_id', '=', company_id or self.env.company.id),
        ]
        if exclude_id and isinstance(exclude_id, int):
            domain.append(('id', '!=', exclude_id))
        return self.sudo().search(domain, limit=1)

    def _check_unique_daily_register(self):
        for register in self:
            if not register.team_id or not register.date:
                continue
            conflict = register._find_team_date_conflict(
                register.team_id.id, register.date, register.company_id.id, register.id
            )
            if conflict:
                raise ValidationError(_(
                    'A daily attendance register already exists for team %s on %s: %s. '
                    'Please open the existing register instead.'
                ) % (register.team_id.name, register.date, conflict.name))

    @api.constrains('team_id', 'date', 'company_id')
    def _constrain_unique_daily_register(self):
        self._check_unique_daily_register()

    def _sync_pending_overtime_reviews(self):
        """Rebuild undecided weekly snapshots after source attendance changes.

        Reviews containing management decisions remain untouched and require
        explicit administrative reconciliation, preserving their audit history.
        """
        if self.env.context.get('sb_skip_overtime_sync'):
            return
        Review = self.env['sb.attendance.overtime.review'].sudo()
        for company_id, date, team_id in set(
                (r.company_id.id, r.date, r.team_id.id) for r in self
                if r.company_id and r.date and r.team_id):
            week_start = date - timedelta(days=date.weekday())
            reviews = Review.search([
                ('company_id', '=', company_id), ('date_from', '=', week_start),
                '|', ('team_id', '=', False), ('team_id', '=', team_id)])
            for review in reviews:
                if (review.line_ids.filtered(lambda l: l.decision != 'pending') or
                        review.team_approval_ids.filtered(lambda l: l.decision != 'pending')):
                    continue
                review.with_context(sb_skip_overtime_sync=True).action_refresh()

    def write(self, vals):
        self._check_foreman_access()
        if not self._is_attendance_admin():
            if any(r.state in ('submitted', 'final') for r in self):
                raise UserError(_('Submitted and finalised registers are locked for foremen.'))
            if 'state' in vals and vals['state'] in ('submitted', 'final') and not self.env.context.get('attendance_workflow'):
                raise UserError(_('Use the attendance workflow buttons to change the register state.'))
            if any(key in vals for key in ('team_id', 'foreman_id', 'company_id')):
                raise UserError(_('Only an attendance administrator may reassign a register.'))
        old_sources = self.env['sb.attendance.register'].browse(self.ids)
        old_keys = [(r.company_id.id, r.date, r.team_id.id) for r in old_sources]
        result = super().write(vals)
        if any(k in vals for k in ('date', 'team_id', 'company_id')):
            self._check_unique_daily_register()
            self.mapped('line_ids')._check_unique_daily_employee()
        if not self.env.context.get('sb_skip_overtime_sync'):
            self._sync_pending_overtime_reviews()
            if any(k in vals for k in ('date', 'team_id', 'company_id')):
                # Refresh reviews associated with the former team/date too.
                for company_id, day, team_id in old_keys:
                    if company_id and day and team_id:
                        old_week = day - timedelta(days=day.weekday())
                        reviews = self.env['sb.attendance.overtime.review'].sudo().search([
                            ('company_id', '=', company_id), ('date_from', '=', old_week),
                            '|', ('team_id', '=', False), ('team_id', '=', team_id)])
                        for review in reviews:
                            if not (review.line_ids.filtered(lambda l: l.decision != 'pending') or
                                    review.team_approval_ids.filtered(lambda l: l.decision != 'pending')):
                                review.with_context(sb_skip_overtime_sync=True).action_refresh()
        return result

    @api.model_create_multi
    def create(self,vals_list):
        if not (self.env.su or self.env.user.has_group('sb_daily_attendance.group_attendance_admin')):
            for vals in vals_list:
                team = self.env['sb.attendance.team'].browse(vals.get('team_id'))
                if not team.exists() or team.sudo().foreman_id.user_id.id != self.env.uid:
                    raise UserError(_('You may only create attendance for your assigned team.'))
                if vals.get('state', 'draft') != 'draft':
                    raise UserError(_('New attendance registers must start Open.'))
        # Validate the register itself before Odoo creates employee lines.
        # This ensures team/date conflicts take precedence over employee conflicts.
        for vals in vals_list:
            team_id = vals.get('team_id')
            day = fields.Date.to_date(vals.get('date') or fields.Date.context_today(self))
            company_id = vals.get('company_id') or self.env.company.id
            if team_id:
                conflict = self._find_team_date_conflict(team_id, day, company_id)
                if conflict:
                    team = self.env['sb.attendance.team'].browse(team_id)
                    raise ValidationError(_(
                        'A daily attendance register already exists for team %s on %s: %s. '
                        'Please open the existing register instead.'
                    ) % (team.name, day, conflict.name))
        for vals in vals_list:
            if vals.get('name','New')=='New':
                vals['name']=self.env['ir.sequence'].next_by_code('sb.attendance.register') or 'ATT-New'
            if not vals.get('line_ids') and vals.get('team_id'):
                team=self.env['sb.attendance.team'].browse(vals['team_id'])
                start=vals.get('start_time',self._default_hour('sb_ops_standard_start',7.5))
                day=fields.Date.to_date(vals.get('date') or fields.Date.context_today(self))
                vals['line_ids']=[(0,0,{'employee_id':e.id,'start_time':start,'status':'present'}) for e in team._members_on(day)]
        records = super().create(vals_list)
        records._check_unique_daily_register()
        records.mapped('line_ids')._check_unique_daily_employee()
        records._sync_pending_overtime_reviews()
        return records

    def action_apply_start(self):
        for r in self:
            if r.state!='draft': raise UserError(_('Start time can only be applied while open.'))
            r.line_ids.filtered(lambda l:l.status=='present' and not l.start_override).with_context(attendance_bulk_update=True).write({'start_time':r.start_time})

    def action_close(self):
        for r in self:
            if r.state!='draft': raise UserError(_('Only open registers can be closed.'))
            r.state='closing'
            r.line_ids.filtered(lambda l:l.status=='present' and not l.finish_override).with_context(attendance_bulk_update=True).write({'finish_time':r.finish_time})

    def action_apply_finish(self):
        for r in self:
            if r.state!='closing': raise UserError(_('Finish time can only be applied during closing.'))
            r.line_ids.filtered(lambda l:l.status=='present' and not l.finish_override).with_context(attendance_bulk_update=True).write({'finish_time':r.finish_time})

    def action_submit(self):
        for r in self:
            if r.state!='closing': raise UserError(_('Open the closing screen first.'))
            if not r.line_ids: raise UserError(_('Add at least one employee.'))
            if any(l.status=='present' and (l.start_time is False or l.finish_time in (False, None, 0.0)) for l in r.line_ids):
                raise UserError(_('Present employees require start and finish times.'))
            r._check_foreman_access()
            r.with_context(attendance_workflow=True).write({'state':'submitted','submitted_by':self.env.uid,'submitted_at':fields.Datetime.now()})

    def action_finalise(self):
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only attendance administrators can finalise.'))
        for r in self:
            if r.state!='submitted': raise UserError(_('Only submitted registers can be finalised.'))
            r.write({'state':'final','finalised_by':self.env.uid,'finalised_at':fields.Datetime.now()})

    def action_reopen(self):
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only attendance administrators can reopen.'))
        self.filtered(lambda r:r.state in ('submitted','final')).write({'state':'closing'})

    def unlink(self):
        keys = [(r.company_id.id, r.date, r.team_id.id) for r in self]
        result = super().unlink()
        if not self.env.context.get('sb_skip_overtime_sync'):
            Review = self.env['sb.attendance.overtime.review'].sudo()
            for company_id, day, team_id in set(keys):
                if not (company_id and day and team_id):
                    continue
                week = day - timedelta(days=day.weekday())
                for review in Review.search([
                        ('company_id', '=', company_id), ('date_from', '=', week),
                        '|', ('team_id', '=', False), ('team_id', '=', team_id)]):
                    if not (review.line_ids.filtered(lambda l: l.decision != 'pending') or
                            review.team_approval_ids.filtered(lambda l: l.decision != 'pending')):
                        review.with_context(sb_skip_overtime_sync=True).action_refresh()
        return result

class Line(models.Model):
    _name='sb.attendance.line'
    _description='Daily Employee Attendance'
    _order='employee_id'
    register_id=fields.Many2one('sb.attendance.register',required=True,ondelete='cascade')
    employee_id=fields.Many2one('hr.employee',required=True)
    status=fields.Selection([('present','Present'),('absent','Employee-related Absence'),('authorised','Authorised Absence'),('sick','Sick Leave'),('prevented','Prevented from Reporting'),('not_scheduled','Not Scheduled')],required=True,default='present')
    start_time=fields.Float()
    finish_time=fields.Float()
    notes=fields.Char()
    # Distinguish missing time from a genuine 00:00 in the UI.
    start_display=fields.Char(compute='_compute_time_display', string='Start (Display)')
    finish_display=fields.Char(compute='_compute_time_display', string='Finish (Display)')
    start_override=fields.Boolean(string='Individual Start Override',default=False,copy=False)
    finish_override=fields.Boolean(string='Individual Finish Override',default=False,copy=False)
    gross_hours = fields.Float(string='Gross Hours', compute='_compute_worked_hours', digits=(12, 2))
    lunch_hours = fields.Float(string='Unpaid Lunch', compute='_compute_worked_hours', digits=(12, 2))
    net_hours = fields.Float(string='Net Hours', compute='_compute_worked_hours', digits=(12, 2))
    break_override_hours = fields.Float(string='Actual Unpaid Break (Hours)', digits=(12, 2), help='Optional actual unpaid break for this employee and day. Enter 0 for no break; leave manual override disabled for the standard policy.')
    break_manual = fields.Boolean(string='Use Actual Break', copy=False)
    lunch_waived = fields.Boolean(string='Lunch Not Taken (Approved)', groups='sb_daily_attendance.group_attendance_admin', copy=False)

    @api.depends('status', 'start_time', 'finish_time', 'register_id.date',
                 'register_id.state', 'employee_id', 'lunch_waived',
                 'break_manual', 'break_override_hours')
    def _compute_worked_hours(self):
        # Calculate a single unpaid break per employee/date, shared across
        # registers. An explicit break is authoritative; otherwise apply one
        # hour only when total gross attendance exceeds five hours.
        Line = self.env['sb.attendance.line'].sudo()
        for line in self:
            line.gross_hours = line.lunch_hours = line.net_hours = 0.0
            if (line.status != 'present' or not line.employee_id or
                    not line.register_id.date or line.register_id.state == 'draft' or
                    not line.finish_time or line.finish_time < line.start_time):
                continue
            gross = max(0.0, line.finish_time - line.start_time)
            day_lines = Line.search([
                ('employee_id', '=', line.employee_id.id),
                ('register_id.date', '=', line.register_id.date),
                ('register_id.state', 'in', ['closing', 'submitted', 'final']),
                ('status', '=', 'present'),
            ], order='id')
            completed = day_lines.filtered(lambda item: item.finish_time and item.finish_time >= item.start_time)
            total = sum(item.finish_time - item.start_time for item in completed)
            manual = completed.filtered('break_manual')
            if any(item.lunch_waived for item in completed):
                allowance = 0.0
            elif manual:
                allowance = sum(item.break_override_hours for item in manual)
            else:
                allowance = 1.0 if total > 5.0 else 0.0
            remaining = min(total, allowance)
            lunch = 0.0
            for item in completed:
                duration = max(0.0, item.finish_time - item.start_time)
                deduction = min(remaining, duration)
                if item.id == line.id:
                    lunch = deduction
                    break
                remaining -= deduction
            line.gross_hours = gross
            line.lunch_hours = lunch
            line.net_hours = max(0.0, gross - lunch)

    @api.constrains('break_manual', 'break_override_hours')
    def _check_break_override(self):
        for line in self:
            if line.break_override_hours < 0 or line.break_override_hours > 24:
                raise ValidationError(_('Actual unpaid break must be between 0 and 24 hours.'))

    _unique_employee_register = models.Constraint('UNIQUE(register_id, employee_id)', 'An employee may only appear once per register.')

    def _check_unique_daily_employee(self):
        # A single attendance status is recorded per employee/day, irrespective
        # of team or whether the employee is present or absent.
        for line in self:
            if not line.employee_id or not line.register_id.date:
                continue
            # Lock the employee row to serialize concurrent captures for that
            # employee; a second transaction waits and then sees the first.
            self.env.cr.execute('SELECT id FROM hr_employee WHERE id = %s FOR UPDATE',
                                (line.employee_id.id,))
            conflict = self.sudo().search([
                ('id', '!=', line.id),
                ('employee_id', '=', line.employee_id.id),
                ('register_id.date', '=', line.register_id.date),
                ('register_id.company_id', '=', line.register_id.company_id.id),
            ], limit=1)
            if conflict:
                raise ValidationError(_(
                    '%s is already captured for %s on attendance register %s (%s). '
                    'An employee may only be captured once per day.'
                ) % (line.employee_id.name, line.register_id.date,
                     conflict.register_id.name, conflict.register_id.team_id.name))

    @api.constrains('employee_id', 'register_id')
    def _constrain_unique_daily_employee(self):
        self._check_unique_daily_employee()



    @api.depends('status', 'start_time', 'finish_time', 'register_id.state')
    def _compute_time_display(self):
        for line in self:
            def format_time(value):
                minutes = round((value or 0.0) * 60)
                return '%02d:%02d' % (minutes // 60, minutes % 60)
            line.start_display = format_time(line.start_time) if line.status == 'present' else False
            line.finish_display = (format_time(line.finish_time)
                                   if line.status == 'present' and line.register_id.state != 'draft'
                                   and line.finish_time not in (False, None, 0.0) else False)

    @api.constrains('start_time','finish_time','status')
    def _check_times(self):
        for l in self:
            for value in (l.start_time,l.finish_time):
                if value is not False and not 0 <= value < 24:
                    raise ValidationError(_('Time must be between 00:00 and 23:59.'))
            # Odoo Float fields read as 0.0 when unset. During opening, 0.0
            # is an empty finish, not midnight, and must not be compared.
            if (l.status == 'present' and l.register_id.state != 'draft'
                    and l.finish_time not in (False, None, 0.0)
                    and l.start_time is not False and l.finish_time < l.start_time):
                raise ValidationError(_('Finish cannot precede start in this MVP.'))

    @api.onchange('status')
    def _onchange_status(self):
        for line in self:
            if line.status != 'present':
                line.start_time = 0.0
                line.finish_time = 0.0
                line.start_override = False
                line.finish_override = False
            elif not line.start_time:
                line.start_time = line.register_id.start_time or 7.5
                if line.register_id.state in ('closing', 'submitted', 'final'):
                    line.finish_time = line.register_id.finish_time or 16.5

    @api.onchange('start_time')
    def _onchange_start_time(self):
        for line in self:
            if line.status == 'present' and line.start_time:
                line.start_override = True

    @api.onchange('finish_time')
    def _onchange_finish_time(self):
        for line in self:
            if line.status == 'present' and line.finish_time:
                line.finish_override = True

    @api.model_create_multi
    def create(self, vals_list):
        if not (self.env.su or self.env.user.has_group('sb_daily_attendance.group_attendance_admin')):
            for vals in vals_list:
                register = self.env['sb.attendance.register'].browse(vals.get('register_id'))
                if register and (register.state in ('submitted', 'final') or register.team_id.sudo().foreman_id.user_id.id != self.env.uid):
                    raise UserError(_('You cannot add attendance lines to this register.'))
        for vals in vals_list:
            if vals.get('lunch_waived') and not (self.env.su or self.env.user.has_group('sb_daily_attendance.group_attendance_admin')):
                raise UserError(_('Only attendance administrators can approve a lunch exception.'))
            if vals.get('status') in ('absent','authorised','sick','prevented','not_scheduled'):
                vals.update({'start_time':0.0,'finish_time':0.0,'start_override':False,'finish_override':False,'break_manual':False,'break_override_hours':0.0})
        records = super().create(vals_list)
        records._check_unique_daily_employee()
        records.mapped('register_id')._sync_pending_overtime_reviews()
        records._sb_sync_sick_leave()
        return records

    def write(self, vals):
        vals = dict(vals)
        if 'lunch_waived' in vals and not (self.env.su or self.env.user.has_group('sb_daily_attendance.group_attendance_admin')):
            raise UserError(_('Only attendance administrators can approve a lunch exception.'))
        if not (self.env.su or self.env.user.has_group('sb_daily_attendance.group_attendance_admin')):
            if any(l.register_id.state in ('submitted', 'final') for l in self):
                raise UserError(_('Submitted and finalised attendance may only be corrected by administrators.'))
            if 'register_id' in vals:
                raise UserError(_('Moving attendance lines between registers is restricted.'))
        if 'finish_time' in vals and any(l.register_id.state=='draft' for l in self) and not (vals.get('status') and vals['status'] != 'present'):
            raise UserError(_('Finish times cannot be recorded before closing.'))
        if vals.get('status') and vals['status'] != 'present':
            vals.update({'start_time':0.0,'finish_time':0.0,'start_override':False,'finish_override':False,'break_manual':False,'break_override_hours':0.0})
        elif vals.get('status') == 'present':
            for line in self:
                if line.status != 'present':
                    # Apply the status transition without re-entering Line.write().
                    # Calling line.write() here recursively processes the same
                    # non-present status until Python's recursion limit is hit.
                    super(Line, line).write({
                        'status': 'present',
                        'start_time': line.register_id.start_time or 7.5,
                        'finish_time': (line.register_id.finish_time or 16.5)
                                       if line.register_id.state != 'draft' else 0.0,
                        'start_override': False,
                        'finish_override': False,
                        'break_manual': False,
                        'break_override_hours': 0.0,
                    })
            vals.pop('status',None)
        if not self.env.context.get('attendance_bulk_update'):
            if 'start_time' in vals and 'start_override' not in vals:
                vals['start_override'] = True
            if 'finish_time' in vals and 'finish_override' not in vals:
                vals['finish_override'] = True
        audit_fields = ('status', 'start_time', 'finish_time', 'notes', 'lunch_waived', 'break_manual', 'break_override_hours')
        changes_by_line = []
        if not self.env.context.get('attendance_bulk_update'):
            for line in self:
                if line.register_id.submitted_at and line.register_id.state in ('closing', 'submitted', 'final'):
                    changes = [(key, line[key], vals[key]) for key in audit_fields
                               if key in vals and line[key] != vals[key]]
                    if changes:
                        changes_by_line.append((line.register_id, line.employee_id.name, changes))
        old_registers = self.mapped('register_id')
        result = super().write(vals)
        if 'employee_id' in vals or 'register_id' in vals:
            self._check_unique_daily_employee()
        for register, employee, changes in changes_by_line:
            details = '; '.join('%s: %s → %s' % (key, old, new) for key, old, new in changes)
            register.message_post(body=_('Attendance correction for %s by %s: %s') %
                                  (employee, self.env.user.name, details))
        (old_registers | self.mapped('register_id'))._sync_pending_overtime_reviews()
        if any(k in vals for k in ('status', 'employee_id', 'register_id')):
            self._sb_sync_sick_leave()
        return result


    def unlink(self):
        if not (self.env.su or self.env.user.has_group('sb_daily_attendance.group_attendance_admin')):
            if any(l.register_id.state in ('submitted', 'final') for l in self):
                raise UserError(_('Submitted attendance lines cannot be deleted by foremen.'))
        registers = self.mapped('register_id')
        result = super().unlink()
        registers._sync_pending_overtime_reviews()
        return result
