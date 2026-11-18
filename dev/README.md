# Local Odoo for pos_tabby

```bash
docker compose -f dev/docker-compose.yml up -d
# first run: create a database with Point of Sale and pos_tabby
docker compose -f dev/docker-compose.yml exec odoo odoo -d tabby_pos --db_host=db --db_user=odoo --db_password=odoo -i point_of_sale,pos_tabby --stop-after-init --http-port=8070
# after code changes: upgrade the module
docker compose -f dev/docker-compose.yml exec odoo odoo -d tabby_pos --db_host=db --db_user=odoo --db_password=odoo -u pos_tabby --stop-after-init --http-port=8070
```

Open http://localhost:8069/web/login?db=tabby_pos (admin / admin), then the POS at
http://localhost:8069/pos/ui?config_id=1.

Setup for a test payment:
- Company currency AED (UAE) or SAR (KSA).
- Point of Sale > Configuration > Payment Methods: a method with "Integrate with" = Tabby and a `sk_test_` key.
- Point of Sale > Configuration > Settings > Tabby: merchant code, terminal id, coordinates (required), wait time.
- Test buyer: otp.success@tabby.ai (reject: otp.rejected@tabby.ai), phone 500000001, OTP 8888.

## Tests

```bash
docker compose -f dev/docker-compose.yml exec odoo odoo -d tabby_pos --db_host=db --db_user=odoo --db_password=odoo -u pos_tabby --test-enable --test-tags /pos_tabby --stop-after-init --http-port=8070
```

The tests mock `requests`; nothing reaches Tabby. Module behaviour is described in `pos_tabby/README.md`.

Local databases used in testing: `tabby_pos` (UAE, AED, merchant code AE) and `tabby_pos_sa`
(KSA, SAR, merchant code SA, created with `--without-demo=all`). The same test key bundle works
for both; SAR sessions go to api.tabby.sa and checkout.tabby.sa.
