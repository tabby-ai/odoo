from odoo import _, models
from odoo.exceptions import UserError

from .pos_tabby_attempt import RELEASED_STATES


class PosSession(models.Model):
    _inherit = 'pos.session'

    def _validate_session(self, *args, **kwargs):
        for session in self:
            session._tabby_check_unlinked()
        return super()._validate_session(*args, **kwargs)

    def _tabby_check_unlinked(self):
        """Refuse to close while Tabby holds money no order of this POS records.

        That is a payment the customer made (or a refund sent) on an order that
        was deleted, or never synced: without this check it would only show in
        the Tabby Payments list.
        """
        self.ensure_one()
        held = self.env['pos.tabby.attempt'].sudo().search([
            ('config_id', '=', self.config_id.id), ('pos_payment_id', '=', False),
            ('state', 'not in', RELEASED_STATES),
        ])._tabby_resolve_unlinked()
        if held:
            raise UserError(_(
                "This session cannot be closed: %s. Each one belongs to an order that was deleted or not synced. "
                "Refund it in the Tabby merchant dashboard (the POS then releases it), or ask your manager to check "
                "Point of Sale > Orders > Tabby Payments.", held._tabby_describe()))
