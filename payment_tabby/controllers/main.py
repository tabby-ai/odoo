from odoo import http
from odoo.http import request
import logging

from ..models.dd import DataDog

_logger = logging.getLogger(__name__)

class TabbyController(http.Controller):
    
    @http.route('/payment/tabby/cancel', type='http', auth='public', methods=['GET'], csrf=False, website=True)
    def tabby_cancel(self, **kwargs):
        """ Handle Tabby payment cancel notifications. """
        reference = kwargs.get('payment_id')

        if not reference:
            DataDog.ddlog(self.env, 'error', 'Customer cancel redirect without payment_id detected', data=kwargs);
            return request.redirect('/shop')

        tx_sudo = request.env['payment.transaction'].sudo().search([('provider_reference', '=', reference)], limit=1)
        if not tx_sudo:
            DataDog.ddlog(self.env, 'error', 'No transaction found on cancel redirect', data=kwargs);
            return request.redirect('/shop')
        
        # cancel only draft/pending transactions
        if tx_sudo.state in ('draft', 'pending') and tx_sudo.sale_order_ids:
            tx_sudo._set_canceled("Payment was canceled by the customer via Tabby.")

            for order in tx_sudo.sale_order_ids:
                request.session['sale_order_id'] = order.id
                order.action_draft()
            # select the Tabby payment method again
            request.session['payment_last_chosen_provider_id'] = tx_sudo.provider_id.id

        return request.redirect('/shop/payment')

    @http.route('/payment/tabby/failure', type='http', auth='public', methods=['GET'], csrf=False, website=True)
    def tabby_failure(self, **kwargs):
        """ Handle Tabby payment failure notifications. """
        _logger.info('Received Tabby failure notification: %s', kwargs)

        reference = kwargs.get('payment_id')

        if not reference:
            DataDog.ddlog(self.env, 'error', 'Tabby failure redirect without payment_id', data=kwargs);
            return request.redirect('/shop')

        tx_sudo = request.env['payment.transaction'].sudo().search([('provider_reference', '=', reference)], limit=1)
        if not tx_sudo:
            DataDog.ddlog(self.env, 'error', 'No transaction found on failure redirect', data=kwargs);
            return request.redirect('/shop')
        
        if tx_sudo.state in ('draft', 'pending') and tx_sudo.sale_order_ids:
            tx_sudo._set_error("Payment is rejected by Tabby")

            for order in tx_sudo.sale_order_ids:
                request.session['sale_order_id'] = order.id
                order.action_draft()
            # select the Tabby payment method again
            request.session['payment_last_chosen_provider_id'] = tx_sudo.provider_id.id

        _logger.info('Transaction %s marked as error due to Tabby failure notification.', tx_sudo.reference)

        return request.redirect('/shop/payment')

    @http.route('/payment/tabby/success', type='http', auth='public', methods=['GET'], csrf=False, website=True)
    def tabby_success(self, **kwargs):
        """ Handle Tabby payment success notifications. """
        reference = kwargs.get('payment_id')

        if not reference:
            DataDog.ddlog(self.env, 'error', 'Tabby success redirect without payment_id', data=kwargs);
            return request.redirect('/shop')

        tx_sudo = request.env['payment.transaction'].sudo().search([('provider_reference', '=', reference)], limit=1)
        if not tx_sudo:
            DataDog.ddlog(self.env, 'error', 'No transaction found on success redirect', data=kwargs);
            return request.redirect('/shop')
        
        if tx_sudo.state in ['draft', 'pending']:
            tx_sudo._tabby_update_payment_status()

        return request.redirect('/shop/payment/validate')

    @http.route('/payment/tabby/webhook', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def tabby_webhook(self, **kwargs):
        """ Handle Tabby webhook notifications. """
        webhook = request.get_json_data();

        log_data = {
            'payment.id': webhook.get('id'),
            'order.reference_id': webhook.get('order', {}).get('reference_id'),
            'body': webhook,
        }
        DataDog.ddlog(self.env, 'info', 'webhook received', data=log_data)

        reference = webhook.get('id')

        if not reference:
            return {"status": "error", "message": "Missing id"}

        tx_sudo = request.env['payment.transaction'].sudo().search([('provider_reference', '=', reference)], limit=1)
        if not tx_sudo:
            DataDog.ddlog(self.env, 'error', 'No transaction found for webhook', data=log_data);
            return {"status": "error", "message": "Transaction not found"}

        if tx_sudo.state in ['draft', 'pending']:
            tx_sudo._tabby_update_payment_status()

        return {"status": "success"}
        
