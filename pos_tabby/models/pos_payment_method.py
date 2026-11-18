import base64
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from . import const
from .tabby_api import TabbyError, TabbyPosAPI

_logger = logging.getLogger(__name__)

SK_PATTERN = re.compile(r'^sk_(test_)?[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$')


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    tabby_secret_key = fields.Char(
        string="Tabby Secret Key",
        groups='point_of_sale.group_pos_manager',
        help="Secret key from the Tabby merchant dashboard. A sk_test_ key runs test payments.")
    tabby_test_mode = fields.Boolean(string="Tabby Test Mode", compute='_compute_tabby_test_mode')

    def _get_payment_terminal_selection(self):
        return super()._get_payment_terminal_selection() + [('tabby', 'Tabby')]

    @api.depends('tabby_secret_key')
    def _compute_tabby_test_mode(self):
        for method in self:
            method.tabby_test_mode = (method.sudo().tabby_secret_key or '').startswith('sk_test_')

    @api.constrains('use_payment_terminal', 'tabby_secret_key')
    def _check_tabby_secret_key(self):
        for method in self.sudo():
            if method.use_payment_terminal != 'tabby':
                continue
            if not method.tabby_secret_key or not SK_PATTERN.match(method.tabby_secret_key):
                raise ValidationError(_(
                    "Invalid Tabby Secret Key format. Must be: sk_[test_]xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"))
            currency = method.company_id.currency_id.name
            if currency not in const.CURRENCY_REGION:
                raise ValidationError(_("Tabby POS supports AED and SAR companies only, not %s.", currency))

    # -- Helpers -----------------------------------------------------------

    def _tabby_check_access(self):
        if not self.env.user.has_group('point_of_sale.group_pos_user'):
            raise AccessError(_("Only Point of Sale users can use the Tabby terminal."))

    def _tabby_api(self, currency):
        self.ensure_one()
        return TabbyPosAPI(self.sudo().tabby_secret_key, currency)

    def _tabby_module_version(self):
        module = self.env['ir.module.module'].sudo().search([('name', '=', 'pos_tabby')], limit=1)
        return module.installed_version or '0'

    def _tabby_session_payload(self, config, data, currency):
        """Build the POST /api/v2/checkout body for a till payment."""
        decimals = self.env['res.currency'].search([('name', '=', currency)], limit=1).decimal_places or 2
        amount = "%.*f" % (decimals, float(data['amount']))
        lang = (self.env.lang or 'en')[:2]
        attachment = {
            'latitude': config.tabby_latitude,
            'longitude': config.tabby_longitude,
            'timestamp': fields.Datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        }
        payment = {
            'amount': amount,
            'currency': currency,
            'description': config.tabby_terminal_id or config.name,
            'order': {
                'reference_id': data.get('order_reference') or data.get('order_uuid'),
                'items': data.get('items') or [],
            },
            'attachment': {
                'body': json.dumps(attachment),
                'content_type': const.ATTACHMENT_CONTENT_TYPE,
            },
            'meta': {
                'tabby_plugin_platform': 'Odoo POS',
                'tabby_plugin_version': self._tabby_module_version(),
                'pos_order_uuid': data.get('order_uuid'),
            },
        }
        buyer = data.get('buyer') or {}
        if buyer.get('phone') or buyer.get('email'):
            payment['buyer'] = {k: v for k, v in buyer.items() if v}
        return {
            'lang': lang if lang in ('en', 'ar') else 'en',
            'merchant_code': config.tabby_merchant_code,
            'payment': payment,
        }

    # -- RPC called from the POS ------------------------------------------

    def tabby_create_session(self, data):
        """Create a Tabby session for the selected payment line.

        data: {config_id, amount, order_uuid, order_reference, items, buyer}
        returns {session_id, payment_id, qr_image, web_url, expires_at} or {error}
        """
        self.ensure_one()
        self._tabby_check_access()
        config = self.env['pos.config'].browse(int(data['config_id'])).exists()
        if not config:
            return {'error': _("POS configuration not found.")}
        if not config.tabby_merchant_code:
            return {'error': _("Set the Tabby merchant code in the POS settings first.")}
        currency = config.currency_id.name
        try:
            api = self._tabby_api(currency)
            session = api.create_session(self._tabby_session_payload(config, data, currency))
            if session.get('status') != 'created':
                return {'error': _("Tabby cannot approve this purchase. Please use another payment method."),
                        'rejected': True}
            product = (session.get('configuration', {}).get('available_products', {}).get('installments') or [{}])[0]
            qr_url = product.get('qr_code')
            if not qr_url and not product.get('web_url'):
                return {'error': _("Tabby returned an incomplete session. Please try again.")}
            qr_png = api.fetch_qr_png(qr_url)
        except TabbyError as exc:
            return {'error': str(exc)}
        return {
            'session_id': session.get('id'),
            'payment_id': (session.get('payment') or {}).get('id'),
            'qr_image': 'data:image/png;base64,' + base64.b64encode(qr_png).decode(),
            'web_url': product.get('web_url'),
            'expires_at': session.get('expires_at'),
        }

    def tabby_payment_status(self, data):
        """data: {config_id, payment_id} -> {status, outcome} or {error}.

        An AUTHORIZED payment is captured here: the goods leave the shop at
        once, and Tabby refunds only captured payments. Capture uses the
        payment id as reference_id, so a repeated poll cannot capture twice.
        """
        self.ensure_one()
        self._tabby_check_access()
        config = self.env['pos.config'].browse(int(data['config_id'])).exists()
        try:
            api = self._tabby_api(config.currency_id.name)
            payment = api.get_payment(data['payment_id'])
        except TabbyError as exc:
            return {'error': str(exc)}
        status = (payment.get('status') or '').upper()
        result = {'status': status}
        if status == 'AUTHORIZED' and not payment.get('captures'):
            try:
                api.capture(payment['id'], {'amount': payment['amount'], 'reference_id': payment['id']})
                result['captured'] = True
            except TabbyError as exc:
                # The customer has paid; do not fail the sale. The payment stays
                # AUTHORIZED in Tabby and needs a manual capture.
                _logger.warning("Tabby POS capture failed for payment %s: %s", payment.get('id'), exc)
                result['capture_error'] = str(exc)
        if status in const.PAID_STATUSES:
            result['outcome'] = 'paid'
        elif status in const.FAILED_STATUSES:
            result['outcome'] = 'failed'
        else:
            result['outcome'] = 'pending'
        return result

    def tabby_cancel_session(self, data):
        """data: {config_id, session_id, payment_id} -> {cancelled} or {status, outcome}.

        A finalized session cannot be cancelled: re-read the payment so the
        till does not drop a purchase the customer already completed.
        """
        self.ensure_one()
        self._tabby_check_access()
        config = self.env['pos.config'].browse(int(data['config_id'])).exists()
        try:
            self._tabby_api(config.currency_id.name).cancel_session(data['session_id'])
            return {'cancelled': True}
        except TabbyError as exc:
            if exc.status == 400 and data.get('payment_id'):
                return self.tabby_payment_status(data)
            return {'error': str(exc)}

    def tabby_refund(self, data):
        """Refund a Tabby payment from a POS return.

        data: {config_id, refunded_line_ids, amount, reference_id}
        The original Tabby payment id is taken from the refunded order's
        Tabby payment line (pos.payment.transaction_id).
        returns {refund_id, payment_id} or {error}
        """
        self.ensure_one()
        self._tabby_check_access()
        config = self.env['pos.config'].browse(int(data['config_id'])).exists()
        lines = self.env['pos.order.line'].browse([int(i) for i in data.get('refunded_line_ids') or []]).exists()
        original = lines.order_id.payment_ids.filtered(
            lambda p: p.payment_method_id.use_payment_terminal == 'tabby' and p.transaction_id and p.amount > 0)
        if not original:
            return {'error': _("No Tabby payment found on the original order.")}
        if len(original.mapped('transaction_id')) > 1:
            return {'error': _("The returned items were paid with several Tabby payments. Refund them one order at a time.")}
        payment_id = original[0].transaction_id
        decimals = config.currency_id.decimal_places or 2
        amount = "%.*f" % (decimals, abs(float(data['amount'])))
        try:
            payment = self._tabby_api(config.currency_id.name).refund(payment_id, {
                'amount': amount,
                'reference_id': data['reference_id'],
                'reason': _("POS return"),
            })
        except TabbyError as exc:
            return {'error': str(exc)}
        refund = next((r for r in payment.get('refunds') or [] if r.get('reference_id') == data['reference_id']), {})
        return {'refund_id': refund.get('id'), 'payment_id': payment_id}
