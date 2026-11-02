import base64
import re
from datetime import timedelta
from zoneinfo import ZoneInfo
from datetime import datetime
from markupsafe import Markup, escape
from odoo import api, fields, models, _
from odoo.exceptions import UserError


class AttendanceReportDistribution(models.Model):
    _name = 'sb.attendance.report.distribution'
    _description = 'Attendance Report Distribution Configuration'
    _rec_name = 'name'

    name = fields.Char(required=True, default='Sambros End-of-Day Attendance')
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    active = fields.Boolean(default=True)
    channel_id = fields.Many2one('discuss.channel', string='Discuss Channel', required=True)
    user_ids = fields.Many2many('res.users', string='Internal Email Recipients', domain=[('share', '=', False)])
    external_emails = fields.Text(string='Additional Email Addresses', help='Comma, semicolon or newline separated email addresses.')
    sender_email = fields.Char(string='Attendance Report Sender', default='admin@sambros.co.za', required=True, help='Used only for attendance distribution emails. Does not change global Odoo mail settings.')
    send_pdf = fields.Boolean(default=True)
    send_excel = fields.Boolean(default=True)
    automatic_enabled = fields.Boolean(default=False, help='Enable scheduled daily end-of-day distribution after testing.')
    send_hour = fields.Integer(default=17, string='Send Hour (SAST)')
    send_minute = fields.Integer(default=15, string='Send Minute (SAST)')
    log_ids = fields.One2many('sb.attendance.report.distribution.log', 'distribution_id', readonly=True)

    def _email_addresses(self):
        self.ensure_one()
        emails = [u.partner_id.email for u in self.user_ids if u.partner_id.email]
        emails += re.split(r'[,;\s]+', self.external_emails or '')
        emails = list(dict.fromkeys(e.strip() for e in emails if e.strip()))
        invalid = [e for e in emails if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', e)]
        if invalid:
            raise UserError(_('Invalid email address(es): %s') % ', '.join(invalid))
        return emails

    def action_send_test(self):
        return self._send_distribution(test=True)

    def action_send_now(self):
        return self._send_distribution(test=False)

    @api.model
    def cron_send_end_of_day(self):
        now = datetime.now(ZoneInfo('Africa/Johannesburg'))
        for config in self.sudo().search([('active', '=', True), ('automatic_enabled', '=', True)]):
            if not (0 <= config.send_hour <= 23 and 0 <= config.send_minute <= 59):
                continue
            current_minute = now.hour * 60 + now.minute
            target_minute = config.send_hour * 60 + config.send_minute
            if not target_minute <= current_minute < target_minute + 5:
                continue
            today = now.date()
            if self.env['sb.attendance.report.distribution.log'].sudo().search_count([
                ('distribution_id', '=', config.id), ('date', '=', today),
                ('is_test', '=', False), ('status', '=', 'queued')]):
                continue
            try:
                with self.env.cr.savepoint():
                    config._send_distribution(test=False, scheduled=True)
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Sambros attendance scheduled distribution failed for %s', config.id)

    def _send_distribution(self, test=False, scheduled=False):
        self.ensure_one()
        if not scheduled and not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only Attendance Administrators can distribute reports.'))
        if not (self.send_pdf or self.send_excel):
            raise UserError(_('Select at least one report attachment.'))
        sender = (self.sender_email or '').strip()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', sender):
            raise UserError(_('Enter a valid attendance report sender email address.'))
        emails = self._email_addresses()
        if not emails:
            raise UserError(_('Select internal users or enter additional email addresses.'))
        monday = fields.Date.context_today(self) - timedelta(days=fields.Date.context_today(self).weekday())
        summary = self.env['sb.attendance.weekly.summary'].create({'date_from': monday, 'company_id': self.company_id.id})
        summary.action_generate()
        attachments = self.env['ir.attachment']
        discuss_attachments = self.env['ir.attachment']
        if self.send_pdf:
            report = self.env.ref('sb_daily_attendance.action_report_weekly_attendance')
            pdf_data, pdf_format = self.env['ir.actions.report']._render_qweb_pdf(report.report_name, [summary.id])
            pdf_attachment = self._make_attachment('Sambros_Weekly_Attendance_%s.pdf' % monday, pdf_data, 'application/pdf')
            attachments |= pdf_attachment
            discuss_attachments |= pdf_attachment
        if self.send_excel:
            summary.action_download_xlsx()
            xlsx = self.env['ir.attachment'].search([('res_model', '=', summary._name), ('res_id', '=', summary.id), ('mimetype', '=', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')], order='id desc', limit=1)
            if not xlsx or not xlsx.file_size:
                raise UserError(_('Excel attachment could not be generated.'))
            attachments |= xlsx
        status = 'PROVISIONAL' if summary.pending_count else 'CURRENT WEEK SNAPSHOT'
        subject = 'Sambros Attendance | %s | %s' % (fields.Date.context_today(self), status)
        if test:
            subject = '[TEST] ' + subject
        heading = 'Sambros Attendance — End-of-Day %sDistribution' % ('Test ' if test else '')
        lines = [heading, 'Reporting week: %s to %s' % (summary.date_from, summary.date_to),
                 'Status: %s' % status, 'Outstanding registers: %s' % summary.pending_count,
                 'Duplicate employee-days: %s' % summary.duplicate_count,
                 'Only submitted and finalised registers contribute to hours.']
        if test:
            lines.append('This is a test distribution.')
        # Explicitly mark escaped HTML as safe for Odoo message_post; plain strings
        # are escaped by the mail API, displaying tags or collapsing newlines.
        body = Markup('<p><strong>%s</strong></p><p>%s</p>') % (escape(lines[0]), Markup('<br/>').join(escape(line) for line in lines[1:]))
        discuss_body = body
        log = self.env['sb.attendance.report.distribution.log'].create({
            'distribution_id': self.id, 'company_id': self.company_id.id,
            'date': fields.Date.context_today(self), 'recipient_emails': ', '.join(emails),
            'status': 'pending', 'pending_count': summary.pending_count, 'is_test': test,
        })
        mail = self.env['mail.mail'].sudo().create({
            'subject': subject, 'body_html': body, 'email_to': ','.join(emails),
            'email_from': sender,
            'reply_to': sender,
            'attachment_ids': [fields.Command.set(attachments.ids)],
            'auto_delete': False,
        })
        # Queue the email. Queueing is not proof of recipient delivery.
        self.channel_id.message_post(body=discuss_body, subject=subject, message_type='comment', attachment_ids=discuss_attachments.ids)
        log.write({'mail_id': mail.id, 'status': 'queued'})
        if scheduled:
            return True
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Distribution queued'),
            'message': _('Discuss message posted; email queued for %s recipient(s). Check outgoing mail and the distribution log.') % len(emails),
            'type': 'success', 'sticky': True}}

    def _make_attachment(self, name, content, mimetype):
        vals = {'name': name, 'type': 'binary', 'mimetype': mimetype, 'res_model': self._name, 'res_id': self.id}
        if 'raw' in self.env['ir.attachment']._fields:
            vals['raw'] = content
        else:
            vals['datas'] = base64.b64encode(content)
        return self.env['ir.attachment'].create(vals)


class AttendanceReportDistributionLog(models.Model):
    _name = 'sb.attendance.report.distribution.log'
    _description = 'Attendance Distribution History'
    _order = 'create_date desc'

    distribution_id = fields.Many2one('sb.attendance.report.distribution', required=True, ondelete='cascade')
    company_id = fields.Many2one('res.company', required=True)
    date = fields.Date(required=True)
    recipient_emails = fields.Text(readonly=True)
    pending_count = fields.Integer(readonly=True)
    status = fields.Selection([('pending', 'Preparing'), ('queued', 'Email Queued / Discuss Posted')], default='pending', readonly=True)
    mail_id = fields.Many2one('mail.mail', readonly=True)
    is_test = fields.Boolean(default=False, readonly=True)
