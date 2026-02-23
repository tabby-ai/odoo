import json
import re

from datetime import datetime

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Command
from odoo.http import request

from odoo.addons.payment.logging import get_payment_logger
from .. import const

from ..models.dd import DataDog
from ..models.api import TabbyAPI



_logger = get_payment_logger(__name__)


class PaymentProvider(models.Model):
    _inherit = 'payment.provider'


    code = fields.Selection(
        selection_add=[('tabby', "Tabby")], ondelete={'tabby': 'set default'})

    state = fields.Selection(
        selection_add=[('test', None)],
        ondelete={'test': 'set default'}
    )
    
    tabby_public_key = fields.Char(
        string="Tabby Public Key",
        help="Public key for Tabby API",
        groups="base.group_system"
    )
    
    tabby_secret_key = fields.Char(
        string="Tabby Secret Key",
        help="Secret key for Tabby API",
        groups="base.group_system"
    )

    def get_tabby_promo_config(self):
        """ Get Tabby promo widget configuration. """
        self.ensure_one()
        if self.code != 'tabby':
            return {}
        lang = self.env.lang[:2]
        result = {}
        try:
            result = {
                'selector': '#tabbyPromo',
                'merchantCode': self.get_merchant_code_from_currency(),
                'publicKey': self.tabby_public_key,
                'shouldInheritBg': True,
                'lang': lang or 'en',
                'email': self.env.user.partner_id.email or '',
                'phone': self.env.user.partner_id.phone or '',
                'source': 'product',
                'sourcePlugin': 'odoo',
            }
        except ValidationError:
            pass

        return result

    def get_tabby_card_config(self, order):
        """ Get Tabby card widget configuration. """
        self.ensure_one()
        if self.code != 'tabby':
            return {}
        if not order:
            return {}
        return {
            'selector': '#installmentsCard',
            'merchantCode': 'AE',
            'publicKey': self.tabby_public_key,
            'currency': order.currency_id.name,
            'price': str(order.amount_total),
            'lang': self.env.lang or 'en',
            'shouldInheritBg': True,
        }   

    def _get_supported_currencies(self):
        """Override to limit the dropdown to a specific list of currencies."""
        # Call super to get the standard list if needed, or define your own
        supported_codes = list(const.COUNTRY_MAP.values())
        return self.env['res.currency'].search([
            ('name', 'in', supported_codes),
            ('active', '=', True)
        ])

    def _get_default_payment_method_codes(self):
        """ Override of `payment` to return the default payment method codes. """
        self.ensure_one()
        if self.code != 'tabby':
            return super()._get_default_payment_method_codes()
        return list(const.PAYMENT_METHODS.values())
        
    def _get_payment_method_codes(self):
        """ Override of `payment` to return supported payment method codes. """
        if self.code == 'tabby':
            return list(const.PAYMENT_METHODS.values())
        return super()._get_payment_method_codes()

    def _compute_feature_support_fields(self):
        """ Override of `payment` to enable additional features. """
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == 'tabby').update({
            'support_manual_capture': 'partial',
            'support_refund': 'partial',
        })

    def get_merchant_code_from_currency(self, currency=None):
        """ Get Tabby merchant code based on currency. """
        if (not currency):
            currency = request.website.currency_id.name
        country_code = next((k for k, v in const.COUNTRY_MAP.items() if v == currency), None)
        if not country_code:
            raise ValidationError(
                _("Currency %s is not supported by Tabby.") % currency
            )
        return country_code

    def _get_merchant_urls(self):
        """ Prepare merchant URLs for Tabby API. """
        base_url = self.env['ir.config_parameter'].sudo().get_param('web.base.url')
        return {
            'success': f"{base_url}/payment/tabby/success",
            'cancel': f"{base_url}/payment/tabby/cancel",
            'failure': f"{base_url}/payment/tabby/failure",
        }

    def get_plugin_version(self):
        """ Get the current version of the Tabby plugin. """
        module = self.env['ir.module.module'].sudo().search(
            [('name', '=', 'payment_tabby')], limit=1)
        return module.installed_version or '1.0'
    
    def write(self, vals):
        res = super(PaymentProvider, self).write(vals)
        
        for provider in self:
            if provider.code == 'tabby':
                if 'tabby_public_key' in vals or 'tabby_secret_key' in vals or 'state' in vals:
                    if self.state in ['enabled', 'test']:
                        provider._register_webhooks()
                    else:
                        self._unregister_webhooks()
                    DataDog.ddlog(self.env, 'info', f'Tabby configuration updated for {provider.name}')
        
        return res
    
    def _register_webhooks(self):
        url = f"{self.env['ir.config_parameter'].sudo().get_param('web.base.url')}/payment/tabby/webhook"
        enabled = self.available_currency_ids.mapped('name')
        mcodes = [k for k, v in const.COUNTRY_MAP.items() if v in enabled]

        _logger.info('Registering webhooks for Tabby provider: %s, mcodes: %s with URL: %s', self.name, mcodes, url)
        api = TabbyAPI(provider=self)
        api.register_webhooks(webhook_url=url, mcodes=mcodes)
        self.env['bus.bus']._sendone(self.env.user.partner_id, 'simple_notification', {
            'type': 'info',
            'title': 'Tabby Webhooks Updated',
            'message': 'Webhooks have been successfully registered/updated with Tabby.',
            'sticky': False,
        })

    def _unregister_webhooks(self):
        url = f"{self.env['ir.config_parameter'].sudo().get_param('web.base.url')}/payment/tabby/webhook"
        mcodes = [k for k, v in const.COUNTRY_MAP.items()]

        _logger.info('Unregistering webhooks for Tabby provider: %s, mcodes: %s with URL: %s', self.name, mcodes, url)
        api = TabbyAPI(provider=self)
        api.unregister_webhooks(webhook_url=url, mcodes=mcodes)

        self.env['bus.bus']._sendone(self.env.user.partner_id, 'simple_notification', {
            'type': 'info',
            'title': 'Tabby Webhooks Unregistered',
            'message': 'Webhooks have been successfully unregistered with Tabby.',
            'sticky': False,
        })

    @api.constrains('tabby_public_key', 'tabby_secret_key', 'state')
    def _check_keys_on_save(self):

        if self.state == 'disabled':
            return

        if re.match(r'^pk_(test_)?[\da-f]{8}\-[\da-f]{4}\-[\da-f]{4}\-[\da-f]{4}\-[\da-f]{12}$', self.tabby_public_key) is None:
            raise ValidationError("Invalid Tabby Public Key format. Must be: pk_[test_]xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")

        if re.match(r'^sk_(test_)?[\da-f]{8}\-[\da-f]{4}\-[\da-f]{4}\-[\da-f]{4}\-[\da-f]{12}$', self.tabby_secret_key) is None:
            raise ValidationError("Invalid Tabby Secret Key format. Must be: sk_[test_]xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
