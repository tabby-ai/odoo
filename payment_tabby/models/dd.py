import requests
import json
import threading
import logging
from odoo import release
from odoo.http import request

_logger = logging.getLogger(__name__)

class DataDog:

    @staticmethod
    def _send_request(payload):
        url = "https://logs.browser-intake-datadoghq.eu/api/v2/logs"
        headers = {
            "Content-Type": "application/json",
            "DD-API-KEY": "pub52c39090d2b6827fe4bad20d337da6ae",
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
            log_entry["data"] = data

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
