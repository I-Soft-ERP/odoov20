from collections import defaultdict
from datetime import timedelta
from io import BytesIO
import base64

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class PayrollAttendanceSummary(models.TransientModel):
    _name = 'sb.attendance.payroll.summary'
    _description = 'Payroll-Ready Attendance Summary'

    date_from = fields.Date(string='Period From', required=True,
                            default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(string='Period To', required=True,
                          default=fields.Date.context_today)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    team_id = fields.Many2one('sb.attendance.team', string='Team (Optional)')
    line_ids = fields.One2many('sb.attendance.payroll.summary.line', 'summary_id', readonly=True)
    employee_count = fields.Integer(readonly=True)
    normal_total = fields.Float(readonly=True, digits=(12, 2))
    overtime_total = fields.Float(readonly=True, digits=(12, 2))
    outstanding_register_count = fields.Integer(string='Unfinalised Registers', readonly=True)
    outstanding_overtime_count = fields.Integer(string='Unapproved Overtime Entries', readonly=True)
    duplicate_count = fields.Integer(string='Duplicate Employee-Days', readonly=True)
    warning = fields.Text(string='Exceptions / Reconciliation', readonly=True)

    def _check_payroll_report_permission(self):
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only Attendance Administrators may prepare payroll attendance reports.'))

    def action_generate(self):
        self.ensure_one()
        self._check_payroll_report_permission()
        if not self.date_from or not self.date_to or self.date_to < self.date_from:
            raise UserError(_('Select a valid date range.'))
        domain = [('date', '>=', self.date_from), ('date', '<=', self.date_to),
                  ('company_id', '=', self.company_id.id)]
        if self.team_id:
            domain.append(('team_id', '=', self.team_id.id))
        registers = self.env['sb.attendance.register'].search(domain)
        pending = registers.filtered(lambda r: r.state != 'final')
        finalised = registers - pending
        by_employee = defaultdict(lambda: {
            'gross': 0.0, 'breaks': 0.0, 'net': 0.0,
            'normal': 0.0, 'weekday': 0.0, 'saturday': 0.0,
            'sunday': 0.0, 'holiday': 0.0,
            'absent': 0, 'authorised': 0, 'prevented': 0,
            'days': set(), 'teams': set(),
        })
        seen = set()
        attendance_by_day = {}
        duplicate_count = 0
        for reg in finalised:
            for line in reg.line_ids.filtered(lambda x: x.employee_id):
                key = (line.employee_id.id, reg.date)
                if key in seen:
                    duplicate_count += 1
                    continue  # Never double-count historical duplicate employee-days.
                seen.add(key)
                attendance_by_day[key] = (reg, line)
                row = by_employee[line.employee_id.id]
                row['teams'].add(reg.team_id.name)
                if line.status == 'present':
                    gross = max(0.0, line.gross_hours)
                    breaks = max(0.0, line.lunch_hours)
                    net = max(0.0, line.net_hours)
                    row['gross'] += gross
                    row['breaks'] += breaks
                    row['net'] += net
                    row['days'].add(reg.date)
                    # Classification is by attendance hours, not pay multipliers.
                    # Overtime approval hours are allocated below from decisions.
                elif line.status in ('absent', 'authorised', 'prevented'):
                    row[line.status] += 1

        # Use individual overtime decisions created by approved team/day actions.
        # Do not rely on a review's overall candidate total, which is not payroll authorisation.
        decisions = self.env['sb.attendance.overtime.line'].search([
            ('work_date', '>=', self.date_from), ('work_date', '<=', self.date_to),
            ('company_id', '=', self.company_id.id),
        ], order='id desc')
        approved = {}
        outstanding = set()
        for decision in decisions:
            if self.team_id and decision.team_id != self.team_id:
                continue
            key = (decision.employee_id.id, decision.work_date)
            # Ignore duplicate review snapshots once a valid approval is found.
            if decision.decision == 'approved' and key not in approved:
                approved[key] = decision
            elif decision.decision == 'pending':
                outstanding.add(key)
        # Validate approved hours against currently finalised attendance.
        valid_approved = {}
        stale_count = 0
        for key, decision in approved.items():
            if key not in seen:
                stale_count += 1
                continue
            matching = finalised.filtered(lambda r: r.date == key[1] and
                                           any(l.employee_id.id == key[0] and l.status == 'present' for l in r.line_ids))
            actual = sum(l.net_hours for reg in matching for l in reg.line_ids
                         if l.employee_id.id == key[0] and l.status == 'present')
            if abs(actual - decision.net_hours) > 0.001:
                stale_count += 1
                continue
            valid_approved[key] = decision
            row = by_employee[key[0]]
            row[decision.category] += decision.approved_hours
        # One normal-hours contribution per unique employee/day.
        # Weekends do not become ordinary hours just because overtime is pending.
        for key, (reg, line) in attendance_by_day.items():
            if line.status != 'present':
                continue
            if reg.date.weekday() < 5:
                approved_line = valid_approved.get(key)
                if not approved_line or approved_line.category != 'holiday':
                    by_employee[key[0]]['normal'] += min(max(line.net_hours, 0.0), 8.0)

        commands = []
        for employee_id, row in sorted(by_employee.items(), key=lambda item: item[0]):
            commands.append(fields.Command.create({
                'employee_id': employee_id,
                'team_names': ', '.join(sorted(row['teams'])),
                'days_worked': len(row['days']),
                'gross_hours': round(row['gross'], 2),
                'break_hours': round(row['breaks'], 2),
                'net_hours': round(row['net'], 2),
                'normal_hours': round(row['normal'], 2),
                'weekday_ot': round(row['weekday'], 2),
                'saturday_ot': round(row['saturday'], 2),
                'sunday_ot': round(row['sunday'], 2),
                'holiday_ot': round(row['holiday'], 2),
                'absent_days': row['absent'],
                'authorised_days': row['authorised'],
                'prevented_days': row['prevented'],
            }))
        warnings = []
        if pending:
            warnings.append('%s unfinalised attendance registers are excluded.' % len(pending))
        pending_keys = outstanding - set(valid_approved)
        if pending_keys:
            warnings.append('%s overtime employee-days remain unapproved.' % len(pending_keys))
        if duplicate_count:
            warnings.append('%s historical duplicate employee-days were excluded.' % duplicate_count)
        if stale_count:
            warnings.append('%s overtime approvals do not match finalised attendance and were excluded.' % stale_count)
        self.write({
            'line_ids': [fields.Command.clear()] + commands,
            'employee_count': len(by_employee),
            'normal_total': round(sum(v['normal'] for v in by_employee.values()), 2),
            'overtime_total': round(sum(v[k] for v in by_employee.values()
                                        for k in ('weekday', 'saturday', 'sunday', 'holiday')), 2),
            'outstanding_register_count': len(pending),
            'outstanding_overtime_count': len(pending_keys),
            'duplicate_count': duplicate_count,
            'warning': '\n'.join(warnings) or 'No exceptions identified in the selected data.',
        })
        return {'type': 'ir.actions.act_window', 'res_model': self._name,
                'res_id': self.id, 'view_mode': 'form', 'target': 'current'}

    def action_export_xlsx(self):
        self.ensure_one()
        self._check_payroll_report_permission()
        # Rebuild at export time to prevent a stale summary being sent to payroll.
        self.action_generate()
        try:
            import xlsxwriter
        except ImportError as exc:
            raise UserError(_('Excel export requires the Python xlsxwriter package on the Odoo server.')) from exc
        buffer = BytesIO()
        book = xlsxwriter.Workbook(buffer, {'in_memory': True})
        sheet = book.add_worksheet('Attendance Summary')
        title = book.add_format({'bold': True, 'font_size': 15, 'font_color': '#583D54'})
        header = book.add_format({'bold': True, 'bg_color': '#583D54', 'font_color': 'white', 'text_wrap': True})
        number = book.add_format({'num_format': '0.00'})
        sheet.merge_range('A1:O1', 'Sambros — Payroll Attendance Summary', title)
        sheet.write('A2', 'Period')
        sheet.write('B2', '%s to %s' % (self.date_from, self.date_to))
        sheet.write('A3', 'Company')
        sheet.write('B3', self.company_id.name)
        headers = ['Employee ID', 'Employee', 'Team(s)', 'Days Worked', 'Gross Hours',
                   'Unpaid Breaks', 'Net Hours', 'Normal Hours', 'Weekday OT Approved',
                   'Saturday OT Approved', 'Sunday OT Approved', 'Public Holiday OT Approved',
                   'Absent Days', 'Authorised Absence Days', 'Prevented Days']
        for col, label in enumerate(headers):
            sheet.write(4, col, label, header)
        for idx, line in enumerate(self.line_ids.sorted(lambda l: l.employee_id.name), start=5):
            values = [line.employee_id.id, line.employee_id.name, line.team_names,
                      line.days_worked, line.gross_hours, line.break_hours, line.net_hours,
                      line.normal_hours, line.weekday_ot, line.saturday_ot,
                      line.sunday_ot, line.holiday_ot, line.absent_days,
                      line.authorised_days, line.prevented_days]
            for col, value in enumerate(values):
                sheet.write(idx, col, value, number if 4 <= col <= 11 else None)
        sheet.set_column('A:A', 13)
        sheet.set_column('B:C', 24)
        sheet.set_column('D:O', 19)
        sheet.freeze_panes(5, 2)
        sheet.autofilter(4, 0, 4 + len(self.line_ids), 14)
        issues = book.add_worksheet('Exceptions')
        issues.set_column('A:A', 85)
        issues.write('A1', 'Reconciliation / Exceptions', title)
        for index, message in enumerate((self.warning or '').splitlines(), start=3):
            issues.write(index, 0, message)
        issues.write('A2', 'Normal hours are weekday net attendance capped at 8 per day; approved overtime is shown separately. No remuneration or statutory payroll calculations are performed.')
        book.close()
        attachment = self.env['ir.attachment'].create({
            'name': 'Sambros_Attendance_%s_%s.xlsx' % (self.date_from, self.date_to),
            'type': 'binary', 'datas': base64.b64encode(buffer.getvalue()),
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'res_model': self._name, 'res_id': self.id,
        })
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id,
                'target': 'self'}


class PayrollAttendanceSummaryLine(models.TransientModel):
    _name = 'sb.attendance.payroll.summary.line'
    _description = 'Payroll Attendance Employee Summary'
    _order = 'employee_id'

    summary_id = fields.Many2one('sb.attendance.payroll.summary', required=True, ondelete='cascade')
    employee_id = fields.Many2one('hr.employee', readonly=True)
    team_names = fields.Char(string='Team(s)', readonly=True)
    days_worked = fields.Integer(readonly=True)
    gross_hours = fields.Float(readonly=True, digits=(12, 2))
    break_hours = fields.Float(readonly=True, digits=(12, 2))
    net_hours = fields.Float(readonly=True, digits=(12, 2))
    normal_hours = fields.Float(readonly=True, digits=(12, 2))
    weekday_ot = fields.Float(string='Weekday OT', readonly=True, digits=(12, 2))
    saturday_ot = fields.Float(string='Saturday OT', readonly=True, digits=(12, 2))
    sunday_ot = fields.Float(string='Sunday OT', readonly=True, digits=(12, 2))
    holiday_ot = fields.Float(string='Public Holiday OT', readonly=True, digits=(12, 2))
    absent_days = fields.Integer(readonly=True)
    authorised_days = fields.Integer(readonly=True)
    prevented_days = fields.Integer(readonly=True)
