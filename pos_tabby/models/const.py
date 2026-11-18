# Tabby live markets for POS: currency -> (country, API host)
CURRENCY_REGION = {
    'AED': ('AE', 'api.tabby.ai'),
    'SAR': ('SA', 'api.tabby.sa'),
}

# Tabby payment status -> outcome for the till
PAID_STATUSES = ('AUTHORIZED', 'CLOSED')
FAILED_STATUSES = ('REJECTED', 'EXPIRED')
PENDING_STATUSES = ('CREATED',)

ATTACHMENT_CONTENT_TYPE = 'application/vnd.tabby.v1+json'
REQUEST_TIMEOUT = 10  # seconds per HTTP call
