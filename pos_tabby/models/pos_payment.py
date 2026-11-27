from odoo import _, models
from odoo.exceptions import UserError

# What makes a recorded Tabby line match the Tabby payment behind it.
LOCKED_FIELDS = ('payment_method_id', 'amount', 'pos_order_id')


class PosPayment(models.Model):
    _inherit = 'pos.payment'

    def _tabby_linked(self):
        return self.env['pos.tabby.attempt'].sudo().search([('pos_payment_id', 'in', self.ids)])

    def write(self, vals):
        if not self.env.su and any(f in vals for f in LOCKED_FIELDS) and self._tabby_linked():
            raise UserError(_("A payment line confirmed by Tabby cannot be changed. Refund it in the Tabby "
                              "merchant dashboard instead."))
        return super().write(vals)

    def unlink(self):
        if not self.env.su and self._tabby_linked():
            raise UserError(_("A payment line confirmed by Tabby cannot be deleted. Refund it in the Tabby "
                              "merchant dashboard instead."))
        return super().unlink()
