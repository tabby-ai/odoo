COUNTRY_MAP = {
    'AE': 'AED',
    'SA': 'SAR',
    'KW': 'KWD',
}

CURRENCY_MAP = {v: k for k, v in COUNTRY_MAP.items()}

ORDER_STATE_MAP = {
    'done': 'complete',
    'sale': 'complete',
    'cancel': 'canceled',
}

API_BASE_URL = "https://api.tabby.ai/api/"

PAYMENT_METHODS = {
    'INSTALLMENTS': 'tabby_installments',
}

DEFAULT_COUNTRY = 'AE'
DEFAULT_LANGUAGE = 'en'
DEFAULT_TERMS_URL = "https://www.tabby.ai/terms-and-conditions"
DEFAULT_PRIVACY_URL = "https://www.tabby.ai/privacy-policy"
