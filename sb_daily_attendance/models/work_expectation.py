from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class AttendanceWorkExpectation(models.Model):
    _name = 'sb.attendance.work.expectation'
    _description = 'Exceptional Team Work Expectation'
    _order = 'date desc, team_id'
    _sql_constraints = [
        ('unique_team_work_date', 'unique(team_id, date)',
         'This team already has a work expectation for the selected date.'),
    ]

    date = fields.Date(required=True, default=fields.Date.context_today, index=True)
    team_id = fields.Many2one('sb.attendance.team', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one('res.company', related='team_id.company_id', store=True, readonly=True)
    reason = fields.Char(string='Work reason', required=True,
                         help='For example: emergency callout or planned weekend work.')

    @api.constrains('date', 'team_id')
    def _check_exceptional_day(self):
        for rec in self:
            if rec.date and rec.team_id and rec.team_id._is_normal_workday(rec.date):
                raise ValidationError(_('An explicit expectation is only needed on non-working days or public holidays.'))
