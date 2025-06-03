# Copyright 2023 Dixmit
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import hashlib
from base64 import b64decode, b64encode
import base64
import io
from io import BytesIO
import time
from datetime import datetime

from PyPDF2 import PdfFileReader, PdfFileWriter
from reportlab.lib.utils import ImageReader
from reportlab.graphics.shapes import Drawing, Line, Rect
from reportlab.lib.colors import black, transparent
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.platypus import Image, Paragraph

from odoo import _, api, fields, models, http
from odoo.exceptions import ValidationError, UserError
from odoo.http import request
from werkzeug.urls import url_join
from odoo.tools import get_lang, DEFAULT_SERVER_DATE_FORMAT
import uuid
from hashlib import sha256
from json import dumps

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase.pdfmetrics import stringWidth

LOG_FIELDS = ['date', 'action', 'partner_id', 'request_state', 'latitude', 'longitude', 'ip', ]


def _fix_image_transparency(image):
    pixels = image.load()
    for x in range(image.size[0]):
        for y in range(image.size[1]):
            if pixels[x, y] == (0, 0, 0, 0):
                pixels[x, y] = (255, 255, 255, 0)


class SignOcaRequest(models.Model):
    _name = "sign.oca.request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "Sign Request"

    def _default_access_token(self):
        return str(uuid.uuid4())

    @api.model
    def initialize_new(self, id, signers, followers, reference, subject, message, send=True, without_mail=False):
        sign_users = self.env['res.users'].search(
            [('partner_id', 'in', [signer['partner_id'] for signer in signers])]).filtered(
            lambda u: u.has_group('sign_oca.sign_oca_group_user'))
        sign_request = self.create({'template_id': id, 'name': reference})
        sign_request.message_subscribe(partner_ids=followers)
        sign_request.activity_update(sign_users)
        sign_request.set_signers(signers)
        if send:
            sign_request.action_sent(subject, message)
        if without_mail:
            sign_request.action_sent_without_mail()
        return {
            'id': sign_request.id,
            'token': sign_request.access_token,
            'sign_token': sign_request.signer_ids.filtered(lambda r: r.partner_id == self.env.user.partner_id)[
                          :1].access_token,
        }

    @api.model
    def activity_update(self, sign_users):
        for user in sign_users:
            self.with_context(mail_activity_quick_update=True).activity_schedule(
                'mail.mail_activity_data_todo',
                user_id=user.id
            )

    name = fields.Char(required=True, string='Document Name')
    active = fields.Boolean(default=True)
    template_id = fields.Many2one("sign.oca.template", readonly=True)
    data = fields.Binary(
        required=True, readonly=True, states={"draft": [("readonly", False)]}
    )
    user_id = fields.Many2one(
        comodel_name="res.users",
        string="Responsible",
        readonly=True,
        states={"draft": [("readonly", False)]},
        default=lambda self: self.env.user,
        required=True,
    )
    favorited_ids = fields.Many2many('res.users', string="Favorite of")
    record_ref = fields.Reference(
        lambda self: [
            (m.model, m.name)
            for m in self.env["ir.model"].search(
                [("transient", "=", False), ("model", "not like", "sign.oca")]
            )
        ],
        string="Object",
        readonly=True,
        states={"draft": [("readonly", False)]},
    )
    signed = fields.Boolean(copy=False)
    signer_ids = fields.One2many(
        "sign.oca.request.signer",
        inverse_name="request_id",
        auto_join=True,
        copy=True,
        readonly=True,
        states={"draft": [("readonly", False)]},
    )
    signer_id = fields.Many2one(
        comodel_name="sign.oca.request.signer",
        compute="_compute_signer_id",
        help="The signer related to the active user.",
    )
    access_token = fields.Char('Security Token', required=True, default=_default_access_token, readonly=True)
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("sent", "Sent"),
            ("signed", "Signed"),
            ("cancel", "Cancelled"),
        ],
        default="draft",
        readonly=True,
        required=True,
        copy=False,
        tracking=True,
    )
    signed_count = fields.Integer(compute="_compute_signed_count")
    signer_count = fields.Integer(compute="_compute_signer_count")
    to_sign = fields.Boolean(compute="_compute_to_sign")
    signatory_data = fields.Serialized(
        default=lambda r: {},
        readonly=True,
        copy=False,
    )
    current_hash = fields.Char(readonly=True, copy=False)
    company_id = fields.Many2one(
        "res.company",
        default=lambda r: r.env.company.id,
        required=True,
        readonly=True,
        states={"draft": [("readonly", False)]},
    )
    next_item_id = fields.Integer(compute="_compute_next_item_id")
    template_tags = fields.Many2many('sign.template.tag', string='Template Tags', related='template_id.tag_ids')
    sign_log_ids = fields.One2many('sign.oca.request.log', 'request_id', string="Logs",
                                   help="Activity logs linked to this request")
    nb_wait = fields.Integer(string="Sent Requests", compute="_compute_count", store=True)
    nb_closed = fields.Integer(string="Completed Signatures", compute="_compute_count", store=True)
    nb_total = fields.Integer(string="Requested Signatures", compute="_compute_count", store=True)
    progress = fields.Char(string="Progress", compute="_compute_count", compute_sudo=True)
    start_sign = fields.Boolean(string="Signature Started", help="At least one signer has signed the document.",
                                compute="_compute_count", compute_sudo=True)
    integrity = fields.Boolean(string="Integrity of the Sign request", compute='_compute_hashes', compute_sudo=True)
    completion_date = fields.Date(string="Completion Date", compute="_compute_count", compute_sudo=True)

    @api.depends('signer_ids.state')
    def _compute_count(self):
        for rec in self:
            wait, closed = 0, 0
            for s in rec.signer_ids:
                if s.state == "sent":
                    wait += 1
                if s.state == "completed":
                    closed += 1
            rec.nb_wait = wait
            rec.nb_closed = closed
            rec.nb_total = wait + closed
            rec.start_sign = bool(closed)
            rec.progress = "{} / {}".format(closed, wait + closed)
            if closed:
                rec.start_sign = True
            signed_requests = rec.signer_ids.filtered('signed_on')
            if wait == 0 and closed and signed_requests:
                last_completed_request = signed_requests.sorted(key=lambda i: i.signed_on, reverse=True)[0]
                rec.completion_date = last_completed_request.signed_on
            else:
                rec.completion_date = None

    @api.onchange("progress", "start_sign")
    def _compute_hashes(self):
        for document in self:
            try:
                document.integrity = self.sign_log_ids._check_document_integrity()
            except Exception:
                document.integrity = False

    def _get_final_recipients(self):
        self.ensure_one()
        all_recipients = set(self.signer_ids.mapped('signer_email'))
        all_recipients |= set(self.mapped('message_follower_ids.partner_id.email'))
        # Remove False from all_recipients to avoid crashing later
        all_recipients.discard(False)
        return all_recipients

    def _get_font(self):
        custom_font = self.env["ir.config_parameter"].sudo().get_param("sign_oca.use_custom_font")
        # The font must be a TTF font. The tool 'otf2ttf' may be useful for conversion.
        if custom_font:
            pdfmetrics.registerFont(TTFont(custom_font, custom_font + ".ttf"))
            return custom_font
        return "Helvetica"

    def _get_normal_font_size(self):
        return 0.015

    def generate_completed_document(self, password=""):
        self.ensure_one()
        if not self.template_id.item_ids:
            self.data = self.template_id.attachment_id.datas
            return

        old_pdf = PdfFileReader(io.BytesIO(base64.b64decode(self.template_id.attachment_id.datas)), strict=False,
                                overwriteWarnings=False)

        isEncrypted = old_pdf.isEncrypted
        if isEncrypted and not old_pdf.decrypt(password):
            # password is not correct
            return

        font = self._get_font()
        normalFontSize = self._get_normal_font_size()

        packet = io.BytesIO()
        can = canvas.Canvas(packet)
        itemsByPage = self.template_id.item_ids.getByPage()
        SignItemValue = self.env['sign.oca.request.signer.value']
        for p in range(0, old_pdf.getNumPages()):
            page = old_pdf.getPage(p)
            # Absolute values are taken as it depends on the MediaBox template PDF metadata, they may be negative
            width = float(abs(page.mediaBox.getWidth()))
            height = float(abs(page.mediaBox.getHeight()))

            # Set page orientation (either 0, 90, 180 or 270)
            rotation = page.get('/Rotate')
            if rotation:
                can.rotate(rotation)
                # Translate system so that elements are placed correctly
                # despite of the orientation
                if rotation == 90:
                    width, height = height, width
                    can.translate(0, -height)
                elif rotation == 180:
                    can.translate(-width, -height)
                elif rotation == 270:
                    width, height = height, width
                    can.translate(-width, 0)

            items = itemsByPage[p + 1] if p + 1 in itemsByPage else []
            for item in items:
                value = SignItemValue.search([('item_id', '=', item.id), ('request_id', '=', self.id)],
                                             limit=1)
                if not value or not value.value:
                    continue

                value = value.value

                if item.field_id.item_type == "text":
                    can.setFont(font, height * item.height * 0.8)
                    can.drawString(width * item.posX, height * (1 - item.posY - item.height * 0.9), value)

                elif item.field_id.item_type == "selection":
                    content = []
                    for option in item.option_ids:
                        if option.id != int(value):
                            content.append("<strike>%s</strike>" % (option.value))
                        else:
                            content.append(option.value)
                    font_size = height * normalFontSize * 0.8
                    can.setFont(font, font_size)
                    text = " / ".join(content)
                    string_width = stringWidth(text.replace("<strike>", "").replace("</strike>", ""), font, font_size)
                    p = Paragraph(text, getSampleStyleSheet()["Normal"])
                    w, h = p.wrap(width, height)
                    posX = width * (item.posX + item.width * 0.5) - string_width // 2
                    posY = height * (1 - item.posY - item.height * 0.5) - h // 2
                    p.drawOn(can, posX, posY)

                elif item.field_id.item_type == "textarea":
                    can.setFont(font, height * normalFontSize * 0.8)
                    lines = value.split('\n')
                    y = (1 - item.posY)
                    for line in lines:
                        y -= normalFontSize * 0.9
                        can.drawString(width * item.posX, height * y, line)
                        y -= normalFontSize * 0.1

                elif item.field_id.item_type == "checkbox":
                    can.setFont(font, height * item.height * 0.8)
                    value = 'X' if value == 'on' else ''
                    can.drawString(width * item.posX, height * (1 - item.posY - item.height * 0.9), value)

                elif item.field_id.item_type == "signature" or item.field_id.item_type == "initial":
                    image_reader = ImageReader(io.BytesIO(base64.b64decode(value[value.find(',') + 1:])))
                    _fix_image_transparency(image_reader._image)
                    can.drawImage(image_reader, width * item.posX, height * (1 - item.posY - item.height),
                                  width * item.width, height * item.height, 'auto', True)

            can.showPage()

        can.save()

        item_pdf = PdfFileReader(packet, overwriteWarnings=False)
        new_pdf = PdfFileWriter()

        for p in range(0, old_pdf.getNumPages()):
            page = old_pdf.getPage(p)
            page.mergePage(item_pdf.getPage(p))
            new_pdf.addPage(page)

        if isEncrypted:
            new_pdf.encrypt(password)

        output = io.BytesIO()
        new_pdf.write(output)
        self.data = base64.b64encode(output.getvalue())
        output.close()

    def send_completed_document(self):
        self.ensure_one()
        if len(self.signer_ids) <= 0 or self.state != 'signed':
            return False

        if not self.data:
            self.generate_completed_document()

        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        attachment = self.env['ir.attachment'].create({
            'name': "%s.pdf" % self.name if self.name.split('.')[-1] != 'pdf' else self.name,
            'datas': self.data,
            'type': 'binary',
            'res_model': self._name,
            'res_id': self.id,
        })
        report_action = self.env.ref('sign_oca.action_sign_oca_request_print_logs')
        public_user = self.env.ref('base.public_user', raise_if_not_found=False)
        if not public_user:
            # public user was deleted, fallback to avoid crash (info may leak)
            public_user = self.env.user
        pdf_content, __ = report_action.with_user(public_user).sudo()._render_qweb_pdf(self.id)
        attachment_log = self.env['ir.attachment'].create({
            'name': "Certificate of completion - %s.pdf" % time.strftime('%Y-%m-%d - %H:%M:%S'),
            'datas': base64.b64encode(pdf_content),
            'type': 'binary',
            'res_model': self._name,
            'res_id': self.id,
        })
        tpl = self.env.ref('sign_oca.sign_oca_template_mail_completed')
        for signer in self.signer_ids:
            if not signer.signer_email:
                continue
            signer_lang = get_lang(self.env, lang_code=signer.partner_id.lang).code
            tpl = tpl.with_context(lang=signer_lang)
            body = tpl._render({
                'record': self,
                'link': url_join(base_url, 'sign_oca/document/%s/%s' % (self.id, signer.access_token)),
                'subject': '%s signed' % self.name,
                'body': False,
            }, engine='ir.qweb', minimal_qcontext=True)

            if not self.create_uid.email:
                raise UserError(_("Please configure the sender's email address"))
            if not signer.partner_id.email:
                raise UserError(_("Please configure the signer's email address"))

            self.env['sign.oca.request']._message_send_mail(
                body, 'mail.mail_notification_light',
                {'record_name': self.name},
                {'model_description': 'signature', 'company': self.create_uid.company_id},
                {'email_from': self.create_uid.email_formatted,
                 'author_id': self.create_uid.partner_id.id,
                 'email_to': signer.partner_id.email_formatted,
                 'subject': _('%s has been signed', self.name),
                 'attachment_ids': [(4, attachment.id), (4, attachment_log.id)]},
                force_send=True,
                lang=signer_lang,
            )

        tpl = self.env.ref('sign_oca.sign_oca_template_mail_completed')
        for follower in self.mapped('message_follower_ids.partner_id') - self.signer_ids.mapped('partner_id'):
            if not follower.email:
                continue
            if not self.create_uid.email:
                raise UserError(_("Please configure the sender's email address"))

            tpl_follower = tpl.with_context(lang=get_lang(self.env, lang_code=follower.lang).code)
            body = tpl._render({
                'record': self,
                'link': url_join(base_url, 'sign/document/%s/%s' % (self.id, self.access_token)),
                'subject': '%s signed' % self.name,
                'body': '',
            }, engine='ir.qweb', minimal_qcontext=True)
            self.env['sign.oca.request']._message_send_mail(
                body, 'mail.mail_notification_light',
                {'record_name': self.name},
                {'model_description': 'signature', 'company': self.create_uid.company_id},
                {'email_from': self.create_uid.email_formatted,
                 'author_id': self.create_uid.partner_id.id,
                 'email_to': follower.email_formatted,
                 'subject': _('%s has been signed', self.name)},
                lang=follower.lang,
            )

        return True

    def _check_after_compute(self):
        for rec in self:
            if rec.state == 'sent' and rec.signer_count == len(rec.signer_ids) and len(
                    rec.signer_ids) > 0:  # All signed
                rec.action_signed()

    def action_signed(self):
        self.write({'state': 'signed'})
        self.env.cr.commit()
        if not self.check_is_encrypted():
            self.send_completed_document()

    def check_is_encrypted(self):
        self.ensure_one()
        if not self.template_id.item_ids:
            return False

        old_pdf = PdfFileReader(io.BytesIO(base64.b64decode(self.template_id.attachment_id.datas)), strict=False,
                                overwriteWarnings=False)
        return old_pdf.isEncrypted

    def go_to_document(self):
        self.ensure_one()
        request_item = self.signer_ids.filtered(
            lambda r: r.partner_id and r.partner_id.id == self.env.user.partner_id.id)[:1]
        return {
            'name': self.name,
            'type': 'ir.actions.client',
            'tag': 'sign_oca.Document',
            'context': {
                'id': self.id,
                'token': self.access_token,
                'sign_token': request_item.access_token if request_item and request_item.state == "sent" else None,
                'create_uid': self.create_uid.id,
                'state': self.state,
            },
        }

    def set_signers(self, signers):
        SignRequestItem = self.env['sign.oca.request.signer']

        for rec in self:
            rec.signer_ids.filtered(lambda r: not r.partner_id or not r.role_id).unlink()
            ids_to_remove = []
            for request_item in rec.signer_ids:
                for i in range(0, len(signers)):
                    if signers[i]['partner_id'] == request_item.partner_id.id and signers[i][
                        'role'] == request_item.role_id.id:
                        signers.pop(i)
                        break
                else:
                    ids_to_remove.append(request_item.id)

            SignRequestItem.browse(ids_to_remove).unlink()
            for signer in signers:
                SignRequestItem.create({
                    'partner_id': signer['partner_id'],
                    'request_id': rec.id,
                    'role_id': signer['role'],
                })

    def action_sent_without_mail(self):
        self.write({'state': 'sent'})
        for sign_request in self:
            for sign_request_item in sign_request.signer_ids:
                sign_request_item.write({'state': 'sent'})
                Log = http.request.env['sign.oca.request.log'].sudo()
                vals = Log._prepare_vals_from_request(sign_request)
                vals['action'] = 'create'
                vals = Log._update_vals_with_http_request(vals)
                Log.create(vals)

    def action_draft(self):
        self.write({'data': None, 'access_token': self._default_access_token()})

    def action_sent(self, subject=None, message=None):
        self.write({'state': 'sent'})
        for sign_request in self:
            ignored_partners = []
            for request_item in sign_request.signer_ids:
                if request_item.state != 'draft':
                    ignored_partners.append(request_item.partner_id.id)
            included_request_items = sign_request.signer_ids.filtered(
                lambda r: not r.partner_id or r.partner_id.id not in ignored_partners)

            if sign_request.send_signature_accesses(subject, message, ignored_partners=ignored_partners):
                Log = http.request.env['sign.oca.request.log'].sudo()
                vals = Log._prepare_vals_from_request(sign_request)
                vals['action'] = 'create'
                vals = Log._update_vals_with_http_request(vals)
                Log.create(vals)
                followers = sign_request.message_follower_ids.mapped('partner_id')
                followers -= sign_request.create_uid.partner_id
                followers -= sign_request.signer_ids.mapped('partner_id')
                if followers:
                    sign_request.send_follower_accesses(followers, subject, message)
                included_request_items.action_sent()
            else:
                sign_request.action_draft()

    @api.model
    def _message_send_mail(self, body, notif_template_xmlid, message_values, notif_values, mail_values,
                           force_send=False, **kwargs):
        """ Shortcut to send an email. """
        default_lang = get_lang(self.env, lang_code=kwargs.get('lang')).code
        lang = kwargs.get('lang', default_lang)
        sign_request = self.with_context(lang=lang)

        msg = sign_request.env['mail.message'].sudo().new(dict(body=body, **message_values))
        notif_layout = sign_request.env.ref(notif_template_xmlid)
        body_html = notif_layout._render(dict(message=msg, **notif_values), engine='ir.qweb', minimal_qcontext=True)
        body_html = sign_request.env['mail.render.mixin']._replace_local_links(body_html)

        mail = sign_request.env['mail.mail'].sudo().create(dict(body_html=body_html, state='outgoing', **mail_values))
        if force_send:
            mail.send()
        return mail

    @api.depends("signatory_data")
    def _compute_next_item_id(self):
        for record in self:
            record.next_item_id = (
                                          record.signatory_data
                                          and max([int(key) for key in record.signatory_data.keys()])
                                          or 0
                                  ) + 1

    @api.depends("signer_ids")
    @api.depends_context("uid")
    def _compute_signer_id(self):
        user = self.env.user
        for record in self:
            user_diff_roles = record.signer_ids.filtered(
                lambda x: x.partner_id == user.partner_id.commercial_partner_id
            )
            record.signer_id = (
                fields.first(user_diff_roles.filtered(lambda x: x.is_allow_signature))
                if user_diff_roles.filtered(lambda x: x.is_allow_signature)
                else fields.first(user_diff_roles)
            )

    @api.depends(
        "signer_id",
        "signer_id.is_allow_signature",
    )
    def _compute_to_sign(self):
        for record in self:
            record.to_sign = (
                record.signer_id.is_allow_signature if record.signer_id else False
            )

    def sign(self):
        self.ensure_one()
        if not self.signer_id:
            return self.get_formview_action()
        return self.signer_id.sign()

    def preview(self):
        self.ensure_one()
        self._set_action_log("view")
        return {
            "type": "ir.actions.client",
            "tag": "sign_oca_preview",
            "name": self.name,
            "params": {
                "res_model": self._name,
                "res_id": self.id,
            },
        }

    def get_info(self):
        self.ensure_one()
        return {
            "name": self.name,
            "items": self.signatory_data,
            "roles": [
                {"id": signer.id, "name": signer.role_id.name}
                for signer in self.signer_ids
            ],
            "fields": [
                {"id": field.id, "name": field.name}
                for field in self.env["sign.oca.field"].search([])
            ],
        }

    def _ensure_draft(self):
        self.ensure_one()
        if not self.signer_ids:
            raise ValidationError(
                _("There are no signers, please fill them before configuring it")
            )
        if not self.state == "draft":
            raise ValidationError(_("You can only configure requests in draft state"))

    def configure(self):
        self._ensure_draft()
        self._set_action_log("configure")
        return {
            "type": "ir.actions.client",
            "tag": "sign_oca_configure",
            "name": self.name,
            "params": {
                "res_model": self._name,
                "res_id": self.id,
            },
        }

    def delete_item(self, item_id):
        self._ensure_draft()
        data = self.signatory_data
        data.pop(str(item_id))
        self.signatory_data = data
        self._set_action_log("delete_field")

    def set_item_data(self, item_id, vals):
        self._ensure_draft()
        data = self.signatory_data
        data[str(item_id)].update(vals)
        self.signatory_data = data
        self._set_action_log("edit_field")

    def add_item(self, item_vals):
        self._ensure_draft()
        item_id = self.next_item_id
        field_id = self.env["sign.oca.field"].browse(item_vals["field_id"])
        signatory_data = self.signatory_data
        signatory_data[item_id] = {
            "id": item_id,
            "field_id": field_id.id,
            "item_type": field_id.item_type,
            "required": False,
            "name": field_id.name,
            "role_id": self.signer_ids[0].role_id.id,
            "page": 1,
            "posX": 0,
            "posY": 0,
            "width": 0,
            "height": 0,
            "value": False,
            "default_value": field_id.default_value,
            "placeholder": "",
        }
        signatory_data[item_id].update(item_vals)
        self.signatory_data = signatory_data
        self._set_action_log("add_field")
        return signatory_data[item_id]

    def cancel(self):
        self.write({"state": "cancel"})
        self._set_action_log("cancel")

    @api.depends("signer_ids")
    def _compute_signer_count(self):
        for record in self:
            record.signer_count = len(record.signer_ids)

    @api.depends("signer_ids", "signer_ids.signed_on")
    def _compute_signed_count(self):
        for record in self:
            record.signed_count = len(record.signer_ids.filtered(lambda r: r.signed_on))

    def open_template(self):
        return self.template_id.configure()

    def send_signature_accesses(self, subject=None, message=None, ignored_partners=[]):
        self.ensure_one()
        if len(self.signer_ids) <= 0 or (set(self.signer_ids.mapped('role_id')) != set(
                self.template_id.item_ids.mapped('role_id'))):
            return False

        self.signer_ids.filtered(
            lambda r: not r.partner_id or r.partner_id.id not in ignored_partners).send_signature_accesses(subject,
                                                                                                           message)
        return True

    def action_send(self, sign_now=False, message=""):
        self.ensure_one()
        if self.state != "draft":
            return
        self._set_action_log("validate")
        self.state = "sent"
        for signer in self.signer_ids:
            signer._portal_ensure_token()
            if sign_now and signer.partner_id == self.env.user.partner_id:
                continue
            view = self.env.ref("sign_oca.sign_oca_template_mail")
            render_result = view._render(
                {"record": signer, "body": message, "link": signer.access_url},
                engine="ir.qweb",
                minimal_qcontext=True,
            )
            self.env["mail.thread"].message_notify(
                body=render_result,
                partner_ids=signer.partner_id.ids,
                subject=_("New document to sign"),
                subtype_id=self.env.ref("mail.mt_comment").id,
                mail_auto_delete=False,
                email_layout_xmlid="mail.mail_notification_light",
            )

    def _check_signed(self):
        self.ensure_one()
        if self.state != "sent":
            return
        if all(self.mapped("signer_ids.signed_on")):
            self.state = "signed"

    def _set_action_log_vals(self, action, **kwargs):
        vals = kwargs.copy()
        vals.update(
            {"action": action, "request_id": self.id, "ip": self._get_action_log_ip()}
        )
        return vals

    def _get_action_log_ip(self):
        if not request or not hasattr(request, "httprequest"):
            # This comes from a server call. Set as localhost
            return "0.0.0.0"
        return request.httprequest.access_route[-1]

    def _set_action_log(self, action, **kwargs):
        self.ensure_one()
        return (
            self.env["sign.oca.request.log"]
            .sudo()
            .create(self._set_action_log_vals(action, **kwargs))
        )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        # for record in records:
        #     record._set_action_log("create")
        return records


class SignOcaRequestSigner(models.Model):
    _name = "sign.oca.request.signer"
    _inherit = "portal.mixin"
    _description = "Sign Request Value"

    @api.model
    def resend_access(self, id):
        sign_request_item = self.browse(id)
        subject = _("Signature Request - %s") % (sign_request_item.request_id.template_id.attachment_id.name)
        self.browse(id).send_signature_accesses(subject=subject)

    def _default_access_token(self):
        return str(uuid.uuid4())

    data = fields.Binary(attachment=True)
    access_token = fields.Char(required=True, default=_default_access_token, readonly=True)
    access_via_link = fields.Boolean('Accessed Through Token')
    request_id = fields.Many2one("sign.oca.request", required=True, ondelete="cascade")
    signer_value_ids = fields.One2many('sign.oca.request.signer.value', 'signer_id', string="Value")
    reference = fields.Char(related='request_id.name', string="Document Name")
    partner_name = fields.Char(related="partner_id.name")
    partner_id = fields.Many2one("res.partner", string='Contact', ondelete="restrict")
    signer_email = fields.Char(related='partner_id.email', readonly=False, depends=(['partner_id']), store=True)
    role_id = fields.Many2one("sign.oca.role", ondelete="restrict")
    signed_on = fields.Datetime(readonly=True)
    signature_hash = fields.Char(readonly=True)
    sms_token = fields.Char('SMS Token', readonly=True)
    model = fields.Char(compute="_compute_model", store=True)
    res_id = fields.Integer(compute="_compute_res_id", store=True)
    is_allow_signature = fields.Boolean(compute="_compute_is_allow_signature")
    state = fields.Selection([
        ("draft", "Draft"),
        ("sent", "To Sign"),
        ("completed", "Completed")
    ], readonly=True, default="draft")
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))

    def action_sent(self):
        self.write({'state': 'sent'})
        self.mapped('request_id')._check_after_compute()

    def action_completed(self):
        date = fields.Date.context_today(self).strftime(DEFAULT_SERVER_DATE_FORMAT)
        self.write({'signed_on': date, 'state': 'completed'})
        self.mapped('request_id')._check_after_compute()

    def _message_send_mail(self, body, notif_template_xmlid, message_values, notif_values, mail_values,
                           force_send=False, **kwargs):
        """ Shortcut to send an email. """
        default_lang = get_lang(self.env, lang_code=kwargs.get('lang')).code
        lang = kwargs.get('lang', default_lang)
        sign_request = self.with_context(lang=lang)

        # the notif layout wrapping expects a mail.message record, but we don't want
        # to actually create the record
        # See @tde-banana-odoo for details
        msg = sign_request.env['mail.message'].sudo().new(dict(body=body, **message_values))
        notif_layout = sign_request.env.ref(notif_template_xmlid)
        body_html = notif_layout._render(dict(message=msg, **notif_values), engine='ir.qweb', minimal_qcontext=True)
        body_html = sign_request.env['mail.render.mixin']._replace_local_links(body_html)

        mail = sign_request.env['mail.mail'].sudo().create(dict(body_html=body_html, state='outgoing', **mail_values))
        if force_send:
            mail.send()
        return mail

    def send_signature_accesses(self, subject=None, message=None):
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        tpl = self.env.ref('sign_oca.sign_oca_template_mail_request')
        for signer in self:
            if not signer.partner_id or not signer.partner_id.email:
                continue
            if not signer.create_uid.email:
                continue
            signer_lang = get_lang(self.env, lang_code=signer.partner_id.lang).code
            tpl = tpl.with_context(lang=signer_lang)
            body = tpl._render({
                'record': signer,
                'link': url_join(base_url, "sign_oca/document/mail/%(request_id)s/%(access_token)s" % {
                    'request_id': signer.request_id.id, 'access_token': signer.access_token}),
                'subject': subject,
                'body': message if message != '<p><br></p>' else False,
            }, engine='ir.qweb', minimal_qcontext=True)

            if not signer.partner_id.email:
                raise UserError(_("Please configure the signer's email address"))
            self.env['sign.oca.request']._message_send_mail(
                body, 'mail.mail_notification_light',
                {'record_name': signer.request_id.name},
                {'model_description': 'signature', 'company': signer.create_uid.company_id},
                {'email_from': signer.create_uid.email_formatted,
                 'author_id': signer.create_uid.partner_id.id,
                 'email_to': signer.partner_id.email_formatted,
                 'subject': subject},
                force_send=True,
                lang=signer_lang,
            )

    @api.depends("request_id.record_ref")
    def _compute_model(self):
        for item in self.filtered(lambda x: x.request_id.record_ref):
            item.model = item.request_id.record_ref._name

    @api.depends("request_id.record_ref")
    def _compute_res_id(self):
        for item in self.filtered(lambda x: x.request_id.record_ref):
            item.res_id = item.request_id.record_ref.id

    @api.depends("signed_on", "partner_id", "partner_id.commercial_partner_id")
    @api.depends_context("uid")
    def _compute_is_allow_signature(self):
        user = self.env.user
        for item in self:
            item.is_allow_signature = bool(
                not item.signed_on
                and item.partner_id == user.partner_id.commercial_partner_id
            )

    def _compute_access_url(self):
        super()._compute_access_url()
        for record in self:
            record.access_url = "/sign_oca/document/%s/%s" % (
                record.id,
                record.access_token,
            )

    @api.onchange("role_id")
    def _onchange_role_id(self):
        for item in self:
            item.partner_id = item.role_id._get_partner_from_record(
                item.request_id.record_ref
            )

    def get_info(self, access_token=False):
        self.ensure_one()
        self._set_action_log("view", access_token=access_token)
        return {
            "role_id": self.role_id.id if not self.signed_on else False,
            "name": self.request_id.template_id.name,
            "items": self.request_id.signatory_data,
            "to_sign": self.request_id.to_sign,
            "partner": {
                "id": self.partner_id.id,
                "name": self.partner_id.name,
                "email": self.partner_id.email,
                "phone": self.partner_id.phone,
            },
        }

    def sign(self, signature):
        self.ensure_one()
        if not isinstance(signature, dict):
            self.data = signature
        else:
            SignItemValue = self.env['sign.oca.request.signer.value']
            request = self.request_id

            signerItems = request.template_id.item_ids.filtered(
                lambda r: not r.role_id or r.role_id.id == self.role_id.id)
            autorizedIDs = set(signerItems.mapped('id'))
            requiredIDs = set(signerItems.filtered('required').mapped('id'))

            itemIDs = {int(k) for k in signature}
            if not (itemIDs <= autorizedIDs and requiredIDs <= itemIDs):  # Security check
                return False

            user = self.env['res.users'].search([('partner_id', '=', self.partner_id.id)], limit=1).sudo()
            for itemId in signature:
                item_value = SignItemValue.search(
                    [('item_id', '=', int(itemId)), ('request_id', '=', request.id)])
                if not item_value:
                    item_value = SignItemValue.create({'item_id': int(itemId), 'request_id': request.id,
                                                       'value': signature[itemId], 'signer_id': self.id})
                else:
                    item_value.write({'value': signature[itemId]})
                if item_value.item_id.field_id.item_type == 'signature':
                    self.data = signature[itemId][signature[itemId].find(',') + 1:]
                    if user:
                        user.sign_signature = self.data
                if item_value.item_id.field_id.item_type == 'initial' and user:
                    user.sign_initials = signature[itemId][signature[itemId].find(',') + 1:]

        return True

    def action_sign(self, items, access_token=False):
        self.ensure_one()
        if self.signed_on:
            raise ValidationError(
                _("Users %s has already signed the document") % self.partner_id.name
            )
        if self.request_id.state != "sent":
            raise ValidationError(_("Request cannot be signed"))
        self.signed_on = fields.Datetime.now()
        # current_hash = self.request_id.current_hash
        signatory_data = self.request_id.signatory_data

        input_data = BytesIO(b64decode(self.request_id.data))
        reader = PdfFileReader(input_data)
        output = PdfFileWriter()
        pages = {}
        for page_number in range(1, reader.numPages + 1):
            pages[page_number] = reader.getPage(page_number - 1)

        for key in signatory_data:
            if signatory_data[key]["role_id"] == self.role_id.id:
                signatory_data[key] = items[key]
                self._check_signable(items[key])
                item = items[key]
                page = pages[item["page"]]
                new_page = self._get_pdf_page(item, page.mediaBox)
                if new_page:
                    page.mergePage(new_page)
                pages[item["page"]] = page
        for page_number in pages:
            output.addPage(pages[page_number])
        output_stream = BytesIO()
        output.write(output_stream)
        output_stream.seek(0)
        signed_pdf = output_stream.read()
        final_hash = hashlib.sha1(signed_pdf).hexdigest()
        # TODO: Review that the hash has not been changed...
        self.request_id.write(
            {
                "signatory_data": signatory_data,
                "data": b64encode(signed_pdf),
                "current_hash": final_hash,
            }
        )
        self.signature_hash = final_hash
        self.request_id._check_signed()
        self._set_action_log("sign", access_token=access_token)
        # TODO: Add a return

    def _check_signable(self, item):
        if not item["required"]:
            return
        if not item["value"]:
            raise ValidationError(_("Field %s is not filled") % item["name"])

    def _get_pdf_page_text(self, item, box):
        packet = BytesIO()
        can = canvas.Canvas(packet, pagesize=(box.getWidth(), box.getHeight()))
        if not item["value"]:
            return False
        par = Paragraph(item["value"], style=self._getParagraphStyle())
        par.wrap(
            item["width"] / 100 * float(box.getWidth()),
            item["height"] / 100 * float(box.getHeight()),
        )
        par.drawOn(
            can,
            item["posX"] / 100 * float(box.getWidth()),
            (100 - item["posY"] - item["height"]) / 100 * float(box.getHeight()),
        )
        can.save()
        packet.seek(0)
        new_pdf = PdfFileReader(packet)
        return new_pdf.getPage(0)

    def _getParagraphStyle(self):
        return ParagraphStyle(name="Oca Sign Style")

    def _get_pdf_page_check(self, item, box):
        packet = BytesIO()
        can = canvas.Canvas(packet, pagesize=(box.getWidth(), box.getHeight()))
        width = item["width"] / 100 * float(box.getWidth())
        height = item["height"] / 100 * float(box.getHeight())
        drawing = Drawing(width=width, height=height)
        drawing.add(
            Rect(
                0,
                0,
                width,
                height,
                strokeWidth=3,
                strokeColor=black,
                fillColor=transparent,
            )
        )
        if item["value"]:
            drawing.add(Line(0, 0, width, height, strokeColor=black, strokeWidth=3))
            drawing.add(Line(0, height, width, 0, strokeColor=black, strokeWidth=3))
        drawing.drawOn(
            can,
            item["posX"] / 100 * float(box.getWidth()),
            (100 - item["posY"] - item["height"]) / 100 * float(box.getHeight()),
        )
        can.save()
        packet.seek(0)
        new_pdf = PdfFileReader(packet)
        return new_pdf.getPage(0)

    def _get_pdf_page_signature(self, item, box):
        packet = BytesIO()
        can = canvas.Canvas(packet, pagesize=(box.getWidth(), box.getHeight()))
        if not item["value"]:
            return False
        par = Image(
            BytesIO(b64decode(item["value"])),
            width=item["width"] / 100 * float(box.getWidth()),
            height=item["height"] / 100 * float(box.getHeight()),
        )
        par.drawOn(
            can,
            item["posX"] / 100 * float(box.getWidth()),
            (100 - item["posY"] - item["height"]) / 100 * float(box.getHeight()),
        )
        can.save()
        packet.seek(0)
        new_pdf = PdfFileReader(packet)
        return new_pdf.getPage(0)

    def _get_pdf_page(self, item, box):
        return getattr(self, "_get_pdf_page_%s" % item["item_type"])(item, box)

    def _set_action_log(self, action, **kwargs):
        self.ensure_one()
        return self.request_id._set_action_log(action, signer_id=self.id, **kwargs)

    def name_get(self):
        result = [(signer.id, (signer.partner_id.display_name)) for signer in self]
        return result


class SignOcaRequestValue(models.Model):
    _name = "sign.oca.request.signer.value"
    _description = "Signature Item Value"
    _rec_name = 'request_id'

    signer_id = fields.Many2one('sign.oca.request.signer', string="Signature Request item", required=True,
                                ondelete='cascade')
    item_id = fields.Many2one('sign.oca.template.item', string="Signature Item", required=True, ondelete='cascade')
    request_id = fields.Many2one(string="Signature Request", required=True, ondelete='cascade',
                                 related='signer_id.request_id')

    value = fields.Text()


class SignRequestLog(models.Model):
    _name = "sign.oca.request.log"
    _description = "Sign Request Log"
    _log_access = False

    user_id = fields.Many2one(
        "res.users",
        ondelete="cascade",
        default=lambda r: r.env.user.id,
    )
    date = fields.Datetime(
        required=True, default=lambda r: fields.Datetime.now()
    )
    partner_id = fields.Many2one(
        "res.partner", default=lambda r: r.env.user.partner_id.id
    )
    request_id = fields.Many2one("sign.oca.request", required=True, ondelete="cascade")
    signer_id = fields.Many2one("sign.oca.request.signer")
    action = fields.Selection(
        selection=[
            ('create', 'Creation'),
            ('open', 'View/Download'),
            ('save', 'Save'),
            ('sign', 'Signature'),
        ],
        required=True,
        readonly=True,
    )
    access_token = fields.Char(readonly=True)
    ip = fields.Char(readonly=True)
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    log_hash = fields.Char(string="Inalterability Hash", readonly=True, copy=False)
    request_state = fields.Selection([
        ("sent", "Before Signature"),
        ("signed", "After Signature"),
        ("canceled", "Canceled")
    ], required=True, string="State of the request on action log", groups="sign.sign_oca_group_manager")

    def _get_or_check_hash(self, vals):
        """ Returns the hash to write on sign log entries """
        if vals['action'] not in ['sign', 'create']:
            return False
        # When we check the hash, we need to restrict the previous activity to logs created before
        domain = [('request_id', '=', vals['request_id']), ('action', 'in', ['create', 'sign'])]
        if 'id' in vals:
            domain.append(('id', '<', vals['id']))
        prev_activity = self.sudo().search(domain, limit=1, order='id desc')
        # Multiple signers lead to multiple creation actions but for them, the hash of the PDF must be calculated.
        previous_hash = ""
        if not prev_activity:
            sign_request = self.env['sign.oca.request'].browse(vals['request_id'])
            body = sign_request.template_id.with_context(bin_size=False).attachment_id.datas
        else:
            previous_hash = prev_activity.log_hash
            body = self._compute_string_to_hash(vals)
        hash = sha256((previous_hash + str(body)).encode('utf-8')).hexdigest()
        return hash

    def _compute_string_to_hash(self, vals):
        values = {}
        for field in LOG_FIELDS:
            values[field] = str(vals[field])
        item_values = self.env['sign.oca.request.signer.value'].search(
            [('request_id', '=', vals['request_id'])]).filtered(
            lambda item: item.signer_id.access_token == vals['access_token'])
        for item_value in item_values:
            values[str(item_value.id)] = str(item_value.value)
        return dumps(values, sort_keys=True, ensure_ascii=True, indent=None)

    def create(self, vals):
        vals['date'] = datetime.utcnow()
        vals['log_hash'] = self._get_or_check_hash(vals)
        res = super(SignRequestLog, self).create(vals)
        return res

    def _prepare_vals_from_item(self, request_item):
        request = request_item.request_id
        return dict(
            signer_id=request_item.id,
            request_id=request.id,
            request_state=request.state,
            latitude=request_item.latitude or 0.0,
            longitude=request_item.longitude or 0.0,
            partner_id=request_item.partner_id.id)

    def _prepare_vals_from_request(self, sign_request):
        return dict(
            request_id=sign_request.id,
            request_state=sign_request.state,
        )

    def _update_vals_with_http_request(self, vals):
        vals.update({
            'user_id': request.env.user.id if not request.env.user._is_public() else None,
            'ip': request.httprequest.remote_addr,
        })
        if not vals.get('partner_id', False):
            vals.update({
                'partner_id': request.env.user.partner_id.id if not request.env.user._is_public() else None
            })
        # NOTE: during signing, this method is always called after the log is generated based on the
        # request item. This means that if the signer accepted the browser geolocation request, the `vals`
        # will already contain much more precise coordinates. We should use the GeoIP ones only if the
        # browser did not send anything
        if 'geoip' in request.session and not (vals.get('latitude') and vals.get('longitude')):
            vals.update({
                'latitude': request.session['geoip'].get('latitude') or 0.0,
                'longitude': request.session['geoip'].get('longitude') or 0.0,
            })
        return vals

    def _check_document_integrity(self):
        """
        Check the integrity of a sign request by comparing the logs hash to the computed values.
        """
        logs = self.filtered(lambda item: item.action in ['sign', 'create'])
        for log in logs:
            vals = {key: value[0] if isinstance(value, tuple) else value for key, value in log.read()[0].items()}
            hash = self._get_or_check_hash(vals)
            if hash != log.log_hash:
                # TODO add logs and comments
                return False
        return True
