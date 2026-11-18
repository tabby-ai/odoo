# Local Odoo 19 for pos_tabby

```bash
docker compose -f dev/docker-compose.yml up -d
# first run: create a database with Point of Sale and pos_tabby
docker compose -f dev/docker-compose.yml exec odoo odoo -d tabby_pos --db_host=db --db_user=odoo --db_password=odoo -i point_of_sale,pos_tabby --stop-after-init --http-port=8070
# after code changes: upgrade the module
docker compose -f dev/docker-compose.yml exec odoo odoo -d tabby_pos --db_host=db --db_user=odoo --db_password=odoo -u pos_tabby --stop-after-init --http-port=8070
```

Open http://localhost:8079/web/login?db=tabby_pos (admin / admin), then the POS at
http://localhost:8079/pos/ui?config_id=1.

Setup for a test payment:
- Company currency AED (UAE) or SAR (KSA).
- Point of Sale > Configuration > Payment Methods: a method with "Integrate with" = Tabby and a `sk_test_` key.
- Point of Sale > Configuration > Settings > Tabby: merchant code, terminal id, coordinates.
- Test buyer: otp.success@tabby.ai (reject: otp.rejected@tabby.ai), phone 500000001, OTP 8888.

## What the module does today (19.0)

- Payment: the till creates a session (`POST /api/v2/checkout`), shows the QR (cashier dialog and customer display), polls `GET /api/v2/payments/{id}` every 5 s for up to 300 s.
- AUTHORIZED payments are captured by the plugin (full amount, reference = payment id); CLOSED counts as paid.
- REJECTED or EXPIRED fails the line; the cashier can retry or use another method.
- Cancel (cashier button, removing the line, closing the payment screen, timeout) calls `POST /api/v2/checkout/{id}/cancel`; a finalized session is re-read so a paid purchase is not lost.
- Refund: a POS return paid back with Tabby calls `POST /api/v2/payments/{id}/refunds` against the original order's Tabby payment.

Local databases used in testing: `tabby_pos` (UAE, AED, merchant code AE) and `tabby_pos_sa`
(KSA, SAR, merchant code SA, created with `--without-demo=all`). The same test key bundle works
for both; SAR sessions go to api.tabby.sa and checkout.tabby.sa.
