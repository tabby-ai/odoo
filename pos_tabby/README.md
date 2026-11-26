# POS Tabby

Tabby Pay Later as a payment terminal in Odoo Point of Sale.

## Setup

1. Point of Sale > Configuration > Payment Methods: a method with "Integrate with" = Tabby and the
   secret key from the Tabby merchant dashboard (`sk_test_` for test payments). The company currency
   must be AED (UAE) or SAR (KSA).
2. Point of Sale > Configuration > Settings > Tabby, per till: merchant code, terminal id,
   terminal latitude and longitude (all required), and how long the till waits for the customer
   (180 to 900 s, default 300 s). Only a POS manager can change these.
3. Merchant codes: every till sends its own code, so a merchant with many branches can use one code
   per branch (MID), not only `AE`/`SA`. Each code must exist in Tabby under the same secret key
   before the till goes live; an unknown code is refused with a message to the cashier.

## How it works

- Payment: the till creates a session (`POST /api/v2/checkout`) and shows its QR to the cashier and
  on the customer display. It reads `GET /api/v2/payments/{id}` every 5 s. An AUTHORIZED payment is
  captured for the full amount (reference `<payment id>-pos`); the line is paid only when the full
  amount is captured.
- Cancel (button, removing the line, timeout) calls `POST /api/v2/checkout/{id}/cancel`. A session
  the customer already finalized is read again, so a paid purchase is never dropped.
- Refund: a POS return paid back with Tabby calls `POST /api/v2/payments/{id}/refunds` on the
  original order's Tabby payment, with a reference fixed per payment line, capped by the value of
  the returned items and by what Tabby has left to refund.
- Every payment and refund is recorded on the server (Point of Sale > Orders > Tabby Payments). The
  till only names its own order and payment line; the Tabby ids come from that record. After a
  reload of the till, the payment screen resumes the open payment of its order.
- When the order is synced, every Tabby line must match a Tabby payment captured in full (or a
  confirmed refund) of the same amount, used once. An open session of the order without a line is
  cancelled; a payment the customer made whose line was removed blocks the order until it is put
  back or refunded in the Tabby merchant dashboard.
- A payment that needs a decision in the dashboard (closed after a partial capture, authorized but
  not captured, or a cancel Tabby did not confirm) stays on its line: the QR closes, the till keeps
  checking every 30 s and releases or completes the line once Tabby shows the outcome.

- An order is set paid only when its Tabby lines are confirmed, whichever path sets it paid. A draft
  order that holds Tabby money cannot be deleted or cancelled, and a POS session cannot be closed
  while Tabby holds money that no order of the POS records (a deleted or unsynced order): refund it
  in the Tabby merchant dashboard and the POS releases it.

## Limits

- One open Tabby request per till at a time (Odoo allows one electronic payment in progress).
- An order paid with several Tabby payments is refunded from the Tabby merchant dashboard.
- Rate limit: a live key allows 100 requests per 10 s per IP and key. Each pending QR polls every
  5 s, so one Odoo server supports about 50 pending QRs at once per live key; a test key allows
  about 5.
