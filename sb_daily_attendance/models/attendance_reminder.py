"""Odoo Discuss-based morning attendance reminders and escalations."""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from odoo import api, fields, models, _, Command
from odoo.exceptions import UserError
from markupsafe import Markup, escape

TZ = ZoneInfo('Africa/Johannesburg')


class AttendanceReminderConfig(models.Model):
    _name = 'sb.attendance.reminder.config'
    _description = 'Attendance Reminder Configuration'
    _rec_name = 'company_id'
    _sql_constraints = [('company_unique', 'unique(company_id)', 'Only one attendance reminder configuration is allowed per company.')]

    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    enabled = fields.Boolean(default=False, help='Enable automatic reminders once recipients have been configured and tested.')
    hr_user_ids = fields.Many2many('res.users', 'sb_attendance_reminder_hr_rel', 'config_id', 'user_id', string='HR Recipients', domain=[('share', '=', False)])
    manager_user_ids = fields.Many2many('res.users', 'sb_attendance_reminder_manager_rel', 'config_id', 'user_id', string='Management Escalation Recipients', domain=[('share', '=', False)])
    weekdays_only = fields.Boolean(default=True, help='Skip Saturday and Sunday unless disabled. Public holidays are not automatically excluded.')

    sample_user_id = fields.Many2one(
        'res.users', string='Sample Reminder Recipient',
        domain=[('share', '=', False), ('active', '=', True)],
        help='Sends a sample Discuss notification without creating attendance or history.')

    sample_stage = fields.Selection([
        ('reminder', 'Foreman Reminder (10:05)'),
        ('escalation', 'Management Escalation (11:00)'),
    ], default='reminder', required=True, string='Sample Notification Type')

    def action_send_sample(self):
        self.ensure_one()
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only Attendance Administrators may send sample reminders.'))
        user = self.sample_user_id
        if not user or not user.active or user.share or not user.partner_id:
            raise UserError(_('Select an active internal Odoo user as the sample recipient.'))
        self.env['sb.attendance.reminder.log'].sudo()._post_sample_discuss(user, self.sample_stage)
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Sample reminder sent'),
                           'message': _('A SAMPLE %s notification was sent to %s. No attendance or notification history was changed.') % (dict(self._fields['sample_stage'].selection)[self.sample_stage], user.name),
                           'type': 'success', 'sticky': True}}

    def action_test_now(self):
        """Safely exercise the actual reminder pipeline without waiting for the cron.

        This sends one first-stage reminder for today's outstanding teams only;
        subsequent tests cannot duplicate it. It does not create any attendance.
        """
        self.ensure_one()
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only Attendance Administrators may test reminders.'))
        count = self.env['sb.attendance.reminder.log'].sudo()._dispatch_for_company(
            self.company_id, 'reminder', fields.Date.context_today(self), manual=True)
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Attendance reminder test'),
                           'message': _('%s team reminder(s) delivered to Discuss. Review Notification History. Existing reminders are not resent.') % count,
                           'type': 'success', 'sticky': True}}


    def action_test_escalation_now(self):
        """Send a real escalation only for today's outstanding registers."""
        self.ensure_one()
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only Attendance Administrators may test escalations.'))
        count = self.env['sb.attendance.reminder.log'].sudo()._dispatch_for_company(
            self.company_id, 'escalation', fields.Date.context_today(self), manual=True)
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': _('Actual attendance escalation test'),
                           'message': _('%s outstanding team escalation(s) delivered. Existing escalations are not resent.') % count,
                           'type': 'success', 'sticky': True}}


class AttendanceReminderLog(models.Model):
    _name = 'sb.attendance.reminder.log'
    _description = 'Attendance Reminder Notification History'
    _order = 'sent_at desc, id desc'
    _sql_constraints = [('unique_stage', 'unique(team_id, attendance_date, stage)',
                         'This team/date/stage notification has already been recorded.')]

    company_id = fields.Many2one('res.company', required=True, readonly=True)
    team_id = fields.Many2one('sb.attendance.team', required=True, readonly=True)
    attendance_date = fields.Date(required=True, readonly=True)
    stage = fields.Selection([('reminder', 'Foreman reminder'), ('escalation', 'HR / management escalation')], required=True, readonly=True)
    recipient_user_ids = fields.Many2many('res.users', 'sb_attendance_reminder_log_user_rel', 'log_id', 'user_id', readonly=True)
    sent_at = fields.Datetime(readonly=True)
    note = fields.Char(readonly=True)

    @api.model
    def _post_discuss(self, recipient, company, team, day, stage):
        """Use the same proven private Discuss conversation mechanism as v.40."""
        sender = self.env.user.partner_id
        target = recipient.partner_id
        if not target or not sender:
            return False
        Channel = self.env['discuss.channel'].sudo()
        name = 'Sambros Attendance Reminder - %s' % recipient.id
        channel = Channel.search([('name', '=', name), ('channel_type', '=', 'group')], limit=1)
        partners = sender | target
        if not channel:
            channel = Channel.create({'name': name, 'channel_type': 'group',
                                      'channel_partner_ids': [Command.link(p.id) for p in partners]})
        else:
            missing = partners - channel.channel_partner_ids
            if missing:
                channel.write({'channel_partner_ids': [Command.link(p.id) for p in missing]})
        heading = 'Attendance reminder' if stage == 'reminder' else 'Attendance escalation'
        instruction = ('Please submit your team attendance in Odoo as soon as operationally possible.'
                       if stage == 'reminder' else
                       'This team has still not submitted attendance. Please follow up with the responsible foreman.')
        body = Markup('<p><strong>%s — Sambros Attendance</strong></p>'
                      '<p><strong>Team:</strong> %s<br/><strong>Date:</strong> %s<br/>'
                      '<strong>Status:</strong> Not submitted by the reporting cutoff</p><p>%s</p>') % (
                          escape(heading), escape(team.name), escape(str(day)), escape(instruction))
        channel.with_context(mail_notify_noemail=True).message_post(
            body=body, message_type='comment', subtype_xmlid='mail.mt_comment', author_id=sender.id)
        return True

    @api.model
    def _post_sample_discuss(self, recipient, stage='reminder'):
        """Dedicated test chat: does not consume a real reminder or alter attendance."""
        sender = self.env.user.partner_id
        target = recipient.partner_id
        if not sender or not target:
            raise UserError(_('Both users must have linked contacts.'))
        Channel = self.env['discuss.channel'].sudo()
        name = 'Sambros Attendance SAMPLE Notification - %s' % recipient.id
        channel = Channel.search([('name', '=', name), ('channel_type', '=', 'group')], limit=1)
        partners = sender | target
        if not channel:
            channel = Channel.create({'name': name, 'channel_type': 'group',
                                      'channel_partner_ids': [Command.link(p.id) for p in partners]})
        else:
            missing = partners - channel.channel_partner_ids
            if missing:
                channel.write({'channel_partner_ids': [Command.link(p.id) for p in missing]})
        if stage == 'escalation':
            heading = 'TEST ONLY — Sambros Attendance Management Escalation'
            detail = 'This is a sample of the 11:00 HR / management escalation.'
        else:
            heading = 'TEST ONLY — Sambros Attendance Foreman Reminder'
            detail = 'This is a sample of the 10:05 foreman reminder.'
        body = Markup(
            '<p><strong>%s</strong></p><p>%s</p>'
            '<p><strong>No real attendance issue is being reported.</strong></p>'
            '<p>No attendance records or real reminder history were changed.</p>'
            '<p>Please check your Odoo Discuss and mobile notification.</p>') % (
                escape(heading), escape(detail))
        channel.with_context(mail_notify_noemail=True).message_post(
            body=body, message_type='comment', subtype_xmlid='mail.mt_comment',
            author_id=sender.id)
        return True

    @api.model
    def _dispatch_for_company(self, company, stage, day, manual=False):
        config = self.env['sb.attendance.reminder.config'].sudo().search([('company_id', '=', company.id)], limit=1)
        if not config or (not config.enabled and not manual):
            return 0
        teams = self.env['sb.attendance.team'].sudo().search([
            ('company_id', '=', company.id), ('active', '=', True)])
        registers = self.env['sb.attendance.register'].sudo().search([
            ('company_id', '=', company.id), ('date', '=', day), ('team_id', 'in', teams.ids)])
        state_by_team = {r.team_id.id: r.state for r in registers}
        count = 0
        for team in teams:
            if not team._expects_register(day):
                continue
            if state_by_team.get(team.id) in ('submitted', 'final'):
                continue
            if self.search_count([('team_id', '=', team.id), ('attendance_date', '=', day), ('stage', '=', stage)]):
                continue
            foreman_user = team.foreman_id.user_id
            if stage == 'reminder':
                recipients = foreman_user | config.hr_user_ids
            else:
                recipients = config.hr_user_ids | config.manager_user_ids
            recipients = recipients.filtered(lambda u: u.active and not u.share and u.partner_id)
            if not recipients:
                continue
            # One history row per team/date/stage. Never mark as sent before a message is posted.
            sent_users = self.env['res.users']
            for user in recipients:
                if self._post_discuss(user, company, team, day, stage):
                    sent_users |= user
            if sent_users:
                self.create({'company_id': company.id, 'team_id': team.id,
                             'attendance_date': day, 'stage': stage,
                             'recipient_user_ids': [Command.set(sent_users.ids)],
                             'sent_at': fields.Datetime.now(),
                             'note': 'Manual test' if manual else 'Automatic scheduled notification'})
                count += 1
        return count

    @api.model
    def cron_check_attendance(self):
        """Run every five minutes. SA local-time gates make execution idempotent."""
        now = datetime.now(TZ)
        day = now.date()
        current_minutes = now.hour * 60 + now.minute
        if current_minutes < 10 * 60 + 5:
            return
        configs = self.env['sb.attendance.reminder.config'].sudo().search([('enabled', '=', True)])
        for config in configs:
            try:
                with self.env.cr.savepoint():
                    self._dispatch_for_company(config.company_id, 'reminder', day)
                    if current_minutes >= 11 * 60:
                        self._dispatch_for_company(config.company_id, 'escalation', day)
            except Exception:
                # Log the failure while allowing other companies to be processed.
                import logging
                logging.getLogger(__name__).exception('Attendance notification failed for company %s', config.company_id.id)
