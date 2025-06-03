# Copyright 2023 Dixmit
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from PyPDF2 import PdfFileReader
import re
import base64
import io
from odoo.tools import pdf
from odoo.exceptions import AccessError


class SignTemplateTag(models.Model):
    _name = "sign.template.tag"
    _description = "Sign Template Tag"
    _order = "name"

    name = fields.Char('Tag Name', required=True, translate=True)
    color = fields.Integer('Color Index')

    _sql_constraints = [
        ('name_uniq', 'unique (name)', "Tag name already exists !"),
    ]


class SignOcaItemSelectionOption(models.Model):
    _name = "sign.oca.item.option"
    _description = "Option of a selection Field"

    value = fields.Text(string="Option")


class SignOcaTemplate(models.Model):
    _name = "sign.oca.template"
    _description = "Sign Oca Template"  # TODO

    def _default_favorited_ids(self):
        return [(4, self.env.user.id)]

    @api.model
    def add_option(self, value):
        option = self.env['sign.oca.item.option'].search([('value', '=', value)])
        option_id = option if option else self.env['sign.oca.item.option'].create({'value': value})
        return option_id.id

    def open_requests(self):
        return {
            "type": "ir.actions.act_window",
            "name": _("Sign requests"),
            "res_model": "sign.oca.request",
            "res_id": self.id,
            "domain": [["template_id.id", "in", self.ids]],
            "views": [[False, 'kanban'], [False, "form"]],
            "context": {'search_default_signed': True}
        }

    def go_to_custom_template(self, sign_directly_without_mail=False):
        self.ensure_one()
        return {
            'name': "Template \"%(name)s\"" % {'name': self.attachment_id.name},
            'type': 'ir.actions.client',
            'tag': 'sign.oca.Template',
            'context': {
                'id': self.id,
                'sign_directly_without_mail': sign_directly_without_mail,
            },
        }

    attachment_id = fields.Many2one('ir.attachment', string="Attachment", required=True, ondelete='cascade')
    name = fields.Char(related='attachment_id.name')
    data = fields.Binary(readonly=False, related='attachment_id.datas')
    filename = fields.Char()
    tag_ids = fields.Many2many('sign.template.tag', string='Tags')
    color = fields.Integer()
    item_ids = fields.One2many("sign.oca.template.item", inverse_name="template_id")
    responsible_count = fields.Integer(compute='_compute_responsible_count', string="Responsible Count")
    request_count = fields.Integer(compute="_compute_request_count")
    model_id = fields.Many2one(
        comodel_name="ir.model",
        string="Model",
        domain=[("transient", "=", False), ("model", "not like", "sign.oca")],
    )
    model = fields.Char(compute="_compute_model", compute_sudo=True, store=True)
    active = fields.Boolean(default=True)
    request_ids = fields.One2many("sign.oca.request", inverse_name="template_id")
    favorited_ids = fields.Many2many('res.users', string="Invited Users", default=lambda s: s._default_favorited_ids())
    share_link = fields.Char(string="Share Link", copy=False)
    redirect_url = fields.Char(string="Redirect Link", default="",
                               help="Optional link for redirection after signature")
    redirect_url_text = fields.Char(string="Link Label", default="Open Link", translate=True,
                                    help="Optional text to display on the button link")
    group_ids = fields.Many2many("res.groups", string="Template Access Group")
    privacy = fields.Selection([('employee', 'All Users'), ('invite', 'On Invitation')],
                               string="Who can Sign", default="invite",
                               help="Set who can use this template:\n"
                                    "- All Users: all users of the Sign application can view and use the template\n"
                                    "- On Invitation: only invited users can view and use the template\n"
                                    "Invited users can always edit the document template.\n"
                                    "Existing requests based on this template will not be affected by changes.")

    @api.model
    def create(self, vals):
        if 'active' in vals and vals['active'] and not self.env.user.has_group('sign_oca.sign_oca_group_manager'):
            raise AccessError(_("Do not have access to create templates"))

        return super(SignOcaTemplate, self).create(vals)

    @api.depends('item_ids.role_id')
    def _compute_responsible_count(self):
        for template in self:
            template.responsible_count = len(template.item_ids.mapped('role_id'))

    @api.model
    def update_from_pdfviewer(self, template_id=None, duplicate=None, sign_items=None, name=None):
        template = self.browse(template_id)
        if not duplicate and len(template.request_ids) > 0:
            return False

        if duplicate:
            new_attachment = template.attachment_id.copy()
            r = re.compile(' \(v(\d+)\)$')
            m = r.search(name)
            v = str(int(m.group(1)) + 1) if m else "2"
            index = m.start() if m else len(name)
            new_attachment.name = name[:index] + " (v" + v + ")"
            template = template.copy({
                'attachment_id': new_attachment.id,
                'favorited_ids': [(4, self.env.user.id)]
            })

        elif name:
            template.attachment_id.name = name

        item_ids = {
            it
            for it in map(int, sign_items)
            if it > 0
        }
        template.item_ids.filtered(lambda r: r.id not in item_ids).unlink()
        for item in template.item_ids:
            values = sign_items.pop(str(item.id))
            values['option_ids'] = [(6, False, [int(op) for op in values.get('option_ids', [])])]
            item.write(values)
        for item in sign_items.values():
            item['template_id'] = template.id
            item['option_ids'] = [(6, False, [int(op) for op in item.get('option_ids', [])])]
            self.env['sign.oca.template.item'].create(item)

        if len(template.item_ids.mapped('role_id')) > 1:
            template.share_link = None

        return template.id

    @api.model
    def upload_template(self, name=None, dataURL=None, active=True):
        mimetype = dataURL[dataURL.find(':') + 1:dataURL.find(',')]
        datas = dataURL[dataURL.find(',') + 1:]
        # TODO: for now, PDF files without extension are recognized as application/octet-stream;base64
        try:
            file_pdf = PdfFileReader(io.BytesIO(base64.b64decode(datas)), strict=False, overwriteWarnings=False)
        except Exception as e:
            raise UserError(_("This file cannot be read. Is it a valid PDF?"))
        file_type = mimetype.replace('application/', '').replace(';base64', '')
        extension = re.compile(re.escape(file_type), re.IGNORECASE)
        name = extension.sub(file_type, name)
        attachment = self.env['ir.attachment'].create({'name': name, 'datas': datas, 'mimetype': mimetype})
        template = self.create(
            {'attachment_id': attachment.id, 'favorited_ids': [(4, self.env.user.id)], 'active': active})

        return {'template': template.id, 'attachment': attachment.id}

    @api.model
    def rotate_pdf(self, template_id=None):
        template = self.browse(template_id)
        if len(template.request_ids) > 0:
            return False

        template.datas = base64.b64encode(pdf.rotate_pdf(base64.b64decode(template.datas)))

        return True

    @api.depends("model_id")
    def _compute_model(self):
        for item in self:
            item.model = item.model_id.model or False

    @api.depends("request_ids")
    def _compute_request_count(self):
        res = self.env["sign.oca.request"].read_group(
            domain=[("template_id", "in", self.ids)],
            fields=["template_id"],
            groupby=["template_id"],
        )
        res_dict = {x["template_id"][0]: x["template_id_count"] for x in res}
        for record in self:
            record.request_count = res_dict.get(record.id, 0)

    def configure(self):
        self.ensure_one()
        return {
            "type": "ir.actions.client",
            "tag": "sign_oca_configure",
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
            "items": {item.id: item.get_info() for item in self.item_ids},
            "roles": [
                {"id": role.id, "name": role.name}
                for role in self.env["sign.oca.role"].search([])
            ],
            "fields": [
                {"id": field.id, "name": field.name}
                for field in self.env["sign.oca.field"].search([])
            ],
        }

    def delete_item(self, item_id):
        self.ensure_one()
        item = self.item_ids.browse(item_id)
        assert item.template_id == self
        item.unlink()

    def set_item_data(self, item_id, vals):
        self.ensure_one()
        item = self.env["sign.oca.template.item"].browse(item_id)
        assert item.template_id == self
        item.write(vals)

    def add_item(self, item_vals):
        self.ensure_one()
        item_vals["template_id"] = self.id
        return self.env["sign.oca.template.item"].create(item_vals).get_info()

    def _get_signatory_data(self):
        items = sorted(
            self.item_ids,
            key=lambda item: (
                item.page,
                item.posY,
                item.posX,
            ),
        )
        tabindex = 1
        signatory_data = {}
        item_id = 1
        for item in items:
            item_data = item._get_full_info()
            item_data["id"] = item_id
            item_data["tabindex"] = tabindex
            tabindex += 1
            signatory_data[item_id] = item_data
            item_id += 1
        return signatory_data

    def _prepare_sign_oca_request_vals_from_record(self, record):
        roles = self.mapped("item_ids.role_id").filtered(
            lambda x: x.partner_type != "empty"
        )
        return {
            "name": self.name,
            "template_id": self.id,
            "record_ref": "%s,%s" % (record._name, record.id),
            "signatory_data": self._get_signatory_data(),
            "data": self.data,
            "signer_ids": [
                (
                    0,
                    0,
                    {
                        "partner_id": role._get_partner_from_record(record),
                        "role_id": role.id,
                    },
                )
                for role in roles
            ],
        }


class SignOcaTemplateItem(models.Model):
    _name = "sign.oca.template.item"
    _description = "Sign Oca Template Item"  # TODO

    def getByPage(self):
        items = {}
        for item in self:
            if item.page not in items:
                items[item.page] = []
            items[item.page].append(item)
        return items

    template_id = fields.Many2one(
        "sign.oca.template", required=True, ondelete="cascade"
    )
    field_id = fields.Many2one("sign.oca.field", ondelete="restrict")
    role_id = fields.Many2one(
        "sign.oca.role", default=lambda r: r._get_default_role(), ondelete="restrict"
    )
    option_ids = fields.Many2many("sign.oca.item.option", string="Selection options")
    required = fields.Boolean()
    # If no role, it will be editable by everyone...
    name = fields.Char(string="Field Name")
    page = fields.Integer(required=True, default=1)
    posX = fields.Float(required=True)
    posY = fields.Float(required=True)
    width = fields.Float()
    height = fields.Float()
    placeholder = fields.Char()

    @api.model
    def _get_default_role(self):
        return self.env.ref("sign_oca.sign_role_customer")

    def get_info(self):
        self.ensure_one()
        return {
            "id": self.id,
            "field_id": self.field_id.id,
            "name": self.field_id.name,
            "role_id": self.role_id.id,
            "page": self.page,
            "posX": self.posX,
            "posY": self.posY,
            "width": self.width,
            "height": self.height,
            "placeholder": self.placeholder,
            "required": self.required,
        }

    def _get_full_info(self):
        """Method used in the wizards in the requests that are created."""
        self.ensure_one()
        vals = self.get_info()
        vals.update(
            {
                "item_type": self.field_id.item_type,
                "value": False,
                "default_value": self.field_id.default_value,
            }
        )
        return vals
