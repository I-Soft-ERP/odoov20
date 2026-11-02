from odoo import fields, models, _


class AttendanceSickLeave(models.Model):
    _inherit = 'sb.attendance.line'

    sick_leave_id = fields.Many2one(
        'hr.leave', string='HR Sick Leave Request', copy=False, readonly=True)
    sick_sync_state = fields.Selection([
        ('none', 'Not Applicable'),
        ('linked', 'Linked to HR'),
        ('review', 'HR Follow-up Required'),
    ], string='Sick Leave Link', default='none', copy=False, readonly=True)
    sick_sync_note = fields.Char(
        string='HR Follow-up Note', copy=False, readonly=True)

    def _sb_sync_sick_leave(self):
        """Link existing Time Off only; never create or approve HR leave automatically.

        Keep attendance operational if Time Off is not configured or a matching
        request is missing. Do not expose medical information to field users.
        """
        if self.env.context.get('sb_skip_sick_sync'):
            return

        Leave = self.env['hr.leave'].sudo() if 'hr.leave' in self.env.registry.models else False
        for line in self:
            def update(values):
                line.with_context(sb_skip_sick_sync=True).sudo().write(values)

            if line.status != 'sick':
                if line.sick_leave_id:
                    update({
                        'sick_sync_state': 'review',
                        'sick_sync_note': _('Attendance status changed; HR must review the existing leave link.'),
                    })
                else:
                    update({'sick_sync_state': 'none', 'sick_sync_note': False})
                continue

            if not Leave:
                update({
                    'sick_sync_state': 'review',
                    'sick_sync_note': _('Time Off is unavailable; HR must review this absence.'),
                })
                continue

            day = line.register_id.date
            employee = line.employee_id
            if not day or not employee:
                update({'sick_sync_state': 'review',
                        'sick_sync_note': _('Employee or attendance date is missing.')})
                continue

            try:
                with self.env.cr.savepoint():
                    # Odoo 20 Time Off uses work_entry_type_id (hr.work.entry.type)
                    # as the visible Time Type. Match existing requests, never
                    # create a second request or make an HR approval decision.
                    domain = [
                        ('employee_id', '=', employee.id),
                        ('request_date_from', '<=', day),
                        ('request_date_to', '>=', day),
                        ('state', 'not in', ('refuse', 'cancel')),
                    ]
                    candidates = Leave.search(domain, order='id desc')
                    matches = candidates.filtered(
                        lambda leave: leave.work_entry_type_id
                        and leave.work_entry_type_id.name == 'Sick Time Off'
                    )
                    if len(matches) == 1:
                        update({'sick_leave_id': matches.id,
                                'sick_sync_state': 'linked', 'sick_sync_note': False})
                    elif len(matches) > 1:
                        update({'sick_sync_state': 'review',
                                'sick_sync_note': _('Multiple sick-leave requests found; HR must reconcile them.')})
                    else:
                        update({'sick_sync_state': 'review',
                                'sick_sync_note': _('No matching Sick Time Off request found; HR follow-up required.')})
            except Exception:
                update({'sick_sync_state': 'review',
                        'sick_sync_note': _('Unable to match Time Off; HR must review the request.')})
