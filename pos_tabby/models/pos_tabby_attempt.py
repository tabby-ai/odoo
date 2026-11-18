import re

from odoo import fields, models

UUID_PATTERN = re.compile(r'^[\da-fA-F]{8}-[\da-fA-F]{4}-[\da-fA-F]{4}-[\da-fA-F]{4}-[\da-fA-F]{12}$')

# States that hold no money and need nothing more from the till.
RELEASED_STATES = ('failed', 'cancelled')


def is_uuid(value):
    return isinstance(value, str) and bool(UUID_PATTERN.match(value))


class PosTabbyAttempt(models.Model):
    """One Tabby payment or refund started from a till.

    The till only ever sends its own order and payment line uuids; every
    Tabby id (session, payment, refund) is read from this record, so a till
    cannot act on a payment it did not create. The record also survives a
    reload of the till, so a pending payment can be resumed or cancelled.
    """
    _name = 'pos.tabby.attempt'
    _description = "Tabby POS attempt"
    _order = 'id desc'
    _rec_name = 'payment_id'

    kind = fields.Selection([('payment', "Payment"), ('refund', "Refund")], required=True, default='payment')
    state = fields.Selection([
        ('pending', "Pending"),  # session open, payment authorized but not captured, or refund not confirmed
        ('paid', "Paid"),  # captured in full
        ('partial', "Partially captured"),  # closed with only part of the amount captured
        ('failed', "Failed"),  # rejected, expired, voided, or a refund Tabby declined
        ('cancelled', "Cancelled"),
        ('refunded', "Refunded"),
    ], required=True, default='pending', index=True)
    config_id = fields.Many2one('pos.config', required=True, readonly=True, index=True)
    payment_method_id = fields.Many2one('pos.payment.method', required=True, readonly=True)
    api_method_id = fields.Many2one('pos.payment.method', required=True, readonly=True,
                                    help="Method whose key runs the Tabby calls: the original payment's method for a refund.")
    company_id = fields.Many2one(related='config_id.company_id', store=True)
    currency_id = fields.Many2one('res.currency', required=True, readonly=True)
    order_uuid = fields.Char(required=True, readonly=True, index=True)
    line_uuid = fields.Char(required=True, index=True)
    order_reference = fields.Char(readonly=True)
    session_id = fields.Char(readonly=True, index=True)
    payment_id = fields.Char(string="Tabby Payment ID", required=True, readonly=True, index=True,
                             help="The Tabby payment; for a refund, the original payment refunded.")
    reference = fields.Char(readonly=True, help="reference_id sent with the refund.")
    refund_id = fields.Char(readonly=True)
    refund_lines = fields.Text(readonly=True, help="Original order line id -> quantity returned (refunds).")
    amount = fields.Monetary(readonly=True)
    captured = fields.Monetary(readonly=True)
    refunded = fields.Monetary(readonly=True)
    tabby_status = fields.Char(readonly=True)
    web_url = fields.Char(readonly=True)
    qr_image = fields.Text(readonly=True)
    message = fields.Char(readonly=True)
    pos_payment_id = fields.Many2one('pos.payment', readonly=True, index=True, ondelete='set null',
                                     help="The POS payment line this attempt was recorded on.")

    def _view(self, **extra):
        self.ensure_one()
        return {
            'state': self.state,
            'session_id': self.session_id,
            'payment_id': self.payment_id,
            'amount': self.amount,
            'captured': self.captured,
            'status': self.tabby_status,
            'qr_image': self.qr_image if self.state == 'pending' else False,
            'web_url': self.web_url,
            'refund_id': self.refund_id,
            **extra,
        }
