from collections import defaultdict
from datetime import date, timedelta
import base64
import io
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class MonthlySummary(models.TransientModel):
    _name = 'sb.attendance.monthly.summary'
    _description = 'Monthly Attendance and Overtime Summary'

    month = fields.Selection([(str(i), date(2026, i, 1).strftime('%B')) for i in range(1, 13)], required=True,
                             default=lambda self: str(fields.Date.context_today(self).month))
    year = fields.Integer(required=True, default=lambda self: fields.Date.context_today(self).year)
    team_id = fields.Many2one('sb.attendance.team', string='Team (Optional)')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    date_from = fields.Date(compute='_compute_period', string='First Week Starts')
    date_to = fields.Date(compute='_compute_period', string='Last Week Ends')
    name = fields.Char(compute='_compute_period')
    week_ids = fields.One2many('sb.attendance.monthly.week', 'summary_id', readonly=True)
    employee_ids = fields.One2many('sb.attendance.monthly.employee', 'summary_id', readonly=True)
    pending_count = fields.Integer(readonly=True)
    duplicate_count = fields.Integer(readonly=True)

    @api.depends('month', 'year')
    def _compute_period(self):
        for rec in self:
            if not rec.month or not rec.year or not 1900 <= rec.year <= 2200:
                rec.date_from = rec.date_to = False
                rec.name = 'Monthly Attendance Summary'
                continue
            first = date(rec.year, int(rec.month), 1)
            next_month = date(rec.year + (int(rec.month) == 12), int(rec.month) % 12 + 1, 1)
            last = next_month - timedelta(days=1)
            first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
            last_sunday = last - timedelta(days=(last.weekday() - 6) % 7)
            rec.date_from = first_sunday - timedelta(days=6)
            rec.date_to = last_sunday
            rec.name = '%s %s — Monthly Attendance & Overtime' % (first.strftime('%B'), rec.year)

    def action_generate(self):
        self.ensure_one()
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Monthly reporting is restricted to Attendance Administrators.'))
        if not self.date_from or not self.date_to:
            raise UserError(_('Select a valid reporting month and year.'))
        weekly_model = self.env['sb.attendance.weekly.summary']
        weeks = [fields.Command.clear()]
        employees = defaultdict(lambda: {'present': 0, 'absent': 0, 'authorised': 0, 'prevented': 0,
                                          'net': 0., 'ot15': 0., 'ot20': 0.})
        pending = duplicates = 0
        cursor = self.date_from
        while cursor <= self.date_to:
            weekly = weekly_model.create({'date_from': cursor, 'company_id': self.company_id.id,
                                          'team_id': self.team_id.id or False})
            weekly.action_generate()
            stats = weekly._daily_team_attendance()
            ot = weekly._overtime_report_data()
            for team in stats['teams']:
                ot_team = ot['teams'].get(team['id'], {})
                days = ot_team.get('days', [])
                ot15 = sum(d['rate_15'] for d in days)
                ot20 = sum(d['rate_20'] for d in days)
                present = sum(d['present'] for d in team['days'])
                weeks.append(fields.Command.create({
                    'week_start': cursor, 'week_end': cursor + timedelta(days=6),
                    'team_id': team['id'], 'present': present, 'absent': team['absent_week'],
                    'attendance_rate': team['rate'] or 0., 'ot15': ot15, 'ot20': ot20,
                }))
            for line in weekly.line_ids:
                key = (line.team_id.id, line.employee_id.id)
                values = employees[key]
                values['present'] += line.present_days
                values['absent'] += line.absent_days
                values['authorised'] += line.authorised_days
                values['prevented'] += line.prevented_days
                values['net'] += line.net_hours
                employee_ot = weekly._overtime_employee_weekly(line.employee_id.id)
                values['ot15'] += employee_ot['rate_15']
                values['ot20'] += employee_ot['rate_20']
            pending += weekly.pending_count
            duplicates += weekly.duplicate_count
            cursor += timedelta(days=7)
        employee_commands = [fields.Command.clear()]
        for (team_id, employee_id), values in sorted(employees.items()):
            employee_commands.append(fields.Command.create({
                'team_id': team_id, 'employee_id': employee_id,
                'present': values['present'], 'absent': values['absent'],
                'authorised': values['authorised'], 'prevented': values['prevented'],
                'net_hours': round(values['net'], 2), 'ot15': round(values['ot15'], 2),
                'ot20': round(values['ot20'], 2),
                'attendance_rate': round(100 * values['present'] / (values['present'] + values['absent']), 1)
                if values['present'] + values['absent'] else 0.,
            }))
        self.write({'week_ids': weeks, 'employee_ids': employee_commands,
                    'pending_count': pending, 'duplicate_count': duplicates})
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'current'}

    def action_download_pdf(self):
        self.ensure_one()
        self.action_generate()
        return self.env.ref('sb_daily_attendance.action_report_monthly_attendance').report_action(self)

    def action_download_xlsx(self):
        self.ensure_one()
        self.action_generate()
        try:
            import xlsxwriter
        except ImportError:
            raise UserError(_('Excel export requires xlsxwriter on the Odoo server.'))
        stream = io.BytesIO()
        book = xlsxwriter.Workbook(stream, {'in_memory': True})
        head = book.add_format({'bold': True, 'bg_color': '#24343D', 'font_color': 'white', 'text_wrap': True})
        numeric = book.add_format({'num_format': '0.00'})
        percent = book.add_format({'num_format': '0.0"%"'})
        for title, columns, records, extract in [
            ('Weekly Team Summary', ['Week start', 'Week end', 'Team', 'Present', 'Absent', 'Attendance %', 'OT 1.5x', 'OT 2.0x', 'Total OT'], self.week_ids,
             lambda r: [str(r.week_start), str(r.week_end), r.team_id.name, r.present, r.absent, r.attendance_rate, r.ot15, r.ot20, r.ot15 + r.ot20]),
            ('Employee Monthly Summary', ['Team', 'Employee', 'Present', 'Absent', 'Authorised', 'Prevented', 'Attendance %', 'Net hours', 'OT 1.5x', 'OT 2.0x', 'Total OT'], self.employee_ids,
             lambda r: [r.team_id.name, r.employee_id.name, r.present, r.absent, r.authorised, r.prevented, r.attendance_rate, r.net_hours, r.ot15, r.ot20, r.ot15 + r.ot20]),
        ]:
            ws = book.add_worksheet(title)
            ws.freeze_panes(1, 2)
            ws.set_landscape()
            ws.set_column(0, 1, 23)
            ws.set_column(2, len(columns) - 1, 15)
            for col, label in enumerate(columns):
                ws.write(0, col, label, head)
            for row, record in enumerate(records, 1):
                for col, value in enumerate(extract(record)):
                    if isinstance(value, (int, float)):
                        ws.write_number(row, col, value, percent if columns[col] == 'Attendance %' else numeric)
                    else:
                        ws.write_string(row, col, value or '')
            if records:
                ws.autofilter(0, 0, len(records), len(columns) - 1)
        book.close()
        content = stream.getvalue()
        vals = {'name': 'Sambros_Monthly_Attendance_%s_%02d.xlsx' % (self.year, int(self.month)),
                'type': 'binary', 'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'res_model': self._name, 'res_id': self.id}
        if 'raw' in self.env['ir.attachment']._fields:
            vals['raw'] = content
        else:
            vals['datas'] = base64.b64encode(content)
        attachment = self.env['ir.attachment'].create(vals)
        if not attachment.file_size:
            raise UserError(_('Excel attachment is empty.'))
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id, 'target': 'self'}


class MonthlyWeek(models.TransientModel):
    _name = 'sb.attendance.monthly.week'
    _description = 'Monthly Attendance Week Team Summary'
    _order = 'week_start, team_id'
    summary_id = fields.Many2one('sb.attendance.monthly.summary', required=True, ondelete='cascade')
    week_start = fields.Date(readonly=True)
    week_end = fields.Date(readonly=True)
    team_id = fields.Many2one('sb.attendance.team', readonly=True)
    present = fields.Integer(readonly=True)
    absent = fields.Integer(readonly=True)
    attendance_rate = fields.Float(readonly=True)
    ot15 = fields.Float(readonly=True)
    ot20 = fields.Float(readonly=True)
    total_ot = fields.Float(compute='_compute_total')

    @api.depends('ot15', 'ot20')
    def _compute_total(self):
        for r in self:
            r.total_ot = r.ot15 + r.ot20


class MonthlyEmployee(models.TransientModel):
    _name = 'sb.attendance.monthly.employee'
    _description = 'Monthly Employee Attendance Summary'
    _order = 'team_id, employee_id'
    summary_id = fields.Many2one('sb.attendance.monthly.summary', required=True, ondelete='cascade')
    team_id = fields.Many2one('sb.attendance.team', readonly=True)
    employee_id = fields.Many2one('hr.employee', readonly=True)
    present = fields.Integer(readonly=True)
    absent = fields.Integer(readonly=True)
    authorised = fields.Integer(readonly=True)
    prevented = fields.Integer(readonly=True)
    attendance_rate = fields.Float(readonly=True)
    net_hours = fields.Float(readonly=True)
    ot15 = fields.Float(readonly=True)
    ot20 = fields.Float(readonly=True)
    total_ot = fields.Float(compute='_compute_total')

    @api.depends('ot15', 'ot20')
    def _compute_total(self):
        for r in self:
            r.total_ot = r.ot15 + r.ot20
