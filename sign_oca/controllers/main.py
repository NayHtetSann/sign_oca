import base64
import io

from odoo import http, _
from odoo.exceptions import AccessError, MissingError
from odoo.http import request

from odoo.addons.base.models.assetsbundle import AssetsBundle
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager
import re
import werkzeug
import mimetypes
from PyPDF2 import PdfFileReader
from odoo.addons.web.controllers.main import content_disposition
from collections import OrderedDict
from odoo.tools import groupby as groupbyelem
from operator import itemgetter
from odoo.osv.expression import AND


class SignController(http.Controller):
    def get_document_qweb_context(self, id, token):
        sign_request = http.request.env['sign.oca.request'].sudo().browse(id).exists()
        if not sign_request:
            if token:
                return http.request.render('sign_oca.deleted_sign_request')
            else:
                return http.request.not_found()

        current_request_item = None
        if token:
            current_request_item = sign_request.signer_ids.filtered(lambda r: r.access_token == token)
            if not current_request_item and sign_request.access_token != token and http.request.env.user.id != sign_request.create_uid.id:
                return http.request.render('sign.deleted_sign_request')
        elif sign_request.create_uid.id != http.request.env.user.id:
            return http.request.not_found()

        sign_item_types = http.request.env['sign.oca.field'].sudo().search_read([])
        if current_request_item:
            for item_type in sign_item_types:
                if item_type['auto_field']:
                    fields = item_type['auto_field'].split('.')
                    auto_field = current_request_item.partner_id
                    for field in fields:
                        if auto_field and field in auto_field:
                            auto_field = auto_field[field]
                        else:
                            auto_field = ""
                            break
                    item_type['auto_field'] = auto_field

            if current_request_item.state != 'completed':
                """ When signer attempts to sign the request again,
                its localisation should be reset.
                We prefer having no/approximative (from geoip) information
                than having wrong old information (from geoip/browser)
                on the signer localisation.
                """
                current_request_item.write({
                    'latitude': request.session['geoip'].get('latitude') if 'geoip' in request.session else 0,
                    'longitude': request.session['geoip'].get('longitude') if 'geoip' in request.session else 0,
                })

        item_values = {}
        sr_values = http.request.env['sign.oca.request.signer.value'].sudo().search(
            [('request_id', '=', sign_request.id), '|', ('signer_id', '=', current_request_item.id),
             ('signer_id.state', '=', 'completed')])
        for value in sr_values:
            item_values[value.item_id.id] = value.value

        Log = request.env['sign.oca.request.log'].sudo()
        vals = Log._prepare_vals_from_item(
            current_request_item) if current_request_item else Log._prepare_vals_from_request(sign_request)
        vals['action'] = 'open'
        vals = Log._update_vals_with_http_request(vals)
        Log.create(vals)

        return {
            'sign_request': sign_request,
            'current_request_item': current_request_item,
            'token': token,
            'nbComments': len(sign_request.message_ids.filtered(lambda m: m.message_type == 'comment')),
            'isPDF': (sign_request.template_id.attachment_id.mimetype.find('pdf') > -1),
            'webimage': re.match('image.*(gif|jpe|jpg|png)', sign_request.template_id.attachment_id.mimetype),
            'hasItems': len(sign_request.template_id.item_ids) > 0,
            'sign_items': sign_request.template_id.item_ids,
            'item_values': item_values,
            'role': current_request_item.role_id.id if current_request_item else 0,
            'readonly': not (current_request_item and current_request_item.state == 'sent'),
            'sign_item_types': sign_item_types,
            'sign_item_select_options': sign_request.template_id.item_ids.mapped('option_ids'),
        }

    @http.route(['/sign_oca/<link>'], type='http', auth='public')
    def share_link(self, link, **post):
        template = http.request.env['sign.oca.template'].sudo().search([('share_link', '=', link)], limit=1)
        if not template:
            return http.request.not_found()

        sign_request = http.request.env['sign.oca.request'].with_user(template.create_uid).create({
            'template_id': template.id,
            'name': "%(template_name)s-public" % {'template_name': template.attachment_id.name},
            'favorited_ids': [(4, template.create_uid.id)],
        })

        request_item = http.request.env['sign.oca.request.signer'].sudo().create(
            {'request_id': sign_request.id, 'role_id': template.item_ids.mapped('role_id').id})
        sign_request.action_sent()

        return http.redirect_with_hash(
            '/sign_oca/document/%(request_id)s/%(access_token)s' % {'request_id': sign_request.id,
                                                                'access_token': request_item.access_token})

    @http.route(["/sign_oca/get_document/<int:id>/<token>"], type='json', auth='user')
    def get_document(self, id, token):
        return http.Response(template='sign_oca._doc_sign', qcontext=self.get_document_qweb_context(id, token)).render()

    @http.route(["/sign_oca/document/<int:id>"], type='http', auth='user')
    def sign_document_user(self, id, **post):
        return self.sign_document_public(id, None)

    @http.route(["/sign_oca/document/mail/<int:id>/<token>"], type='http', auth='public')
    def sign_document_from_mail(self, id, token):
        sign_request = request.env['sign.oca.request'].sudo().browse(id)
        if not sign_request:
            return http.request.render('sign_oca.deleted_sign_request')
        current_request_item = sign_request.signer_ids.filtered(lambda r: r.access_token == token)
        current_request_item.access_via_link = True
        return werkzeug.redirect('/sign_oca/document/%s/%s' % (id, token))

    @http.route(["/sign_oca/document/<int:id>/<token>"], type='http', auth='public')
    def sign_document_public(self, id, token, **post):
        document_context = self.get_document_qweb_context(id, token)
        document_context['portal'] = post.get('portal')
        if not isinstance(document_context, dict):
            return document_context

        current_request_item = document_context.get('current_request_item')
        if current_request_item and current_request_item.partner_id.lang:
            http.request.env.context = dict(http.request.env.context, lang=current_request_item.partner_id.lang)
        return http.request.render('sign_oca.doc_sign', document_context)

    @http.route(['/sign_oca/download/<int:id>/<token>/<download_type>'], type='http', auth='public')
    def download_document(self, id, token, download_type, **post):
        sign_request = http.request.env['sign.oca.request'].sudo().browse(id).exists()
        if not sign_request or sign_request.access_token != token:
            return http.request.not_found()

        document = None
        if download_type == "log":
            report_action = http.request.env.ref('sign_oca.action_sign_oca_request_print_logs').sudo()
            pdf_content, __ = report_action._render_qweb_pdf(sign_request.id)
            pdfhttpheaders = [
                ('Content-Type', 'application/pdf'),
                ('Content-Length', len(pdf_content)),
                ('Content-Disposition', 'attachment; filename=' + "Certificate.pdf;")
            ]
            return request.make_response(pdf_content, headers=pdfhttpheaders)
        elif download_type == "origin":
            document = sign_request.template_id.attachment_id.datas
        elif download_type == "completed":
            document = sign_request.data
            if not document:  # if the document is completed but the document is encrypted
                return http.redirect_with_hash(
                    '/sign_oca/password/%(request_id)s/%(access_token)s' % {'request_id': id, 'access_token': token})

        if not document:
            # Shouldn't it fall back on 'origin' download type?
            return http.redirect_with_hash(
                "/sign_oca/document/%(request_id)s/%(access_token)s" % {'request_id': id, 'access_token': token})

        # Avoid to have file named "test file.pdf (V2)" impossible to open on Windows.
        # This line produce: test file (V2).pdf
        extension = '.' + sign_request.template_id.attachment_id.mimetype.replace('application/', '').replace(';base64',
                                                                                                              '')
        filename = sign_request.name.replace(extension, '') + extension

        return http.request.make_response(
            base64.b64decode(document),
            headers=[
                ('Content-Type', mimetypes.guess_type(filename)[0] or 'application/octet-stream'),
                ('Content-Disposition', content_disposition(filename))
            ]
        )

    @http.route([
        '/sign_oca/sign/<int:id>/<token>',
        '/sign_oca/sign/<int:id>/<token>/<sms_token>'
    ], type='json', auth='public')
    def sign(self, id, token, sms_token=False, signature=None):
        request_item = http.request.env['sign.oca.request.signer'].sudo().search(
            [('request_id', '=', id), ('access_token', '=', token), ('state', '=', 'sent')], limit=1)
        if not request_item:
            return False
        if request_item.role_id and request_item.role_id.sms_authentification:
            if not sms_token:
                return {
                    'sms': True
                }
            if sms_token != request_item.sms_token:
                return False
            if sms_token == request_item.sms_token:
                request_item.request_id._message_log(
                    body=_('%s validated the signature by SMS with the phone number %s.') % (
                    request_item.partner_id.display_name, request_item.partner_id.mobile))

        if not request_item.sign(signature):
            return False

        # mark signature as done in next activity
        user_ids = http.request.env['res.users'].search([('partner_id', '=', request_item.partner_id.id)])
        sign_users = user_ids.filtered(lambda u: u.has_group('sign_oca.sign_oca_group_user'))
        for sign_user in sign_users:
            request_item.request_id.activity_feedback(['mail.mail_activity_data_todo'], user_id=sign_user.id)

        Log = request.env['sign.oca.request.log'].sudo()
        vals = Log._prepare_vals_from_item(request_item)
        vals['action'] = 'sign'
        vals['access_token'] = token
        vals = Log._update_vals_with_http_request(vals)
        Log.create(vals)
        request_item.action_completed()
        return True

    @http.route(['/sign_oca/password/<int:sign_request_id>/<token>'], type='http', auth='public')
    def check_password_page(self, sign_request_id, token, **post):
        values = http.request.params.copy()
        request_item = http.request.env['sign.oca.request.signer'].sudo().search([
            ('request_id', '=', sign_request_id),
            ('state', '=', 'completed'),
            ('request_id.access_token', '=', token)], limit=1)
        if not request_item:
            return http.request.not_found()

        if 'password' not in http.request.params:
            return http.request.render('sign_oca.encrypted_ask_password')

        password = http.request.params['password']
        template_id = request_item.request_id.template_id

        old_pdf = PdfFileReader(io.BytesIO(base64.b64decode(template_id.attachment_id.datas)), strict=False,
                                overwriteWarnings=False)
        if old_pdf.isEncrypted and not old_pdf.decrypt(password):
            values['error'] = _("Wrong password")
            return http.request.render('sign_oca.encrypted_ask_password', values)

        request_item.request_id.generate_completed_document(password)
        request_item.request_id.send_completed_document()
        return http.redirect_with_hash(
            '/sign_oca/document/%(request_id)s/%(access_token)s' % {'request_id': sign_request_id, 'access_token': token})

    @http.route(['/sign_oca/get_signature/<int:request_id>/<item_access_token>'], type='json', auth='public')
    def sign_get_user_signature(self, request_id, item_access_token, signature_type='signature'):
        sign_request_item = http.request.env['sign.oca.request.signer'].sudo().search([
            ('request_id', '=', request_id),
            ('access_token', '=', item_access_token)
        ])
        if not sign_request_item:
            return False

        sign_request_user = http.request.env['res.users'].sudo().search(
            [('partner_id', '=', sign_request_item.partner_id.id)], limit=1)
        if sign_request_user and signature_type == 'signature':
            return sign_request_user.sign_signature
        elif sign_request_user and signature_type == 'initial':
            return sign_request_user.sign_initials
        return False

    @http.route(['/sign_oca/send_public/<int:id>/<token>'], type='json', auth='public')
    def make_public_user(self, id, token, name=None, mail=None):
        sign_request = http.request.env['sign.oca.request'].sudo().search([('id', '=', id), ('access_token', '=', token)])
        if not sign_request or len(sign_request.signer_ids) != 1 or sign_request.signer_ids.partner_id:
            return False

        ResPartner = http.request.env['res.partner'].sudo()
        partner = ResPartner.search([('email', '=', mail)], limit=1)
        if not partner:
            partner = ResPartner.create({'name': name, 'email': mail})
        sign_request.signer_ids[0].write({'partner_id': partner.id})

    @http.route(['/sign_oca/encrypted/<int:sign_request_id>'], type='json', auth='public')
    def check_encrypted(self, sign_request_id):
        request_item = http.request.env['sign.oca.request.signer'].sudo().search([('request_id', '=', sign_request_id)],
                                                                           limit=1)
        if not request_item:
            return False

        # we verify that the document is completed by all signor
        if request_item.request_id.nb_total != request_item.request_id.nb_closed:
            return False
        template_id = request_item.request_id.template_id

        old_pdf = PdfFileReader(io.BytesIO(base64.b64decode(template_id.attachment_id.datas)), strict=False,
                                overwriteWarnings=False)
        return True if old_pdf.isEncrypted else False

    @http.route("/sign_oca/get_assets.<any(css,js):ext>", type="http", auth="public")
    def get_sign_resources(self, ext):
        xmlid = "sign_oca.sign_assets"
        files, _remains = request.env["ir.qweb"]._get_asset_content(
            xmlid, options=request.context
        )
        asset = AssetsBundle(xmlid, files)
        mock_attachment = getattr(asset, ext)()
        if isinstance(
            mock_attachment, list
        ):  # suppose that CSS asset will not required to be split in pages
            mock_attachment = mock_attachment[0]
        _status, headers, content = request.env["ir.http"].binary_content(
            id=mock_attachment.id, unique=asset.checksum
        )
        content_base64 = base64.b64decode(content) if content else ""
        headers.append(("Content-Length", len(content_base64)))
        return request.make_response(content_base64, headers)

    @http.route("/sign_oca/render_assets_pdf_iframe", type="json", auth="public")
    def render_assets_pdf_iframe(self, **kw):
        context = {'debug': kw.get('debug')} if 'debug' in kw else {}
        return request.env['ir.ui.view'].sudo()._render_template('sign_oca.compiled_assets_pdf_iframe', context)


class PortalSign(CustomerPortal):

    def _prepare_home_portal_values(self, counters):
        values = super(CustomerPortal, self)._prepare_home_portal_values(counters)
        if 'sign_count' in counters:
            partner_id = request.env.user.partner_id
            values['sign_count'] = request.env['sign.oca.request.signer'].sudo().search_count([
                ('partner_id', '=', partner_id.id), ('state', '!=', 'draft')
            ])
        return values

    @http.route(['/my/signatures', '/my/signatures/page/<int:page>'], type='http', auth='user', website=True)
    def portal_my_signatures(self, page=1, date_begin=None, date_end=None, sortby=None, search=None, search_in='all',
                             groupby='none', filterby=None, **kw):

        values = self._prepare_portal_layout_values()
        partner_id = request.env.user.partner_id
        SignRequestItem = request.env['sign.oca.request.signer'].sudo()
        default_domain = [('partner_id', '=', partner_id.id), ('state', '!=', 'draft')]

        searchbar_sortings = {
            'new': {'label': _('Newest'), 'order': 'request_id desc'},
            'date': {'label': _('Signing Date'), 'order': 'signed_on desc'},
        }

        searchbar_filters = {
            'all': {'label': _('All'), 'domain': default_domain},
            'tosign': {'label': _('To sign'), 'domain': AND([default_domain, [('state', '=', 'sent'),
                                                                              ('request_id.state', '=', 'sent')]])},
            'completed': {'label': _('Completed'), 'domain': AND([default_domain, [('state', '=', 'completed')]])},
            'signed': {'label': _('Fully Signed'),
                       'domain': AND([default_domain, [('request_id.state', '=', 'signed')]])},
        }

        searchbar_inputs = {
            'all': {'input': 'all', 'label': _('Search <span class="nolabel"> (in Document)</span>')},
        }

        searchbar_groupby = {
            'none': {'input': 'none', 'label': _('None')},
            'state': {'input': 'state', 'label': _('Status')},
        }

        # default sortby order
        if not sortby:
            sortby = 'new'
        sort_order = searchbar_sortings[sortby]['order']
        # default filter by value
        if not filterby:
            filterby = 'all'
        domain = searchbar_filters[filterby]['domain']
        if date_begin and date_end:
            domain = AND([domain, [('signed_on', '>', date_begin), ('signed_on', '<=', date_end)]])
        # search only the document name
        if search and search_in:
            domain = AND([domain, [('reference', 'ilike', search)]])
        pager = portal_pager(
            url='/my/signatures',
            url_args={'date_begin': date_begin, 'date_end': date_end, 'sortby': sortby, 'filterby': filterby,
                      'search_in': search_in, 'search': search},
            total=SignRequestItem.search_count(domain),
            page=page,
            step=self._items_per_page
        )

        # content according to pager and archive selected
        if groupby == 'state':
            sort_order = 'state, %s' % sort_order

        # search the count to display, according to the pager data
        sign_requests_items = SignRequestItem.search(domain, order=sort_order, limit=self._items_per_page,
                                                     offset=pager['offset'])
        request.session['my_signatures_history'] = sign_requests_items.ids[:100]
        if groupby == 'state':
            grouped_signatures = [SignRequestItem.concat(*g)
                                  for k, g in groupbyelem(sign_requests_items, itemgetter('state'))]
        else:
            grouped_signatures = [sign_requests_items]

        values.update({
            'date': date_begin,
            'grouped_signatures': grouped_signatures,
            'page_name': 'signatures',
            'pager': pager,
            'default_url': '/my/signatures',
            'searchbar_sortings': searchbar_sortings,
            'searchbar_filters': OrderedDict(sorted(searchbar_filters.items())),
            'searchbar_groupby': searchbar_groupby,
            'searchbar_inputs': searchbar_inputs,
            'search_in': search_in,
            'groupby': groupby,
            'sortby': sortby,
            'filterby': filterby,
        })
        return request.render('sign_oca.sign_portal_my_requests', values)

    @http.route(['/my/signature/<int:item_id>'], type='http', auth='public', website=True)
    def portal_my_signature(self, item_id, access_token=None, **kwargs):
        partner_id = request.env.user.partner_id
        sign_item_sudo = request.env['sign.request.item'].sudo().browse(item_id)
        try:
            if sign_item_sudo.state != 'draft' and not sign_item_sudo.partner_id == partner_id:
                return request.redirect('/my/')
            url = f'/sign_oca/document/{sign_item_sudo.request_id.id}/{sign_item_sudo.access_token}?portal=1'
            values = {
                'page_name': 'signature',
                'my_sign_item': sign_item_sudo,
                'url': url
            }
            values = self._get_page_view_values(sign_item_sudo, sign_item_sudo.access_token, values,
                                                'my_signatures_history', False, **kwargs)
            return request.render('sign_oca.sign_portal_my_request', values)
        except MissingError:
            return request.redirect('/my')
