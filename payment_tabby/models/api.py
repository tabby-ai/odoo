
import json
import logging
import pprint
from .. import const
from .dd import DataDog


from uuid import uuid4

import requests

from odoo.addons.payment import utils as payment_utils

_logger = logging.getLogger(__name__)

class TabbyAPI:
    BASE_URL = const.API_BASE_URL

    def __init__(self, provider):
        self.public_key = provider.tabby_public_key
        self.secret_key = provider.tabby_secret_key
        self.env = provider.env

    def _get_headers(self, mcode=None):
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.secret_key}",
        }
        if mcode:
            headers["X-Merchant-Code"] = mcode
        return headers

    def _request(self, method, endpoint, data=None, mcode=None):

        if not self.secret_key:
            return {'status':'error', 'message': f"No secret key configured"}

        url = f"{self.BASE_URL}{endpoint}"
        headers = self._get_headers(mcode)

        if (method not in ['POST', 'GET', 'PUT', 'DELETE']):
            raise ValueError("Unsupported HTTP method")
        
        try:
            response = requests.request(method, url, headers=headers, data=json.dumps(data) if data else None, timeout=10)
            response.raise_for_status()

            rjson = None
            try:
                rjson = response.json()
            except json.JSONDecodeError:
                pass
            log_data = {
                "request.url" : url,
                "request.body" : data,
                "request.method" : method,
                "response.body" : rjson or response.text,
                "response.status" : response.status_code,
                "response.error" : ''
            }
            DataDog.ddlog(self.env, 'info', 'api call', data=log_data);
        except requests.exceptions.RequestException as e:
            _logger.error('Tabby API Request Failed: %s', e)
            rjson = None
            try:
                rjson = response.json()
            except json.JSONDecodeError:
                pass
            log_data = {
                "request.url" : url,
                "request.body" : data,
                "request.method" : method,
                "response.body" : rjson or response.text,
                "response.status" : response.status_code,
                "response.error" : ''
            }
            DataDog.ddlog(self.env, 'info', 'api call', data=log_data);
            return {"status": "error", "message": str(e)}
        
        try:
            return response.json()
        except json.JSONDecodeError:
            _logger.warning("Tabby API response error: %s", response.text)
            return {"status": "error", "message": "Failed to decode JSON response"}

    def createSession(self, data):
        return self._request("POST", f'v2/checkout', data=data)

    def get_payment(self, payment_id):
        return self._request("GET", f'v2/payments/{payment_id}')

    def capture(self, payment_id, data):
        return self._request("POST", f'v2/payments/{payment_id}/captures', data=data);

    def refund(self, payment_id, data):
        return self._request("POST", f'v2/payments/{payment_id}/refunds', data=data);

    def close(self, payment_id):
        return self._request("POST", f'v2/payments/{payment_id}/close');

    def register_webhooks(self, webhook_url, mcodes):
        for mcode in mcodes:
            hooks = self.get_webhooks(mcode)
            if self.isNotAuthorized(hooks):
                _logger.error('Merchant code %s not found when registering webhook.', mcode)
                continue

            _logger.info("Webhook object: %s", hooks);
            hook = next((h for h in hooks if h['url'] == webhook_url), None)
            registered = False
            if hook:
                registered = True
                if self.getIsTest() != hook.get('is_test', False):
                    self.update_webhook(hook.get('id'), webhook_url, mcode)
                    _logger.info('Updated webhook for mcode %s to is_test=%s', mcode, self.getIsTest())

                _logger.info('Webhook already registered for mcode %s: %s', mcode, webhook_url)
                continue
            if not registered:
                response = self.register_webhook(webhook_url, mcode)
                _logger.info('Registered webhook for mcode %s: %s', mcode, response)

    def unregister_webhooks(self, webhook_url, mcodes):
        for mcode in mcodes:
            hooks = self.get_webhooks(mcode)
            if isinstance(hooks, dict) and hooks.get('status', '') == 'error':
                _logger.error('Merchant code %s not found when unregistering webhook.', mcode)
                continue;
            if self.isNotAuthorized(hooks):
                _logger.error('Merchant code %s not found when unregistering webhook.', mcode)
                continue

            hook = next((h for h in hooks if h['url'] == webhook_url), None)
            if hook:
                _logger.info('Unregistering webhook for mcode %s: %s, %s', mcode, webhook_url, hook)
                self.delete_webhook(hook.get('id'), mcode)

    def isNotAuthorized(self, response):
        return hasattr(response, 'errorType') and response.errorType in ['not_authorized', 'not_found']

    def getIsTest(self):
        return self.secret_key.startswith('sk_test_')

    def get_webhooks(self, mcode):
        webhooks = self._request("GET", f"v1/webhooks", mcode=mcode)
        if not isinstance(webhooks, list):
            webhooks = [webhooks]
        return webhooks

    def register_webhook(self, webhook_url, mcode):
        data = {
            "url": webhook_url,
            "is_test": self.secret_key.startswith('sk_test_'),
        }
        return self._request("POST", f"v1/webhooks", data=data, mcode=mcode)

    def update_webhook(self, hook_id, webhook_url, mcode):
        data = {
            "url": webhook_url,
        }
        return self._request("PUT", f"v1/webhooks/{hook_id}", data=data, mcode=mcode)

    def delete_webhook(self, hook_id, mcode):
        return self._request("DELETE", f"v1/webhooks/{hook_id}", mcode=mcode)
