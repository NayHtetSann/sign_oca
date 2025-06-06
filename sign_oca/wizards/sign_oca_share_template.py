# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

import uuid

from odoo import api, fields, models, _


class SignTemplateShare(models.TransientModel):
    _name = 'sign.oca.template.share'
    _description = 'Sign Share Template'

    @api.model
    def default_get(self, fields):
        res = super(SignTemplateShare, self).default_get(fields)
        if 'url' in fields:
            template = self.env['sign.oca.template'].browse(res.get('template_id'))
            if template.responsible_count > 1:
                res['url'] = False
            else:
                if not template.share_link:
                    template.share_link = str(uuid.uuid4())
                base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
                res['url'] = "%s/sign_oca/%s" % (base_url, template.share_link)
        return res

    template_id = fields.Many2one(
        'sign.oca.template', required=True, ondelete='cascade',
        default=lambda s: s.env.context.get("active_id", None),
    )
    url = fields.Char(string="Link to Share")
    is_one_responsible = fields.Boolean()

    def open(self):
        return {
            'name': _('Sign'),
            'type': 'ir.actions.act_url',
            'url': '/sign_oca/%s' % (self.template_id.share_link),
        }
