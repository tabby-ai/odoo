{
    'name': "POS Tabby",
    'version': '18.0.0.1.0',
    'category': 'Sales/Point of Sale',
    'sequence': 6,
    'summary': "Accept Tabby Pay Later in Odoo Point of Sale",
    'description': """
Tabby payment terminal for Odoo Point of Sale.

The cashier selects Tabby on the payment screen, the customer scans a QR code
with their phone and completes the purchase on the Tabby checkout. The till
polls the payment status until it is paid, rejected or expired. Every Tabby
payment and refund is recorded server side (Point of Sale > Orders > Tabby
Payments), so a reload of the till resumes it and a paid order is accepted
only when Tabby confirms its Tabby lines.
    """,
    'author': "Tabby",
    'website': "https://tabby.ai",
    'depends': ['point_of_sale'],
    'data': [
        'security/ir.model.access.csv',
        'security/pos_tabby_security.xml',
        'views/pos_tabby_attempt_views.xml',
        'views/pos_payment_method_views.xml',
        'views/res_config_settings_views.xml',
    ],
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_tabby/static/src/**/*',
        ],
    },
    'installable': True,
    'license': 'Other OSI approved licence',
}
