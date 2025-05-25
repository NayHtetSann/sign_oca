# Copyright 2024 ForgeFlow S.L. (http://www.forgeflow.com)
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    signer_ids = fields.One2many(
        comodel_name="sign.oca.request.signer", inverse_name="partner_id"
    )
    signer_count = fields.Integer(compute='_compute_signature_count', string="# Signatures")

    def _compute_signature_count(self):
        signature_data = self.env['sign.oca.request.signer'].sudo().read_group([('partner_id', 'in', self.ids)],
                                                                         ['partner_id'], ['partner_id'])
        signature_data_mapped = dict((data['partner_id'][0], data['partner_id_count']) for data in signature_data)
        for partner in self:
            partner.signer_count = signature_data_mapped.get(partner.id, 0)

    def _compute_signers_count(self):
        for rec in self:
            rec.signer_count = len(rec.signer_ids)

    def action_show_signer_ids(self):
        self.ensure_one()
        result = self.env["ir.actions.act_window"]._for_xml_id(
            "sign_oca.sign_oca_request_signer_act_window"
        )
        result["domain"] = [("id", "in", self.signer_ids.ids)]
        return result
