import json
from datetime import date
from urllib.request import Request, urlopen
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class OvertimeRules(models.Model):
    _name = 'sb.attendance.overtime.rules'
    _description = 'Sambros Overtime Rules'

    name = fields.Char(default='Sambros Standard Working Week', required=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    weekday_start = fields.Float(default=16.5, required=True, string='Weekday Overtime Starts')
    weekday_rate = fields.Float(default=1.5)
    saturday_rate = fields.Float(default=1.5)
    sunday_rate = fields.Float(default=2.0)
    illustrative_hourly_rate = fields.Float(string='Illustrative Hourly Rate (R)', default=100.0, digits=(12, 2), help='For testing public holiday remuneration only; not an employee wage and never posted to payroll.')
    active = fields.Boolean(default=True)
    holiday_sync_status = fields.Char(compute='_compute_holiday_status')

    @api.constrains('weekday_start', 'weekday_rate', 'saturday_rate', 'sunday_rate', 'illustrative_hourly_rate')
    def _check_rules(self):
        for r in self:
            if not 0 <= r.weekday_start < 24 or min(r.weekday_rate, r.saturday_rate, r.sunday_rate) <= 0 or r.illustrative_hourly_rate < 0:
                raise ValidationError(_('Invalid overtime threshold or multiplier.'))

    @api.depends('company_id')
    def _compute_holiday_status(self):
        for r in self:
            count = self.env['sb.attendance.public.holiday'].search_count([('year', '=', fields.Date.today().year)])
            r.holiday_sync_status = _('%s dates cached for current year') % count

    def action_sync_holidays(self):
        self.ensure_one()
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only attendance administrators may synchronise holidays.'))
        years = {fields.Date.today().year, fields.Date.today().year + 1}
        for year in years:
            self.env['sb.attendance.public.holiday']._sync_year(year)
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Public holidays updated'), 'type': 'success', 'sticky': False}}


class PublicHoliday(models.Model):
    _name = 'sb.attendance.public.holiday'
    _description = 'Automatically Retrieved South African Public Holiday'
    _order = 'holiday_date'
    _sql_constraints = [('unique_holiday_date', 'unique(holiday_date)', 'Holiday date must be unique.')]

    holiday_date = fields.Date(required=True, index=True)
    name = fields.Char(required=True)
    year = fields.Integer(required=True, index=True)
    source = fields.Char(default='Nager.Date API', readonly=True)
    verified_on = fields.Datetime(readonly=True)

    @api.model
    def _sync_year(self, year):
        url = 'https://date.nager.at/api/v3/PublicHolidays/%s/ZA' % int(year)
        try:
            with urlopen(Request(url, headers={'User-Agent': 'Sambros-Odoo-Attendance/1.0'}), timeout=12) as response:
                data = json.load(response)
            if not isinstance(data, list) or not data or any('date' not in item or 'localName' not in item for item in data):
                raise ValueError('Unexpected public holiday response')
            for item in data:
                day = date.fromisoformat(item['date'])
                if day.year != int(year):
                    continue
                existing = self.search([('holiday_date', '=', day)], limit=1)
                vals = {'name': item['localName'], 'year': year, 'verified_on': fields.Datetime.now()}
                if existing:
                    existing.write(vals)
                else:
                    self.create(dict(vals, holiday_date=day))
        except Exception as exc:
            raise UserError(_('Holiday synchronisation failed for %s: %s. Existing cached dates were preserved. Do not approve unverified holiday classifications.') % (year, exc)) from exc

    @api.model
    def _is_holiday(self, day):
        # Fail closed when the relevant year has not been retrieved.
        if not self.search_count([('year', '=', day.year)]):
            raise UserError(_('South African public holidays for %s have not been synchronised. Open Overtime Rules and select Synchronise Public Holidays.') % day.year)
        return bool(self.search_count([('holiday_date', '=', day)]))
