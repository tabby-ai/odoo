import base64
import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

from . import const
from .pos_tabby_attempt import RELEASED_STATES, is_uuid
from .tabby_api import TabbyError, TabbyPosAPI

_logger = logging.getLogger(__name__)

SK_PATTERN = re.compile(r'^sk_(test_)?[\da-f]{8}-[\da-f]{4}-[\da-f]{4}-[\da-f]{4}-[\da-f]{12}$')


class PosPaymentMethod(models.Model):
    _inherit = 'pos.payment.method'

    tabby_secret_key = fields.Char(
        string="Tabby Secret Key",
        groups='point_of_sale.group_pos_manager',
        copy=False,
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
        The till names its own order and payment line by uuid.
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
        for key in ('order_uuid', 'line_uuid'):
            if not is_uuid(data.get(key)):
                raise ValidationError(_("Invalid %s.", key))
        return config

    def _tabby_lock(self, model, record_id):
        """Serialize concurrent Tabby calls on one record.

        The no-op UPDATE makes a concurrent transaction that updates the same
        row wait, then fail with a serialization error once this one commits;
        Odoo retries that request, which then sees what this one recorded.
        """
        self.env.cr.execute('UPDATE "%s" SET write_date = write_date WHERE id = %%s' % self.env[model]._table, [record_id])

    def _tabby_api(self, currency):
        self.ensure_one()
        return TabbyPosAPI(self.sudo().tabby_secret_key, currency,
                           user_agent="TabbyOdooPOS/%s" % self._tabby_module_version())

    @staticmethod
    def _tabby_sum(entries):
        return sum(float(e.get('amount') or 0) for e in entries or [])

    @staticmethod
    def _tabby_format(currency, amount):
        return "%.*f" % (currency.decimal_places, currency.round(float(amount)))

    def _tabby_module_version(self):
        module = self.env['ir.module.module'].sudo().search([('name', '=', 'pos_tabby')], limit=1)
        return module.installed_version or '0'

    def _tabby_order_closed(self, data):
        """A validated order takes no new Tabby payment or refund (replays of its own still answer)."""
        order = self.env['pos.order'].sudo().search(
            [('uuid', '=', data['order_uuid']), ('state', 'not in', ('draft', 'cancel'))], limit=1)
        if order:
            return {'error': _("Order %s is already validated. Start a new order.", order.pos_reference or order.name)}
        return None

    def _tabby_config_error(self, config):
        if not config.tabby_merchant_code:
            return _("Set the Tabby merchant code in the POS settings first.")
        if not config.tabby_terminal_id:
            return _("Set the Tabby terminal id in the POS settings first.")
        if not config.tabby_latitude and not config.tabby_longitude:
            return _("Set the Tabby terminal coordinates in the POS settings first.")
        return None

    def _tabby_session_payload(self, config, data, currency):
        """Build the POST /api/v2/checkout body for a till payment."""
        lang = (self.env.lang or 'en')[:2]
        attachment = {
            'latitude': config.tabby_latitude,
            'longitude': config.tabby_longitude,
            'timestamp': fields.Datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000Z'),
        }
        payment = {
            'amount': self._tabby_format(currency, data['amount']),
            'currency': currency.name,
            'description': config.tabby_terminal_id,
            'order': {
                'reference_id': data.get('order_reference') or data['order_uuid'],
                'items': data.get('items') or [],
            },
            'attachment': {
                'body': json.dumps(attachment),
                'content_type': const.ATTACHMENT_CONTENT_TYPE,
            },
            'meta': {
                'tabby_plugin_platform': 'Odoo POS',
                'tabby_plugin_version': self._tabby_module_version(),
                'pos_order_uuid': data['order_uuid'],
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

    def _tabby_rejection_message(self, session):
        product = ((session.get('configuration') or {}).get('products') or {}).get('installments') or {}
        reason = product.get('rejection_reason')
        messages = {
            'order_amount_too_high': _("The amount is above what Tabby can approve for this customer. Try a lower amount or another payment method."),
            'order_amount_too_low': _("The amount is below the Tabby minimum for this store. Please use another payment method."),
            'not_available': _("Tabby cannot approve this purchase. Please use another payment method."),
        }
        if reason in messages:
            return messages[reason]
        if reason:
            return _("Tabby cannot approve this purchase (%s). Please use another payment method.", reason)
        return _("Tabby did not approve this purchase. Please use another payment method. If every Tabby payment "
                 "on this POS is declined, ask your manager to check that its merchant code is active in Tabby.")

    def _tabby_qr_image(self, api, product):
        """PNG of the session QR: Tabby's own image, else one built from web_url."""
        qr_url = product.get('qr_code')
        if qr_url:
            for attempt in range(2):  # hpp_link_qr sometimes answers 500 once
                try:
                    return api.fetch_qr_png(qr_url)
                except TabbyError as exc:
                    _logger.warning("Tabby POS QR fetch failed (try %s): %s", attempt + 1, exc)
        web_url = product.get('web_url') or ''
        if web_url.startswith('https://'):
            try:
                return self.env['ir.actions.report'].barcode('QR', web_url, width=400, height=400)
            except Exception as exc:  # noqa: BLE001 - any rendering error means no QR
                _logger.warning("Tabby POS QR fallback failed: %s", exc.__class__.__name__)
        return None

    # -- Attempts ----------------------------------------------------------

    def _tabby_attempts(self, config, data, kind):
        return self.env['pos.tabby.attempt'].sudo().search([
            ('config_id', '=', config.id),
            ('payment_method_id', '=', self.id),
            ('kind', '=', kind),
            ('order_uuid', '=', data['order_uuid']),
        ])

    def _tabby_line_attempt(self, config, data, kind='payment'):
        return self._tabby_attempts(config, data, kind).filtered(lambda a: a.line_uuid == data['line_uuid'])[:1]

    def _tabby_open_attempt(self, config, data, kind, amount):
        """The open attempt this line should continue, if any.

        That is the line's own attempt, or one of the same order whose line the
        till no longer has (the till was reloaded, or the line was removed):
        an attempt of the same amount moves to this line, so a new line never
        takes the same money twice. Returns an attempt (maybe empty) or {error}.
        """
        attempts = self._tabby_attempts(config, data, kind).filtered(
            lambda a: a.state not in RELEASED_STATES and not a.pos_payment_id)
        own = attempts.filtered(lambda a: a.line_uuid == data['line_uuid'])[:1]
        if own:
            return own
        present = set(data.get('line_uuids') or []) | {data['line_uuid']}
        orphans = attempts.filtered(lambda a: a.line_uuid not in present)
        for attempt in orphans:
            if not attempt.currency_id.compare_amounts(attempt.amount, amount):
                attempt.line_uuid = data['line_uuid']
                return attempt
        if orphans:
            attempt = orphans[0]
            what = _("payment") if kind == 'payment' else _("refund")
            return {'error': _(
                "Tabby %(what)s %(id)s of %(amount)s on this order is not resolved. Add a Tabby line of that amount "
                "to continue it, or resolve it in the Tabby merchant dashboard.",
                what=what, id=attempt.payment_id, amount=self._tabby_format(attempt.currency_id, attempt.amount))}
        return self.env['pos.tabby.attempt']

    def _tabby_settle(self, api, payment, attempt):
        """Capture what is left of an AUTHORIZED payment and record the outcome.

        The outcome is 'paid' only when the full amount is captured: the goods
        leave the shop at once and Tabby refunds only captured payments. A
        CLOSED payment without captures was voided; one closed after part of
        it was captured is 'partial' until that part is refunded. Refunds count:
        a payment refunded in full no longer pays anything.
        """
        currency = attempt.currency_id
        status = (payment.get('status') or '').upper()
        amount = float(payment.get('amount') or attempt.amount)
        captured = self._tabby_sum(payment.get('captures'))
        result = {'status': status, 'payment_id': attempt.payment_id}
        if status == 'AUTHORIZED' and currency.compare_amounts(captured, amount) < 0:
            try:
                # reference_id makes a repeated capture of the same remainder idempotent
                api.capture(attempt.payment_id, {
                    'amount': self._tabby_format(currency, amount - captured),
                    'reference_id': "%s-pos" % attempt.payment_id,
                })
                payment = api.get_payment(attempt.payment_id)
                status = (payment.get('status') or '').upper()
                captured = self._tabby_sum(payment.get('captures'))
                result['status'] = status
            except TabbyError as exc:
                _logger.warning("Tabby POS capture failed for payment %s: %s", attempt.payment_id, exc)
                result['capture_error'] = str(exc)
        refunded = self._tabby_sum(payment.get('refunds'))
        # What Tabby keeps for the sale: a payment refunded in the dashboard pays nothing.
        if status in const.PAID_STATUSES and amount > 0 and currency.compare_amounts(captured - refunded, amount) >= 0:
            state = 'paid'
        elif status == 'CLOSED' and currency.compare_amounts(captured - refunded, 0) > 0:
            state = 'partial'
        elif status == 'CLOSED' or status in const.FAILED_STATUSES:
            state = 'failed'  # voided, rejected, expired, or a partial capture refunded in full
        else:
            state = 'pending'  # CREATED, or AUTHORIZED still waiting for the capture
        attempt.write({'state': state, 'tabby_status': status, 'captured': captured, 'refunded': refunded})
        result.update(outcome=state, captured=self._tabby_format(currency, captured))
        return result

    def _tabby_refresh(self, attempt, force=False):
        """Read the attempt's payment from Tabby and settle it."""
        if not force and attempt.state in RELEASED_STATES:
            return {'outcome': 'failed', 'status': attempt.tabby_status, 'payment_id': attempt.payment_id}
        if not force and attempt.state == 'paid' and attempt.pos_payment_id:
            return {'outcome': 'paid', 'status': attempt.tabby_status, 'payment_id': attempt.payment_id}
        api = attempt.api_method_id._tabby_api(attempt.currency_id.name)
        try:
            payment = api.get_payment(attempt.payment_id)
        except TabbyError as exc:
            return {'error': str(exc), 'outcome': 'pending', 'payment_id': attempt.payment_id}
        return self._tabby_settle(api, payment, attempt)

    def _tabby_cancel(self, attempt):
        """Cancel the attempt's session.

        returns {cancelled: True} once Tabby confirms the cancel; when the
        session is already finalized, the payment outcome instead (the
        customer may have paid); {error, uncertain: True} when it is unknown.
        """
        if attempt.state in RELEASED_STATES:
            return {'cancelled': True}
        if attempt.state == 'paid' and attempt.pos_payment_id:
            return {'outcome': 'paid', 'status': attempt.tabby_status, 'payment_id': attempt.payment_id}
        api = attempt.api_method_id._tabby_api(attempt.currency_id.name)
        try:
            api.cancel_session(attempt.session_id)
            attempt.write({'state': 'cancelled'})
            return {'cancelled': True}
        except TabbyError:
            try:
                return self._tabby_settle(api, api.get_payment(attempt.payment_id), attempt)
            except TabbyError as exc:
                return {'error': str(exc), 'uncertain': True, 'payment_id': attempt.payment_id}

    # -- RPC called from the POS ------------------------------------------
    # Every call names the till's own order and payment line (uuids); the
    # Tabby ids are read from the pos.tabby.attempt the server recorded.

    def tabby_create_session(self, data):
        """Create a Tabby session for a payment line, or continue its open one.

        data: {config_id, order_uuid, line_uuid, line_uuids, amount, order_reference, items, buyer}
        returns the attempt ({state, payment_id, qr_image, ...}) or {error}
        """
        config = self._tabby_context(data)
        error = self._tabby_config_error(config)
        if error:
            return {'error': error}
        if config.current_session_id.state == 'closing_control':
            # The session is closing: a new QR could take money after the close check.
            return {'error': _("This POS session is closing. Open a new session to take a Tabby payment.")}
        currency = config.currency_id
        amount = currency.round(float(data.get('amount') or 0))
        if currency.compare_amounts(amount, 0) <= 0:
            return {'error': _("A Tabby payment needs a positive amount.")}
        # One session per line, also when two devices send the same order.
        self._tabby_lock('pos.config', config.id)
        attempt = self._tabby_open_attempt(config, data, 'payment', amount)
        if isinstance(attempt, dict):
            return attempt
        if not attempt:
            closed = self._tabby_order_closed(data)
            if closed:
                return closed
        if attempt and currency.compare_amounts(attempt.amount, amount):
            # The cashier changed the amount after the session was created.
            result = self._tabby_cancel(attempt) if attempt.state == 'pending' else {}
            if not result.get('cancelled'):
                return {'error': _(
                    "Tabby payment %(id)s for this line is for %(amount)s %(cur)s. Set the line back to that "
                    "amount, or resolve the payment in the Tabby merchant dashboard.",
                    id=attempt.payment_id, amount=self._tabby_format(currency, attempt.amount), cur=currency.name)}
            attempt = self.env['pos.tabby.attempt']
        if attempt:
            return attempt._view(resumed=True)
        try:
            api = self._tabby_api(currency.name)
            session = api.create_session(self._tabby_session_payload(config, {**data, 'amount': amount}, currency))
        except TabbyError as exc:
            if exc.status == 401 and 'merchant_code' in str(exc):
                return {'error': _(
                    "Merchant code \"%s\" of this POS is not set up in Tabby for this secret key. Ask your manager "
                    "to check the Tabby settings of this POS.", config.tabby_merchant_code)}
            return {'error': str(exc)}
        session_id = session.get('id')
        payment = session.get('payment') or {}
        if session.get('status') != 'created':
            return {'error': self._tabby_rejection_message(session), 'rejected': True}
        if not is_uuid(session_id) or not is_uuid(payment.get('id')):
            return {'error': _("Tabby returned an incomplete session. Please try again.")}
        product = (((session.get('configuration') or {}).get('available_products') or {}).get('installments') or [{}])[0]
        attempt = self.env['pos.tabby.attempt'].sudo().create({
            'kind': 'payment',
            'config_id': config.id,
            'payment_method_id': self.id,
            'api_method_id': self.id,
            'currency_id': currency.id,
            'order_uuid': data['order_uuid'],
            'line_uuid': data['line_uuid'],
            'order_reference': data.get('order_reference'),
            'session_id': session_id,
            'payment_id': payment['id'],
            'amount': float(payment.get('amount') or amount),
            'tabby_status': (payment.get('status') or 'CREATED').upper(),
            'web_url': product.get('web_url'),
        })
        qr_png = self._tabby_qr_image(api, product)
        if not qr_png:
            result = self._tabby_cancel(attempt)
            if result.get('cancelled'):
                return {'error': _("Tabby could not show a QR code for this payment. Please try again.")}
            return attempt._view(message=_(
                "Tabby could not show a QR code for payment %s and did not confirm its cancellation. "
                "The till keeps checking it.", attempt.payment_id))
        attempt.qr_image = 'data:image/png;base64,' + base64.b64encode(qr_png).decode()
        return attempt._view()

    def tabby_resume(self, data):
        """data: {config_id, order_uuid, line_uuid} -> the line's payment attempt, after a till reload."""
        config = self._tabby_context(data)
        attempt = self._tabby_line_attempt(config, data)
        if not attempt:
            return {'found': False}
        return attempt._view(found=True)

    def tabby_payment_status(self, data):
        """data: {config_id, order_uuid, line_uuid} -> {status, outcome, captured, ...} or {error}."""
        config = self._tabby_context(data)
        attempt = self._tabby_line_attempt(config, data)
        if not attempt:
            return {'outcome': 'failed', 'error': _("No Tabby payment was started for this line.")}
        return self._tabby_refresh(attempt)

    def tabby_cancel_session(self, data):
        """data: {config_id, order_uuid, line_uuid} -> see _tabby_cancel."""
        config = self._tabby_context(data)
        attempt = self._tabby_line_attempt(config, data)
        if not attempt:
            return {'cancelled': True}  # nothing was sent to Tabby for this line
        return self._tabby_cancel(attempt)

    def tabby_refund(self, data):
        """Refund a Tabby payment from a POS return.

        data: {config_id, order_uuid, line_uuid, line_uuids, amount, refunded_lines: [{id, qty}]}
        Every returned line must come from one original order of this company,
        paid with one Tabby payment of a method enabled on this POS. The amount
        may not exceed the value of the returned items, nor what Tabby has
        left to refund. The refund reference is fixed per payment line, so a
        retry after a lost response finds the same refund instead of refunding
        twice. returns {refund_id, payment_id} or {error[, uncertain]}
        """
        config = self._tabby_context(data)
        currency = config.currency_id
        amount = currency.round(abs(float(data.get('amount') or 0)))
        if currency.is_zero(amount):
            return {'error': _("A Tabby refund needs an amount.")}
        self._tabby_lock('pos.config', config.id)
        attempt = self._tabby_open_attempt(config, data, 'refund', amount)
        if isinstance(attempt, dict):
            return attempt
        if attempt and currency.compare_amounts(attempt.amount, amount):
            return {'error': _(
                "A Tabby refund of %(amount)s was started for this line. Check payment %(id)s in the Tabby "
                "merchant dashboard; do not refund this line again.",
                amount=self._tabby_format(currency, attempt.amount), id=attempt.payment_id)}
        if attempt.state == 'refunded':
            return {'refund_id': attempt.refund_id, 'payment_id': attempt.payment_id}
        # A refund already sent once may have moved money: until Tabby shows its
        # reference, any failure (even of the lookup) leaves it uncertain.
        sent_before = bool(attempt)
        if not attempt:
            attempt = self._tabby_order_closed(data) or self._tabby_new_refund(config, data, amount)
            if isinstance(attempt, dict):
                return attempt
        api = attempt.api_method_id._tabby_api(currency.name)
        try:
            payment = api.get_payment(attempt.payment_id)
            existing = next((r for r in payment.get('refunds') or [] if r.get('reference_id') == attempt.reference), None)
            if existing:
                done = float(existing.get('amount') or 0)
                attempt.write({'state': 'refunded', 'refund_id': existing.get('id'), 'amount': done})
                if currency.compare_amounts(done, amount):
                    return {'error': _(
                        "Tabby already refunded %(amount)s with this line's reference. Check payment %(id)s in "
                        "the Tabby merchant dashboard; do not refund this line again.",
                        amount=existing.get('amount'), id=attempt.payment_id)}
                return {'refund_id': existing.get('id'), 'payment_id': attempt.payment_id}
            if attempt.refund_posted:
                # Tabby accepted it already: wait for it to list the reference, never send it again.
                return {'error': _("Tabby did not confirm the refund yet."), 'uncertain': True}
            # What Tabby holds now: refunds made anywhere (POS, dashboard, a
            # dispute) count, so the POS never refunds money Tabby already gave back.
            outside = self._tabby_sync_sale(payment, attempt)
            refundable = self._tabby_sum(payment.get('captures')) - self._tabby_sum(payment.get('refunds'))
            if currency.compare_amounts(amount, refundable) > 0:
                attempt.write({'state': 'failed', 'message': "refundable %s" % refundable})
                if currency.compare_amounts(outside, 0) > 0:
                    return {'error': _(
                        "Only %(left)s %(cur)s is left to refund on this Tabby payment: %(outside)s %(cur)s was "
                        "already refunded outside the POS (merchant dashboard or a dispute).",
                        left=self._tabby_format(currency, max(refundable, 0.0)), cur=currency.name,
                        outside=self._tabby_format(currency, outside))}
                return {'error': _("Only %(left)s %(cur)s is left to refund on this Tabby payment.",
                                   left=self._tabby_format(currency, max(refundable, 0.0)), cur=currency.name)}
            payment = api.refund(attempt.payment_id, {
                'amount': self._tabby_format(currency, amount),
                'reference_id': attempt.reference,
                'reason': _("POS return"),
            })
        except TabbyError as exc:
            # No HTTP status (network), rate limit or a 5xx: the refund may have happened.
            uncertain = sent_before or exc.status is None or exc.status == 429 or exc.status >= 500
            if not uncertain:
                attempt.write({'state': 'failed', 'message': str(exc)[:200]})
            return {'error': str(exc), 'uncertain': uncertain}
        refund = self._tabby_find_refund(payment, attempt)
        if not refund:
            try:
                refund = self._tabby_find_refund(api.get_payment(attempt.payment_id), attempt)
            except TabbyError:
                refund = None
        if not refund:
            # The refund was sent, but Tabby does not list it: it stays pending
            # under the same reference until Tabby shows it.
            attempt.write({'refund_posted': True})
            return {'error': _("Tabby did not confirm the refund yet."), 'uncertain': True}
        attempt.write({'state': 'refunded', 'refund_id': refund.get('id')})
        return {'refund_id': refund.get('id'), 'payment_id': attempt.payment_id}

    def _tabby_sync_sale(self, payment, refund_attempt):
        """Store on the original sale what Tabby shows now; return the amount refunded outside the POS."""
        Attempt = self.env['pos.tabby.attempt'].sudo()
        sale = Attempt.search([('kind', '=', 'payment'), ('payment_id', '=', refund_attempt.payment_id),
                               ('state', '=', 'paid')], limit=1)
        captured = self._tabby_sum(payment.get('captures'))
        refunded = self._tabby_sum(payment.get('refunds'))
        if sale:
            sale.write({'captured': captured, 'refunded': refunded,
                        'tabby_status': (payment.get('status') or sale.tabby_status or '').upper()})
        ours = set(Attempt.search([('kind', '=', 'refund'), ('payment_id', '=', refund_attempt.payment_id)]).mapped('reference'))
        return sum(float(r.get('amount') or 0) for r in payment.get('refunds') or [] if r.get('reference_id') not in ours)

    @staticmethod
    def _tabby_find_refund(payment, attempt):
        return next((r for r in (payment or {}).get('refunds') or [] if r.get('reference_id') == attempt.reference), None)

    def _tabby_new_refund(self, config, data, amount):
        """Check a new refund against the return and record its attempt."""
        currency = config.currency_id
        qty_by_line = {}
        for entry in data.get('refunded_lines') or []:
            line_id = int(entry['id'])
            qty_by_line[line_id] = qty_by_line.get(line_id, 0.0) + abs(float(entry.get('qty') or 0))
        lines = self.env['pos.order.line'].browse(list(qty_by_line)).exists()
        if not lines:
            return {'error': _("Tabby refunds work from a return of a Tabby-paid order (Actions > Refund).")}
        lines.check_access('read')
        if len(lines.order_id) != 1:
            return {'error': _("A Tabby refund covers items from one original order. Return each order separately.")}
        original_order = lines.order_id
        if original_order.company_id != config.company_id or original_order.currency_id != currency:
            return {'error': _("The original order belongs to another company or currency.")}
        # The original Tabby payment comes from attempts checked at order sync,
        # never from a transaction id the till wrote.
        Attempt = self.env['pos.tabby.attempt'].sudo()
        paid = Attempt.search([('kind', '=', 'payment'), ('state', '=', 'paid'),
                               ('pos_payment_id', 'in', original_order.payment_ids.ids)])
        if not paid:
            return {'error': _("No Tabby payment found on the original order.")}
        if len(paid) > 1:
            return {'error': _("The original order was paid with several Tabby payments. Refund it from the Tabby merchant dashboard.")}
        if paid.payment_method_id not in config.payment_method_ids:
            return {'error': _("The original order was paid with Tabby method %s, which is not enabled on this POS.",
                               paid.payment_method_id.name)}
        # Every refund of this payment, from any till, waits for the one before.
        self._tabby_lock('pos.payment', paid.pos_payment_id.id)
        refunds = Attempt.search([('kind', '=', 'refund'), ('payment_id', '=', paid.payment_id),
                                  ('state', 'in', ('pending', 'refunded')), ('pos_payment_id', '=', False)])
        this_order = refunds.filtered(lambda a: a.order_uuid == data['order_uuid'])
        # Quantities held by Tabby refunds of other returns not synced yet (one list per return).
        held = {}
        for order_uuid in set((refunds - this_order).mapped('order_uuid')):
            entry = (refunds - this_order).filtered(lambda a: a.order_uuid == order_uuid)[:1]
            for line_id, qty in json.loads(entry.refund_lines or '{}').items():
                held[int(line_id)] = held.get(int(line_id), 0.0) + qty
        # Cap: the value of the items returned, not more than is left on the
        # original lines after synced returns (drafts and this return excluded)
        # and returns holding a Tabby refund, minus this return's other refunds.
        value = 0.0
        for line in lines:
            if not line.qty:
                continue
            returned = -sum(line.refund_orderline_ids.filtered(
                lambda r: r.order_id.state not in ('draft', 'cancel') and r.order_id.uuid != data['order_uuid']).mapped('qty'))
            left = max(line.qty - returned - held.get(line.id, 0.0), 0.0)
            value += line.price_subtotal_incl / line.qty * min(qty_by_line[line.id], left)
        left = currency.round(value - sum(this_order.mapped('amount')))
        if currency.compare_amounts(amount, left) > 0:
            return {'error': _("The refund is more than the value of the returned items: at most %(left)s %(cur)s.",
                               left=self._tabby_format(currency, max(left, 0.0)), cur=currency.name)}
        return self.env['pos.tabby.attempt'].sudo().create({
            'kind': 'refund',
            'config_id': config.id,
            'payment_method_id': self.id,
            'api_method_id': paid.api_method_id.id,
            'currency_id': currency.id,
            'order_uuid': data['order_uuid'],
            'line_uuid': data['line_uuid'],
            'payment_id': paid.payment_id,
            'refund_lines': json.dumps({str(k): v for k, v in qty_by_line.items()}),
            'reference': "%s-%s" % (data['order_uuid'], data['line_uuid']),
            'amount': amount,
        })
