from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    pos_tabby_merchant_code = fields.Char(related='pos_config_id.tabby_merchant_code', readonly=False)
    pos_tabby_terminal_id = fields.Char(related='pos_config_id.tabby_terminal_id', readonly=False)
    pos_tabby_latitude = fields.Float(related='pos_config_id.tabby_latitude', readonly=False)
    pos_tabby_longitude = fields.Float(related='pos_config_id.tabby_longitude', readonly=False)
    pos_tabby_timeout = fields.Integer(related='pos_config_id.tabby_timeout', readonly=False)
