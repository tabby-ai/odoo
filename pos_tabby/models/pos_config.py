from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


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

    @api.constrains('tabby_latitude', 'tabby_longitude', 'tabby_timeout')
    def _check_tabby_terminal(self):
        for config in self:
            if not -90 <= config.tabby_latitude <= 90 or not -180 <= config.tabby_longitude <= 180:
                raise ValidationError(_("The Tabby terminal coordinates are out of range."))
            if not 180 <= (config.tabby_timeout or 300) <= 900:
                raise ValidationError(_("The Tabby wait must be between 180 and 900 seconds."))
