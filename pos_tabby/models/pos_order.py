from odoo import _, api, models
from odoo.exceptions import UserError

from .pos_tabby_attempt import RELEASED_STATES


class PosOrder(models.Model):
    _inherit = 'pos.order'

    @api.model
    def _process_order(self, order, existing_order):
        order_id = super()._process_order(order, existing_order)
        pos_order = self.browse(order_id)
        if pos_order.state not in ('draft', 'cancel'):
            pos_order._tabby_check_payments()
        return order_id

    def _tabby_check_payments(self):
        """Accept a paid order only when Tabby confirms its Tabby lines.

        Every Tabby payment line must match a fully captured attempt of this
        order and line (a refund line, a confirmed refund) with the same
        amount, used once. An attempt of this order without a line must hold
        no money: open sessions are cancelled here, and a payment the customer
        made whose line was removed blocks the order until it is put back or
        refunded in the Tabby merchant dashboard.
        """
        self.ensure_one()
        Attempt = self.env['pos.tabby.attempt'].sudo()
        currency = self.currency_id
        linked = Attempt
        for payment in self.payment_ids:
            method = payment.payment_method_id
            if method.use_payment_terminal != 'tabby' or currency.is_zero(payment.amount):
                continue
            kind = 'payment' if payment.amount > 0 else 'refund'
            attempt = Attempt.search([
                ('kind', '=', kind), ('order_uuid', '=', self.uuid), ('line_uuid', '=', payment.uuid),
                ('payment_method_id', '=', method.id), ('state', 'not in', RELEASED_STATES),
            ], limit=1)
            if attempt and kind == 'payment' and attempt.state != 'paid':
                method._tabby_refresh(attempt)
            expected = 'paid' if kind == 'payment' else 'refunded'
            if (not attempt or attempt.state != expected or attempt.currency_id != currency
                    or currency.compare_amounts(attempt.amount, abs(payment.amount))
                    or (attempt.pos_payment_id and attempt.pos_payment_id != payment)):
                raise UserError(_(
                    "%(order)s: Tabby has not confirmed the %(kind)s of %(amount)s %(cur)s on its Tabby line. "
                    "Send the line to Tabby again, or remove it and use another payment method.",
                    order=self.pos_reference or self.name, kind=_("payment") if kind == 'payment' else _("refund"),
                    amount=method._tabby_format(currency, abs(payment.amount)), cur=currency.name))
            attempt.write({'pos_payment_id': payment.id})
            if not payment.transaction_id:
                payment.transaction_id = attempt.refund_id or attempt.payment_id
            linked |= attempt

        for attempt in Attempt.search([('order_uuid', '=', self.uuid), ('state', 'not in', RELEASED_STATES),
                                       ('id', 'not in', linked.ids)]):
            method = attempt.payment_method_id
            if attempt.kind == 'payment':
                if attempt.state == 'pending':
                    method._tabby_cancel(attempt)
                else:
                    method._tabby_refresh(attempt, force=True)
                if attempt.state in RELEASED_STATES:
                    continue
                if attempt.tabby_status == 'CLOSED' and currency.compare_amounts(attempt.captured - attempt.refunded, 0) <= 0:
                    continue  # refunded in full in the dashboard
                raise UserError(_(
                    "%(order)s: Tabby payment %(id)s of %(amount)s %(cur)s is %(state)s, but the order has no "
                    "Tabby line for it. Add a Tabby line of that amount, or refund the payment in the Tabby "
                    "merchant dashboard.",
                    order=self.pos_reference or self.name, id=attempt.payment_id,
                    amount=method._tabby_format(currency, attempt.amount), cur=currency.name,
                    state=dict(attempt._fields['state'].selection).get(attempt.state)))
            if attempt.state == 'pending':
                # A refund sent without a confirmed answer: did the money move?
                try:
                    payment = attempt.api_method_id._tabby_api(currency.name).get_payment(attempt.payment_id)
                except Exception:  # noqa: BLE001 - unknown means the order must wait
                    payment = None
                if payment is not None and not any(r.get('reference_id') == attempt.reference
                                                   for r in payment.get('refunds') or []):
                    attempt.write({'state': 'failed', 'message': "not found in Tabby at order sync"})
                    continue
            raise UserError(_(
                "%(order)s: a Tabby refund of %(amount)s %(cur)s on payment %(id)s went to the customer or is not "
                "confirmed, but the order has no Tabby line for it. Add a Tabby refund line of that amount, or check "
                "the payment in the Tabby merchant dashboard.",
                order=self.pos_reference or self.name, amount=method._tabby_format(currency, attempt.amount),
                cur=currency.name, id=attempt.payment_id))
