# Copyright 2023 Dixmit
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class SignOcaField(models.Model):
    _name = "sign.oca.field"
    _description = "Signature Field Type"

    name = fields.Char(required=True)
    item_type = fields.Selection(
        [
            ('signature', "Signature"),
            ('initial', "Initial"),
            ('text', "Text"),
            ('textarea', "Multiline Text"),
            ('checkbox', "Checkbox"),
            ('selection', "Selection"), ],
        required=True,
        default="text",
    )
    tip = fields.Char(required=True, default="fill in", translate=True)
    placeholder = fields.Char(translate=True)

    default_width = fields.Float(string="Default Width", digits=(4, 3), required=True, default=0.150)
    default_height = fields.Float(string="Default Height", digits=(4, 3), required=True, default=0.015)
    auto_field = fields.Char(string="Automatic Partner Field",
                             help="Partner field to use to auto-complete the fields of this type")
    default_value = fields.Char()
