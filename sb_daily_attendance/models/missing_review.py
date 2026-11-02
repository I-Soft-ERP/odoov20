from datetime import timedelta
from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError


class AttendanceMissingReview(models.TransientModel):
    _name = 'sb.attendance.missing.review'
    _description = 'Missing Attendance Review'

    name = fields.Char(default='Attendance Exception Review', readonly=True)
    date_from = fields.Date(required=True, default=fields.Date.context_today)
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    team_id = fields.Many2one('sb.attendance.team', string='Team (optional)')
    line_ids = fields.One2many('sb.attendance.missing.review.line', 'review_id', readonly=True)
    reviewed_team_days = fields.Integer(string='Team-days checked', readonly=True)
    skipped_nonworking = fields.Integer(string='Non-working team-days', readonly=True)
    skipped_no_members = fields.Integer(string='Team-days without effective members', readonly=True)
    exceptions_count = fields.Integer(string='Exceptions found', readonly=True)
    review_summary = fields.Text(string='Review explanation', readonly=True)

    def action_run(self):
        self.ensure_one()
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise AccessError(_('Only attendance administrators can run this review.'))
        if self.date_from > self.date_to:
            raise ValidationError(_('The end date must not precede the start date.'))
        if (self.date_to - self.date_from).days > 92:
            raise ValidationError(_('Select a period of no more than 93 days.'))
        # Reset prior transient results before running again on the same wizard.
        self.write({'line_ids': [fields.Command.clear()],
                    'reviewed_team_days': 0, 'skipped_nonworking': 0,
                    'skipped_no_members': 0, 'exceptions_count': 0,
                    'review_summary': False})
        teams = self.env['sb.attendance.team'].search([
            ('company_id', 'in', self.env.companies.ids),
            ('id', '=', self.team_id.id) if self.team_id else ('id', '!=', 0),
        ])
        Register = self.env['sb.attendance.register']
        commands = []
        checked_days = set()
        nonworking_days = set()
        no_member_days = set()
        day = self.date_from
        while day <= self.date_to:
            for team in teams:
                calendar = team.company_id.resource_calendar_id
                if calendar and not any(int(a.dayofweek) == day.weekday() for a in calendar.attendance_ids):
                    nonworking_days.add((team.id, day))
                    continue
                members = team._members_on(day)
                if not members:
                    no_member_days.add((team.id, day))
                    continue
                checked_days.add((team.id, day))
                registers = Register.search([
                    ('team_id', '=', team.id), ('date', '=', day),
                    ('company_id', '=', team.company_id.id),
                ])
                if not registers:
                    commands.append(fields.Command.create({
                        'date': day, 'team_id': team.id, 'issue': 'no_register',
                        'expected_count': len(members), 'recorded_count': 0,
                        'missing_employee_ids': [fields.Command.set(members.ids)],
                    }))
                    continue
                # One expected employee is counted only once per team/day, even
                # if legacy data contains multiple registers for that day.
                recorded = registers.mapped('line_ids.employee_id')
                missing = members - recorded
                if missing:
                    commands.append(fields.Command.create({
                        'date': day, 'team_id': team.id, 'register_id': registers[0].id,
                        'issue': 'missing_employees', 'expected_count': len(members),
                        'recorded_count': len(members & recorded),
                        'missing_employee_ids': [fields.Command.set(missing.ids)],
                    }))
            day += timedelta(days=1)
        # Enforce mutually exclusive team/day classifications for the summary.
        checked_days.difference_update(nonworking_days, no_member_days)
        no_member_days.difference_update(nonworking_days)
        checked, nonworking, no_members = map(len, (checked_days, nonworking_days, no_member_days))
        summary = _('%s team-day(s) checked; %s non-working team-day(s) skipped; '
                    '%s team-day(s) skipped because no employees had effective team membership. '
                    '%s exception(s) found.') % (checked, nonworking, no_members, len(commands))
        if no_members:
            summary += '\n' + _('Check team membership From dates if you expected attendance before the team was created. Historical membership dates must be confirmed, not assumed.')
        if not teams:
            summary += '\n' + _('No teams matched the selected company and team filter.')
        self.write({
            'line_ids': commands,
            'reviewed_team_days': checked,
            'skipped_nonworking': nonworking,
            'skipped_no_members': no_members,
            'exceptions_count': len(commands),
            'review_summary': summary,
        })
        return {
            'type': 'ir.actions.act_window', 'name': _('Missing Attendance Review'),
            'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
            'target': 'current',
        }


class AttendanceMissingReviewLine(models.TransientModel):
    _name = 'sb.attendance.missing.review.line'
    _description = 'Missing Attendance Review Result'
    _order = 'date desc, team_id'

    review_id = fields.Many2one('sb.attendance.missing.review', required=True, ondelete='cascade')
    date = fields.Date(readonly=True)
    team_id = fields.Many2one('sb.attendance.team', readonly=True)
    register_id = fields.Many2one('sb.attendance.register', readonly=True)
    issue = fields.Selection([
        ('no_register', 'No register'),
        ('missing_employees', 'Missing employee records'),
    ], readonly=True)
    expected_count = fields.Integer(readonly=True)
    recorded_count = fields.Integer(readonly=True)
    missing_employee_ids = fields.Many2many('hr.employee', string='Missing employees', readonly=True)
    missing_employee_names = fields.Text(string='Missing employees', compute='_compute_missing_employee_names')

    @api.depends('missing_employee_ids')
    def _compute_missing_employee_names(self):
        for line in self:
            line.missing_employee_names = '\n'.join(line.missing_employee_ids.mapped('name'))
