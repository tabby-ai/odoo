from odoo import fields, models


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
