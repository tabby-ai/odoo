import logging
from . import api as TabbyAPI
from datetime import datetime, timedelta
from odoo import api, fields, models, _
from werkzeug.urls import url_decode, url_parse
from .. import const

_logger = logging.getLogger(__name__)

class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    def _get_specific_rendering_values(self, processing_values):
        """ Return Tabby redirect URL for template rendering. """
        res = super()._get_specific_rendering_values(processing_values)
        
        if self.provider_code != 'tabby':
            return res
        
        session = self._tabby_create_session(processing_values)

        if isinstance(session, dict) and session.get('status') == 'created':
            res['is_available'] = True
            web_url = session.get('configuration', {}).get('available_products', {}).get('installments')[0].get('web_url')
            parsed_url = url_parse(web_url)
            params = url_decode(parsed_url.query)
            res['api_url'] = web_url
            res['params'] = params

            self.provider_reference = session.get('payment', {}).get('id', None)
            self._set_pending()
        else:
            res['is_available'] = False
            res['api_url'] = None
            self._set_error(_("Sorry, Tabby is unable to approve this purchase. Please use an alternative payment method for your order."));
            return res
        
        return res
    
    def _tabby_create_session(self, processing_values):
        """ Call Tabby API to create payment session. """
        api = TabbyAPI.TabbyAPI(provider=self.provider_id)
        return api.createSession(self._get_tabby_session_data(processing_values))

    def _get_tabby_session_data(self, processing_values):
        """ Prepare data for Tabby session creation. """
        order = self.sale_order_ids[:1]
        lang = self.env.context.get('lang')[:2]
        return {
            'lang': lang if lang in ['en', 'ar'] else 'en',
            'merchant_code': self.provider_id.get_merchant_code_from_currency(order.currency_id.name),
            'merchant_urls': self.provider_id._get_merchant_urls(),
            'payment': self.get_payment_object(order),
        }

    def get_payment_object(self, order):
        """ Prepare payment object for Tabby API. """
        return {
            'amount': self.format(order.currency_id, order.amount_total),
            'currency': order.currency_id.name,
            'description': "Sales order #%s" % order.name,
            'order': self.get_order_object(),
            'buyer': self.get_buyer_object(order),
            'shipping_address': self.get_shipping_address_object(order),
            'buyer_history': self.get_buyer_history_object(order),
            'order_history': self.get_order_history_object(order),
            'meta': {
                'tabby_plugin_platform': 'Odoo',
                'tabby_plugin_version': self.provider_id.get_plugin_version(),
                'txref': str(self.reference),
            }
        }

    def get_buyer_object(self, order):
        return {
            'email': order.partner_id.email,
            'name': order.partner_id.name,
            'phone': order.partner_id.phone or order.partner_id.mobile,
        }

    def get_shipping_address_object(self, order):
        shipping = order.partner_shipping_id
        return {
            'address': " ".join(filter(None, [shipping.street, shipping.street2])),
            'city': str(shipping.city),
            'zip': str(shipping.zip or ''),
        }

    def get_buyer_history_object(self, order):
        return {
            'registered_since': order.partner_id.create_date.isoformat(timespec='seconds') + "Z",
            'loyalty_level': self.get_customer_loyality_level(order),
        }

    def get_sale_order_contacts(self, order):
        partners = (
            order.partner_id |
            order.partner_invoice_id |
            order.partner_shipping_id
        ).filtered(lambda p: p)

        phones = list(set(filter(None, partners.mapped('phone'))))

        emails = list(set(filter(None, partners.mapped('email'))))

        return phones + emails

    def get_customer_loyality_level(self, order):
        contacts = self.get_sale_order_contacts(order)

        return self.env['sale.order'].search_count([
            ('state', 'in', ['sale', 'done']),
            '|',
            ('partner_id.email', 'in', contacts),
            ('partner_id.phone', 'in', contacts),
        ])

    def get_order_history_object(self, order):
        contacts = self.get_sale_order_contacts(order)
        domain = [
            ('state', 'in', const.ORDER_STATE_MAP.keys()),
            '|',
            ('partner_id.email', 'in', contacts),
            ('partner_id.phone', 'in', contacts),
        ]

        ho = self.env['sale.order'].search(domain, limit=10, order='date_order desc')

        return [self.get_order_history_order_object(o) for o in ho]

    def get_order_history_order_object(self, order):
        transaction = order.get_portal_last_transaction()
        provider_name = transaction.provider_id.name if transaction else "cod"
        return {
            'amount': self.format(order.currency_id, order.amount_total),
            'payment_method': transaction.payment_method_id.name if transaction.payment_method_id else provider_name,
            'purchased_at': order.date_order.isoformat(timespec='seconds') + "Z",
            'status': const.ORDER_STATE_MAP.get(order.state),
            'buyer': self.get_buyer_object(order),
            'shipping_address': self.get_shipping_address_object(order),
            'items': self.get_order_history_order_items_object(order),
        }
    def get_order_history_order_items_object(self, order):
        return [self.get_order_history_order_item_object(line) for line in order.order_line if line.product_id and not line.is_delivery]

    def get_order_history_order_item_object(self, line):
        return {
            'quantity': int(line.product_uom_qty),
            'title': line.product_id.name,
            'unit_price': self._get_tabby_item_unit_price(line),
            'reference_id': self._get_tabby_item_reference_id(line),
            'ordered': int(line.product_uom_qty),
            'captured': int(line.qty_invoiced),
            'shipped': int(line.qty_delivered) if hasattr(line, 'qty_delivered') else 0,
            'refunded': int(sum(
                inv_line.quantity 
                for inv_line in line.invoice_lines 
                if inv_line.move_id.move_type == 'out_refund' and inv_line.move_id.state == 'posted'
            )),
        }

    def _get_tabby_item_unit_price(self, line):
        return self.format(
                line.order_id.currency_id,
                line.price_total / line.product_uom_qty if line.product_uom_qty else 0.0
            )

    def _get_tabby_item_reference_id(self, line):
        return line.product_id.default_code or str(line.product_id.id)

    def get_order_object(self):
        """ Prepare order object for Tabby API. """
        order = self.sale_order_ids[:1]
        if (not order):
            return {}
        return {
            'reference_id': str(order.name),
            'shipping_amount': str(self.get_shipping_amount(order)),
            'discount_amount': str(self.get_discount_amount(order)),
            'tax_amount': str(order.amount_tax),
            'items': self.get_order_items(order),
        }
    def get_shipping_amount(self, order):
        delivery_line = order.order_line.filtered(lambda l: l.is_delivery)
        if delivery_line:
            # price_total includes all applied taxes
            return sum(delivery_line.mapped('price_total'))
        return 0.0

    def get_discount_amount(self, order):
        total_discount = 0.0
        for line in order.order_line:
            # Calculate: (Unit Price * Quantity) - Subtotal
            # This gives the raw discount amount before taxes
            line_discount = (line.price_unit * line.product_uom_qty) - line.price_subtotal
            total_discount += line_discount
        return total_discount

    def get_order_items(self, order):
        """ Prepare order items for Tabby API. """
        items = []
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        for line in order.order_line:
            if not line.product_id or line.is_delivery:
                continue
            item = {
                'title': line.name,
                'quantity': int(line.product_uom_qty),
                'unit_price': self._get_tabby_item_unit_price(line),
                'reference_id': self._get_tabby_item_reference_id(line),
                'description': line.name,
                'image_url': f"{base_url}/web/image?model=product.product&id={line.product_id.id}&field=image_1920",
                'product_url': f"{base_url}/{line.product_id.website_url}",
                'category': str(line.product_id.categ_id.name or 'Uncategorized'),
            }
            items.append(item)
        return items

    def _send_capture_request(self):
        if self.provider_code != 'tabby':
            return super()._send_capture_request()

        self.ensure_one()

        api = TabbyAPI.TabbyAPI(provider=self.provider_id)

        response = api.capture(
            self.source_transaction_id.provider_reference if self.source_transaction_id else self.provider_reference,
            self._get_tabby_capture_data()
        )

        self._process('tabby', {'type': 'capture', 'response': response})

    def _get_tabby_capture_data(self):
        if self.reference != self.source_transaction_id.reference:
            return {
                'amount': str(round(self.amount, self.currency_id.decimal_places)),
                'reference_id': str(self.reference),
            }
        order = self.source_transaction_id.sale_order_ids[:1]
        return {
            'amount': str(round(self.amount, self.currency_id.decimal_places)),
            'tax_amount': str(order.amount_tax),
            'shipping_amount': str(self.get_shipping_amount(order)),
            'reference_id': str(self.reference),
            'items': [
                {
                    'title': line.display_name,
                    'description': line.name,
                    'quantity': int(line.product_uom_qty),
                    'unit_price': self._get_tabby_item_unit_price(line),
                    'reference_id': self._get_tabby_item_reference_id(line)
                } for line in order.order_line if line.product_id and not line.is_delivery
            ]
        }

    def _send_refund_request(self, amount_to_refund=None):
        if self.provider_code != 'tabby':
            return super()._send_refund_request(amount_to_refund=amount_to_refund)
        auth_txn = self.source_transaction_id

        if auth_txn.source_transaction_id:
            auth_txn = auth_txn.source_transaction_id

        if not auth_txn.provider_reference:
            raise ValidationError(_("No Tabby payment ID found for this transaction."))

        refund_amount = abs(amount_to_refund or self.amount)

        self.ensure_one()

        api = TabbyAPI.TabbyAPI(provider=self.provider_id)

        response = api.refund(
            auth_txn.provider_reference,
            {
                'amount': str(refund_amount),
                'reason': f"Refund transaction {self.reference}",
                'reference_id': str(self.reference),
            }
        )

        self._process('tabby', {'type': 'refund', 'response': response});

    def _send_void_request(self):
        if self.provider_code != 'tabby':
            return super()._send_refund_request(amount_to_refund=amount_to_refund)

        if not self.source_transaction_id.provider_reference:
            raise ValidationError(_("No Tabby payment ID found for this transaction."))

        self.ensure_one()

        api = TabbyAPI.TabbyAPI(provider=self.provider_id)

        response = api.close(self.source_transaction_id.provider_reference)

        self._process('tabby', {'type': 'void', 'response': response});

         
    def _tabby_update_payment_status(self):
        """ Retrieve payment status from Tabby API. """
        api = TabbyAPI.TabbyAPI(provider=self.provider_id)
        payment = api.get_payment(self.provider_reference)
        return self._process('tabby', {'type': 'update', 'response': payment})

    def _extract_amount_data(self, data):
        """ Override of `payment` to extract Tabby payment data. """
        if self.provider_code != 'tabby':
            return super()._extract_payment_data(data)

        if data.get('type') == 'void':
            return None
        
        payment = data.get('response')

        amount = {
            'currency_code': payment.get('currency', None),
            'amount': float(payment.get('amount') or 0),
        }

        if not amount.get('currency_code'):
            return None

        if data.get('type') == 'refund':
            refunds = [r for r in payment.get('refunds', []) if r.get('reference_id') == self.reference]

            if not refunds:
                return None

            amount['amount'] = float(refunds[0].get('amount'))
        elif data.get('type') == 'capture':
            captures = [c for c in payment.get('captures', []) if c.get('reference_id') == self.reference]
            if not captures:
                return None
            amount['amount'] = float(captures[0].get('amount'))

        return amount

    def _extract_reference(self, provider_code, payment_data):
        if provider_code != 'tabby':
            return super()._extract_reference(provider_code, payment_data)
        
        return payment_data.get('response', {}).get('meta', {}).get('txref')

    def _apply_updates(self, payment_data):
        """Override of `payment' to update the transaction based on the payment data."""
        if self.provider_code != 'tabby':
            return super()._apply_updates(payment_data)

        payment = payment_data.get('response')

        if payment_data.get('type') == 'void':
            if payment.get('status') == 'CLOSED':
                self._set_canceled();
                self.env.ref('payment.cron_post_process_payment_tx')._trigger()
                return True

        if payment_data.get('type') == 'refund':
            if len(payment.get('refunds', [])):
                refunds = [r for r in payment.get('refunds', []) if r.get('reference_id') == self.reference]
                if not refunds:
                    return False
                self.provider_reference = refunds[0].get('id')
                self._set_done()
                self.env.ref('payment.cron_post_process_payment_tx')._trigger()
                return True

        if payment_data.get('type') == 'capture':
            if len(payment.get('captures', [])):
                captures = [c for c in payment.get('captures', []) if c.get('reference_id') == self.reference]
                if not captures:
                    return False
                if not self.provider_reference:
                    self.provider_reference = captures[0].get('id')
                self._set_done()
                self.env.ref('payment.cron_post_process_payment_tx')._trigger()
                return True

        status = payment.get('status')
        if status == 'error':
            _logger.error('Transaction %s not changed due to Tabby API error response.', self.reference)
            return False
        if status == 'CREATED':
            if self.state == 'draft':
                self._set_pending()
                _logger.info('Transaction %s marked as pending.', self.reference)
        elif status == 'AUTHORIZED':
            if self.state in ['draft', 'pending']:
                self._set_authorized()
                _logger.info('Transaction %s marked as authorized.', self.reference)
                if (not self.provider_id.capture_manually):
                    self._send_capture_request()
        elif status == 'CLOSED':
            if self.state in ['draft', 'pending', 'authorized']:
                self._set_done()
                _logger.info('Transaction %s marked as done.', self.reference)        
                self.env.ref('payment.cron_post_process_payment_tx')._trigger()
        elif status == 'REJECTED':
            self._set_error()
            _logger.info('Transaction %s marked as rejected.', self.reference)
        else:
            self._set_error()
            _logger.error('Transaction %s marked as error due to unknown status: %s', self.reference, status)
        return True    

    @api.model
    def _cron_tabby_check_pending(self):
        time_limit = fields.Datetime.now() - timedelta(minutes=30)
        txs = self.search([
            ('provider_code', '=', 'tabby'),
            ('state', 'in', ['draft', 'pending']),
            ('create_date', '>=', time_limit)
        ])

        _logger.info('Tabby cron. Total transactions: %s', len(txs))

        for tx in txs:
            txs._tabby_update_payment_status()

    def format(self, currency, amount):
        return f"{amount:.{currency.decimal_places}f}"
