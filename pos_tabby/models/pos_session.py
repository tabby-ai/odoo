from odoo import _, models
from odoo.exceptions import UserError

from .pos_tabby_attempt import RELEASED_STATES


class PosSession(models.Model):
    _inherit = 'pos.session'

    def _cannot_close_session(self, *args, **kwargs):
        # The till shows this message and stays open instead of sending the
        # cashier to the back end with a generic error.
        result = super()._cannot_close_session(*args, **kwargs)
        if result:
            return result
        try:
            self._tabby_check_unlinked()
        except UserError as exc:
            return {'successful': False, 'message': exc.args[0], 'redirect': False}
        return result

    def update_closing_control_state_session(self, *args, **kwargs):
        # The closing popup moves the session to closing_control before it
        # closes it; once there, an order synced from another device finds no
        # open session. Refuse before that step, while orders can still sync.
        for session in self:
            session._tabby_check_unlinked()
        return super().update_closing_control_state_session(*args, **kwargs)

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
