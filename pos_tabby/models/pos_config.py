from odoo import _, api, fields, models
from odoo.exceptions import AccessError, ValidationError

# Branch routing and terminal identity: set by a POS manager, never at the till.
TABBY_FIELDS = ('tabby_merchant_code', 'tabby_terminal_id', 'tabby_latitude', 'tabby_longitude', 'tabby_timeout')


class PosConfig(models.Model):
    _inherit = 'pos.config'

    tabby_merchant_code = fields.Char(
        string="Tabby Merchant Code",
        help="Merchant code of this branch in Tabby. Sent with every session created from this POS.")
    tabby_terminal_id = fields.Char(
        string="Tabby Terminal ID",
        help="Unique id of this till. Sent as the payment description.")
    tabby_latitude = fields.Float(string="Terminal Latitude", digits=(9, 6))
    tabby_longitude = fields.Float(string="Terminal Longitude", digits=(9, 6))
    tabby_timeout = fields.Integer(
        string="Tabby Wait (seconds)", default=300,
        help="How long the till waits for the customer before it cancels the Tabby session (180 to 900).")

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._tabby_clean(vals) for vals in vals_list])

    def write(self, vals):
        if any(f in vals for f in TABBY_FIELDS) and not self.env.su \
                and not self.env.user.has_group('point_of_sale.group_pos_manager'):
            raise AccessError(_("Only a Point of Sale manager can change the Tabby settings of a POS."))
        return super().write(self._tabby_clean(vals))

    @staticmethod
    def _tabby_clean(vals):
        for field in ('tabby_merchant_code', 'tabby_terminal_id'):
            if isinstance(vals.get(field), str):
                vals = {**vals, field: vals[field].strip() or False}
        return vals

    @api.constrains('tabby_latitude', 'tabby_longitude', 'tabby_timeout')
    def _check_tabby_terminal(self):
        for config in self:
            if not -90 <= config.tabby_latitude <= 90 or not -180 <= config.tabby_longitude <= 180:
                raise ValidationError(_("The Tabby terminal coordinates are out of range."))
            if not 180 <= (config.tabby_timeout or 300) <= 900:
                raise ValidationError(_("The Tabby wait must be between 180 and 900 seconds."))
