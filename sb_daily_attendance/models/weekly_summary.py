import re
from datetime import timedelta
from collections import defaultdict
import base64
import io
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class WeeklySummary(models.TransientModel):
    _name = 'sb.attendance.weekly.summary'
    _description = 'Weekly Attendance Summary'

    def _default_monday(self):
        today = fields.Date.context_today(self)
        return today - timedelta(days=today.weekday())

    date_from = fields.Date(string='Week Starting', required=True, default=_default_monday)
    date_to = fields.Date(string='Week Ending', compute='_compute_date_to')
    team_id = fields.Many2one('sb.attendance.team', string='Team (Optional)')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    line_ids = fields.One2many('sb.attendance.weekly.summary.line', 'summary_id', string='Employee Summary', readonly=True)
    employee_count = fields.Integer(readonly=True)
    gross_total = fields.Float(readonly=True, digits=(12, 2))
    break_total = fields.Float(readonly=True, digits=(12, 2))
    net_total = fields.Float(readonly=True, digits=(12, 2))
    present_days_total = fields.Integer(readonly=True)
    absent_days_total = fields.Integer(readonly=True)
    authorised_days_total = fields.Integer(readonly=True)
    prevented_days_total = fields.Integer(readonly=True)
    not_scheduled_days_total = fields.Integer(readonly=True)
    pending_count = fields.Integer(string='Unsubmitted Registers', readonly=True)
    pending_details = fields.Text(string='Outstanding Registers', readonly=True)
    pending_line_ids = fields.One2many('sb.attendance.weekly.pending', 'summary_id', string='Outstanding Registers', readonly=True)
    duplicate_count = fields.Integer(string='Duplicate Employee-Days', readonly=True)
    name = fields.Char(string='Report', compute='_compute_name')

    @api.depends('date_from', 'date_to')
    def _compute_name(self):
        for rec in self:
            rec.name = 'Weekly Attendance Summary — %s to %s' % (rec.date_from, rec.date_to) if rec.date_from and rec.date_to else 'Weekly Attendance Summary'


    @api.depends('date_from')
    def _compute_date_to(self):
        for rec in self:
            rec.date_to = rec.date_from + timedelta(days=6) if rec.date_from else False

    def action_generate(self):
        self.ensure_one()
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Weekly reporting is restricted to Attendance Administrators.'))
        if not self.date_from:
            raise UserError(_('Please select the first day of the reporting week.'))
        end = self.date_from + timedelta(days=6)
        domain = [('date', '>=', self.date_from), ('date', '<=', end),
                  ('company_id', '=', self.company_id.id)]
        if self.team_id:
            domain.append(('team_id', '=', self.team_id.id))
        registers = self.env['sb.attendance.register'].search(domain)
        # Draft and closing records are incomplete; report them separately, never as hours.
        pending = registers.filtered(lambda r: r.state in ('draft', 'closing'))
        confirmed = registers.filtered(lambda r: r.state in ('submitted', 'final'))
        grouped = defaultdict(lambda: {'gross': 0.0, 'break': 0.0, 'net': 0.0,
                                       'present': 0, 'absent': 0, 'authorised': 0,
                                       'prevented': 0, 'not_scheduled': 0, 'days': {}, 'day_counts': defaultdict(int)})
        for reg in confirmed:
            for line in reg.line_ids:
                if not line.employee_id:
                    continue
                key = (line.employee_id.id, reg.team_id.id)
                row = grouped[key]
                day_index = (reg.date - self.date_from).days
                if 0 <= day_index <= 6:
                    day = row['days'].setdefault(day_index, {'net': 0.0, 'statuses': []})
                    row['day_counts'][day_index] += 1
                    if line.status == 'present':
                        day['net'] += line.net_hours
                    else:
                        day['statuses'].append({
                            'absent': 'ABS', 'authorised': 'AUTH',
                            'prevented': 'PREV', 'not_scheduled': 'OFF',
                        }.get(line.status, line.status))
                if line.status in ('present', 'absent', 'authorised', 'prevented', 'not_scheduled'):
                    row[line.status] += 1
                if line.status == 'present':
                    row['gross'] += line.gross_hours
                    row['break'] += line.lunch_hours
                    row['net'] += line.net_hours
        duplicates = sum(max(0, count - 1) for row in grouped.values() for count in row['day_counts'].values())
        commands = [fields.Command.clear()]
        for (employee_id, team_id), values in sorted(grouped.items()):
            commands.append(fields.Command.create({
                'employee_id': employee_id, 'team_id': team_id,
                'present_days': values['present'], 'absent_days': values['absent'],
                'authorised_days': values['authorised'],
                'prevented_days': values['prevented'],
                'not_scheduled_days': values['not_scheduled'],
                'gross_hours': round(values['gross'], 2),
                'break_hours': round(values['break'], 2),
                'net_hours': round(values['net'], 2),
                **{'day_%s' % d: (
                    ('%.2f h' % values['days'][d]['net'] if values['days'][d]['net'] or not values['days'][d]['statuses'] else '')
                    + (' / ' if values['days'][d]['net'] and values['days'][d]['statuses'] else '')
                    + ', '.join(values['days'][d]['statuses'])
                    + (' [DUPLICATE]' if values['day_counts'][d] > 1 else '')
                ) if d in values['days'] else '—' for d in range(7)},
            }))
        self.write({
            'line_ids': commands,
            'pending_line_ids': [fields.Command.clear()] + [fields.Command.create({
                'register_id': reg.id, 'register_date': reg.date,
                'team_id': reg.team_id.id, 'register_state': reg.state,
            }) for reg in pending.sorted(key=lambda r: (r.date, r.name))],
            'duplicate_count': duplicates,
            'employee_count': len({employee_id for employee_id, _ in grouped}),
            'gross_total': round(sum(v['gross'] for v in grouped.values()), 2),
            'break_total': round(sum(v['break'] for v in grouped.values()), 2),
            'net_total': round(sum(v['net'] for v in grouped.values()), 2),
            'present_days_total': sum(v['present'] for v in grouped.values()),
            'absent_days_total': sum(v['absent'] for v in grouped.values()),
            'authorised_days_total': sum(v['authorised'] for v in grouped.values()),
            'prevented_days_total': sum(v['prevented'] for v in grouped.values()),
            'not_scheduled_days_total': sum(v['not_scheduled'] for v in grouped.values()),
            'pending_count': len(pending),
            'pending_details': '\n'.join('%s | %s | %s | %s' % (r.name, r.date, r.team_id.name, dict(r._fields['state'].selection).get(r.state, r.state)) for r in pending.sorted(key=lambda r: (r.date, r.name))) or 'None',
        })
        return {'type': 'ir.actions.act_window', 'name': _('Weekly Attendance Summary'),
                'res_model': self._name, 'res_id': self.id, 'view_mode': 'form', 'target': 'current'}


    def _daily_team_attendance(self):
        """Build management totals from the same employee rows shown in the report.

        Counts represent confirmed employee/day outcomes. Missing days stay blank;
        authorised/prevented/off outcomes are not treated as absences.
        """
        self.ensure_one()
        teams = {}
        for line in self.line_ids:
            team_id = line.team_id.id
            if team_id not in teams:
                teams[team_id] = {'id': team_id, 'name': line.team_id.name or 'Unassigned',
                                  'days': [{'present': 0, 'absent': 0, 'other': 0} for _ in range(7)]}
            for day_idx in range(7):
                value = (getattr(line, 'day_%s' % day_idx) or '').strip()
                if not value or value in ('—', '-'):
                    continue
                # Mixed and duplicate records must not be silently counted twice.
                if '[DUPLICATE]' in value or ' / ' in value or ',' in value:
                    teams[team_id]['days'][day_idx]['other'] += 1
                elif re.fullmatch(r'[0-9]+(?:\.[0-9]+)? h', value):
                    teams[team_id]['days'][day_idx]['present'] += 1
                elif value == 'ABS':
                    teams[team_id]['days'][day_idx]['absent'] += 1
                else:
                    teams[team_id]['days'][day_idx]['other'] += 1
        rows = sorted(teams.values(), key=lambda r: r['name'].lower())
        totals = [{'present': sum(r['days'][d]['present'] for r in rows),
                   'absent': sum(r['days'][d]['absent'] for r in rows),
                   'other': sum(r['days'][d]['other'] for r in rows)} for d in range(7)]
        for row in rows:
            present = sum(d['present'] for d in row['days'])
            absent = sum(d['absent'] for d in row['days'])
            row['absent_week'] = absent
            row['rate'] = round(100 * present / (present + absent), 1) if present + absent else None
            for d in row['days']:
                denominator = d['present'] + d['absent']
                d['rate'] = round(100 * d['present'] / denominator, 1) if denominator else None
        present = sum(d['present'] for d in totals)
        absent = sum(d['absent'] for d in totals)
        for d in totals:
            denominator = d['present'] + d['absent']
            d['rate'] = round(100 * d['present'] / denominator, 1) if denominator else None
        rate = round(100 * present / (present + absent), 1) if present + absent else None
        return {'teams': rows, 'totals': totals, 'rate': rate,
                'present': present, 'absent': absent,
                'exceptions': sum(d['other'] for d in totals)}

    def _daily_team_attendance_for_team(self, team_id):
        """One team's daily counts, sourced from the displayed employee rows."""
        summary = self._daily_team_attendance()
        for team in summary['teams']:
            if team['id'] == team_id:
                return team
        return {'days': [{'present': 0, 'absent': 0, 'other': 0, 'rate': None} for _ in range(7)],
                'absent_week': 0, 'rate': None}

    def _overtime_report_data(self):
        """Read existing overtime reviews; never approve or calculate payroll here.

        The newest review line wins for each employee/date. This avoids adding
        duplicate snapshots when management has regenerated a weekly review.
        """
        self.ensure_one()
        domain = [('work_date', '>=', self.date_from), ('work_date', '<=', self.date_to),
                  ('review_id.company_id', '=', self.company_id.id)]
        if self.team_id:
            domain.append(('team_id', '=', self.team_id.id))
        lines = self.env['sb.attendance.overtime.line'].search(domain, order='id desc')
        selected = {}
        for item in lines:
            key = (item.employee_id.id, item.work_date)
            if key not in selected:
                selected[key] = item
        categories = [('weekday', 'Weekday 1.5x'), ('saturday', 'Saturday 1.5x'),
                      ('sunday', 'Sunday 2.0x'), ('holiday', 'Public holiday')]
        teams = {}
        for item in selected.values():
            team = teams.setdefault(item.team_id.id, {'days': [{'candidate': 0.0, 'approved': 0.0, 'rate_15': 0.0, 'rate_20': 0.0} for _ in range(7)],
                'categories': {k: {'candidate': 0.0, 'approved': 0.0} for k, _ in categories}})
            day = (item.work_date - self.date_from).days
            if not 0 <= day < 7:
                continue
            candidate = item.candidate_hours or 0.0
            approved = item.approved_hours if item.decision == 'approved' else 0.0
            team['days'][day]['candidate'] += candidate
            team['days'][day]['approved'] += approved
            if item.category in ('weekday', 'saturday'):
                team['days'][day]['rate_15'] += candidate
            elif item.category == 'sunday':
                team['days'][day]['rate_20'] += candidate
            cat = item.category
            if cat in team['categories']:
                team['categories'][cat]['candidate'] += candidate
                team['categories'][cat]['approved'] += approved
        totals = {k: {'candidate': 0.0, 'approved': 0.0} for k, _ in categories}
        for team in teams.values():
            for key in totals:
                for field in ('candidate', 'approved'):
                    totals[key][field] += team['categories'][key][field]
        return {'entries': selected, 'teams': teams, 'totals': totals, 'categories': categories}

    def _overtime_employee_weekly(self, employee_id):
        """Candidate hours by rate; holidays remain separate pending statutory assessment."""
        self.ensure_one()
        totals = {'rate_15': 0.0, 'rate_20': 0.0}
        for (emp_id, _day), item in self._overtime_report_data()['entries'].items():
            if emp_id != employee_id:
                continue
            if item.category in ('weekday', 'saturday'):
                totals['rate_15'] += item.candidate_hours or 0.0
            elif item.category == 'sunday':
                totals['rate_20'] += item.candidate_hours or 0.0
        return totals

    def _overtime_cell(self, employee_id, day_index):
        self.ensure_one()
        day = self.date_from + timedelta(days=day_index)
        entry = self._overtime_report_data()['entries'].get((employee_id, day))
        if not entry:
            return False
        return {'category': entry.category, 'candidate': entry.candidate_hours,
                'approved': entry.approved_hours if entry.decision == 'approved' else 0.0,
                'decision': entry.decision, 'rate': entry.rate_multiplier}

    def action_download_pdf(self):
        self.ensure_one()
        self.action_generate()
        return self.env.ref('sb_daily_attendance.action_report_weekly_attendance').report_action(self)

    def action_download_xlsx(self):
        self.ensure_one()
        self.action_generate()
        try:
            import xlsxwriter
        except ImportError:
            raise UserError(_('Excel export requires the Python xlsxwriter package on the Odoo server.'))
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        # Sambros corporate palette, aligned to the Scope of Work manual.
        charcoal, red, pale = '#303238', '#B3262D', '#F0F1F2'
        title = workbook.add_format({'bold': True, 'font_name': 'Aptos Display', 'font_size': 17, 'font_color': charcoal})
        subtitle = workbook.add_format({'font_name': 'Aptos', 'font_size': 10, 'font_color': '#656970'})
        heading = workbook.add_format({'bold': True, 'font_name': 'Aptos', 'bg_color': charcoal, 'font_color': 'white', 'bottom': 2, 'bottom_color': red, 'text_wrap': True, 'valign': 'vcenter'})
        label_fmt = workbook.add_format({'bold': True, 'font_name': 'Aptos', 'font_color': charcoal})
        text_fmt = workbook.add_format({'font_name': 'Aptos', 'font_color': charcoal})
        alternate = workbook.add_format({'font_name': 'Aptos', 'bg_color': pale, 'font_color': charcoal})
        number = workbook.add_format({'num_format': '0.00', 'font_name': 'Aptos', 'font_color': charcoal})
        number_alt = workbook.add_format({'num_format': '0.00', 'font_name': 'Aptos', 'bg_color': pale, 'font_color': charcoal})
        integer = workbook.add_format({'num_format': '0', 'font_name': 'Aptos', 'font_color': charcoal})
        integer_alt = workbook.add_format({'num_format': '0', 'font_name': 'Aptos', 'bg_color': pale, 'font_color': charcoal})
        warning = workbook.add_format({'bold': True, 'font_name': 'Aptos', 'font_color': red})
        absence = workbook.add_format({'bold': True, 'font_name': 'Aptos', 'font_color': red})
        absence_alt = workbook.add_format({'bold': True, 'font_name': 'Aptos', 'font_color': red, 'bg_color': pale})
        total_fmt = workbook.add_format({'bold': True, 'font_name': 'Aptos', 'bg_color': charcoal, 'font_color': 'white', 'num_format': '0.00'})
        ws = workbook.add_worksheet('Weekly Attendance')
        ws.freeze_panes(7, 2)
        ws.hide_gridlines(2)
        ws.set_landscape()
        ws.fit_to_pages(1, 0)
        ws.repeat_rows(6)
        ws.set_header('&LSAMBROS ELECTRICAL CONSULTANTS&RWEEKLY ATTENDANCE')
        ws.set_footer('&LInternal operations record&RPage &P of &N')
        ws.set_row(0, 30)
        ws.set_row(6, 36)
        ws.set_column('A:A', 22)
        ws.set_column('B:B', 28)
        ws.set_column('C:I', 17)
        ws.set_column('J:N', 16)
        ws.set_column('O:Q', 17)
        ws.set_column('R:T', 16)
        ws.merge_range(0, 0, 0, 8, 'SAMBROS  |  WEEKLY ATTENDANCE', title)
        ws.write(0, 9, 'OPERATIONS REPORT', subtitle)
        ws.write(1, 0, 'Period', label_fmt)
        ws.write(1, 1, '%s to %s' % (self.date_from, self.date_to))
        ws.write(2, 0, 'Team', label_fmt)
        ws.write(2, 1, self.team_id.name if self.team_id else 'All teams')
        ws.write(3, 0, 'Status', label_fmt)
        ws.write(3, 1, 'PROVISIONAL - OUTSTANDING REGISTERS' if self.pending_count else 'Complete - based on submitted/finalised registers', warning if self.pending_count else None)
        ws.write(4, 0, 'Outstanding registers', label_fmt)
        ws.write_number(4, 1, self.pending_count)
        ws.write(5, 0, 'Duplicate employee-days', label_fmt)
        ws.write_number(5, 1, self.duplicate_count)
        headers = ['Team', 'Employee', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun', 'Present', 'Absent', 'Authorised', 'Prevented', 'Not Scheduled', 'Gross Hours', 'Break Hours', 'Net Hours', 'OT 1.5x (h)', 'OT 2.0x (h)', 'Total OT (h)']
        for col, name in enumerate(headers):
            ws.write(6, col, name, heading)
        summary_label = workbook.add_format({'bold': True, 'bg_color': '#e5e9ec', 'font_color': charcoal})
        summary_pct = workbook.add_format({'bold': True, 'bg_color': '#e5e9ec', 'num_format': '0.0%', 'font_color': charcoal})
        def write_team_summary(row, summary):
            for label, key in [('Present', 'present'), ('Absent', 'absent'), ('Attendance %', 'rate')]:
                ws.write(row, 1, label, summary_label)
                for day_idx, counts in enumerate(summary['days']):
                    if counts['present'] or counts['absent'] or counts['other']:
                        if key == 'rate':
                            if counts['rate'] is not None:
                                ws.write_number(row, day_idx + 2, counts['rate'] / 100, summary_pct)
                        else:
                            ws.write_number(row, day_idx + 2, counts[key], summary_label)
                if key == 'absent':
                    ws.write_number(row, 9, summary['absent_week'], summary_label)
                elif key == 'rate' and summary['rate'] is not None:
                    ws.write_number(row, 16, summary['rate'] / 100, summary_pct)
                row += 1
            return row
        row = 7
        current_team_id = None
        management = self._daily_team_attendance()
        summaries = {team['id']: team for team in management['teams']}
        ordered_lines = list(self.line_ids.sorted(key=lambda l: (l.team_id.name or '', l.employee_id.name or '')))
        for line_index, line in enumerate(ordered_lines):
            if current_team_id is not None and line.team_id.id != current_team_id:
                row = write_team_summary(row, summaries[current_team_id])
            current_team_id = line.team_id.id
            row_alt = row % 2 == 0
            ws.set_row(row, 21)
            values = [line.team_id.name or '', line.employee_id.name or ''] + [getattr(line, 'day_%s' % d) or '' for d in range(7)] + [line.present_days, line.absent_days, line.authorised_days, line.prevented_days, line.not_scheduled_days, line.gross_hours, line.break_hours, line.net_hours]
            for col, value in enumerate(values):
                if 2 <= col <= 8:
                    # Daily display values are text in the Odoo summary because
                    # the PDF must show status codes. Export pure worked hours
                    # as numeric cells for formulas and pivot tables.
                    daily = str(value or '').strip()
                    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)\s*h', daily)
                    if match:
                        ws.write_number(row, col, float(match.group(1)), number_alt if row_alt else number)
                    elif daily in ('', '—', '-'):
                        ws.write_blank(row, col, None, alternate if row_alt else text_fmt)
                    else:
                        # Preserve ABS/AUTH/PREV/OFF and mixed-status or
                        # duplicate indicators; never misrepresent these as hours.
                        ws.write_string(row, col, daily, (absence_alt if row_alt else absence) if daily in ('ABS', 'PREV') else (alternate if row_alt else text_fmt))
                elif isinstance(value, (int, float)):
                    ws.write_number(row, col, value, (number_alt if row_alt else number) if col >= 14 else (integer_alt if row_alt else integer))
                else:
                    ws.write_string(row, col, str(value), alternate if row_alt else text_fmt)
            employee_ot = self._overtime_employee_weekly(line.employee_id.id)
            ws.write_number(row, 17, employee_ot['rate_15'], number)
            ws.write_number(row, 18, employee_ot['rate_20'], number)
            ws.write_number(row, 19, employee_ot['rate_15'] + employee_ot['rate_20'], number)
            row += 1
        if current_team_id is not None:
            row = write_team_summary(row, summaries[current_team_id])
        total_row = row
        ws.write(total_row, 13, 'TOTAL', heading)
        for col, value in [(14, self.gross_total), (15, self.break_total), (16, self.net_total)]:
            ws.write_number(total_row, col, value, total_fmt)
        # Team-level daily summaries are placed immediately below employee rows
        # in the PDF. Keep Excel focused on the employee and outstanding registers.
        ot = self._overtime_report_data()
        otws = workbook.add_worksheet('Overtime Review')
        otws.hide_gridlines(2)
        otws.freeze_panes(4, 3)
        otws.set_column('A:A', 22)
        otws.set_column('B:B', 27)
        otws.set_column('C:C', 15)
        otws.set_column('D:D', 19)
        otws.set_column('E:H', 17)
        otws.merge_range(0, 0, 0, 7, 'SAMBROS | OVERTIME REVIEW (CANDIDATE VS APPROVED)', title)
        otws.write(1, 0, 'Period', label_fmt)
        otws.write(1, 1, '%s to %s' % (self.date_from, self.date_to))
        otws.write(2, 0, 'Note', label_fmt)
        otws.write(2, 1, 'Public holiday pay requires statutory assessment; no payroll values calculated.', subtitle)
        for c, name in enumerate(['Team', 'Employee', 'Date', 'Category', 'Rate', 'Candidate h', 'Approved h', 'Decision']):
            otws.write(3, c, name, heading)
        for r, item in enumerate(sorted(ot['entries'].values(), key=lambda x: (x.team_id.name or '', x.employee_id.name or '', x.work_date)), 4):
            values = [item.team_id.name or '', item.employee_id.name or '', str(item.work_date),
                      dict(item._fields['category'].selection).get(item.category, item.category),
                      item.rate_multiplier, item.candidate_hours,
                      item.approved_hours if item.decision == 'approved' else 0.0,
                      dict(item._fields['decision'].selection).get(item.decision, item.decision)]
            for c, value in enumerate(values):
                if isinstance(value, (int, float)):
                    otws.write_number(r, c, value, number)
                else:
                    otws.write_string(r, c, str(value), text_fmt)
        otrow = 6 + len(ot['entries'])
        otws.write(otrow, 0, 'WEEKLY CATEGORY TOTALS', heading)
        for c, label in [(3, 'Category'), (5, 'Candidate h'), (6, 'Approved h')]:
            otws.write(otrow + 1, c, label, heading)
        for idx, (cat, label) in enumerate(ot['categories'], otrow + 2):
            otws.write(idx, 3, label, text_fmt)
            otws.write_number(idx, 5, ot['totals'][cat]['candidate'], number)
            otws.write_number(idx, 6, ot['totals'][cat]['approved'], number)
        pending = workbook.add_worksheet('Outstanding Registers')
        pending.set_column('A:D', 25)
        pending.hide_gridlines(2)
        pending.freeze_panes(1, 0)
        pending.set_row(0, 28)
        pending.autofilter(0, 0, max(1, len(self.pending_line_ids)), 3)
        for col, name in enumerate(['Register', 'Date', 'Team', 'Status']):
            pending.write(0, col, name, heading)
        for row, line in enumerate(self.pending_line_ids, 1):
            for col, value in enumerate([line.register_id.name or '', str(line.register_date or ''), line.team_id.name or '', dict(line._fields['register_state'].selection).get(line.register_state, line.register_state or '')]):
                pending.write_string(row, col, value, alternate if row % 2 == 0 else text_fmt)
        workbook.close()
        xlsx_bytes = output.getvalue()
        if not xlsx_bytes.startswith(b'PK') or len(xlsx_bytes) < 1000:
            raise UserError(_('Excel generation failed: no valid XLSX workbook was produced.'))
        # Odoo 20 stores binary attachment content using the raw field.
        # Avoid assigning base64-encoded content to raw, which expects bytes.
        attachment_vals = {
            'name': 'Sambros_Weekly_Attendance_%s.xlsx' % self.date_from,
            'type': 'binary',
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'res_model': self._name, 'res_id': self.id,
        }
        attachment_model = self.env['ir.attachment']
        if 'raw' in attachment_model._fields:
            attachment_vals['raw'] = xlsx_bytes
        else:
            attachment_vals['datas'] = base64.b64encode(xlsx_bytes)
        attachment = attachment_model.create(attachment_vals)
        # Fail visibly instead of silently downloading a zero-byte file.
        if not attachment.file_size:
            raise UserError(_('Excel export failed: Odoo saved a zero-byte attachment. Check attachment storage and server logs.'))
        return {'type': 'ir.actions.act_url',
                'url': '/web/content/%s?download=true' % attachment.id,
                'target': 'self'}


class WeeklySummaryLine(models.TransientModel):
    _name = 'sb.attendance.weekly.summary.line'
    _description = 'Weekly Employee Attendance Summary'
    _order = 'team_id, employee_id'

    summary_id = fields.Many2one('sb.attendance.weekly.summary', required=True, ondelete='cascade')
    team_id = fields.Many2one('sb.attendance.team', readonly=True)
    employee_id = fields.Many2one('hr.employee', readonly=True)
    present_days = fields.Integer(readonly=True)
    absent_days = fields.Integer(readonly=True)
    authorised_days = fields.Integer(readonly=True)
    prevented_days = fields.Integer(readonly=True)
    not_scheduled_days = fields.Integer(readonly=True)
    gross_hours = fields.Float(readonly=True, digits=(12, 2))
    break_hours = fields.Float(readonly=True, digits=(12, 2))
    net_hours = fields.Float(readonly=True, digits=(12, 2))
    day_0 = fields.Char(string='Mon', readonly=True)
    day_1 = fields.Char(string='Tue', readonly=True)
    day_2 = fields.Char(string='Wed', readonly=True)
    day_3 = fields.Char(string='Thu', readonly=True)
    day_4 = fields.Char(string='Fri', readonly=True)
    day_5 = fields.Char(string='Sat', readonly=True)
    day_6 = fields.Char(string='Sun', readonly=True)


class WeeklyPendingRegister(models.TransientModel):
    _name = 'sb.attendance.weekly.pending'
    _description = 'Outstanding Weekly Attendance Register'
    _order = 'register_date, register_id'

    summary_id = fields.Many2one('sb.attendance.weekly.summary', required=True, ondelete='cascade')
    register_id = fields.Many2one('sb.attendance.register', string='Register', readonly=True)
    register_date = fields.Date(string='Date', readonly=True)
    team_id = fields.Many2one('sb.attendance.team', string='Team', readonly=True)
    register_state = fields.Selection([('draft', 'Open'), ('closing', 'Closing'), ('submitted', 'Submitted'), ('final', 'Finalised')], string='Status', readonly=True)
