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

    def _tabby_context(self, data):
        """Check access and return the POS config the call is made from.

        The secret key is read with sudo, so the caller must be allowed to
        read both this payment method and the config, and the method must be
        one of the config's payment methods (no use of another company's key).
        """
        self.ensure_one()
        self._tabby_check_access()
        if self.use_payment_terminal != 'tabby':
            raise AccessError(_("This payment method is not a Tabby terminal."))
        self.check_access('read')
        config = self.env['pos.config'].browse(int(data.get('config_id') or 0)).exists()
        if not config:
            raise AccessError(_("POS configuration not found."))
        config.check_access('read')
        if self not in config.payment_method_ids:
            raise AccessError(_("This payment method is not enabled on this POS."))
        return config

    def _tabby_api(self, currency):
        self.ensure_one()
        return TabbyPosAPI(self.sudo().tabby_secret_key, currency)

    @staticmethod
    def _tabby_sum(entries):
        return sum(float(e.get('amount') or 0) for e in entries or [])

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
        config = self._tabby_context(data)
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
        """data: {config_id, payment_id} -> {status, outcome, ...} or {error}.

        outcome is 'paid' only when the full amount is captured: the goods
        leave the shop at once and Tabby refunds only captured payments. An
        AUTHORIZED payment is captured here for the remaining amount; a
        CLOSED payment without captures was voided, not paid.
        """
        config = self._tabby_context(data)
        try:
            api = self._tabby_api(config.currency_id.name)
            payment = api.get_payment(data['payment_id'])
        except TabbyError as exc:
            return {'error': str(exc)}
        return self._tabby_settle(api, payment)

    def _tabby_settle(self, api, payment):
        status = (payment.get('status') or '').upper()
        amount = float(payment.get('amount') or 0)
        captured = self._tabby_sum(payment.get('captures'))
        result = {'status': status}
        if status == 'AUTHORIZED' and amount - captured > 0.001:
            remaining = "%.2f" % (amount - captured) if payment.get('currency') != 'KWD' else "%.3f" % (amount - captured)
            try:
                # reference_id makes a repeated capture of the same remainder idempotent
                api.capture(payment['id'], {'amount': remaining, 'reference_id': "%s-pos" % payment['id']})
                payment = api.get_payment(payment['id'])
                status = (payment.get('status') or '').upper()
                captured = self._tabby_sum(payment.get('captures'))
                result['status'] = status
            except TabbyError as exc:
                _logger.warning("Tabby POS capture failed for payment %s: %s", payment.get('id'), exc)
                result['capture_error'] = str(exc)
        fully_captured = amount > 0 and captured >= amount - 0.001
        result['captured'] = captured
        if status in const.PAID_STATUSES and fully_captured:
            result['outcome'] = 'paid'
        elif status == 'CLOSED' and captured > 0:
            result['outcome'] = 'partial'  # closed after a partial capture: needs a manual decision
        elif status == 'CLOSED' or status in const.FAILED_STATUSES:
            result['outcome'] = 'failed'  # CLOSED without any capture: voided
        else:
            result['outcome'] = 'pending'  # CREATED, or AUTHORIZED still waiting for capture
        return result

    def tabby_cancel_session(self, data):
        """data: {config_id, session_id, payment_id}

        returns {cancelled: True} once Tabby confirms the cancel; when the
        session is already finalized, the payment status instead (the
        customer may have paid); {error, uncertain: True} when it is unknown.
        """
        config = self._tabby_context(data)
        try:
            api = self._tabby_api(config.currency_id.name)
            api.cancel_session(data['session_id'])
            return {'cancelled': True}
        except TabbyError as exc:
            if not data.get('payment_id'):
                return {'error': str(exc), 'uncertain': True}
            try:
                return self._tabby_settle(api, api.get_payment(data['payment_id']))
            except TabbyError as exc2:
                return {'error': str(exc2), 'uncertain': True}

    def tabby_refund(self, data):
        """Refund a Tabby payment from a POS return.

        data: {config_id, refunded_line_ids, amount, reference_id}
        Every returned line must come from one original order paid with one
        Tabby payment; the amount may not exceed what is left to refund. The
        reference_id is stable per return order, so a retry after a lost
        response does not refund twice.
        returns {refund_id, payment_id} or {error}
        """
        config = self._tabby_context(data)
        lines = self.env['pos.order.line'].browse([int(i) for i in data.get('refunded_line_ids') or []]).exists()
        lines.check_access('read')
        if len(lines.order_id) != 1:
            return {'error': _("A Tabby refund covers items from one original order. Return each order separately.")}
        original = lines.order_id.payment_ids.filtered(
            lambda p: p.payment_method_id.use_payment_terminal == 'tabby' and p.transaction_id and p.amount > 0)
        if not original:
            return {'error': _("No Tabby payment found on the original order.")}
        if len(set(original.mapped('transaction_id'))) > 1:
            return {'error': _("The original order was paid with several Tabby payments. Refund it from the Tabby merchant dashboard.")}
        payment_id = original[0].transaction_id
        # The refund runs with the key and currency of the original payment.
        method = original[0].payment_method_id
        currency = original[0].pos_order_id.currency_id
        decimals = currency.decimal_places or 2
        amount = abs(float(data['amount']))
        try:
            api = method._tabby_api(currency.name)
            payment = api.get_payment(payment_id)
            existing = next((r for r in payment.get('refunds') or [] if r.get('reference_id') == data['reference_id']), None)
            if existing:
                if abs(float(existing.get('amount') or 0) - amount) > 0.001:
                    return {'error': _("A Tabby refund with this reference already exists for %(amount)s. Remove this line and add a new one.",
                                       amount=existing.get('amount'))}
                return {'refund_id': existing.get('id'), 'payment_id': payment_id}
            refundable = self._tabby_sum(payment.get('captures')) - self._tabby_sum(payment.get('refunds'))
            if amount > refundable + 0.001:
                return {'error': _("Only %(left)s %(cur)s is left to refund on this Tabby payment.",
                                   left="%.*f" % (decimals, refundable), cur=currency.name)}
            payment = api.refund(payment_id, {
                'amount': "%.*f" % (decimals, amount),
                'reference_id': data['reference_id'],
                'reason': _("POS return"),
            })
        except TabbyError as exc:
            return {'error': str(exc)}
        refund = next((r for r in payment.get('refunds') or [] if r.get('reference_id') == data['reference_id']), {})
        return {'refund_id': refund.get('id'), 'payment_id': payment_id}
