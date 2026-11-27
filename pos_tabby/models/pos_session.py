from odoo import _, models

from .pos_tabby_attempt import RELEASED_STATES


class PosSession(models.Model):
    _inherit = 'pos.session'

    def _validate_session(self, *args, **kwargs):
        # Closing never waits for Tabby: open sessions of the POS are cancelled,
        # and money Tabby holds without an order is written to the session so a
        # manager sees it (also in Point of Sale > Orders > Tabby Payments).
        for session in self:
            session._tabby_report_unlinked()
        return super()._validate_session(*args, **kwargs)

    def _tabby_report_unlinked(self):
        self.ensure_one()
        try:
            held = self.env['pos.tabby.attempt'].sudo().search([
                ('config_id', '=', self.config_id.id), ('pos_payment_id', '=', False),
                ('state', 'not in', RELEASED_STATES),
            ])._tabby_resolve_unlinked()
        except Exception:  # noqa: BLE001 - Tabby or the network down must not stop the close
            return self.env['pos.tabby.attempt']
        if held:
            self.message_post(body=_(
                "Tabby holds money that no order of this POS records: %s. Refund it in the Tabby merchant "
                "dashboard, or record it and mark it reconciled in Point of Sale > Orders > Tabby Payments.",
                held._tabby_describe()))
        return held
