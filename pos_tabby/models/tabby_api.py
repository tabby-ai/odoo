import json
import logging
from urllib.parse import urlparse

import requests

from . import const

_logger = logging.getLogger(__name__)


class TabbyError(Exception):
    """Raised when Tabby answers with an error or cannot be reached."""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


class TabbyPosAPI:
    """Minimal client for the Tabby POS flow.

    Only the secret key is used. Every call goes to the regional API host of
    the currency (api.tabby.ai for AED, api.tabby.sa for SAR). Request and
    response bodies are never logged: they carry shopper data.
    """

    def __init__(self, secret_key, currency):
        if not secret_key:
            raise TabbyError("Tabby secret key is not configured.")
        if currency not in const.CURRENCY_REGION:
            raise TabbyError("Currency %s is not supported by Tabby POS." % currency)
        self.secret_key = secret_key
        self.host = const.CURRENCY_REGION[currency][1]

    # -- HTTP --------------------------------------------------------------

    def _request(self, method, path, payload=None, auth=True, raw=False):
        url = path if path.startswith("https://") else "https://%s%s" % (self.host, path)
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer %s" % self.secret_key
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = requests.request(
                method, url, headers=headers,
                data=json.dumps(payload) if payload is not None else None,
                timeout=const.REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as exc:
            _logger.warning("Tabby POS %s %s failed: %s", method, urlparse(url).path, exc.__class__.__name__)
            raise TabbyError("Tabby is unreachable. Please use another payment method.") from exc

        _logger.info("Tabby POS %s %s -> %s", method, urlparse(url).path, resp.status_code)
        if raw:
            if resp.status_code != 200:
                raise TabbyError("Tabby returned HTTP %s." % resp.status_code, status=resp.status_code)
            return resp
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code >= 400:
            message = body.get("error") or body.get("message") or "Tabby returned HTTP %s." % resp.status_code
            raise TabbyError(message, status=resp.status_code, payload=body)
        return body

    # -- Endpoints ---------------------------------------------------------

    def create_session(self, payload):
        return self._request("POST", "/api/v2/checkout", payload)

    def get_payment(self, payment_id):
        return self._request("GET", "/api/v2/payments/%s" % payment_id)

    def cancel_session(self, session_id):
        return self._request("POST", "/api/v2/checkout/%s/cancel" % session_id, {})

    def capture(self, payment_id, payload):
        return self._request("POST", "/api/v2/payments/%s/captures" % payment_id, payload)

    def refund(self, payment_id, payload):
        return self._request("POST", "/api/v2/payments/%s/refunds" % payment_id, payload)

    def fetch_qr_png(self, qr_url):
        """Download the QR image Tabby generated for the session (no auth)."""
        parsed = urlparse(qr_url or "")
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (host.endswith(".tabby.ai") or host.endswith(".tabby.sa")):
            raise TabbyError("Unexpected QR code URL.")
        return self._request("GET", qr_url, auth=False, raw=True).content
