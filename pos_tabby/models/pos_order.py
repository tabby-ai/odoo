import json

from odoo import _, models
from odoo.exceptions import UserError

from .pos_tabby_attempt import RELEASED_STATES


class PosOrder(models.Model):
    _inherit = 'pos.order'

    def action_pos_order_paid(self):
        # Every way to a paid order (sync from the till, the backend payment
        # wizard, a direct call on a draft) goes through here.
        for order in self:
            order._tabby_check_payments()
        return super().action_pos_order_paid()

    def write(self, vals):
        # A direct write of the state (an RPC from the till) skips
        # action_pos_order_paid: check the Tabby lines here too. No context
        # flag skips it (a client can send any context); a second check of an
        # order is cheap, its attempts are linked and Tabby is not called again.
        if vals.get('state') in ('paid', 'done', 'invoiced'):
            for order in self.filtered(lambda o: o.state == 'draft'):
                order._tabby_check_payments()
        elif vals.get('state') == 'cancel':
            # Cancel from the till, the back end or remove_from_ui: a draft that
            # holds Tabby money cannot go, the customer would stay charged.
            self.check_access('write')  # before any call to Tabby
            for order in self.filtered(lambda o: o.state == 'draft'):
                held = self.env['pos.tabby.attempt'].sudo().search(
                    [('order_uuid', '=', order.uuid)])._tabby_resolve_unlinked()
                if held:
                    raise UserError(_(
                        "%(order)s cannot be cancelled: %(what)s. Finish the order, or refund the payment in the "
                        "Tabby merchant dashboard first.",
                        order=order.pos_reference or order.name, what=held._tabby_describe()))
        return super().write(vals)

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
                # The session was created with this POS's merchant code, not another branch's.
                ('config_id', '=', self.config_id.id),
            ], limit=1)
            if attempt and kind == 'payment' and not attempt.pos_payment_id:
                # Read Tabby again before the first sale it pays: it may have
                # been refunded in the dashboard since the till saw it paid.
                refreshed = method._tabby_refresh(attempt, force=True)
                if refreshed.get('error'):
                    raise UserError(_(
                        "%(order)s: Tabby could not be reached to confirm payment %(id)s. Try again in a moment.",
                        order=self.pos_reference or self.name, id=attempt.payment_id))
            expected = 'paid' if kind == 'payment' else 'refunded'
            if (not attempt or attempt.state != expected or attempt.currency_id != currency
                    or currency.compare_amounts(attempt.amount, abs(payment.amount))
                    or (attempt.pos_payment_id and attempt.pos_payment_id != payment)):
                raise UserError(_(
                    "%(order)s: Tabby has not confirmed the %(kind)s of %(amount)s %(cur)s on its Tabby line. "
                    "Send the line to Tabby again, or remove it and use another payment method.",
                    order=self.pos_reference or self.name, kind=_("payment") if kind == 'payment' else _("refund"),
                    amount=method._tabby_format(currency, abs(payment.amount)), cur=currency.name))
            if kind == 'refund':
                self._tabby_check_refund_lines(attempt)
            attempt.write({'pos_payment_id': payment.id})
            # The Tabby id on the line is the attempt's, whatever the till sent.
            canonical = attempt.refund_id if kind == 'refund' else attempt.payment_id
            if canonical and payment.transaction_id != canonical:
                payment.transaction_id = canonical
            linked |= attempt

        # Tabby pays at most the order: never more, handed back as cash change.
        tabby = self.payment_ids.filtered(lambda p: p.payment_method_id.use_payment_terminal == 'tabby')
        tabby_total = abs(sum(tabby.mapped('amount')))
        if tabby and currency.compare_amounts(tabby_total, abs(self.amount_total)) > 0:
            raise UserError(_(
                "%(order)s: the Tabby lines (%(tabby)s %(cur)s) are more than the order total. Tabby cannot pay "
                "change: set the Tabby line to the amount it pays.",
                order=self.pos_reference or self.name, tabby=self.payment_ids[:1].payment_method_id._tabby_format(
                    currency, tabby_total), cur=currency.name))

        held = Attempt.search([('order_uuid', '=', self.uuid), ('id', 'not in', linked.ids)])._tabby_resolve_unlinked()
        if held:
            raise UserError(_(
                "%(order)s: %(what)s, but the order has no Tabby line for it. Add a Tabby line of that amount, "
                "or resolve it in the Tabby merchant dashboard.",
                order=self.pos_reference or self.name, what=held._tabby_describe()))

    def _tabby_check_refund_lines(self, attempt):
        """A Tabby refund may not cover more items than this return gives back,
        nor may all returns of an item (in any tender) give back more than was sold."""
        returned = {}
        for line in self.lines.filtered('refunded_orderline_id'):
            key = line.refunded_orderline_id.id
            returned[key] = returned.get(key, 0.0) + abs(line.qty)
        for line_id, qty in json.loads(attempt.refund_lines or '{}').items():
            if qty > returned.get(int(line_id), 0.0) + 1e-6:
                raise UserError(_(
                    "%(order)s: Tabby refund %(id)s covers more items than this return gives back. Check the "
                    "refund in the Tabby merchant dashboard.",
                    order=self.pos_reference or self.name, id=attempt.payment_id))
        for original in self.lines.refunded_orderline_id:
            others = original.refund_orderline_ids.filtered(
                lambda r: r.order_id != self and r.order_id.state not in ('draft', 'cancel'))
            total = sum(abs(q) for q in others.mapped('qty')) + returned.get(original.id, 0.0)
            if total > original.qty + 1e-6:
                raise UserError(_(
                    "%(order)s: %(product)s was already returned on another order. Check Tabby refund %(id)s in "
                    "the Tabby merchant dashboard.",
                    order=self.pos_reference or self.name, product=original.full_product_name or original.product_id.display_name,
                    id=attempt.payment_id))
