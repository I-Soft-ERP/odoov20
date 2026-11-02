# Stage 1: test Odoo Discuss chat delivery, independently of outgoing email.
# This is NOT a guarantee of operating-system push delivery.
from odoo import fields, models, _, Command
from odoo.exceptions import UserError
from markupsafe import Markup, escape


class AttendancePushTest(models.TransientModel):
    _name = 'sb.attendance.push.test'
    _description = 'Attendance Mobile Notification Test'

    target_user_id = fields.Many2one(
        'res.users', string='Test recipient', required=True,
        domain="[('share', '=', False), ('active', '=', True)]",
    )
    instructions = fields.Text(readonly=True, default=lambda self: _(
        'This test sends a message to a dedicated Odoo Discuss conversation, not an email. '
        'First confirm the message in Discuss on desktop and mobile. Then check whether '
        'Android and iOS display a notification when the app is backgrounded or locked. '
        'A Discuss message does not by itself prove operating-system push support.'
    ))

    def action_send_test(self):
        self.ensure_one()
        if not self.env.user.has_group('sb_daily_attendance.group_attendance_admin'):
            raise UserError(_('Only Attendance Administrators can run mobile notification tests.'))
        user = self.target_user_id
        if not user.active or user.share or not user.partner_id:
            raise UserError(_('Select an active internal Odoo user linked to a contact.'))

        # Dedicated private group conversation: reliable visibility in Discuss, not Inbox.
        # Reuse the conversation to avoid generating a new chat for every test.
        recipient = user.partner_id
        sender = self.env.user.partner_id
        partners = (recipient | sender)
        channel_name = 'Sambros Attendance Notification Test - %s' % user.id
        Channel = self.env['discuss.channel'].sudo()
        channel = Channel.search([
            ('name', '=', channel_name),
            ('channel_type', '=', 'group'),
        ], limit=1)
        if not channel:
            channel = Channel.create({
                'name': channel_name,
                'channel_type': 'group',
                'channel_partner_ids': [Command.link(p.id) for p in partners],
            })
        else:
            missing = partners - channel.channel_partner_ids
            if missing:
                channel.write({'channel_partner_ids': [Command.link(p.id) for p in missing]})

        body = Markup('<p><strong>Sambros Attendance — Discuss delivery test</strong></p>'
                      '<p>Recipient: %s</p>'
                      '<p>This is an internal Odoo Discuss message. Please check the '
                      'Discuss conversations list on desktop and mobile, then test '
                      'background and lock-screen notifications.</p>') % escape(user.name)
        message = channel.with_context(mail_notify_noemail=True).message_post(
            body=body,
            message_type='comment',
            subtype_xmlid='mail.mt_comment',
            author_id=sender.id,
        )
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {
                'title': _('Discuss test message created'),
                'message': _('Message %(message)s posted to Discuss conversation "%(channel)s" for %(user)s. Check Discuss chats, not only the Inbox. Mobile push delivery remains unverified.',
                             message=message.id, channel=channel.name, user=user.name),
                'type': 'success', 'sticky': True,
            },
        }
