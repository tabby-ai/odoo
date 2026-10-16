import requests
import json
import threading
import logging
import re
from odoo import release
from odoo.http import request

_logger = logging.getLogger(__name__)

class DataDog:

    @staticmethod
    def _send_request(payload):
        url = "https://logs.browser-intake-datadoghq.eu/api/v2/logs"
        headers = {
            "Content-Type": "application/json",
            "DD-API-KEY": "pubc0ef0c7691ebf9eb4c1d08d6c711cfc5",
        }
        try:
            response = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
        except Exception as e:
            #_logger.warning("DataDog Background Log Failed: %s", e)
            pass

    @classmethod
    def ddlog(cls, env, status, message, exception=None, data=None):

        hostname = cls.get_hostname(env)
        module_version = cls.get_module_version(env)

        log_entry = {
            "status": status,
            "message": message,
            "service": "odoo",
            "sversion": release.version,
            "hostname": hostname,
            "ddsource": "python",
            "ddtags": f"env:prod,version:{module_version}",
        }

        if exception:
            log_entry["error.kind"] = getattr(exception, 'code', type(exception).__name__)
            log_entry["error.message"] = str(exception)

        if data:
            log_entry["data"] = cls.mask_data(data)

        thread = threading.Thread(target=cls._send_request, args=(log_entry,), daemon=True)
        thread.start()

    @staticmethod
    def get_hostname(env):
        if request and hasattr(request, 'httprequest'):
            return request.httprequest.host

        return env['website'].get_current_website().domain or 'localhost'

    @staticmethod
    def get_module_version(env):
        module = env['ir.module.module'].sudo().search([('name', '=', 'payment_tabby')], limit=1)
        return module.installed_version or 'unknown'

    @classmethod
    def mask_data(cls, data, parent=''):
        # Check if the data is a dictionary or list (PHP's is_array handles both)
        if isinstance(data, dict):
            return {
                key: cls.mask_data(value, key if isinstance(key, str) else parent)
                if isinstance(value, (dict, list))
                else cls.mask_scalar(key, value, parent)
                for key, value in data.items()
            }
        elif isinstance(data, list):
            return [
                cls.mask_data(item, parent)
                if isinstance(item, (dict, list))
                else cls.mask_scalar('', item, parent)
                for item in data
            ]

        return cls.mask_scalar('', data, parent)

    @classmethod
    def mask_scalar(cls, key, value, parent=''):
        # Equivalent to is_null($value) || $value === ''
        if value is None or value == '':
            return value

        k = str(key).lower()

        if k == 'email':
            return cls.mask_email(value)
        if k in ('phone', 'telephone'):
            return '***' + str(value)[-3:]
        if k in ('dob', 'address', 'address_1', 'address_2', 'first_name', 'last_name'):
            return '***'
        if k == 'name' and str(parent).lower() == 'buyer':
            return '***'
        if isinstance(value, str):
            return cls.mask_string(value)

        return value

    @classmethod
    def mask_email(cls, value):
        if not isinstance(value, str) or '@' not in value:
            return '***'
        # Keep the domain - equivalent to strrpos + substr
        pos = value.rfind('@')
        return '***' + value[pos:]

    @classmethod
    def mask_string(cls, value):
        if not isinstance(value, str):
            return value
        # 1. Secret keys (Authorization headers, request bodies)
        value = re.sub(r'sk_[0-9a-zA-Z_.\-]+([0-9a-zA-Z]{4})', r'sk_...\1', value)
        # 2. PII fields inside JSON-encoded bodies (keeping regex capture groups intact)
        value = re.sub(r'("email"\s*:\s*")((?:\\.|[^"\\])*)(@(?:\\.|[^"\\])*")', r'\1***\3', value)
        value = re.sub(r'("(?:phone|telephone)"\s*:\s*")((?:\\.|[^"\\])*?)([0-9]{0,3}")', r'\1***\3', value)
        value = re.sub(r'("(?:name|first_name|last_name|dob|address|address_1|address_2)"\s*:\s*")((?:\\.|[^"\\])*)(")', r'\1***\3', value)

        return value
