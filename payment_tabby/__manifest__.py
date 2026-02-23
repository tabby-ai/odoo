{
    'name': "Payment Provider: Tabby",
    'version': '19.0.1.0.1',
    'category': 'Accounting/Payment Providers',
    'sequence': 350,
    'summary': "Tabby payment provider",
    'description': """
Tabby, the shopping and payments app, is the Middle East's largest Buy Now Pay Later provider,
providing shopping and financial solutions for more than 11 million customers 
and more than 40,000 retailers in the region.
    """,
    'author': "Tabby",
    'website': "https://tabby.ai",
    'depends': ['website', 'sale', 'website_sale', 'payment'],
    'data': [
        'views/payment_provider_views.xml',
        'views/payment_tabby_templates.xml',
        'views/tabby_promo_templates.xml',
        'views/payment_redirect_form.xml',
        
        'data/ir_cron_data.xml',

        'data/payment_method_data.xml',
        'data/payment_provider_data.xml',        
    ],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'license': 'LGPL-3',
    'installable': True,
}

