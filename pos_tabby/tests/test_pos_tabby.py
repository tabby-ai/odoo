import json
import re
import uuid
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, tagged

SESSION_ID = "11111111-1111-4111-8111-111111111111"
PAYMENT_ID = "22222222-2222-4222-8222-222222222222"
QR_PNG = b"\x89PNG fake"


def new_uuid():
    return str(uuid.uuid4())


class FakeResponse:
    def __init__(self, status, body=None, content=b""):
        self.status_code = status
        self._body = body
        self.content = content

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeTabby:
    """Stands in for requests.request: answers by method and path, records calls."""

    def __init__(self):
        self.calls = []
        self.payment = {'id': PAYMENT_ID, 'status': 'CREATED', 'amount': '100.00', 'currency': 'AED',
                        'captures': [], 'refunds': []}
        self.session_status = 'created'
        self.rejection_reason = None
        self.qr_status = [200]  # one status per QR fetch, the last repeats
        self.qr_url = 'https://api.tabby.ai/api/v2/checkout/%s/hpp_link_qr' % SESSION_ID
        self.web_url = 'https://checkout.tabby.ai/?sessionId=%s' % SESSION_ID
        self.cancel_status = 200
        self.capture_status = 200
        self.refund_status = 200
        self.get_status = 200
        self.create_status = 200
        self.link_status = 200
        self.refund_listed = True  # False: the refund POST answers without listing the refund

    def __call__(self, method, url, headers=None, data=None, timeout=None, allow_redirects=True):
        path = re.sub(r'^https://[^/]+', '', url)
        body = json.loads(data) if data else None
        self.calls.append((method, path, body, headers))
        if method == 'POST' and path == '/api/v2/checkout' and self.create_status != 200:
            return FakeResponse(self.create_status, {'error': 'merchant_code not found for this api key'})
        if method == 'POST' and path == '/api/v2/checkout':
            installments = {'web_url': self.web_url}
            if self.qr_url:
                installments['qr_code'] = self.qr_url
            return FakeResponse(200, {
                'id': SESSION_ID, 'status': self.session_status,
                'payment': {'id': PAYMENT_ID, 'amount': body['payment']['amount'], 'status': 'CREATED'},
                'configuration': {
                    'available_products': {'installments': [installments]} if self.session_status == 'created' else {},
                    'products': {'installments': {'rejection_reason': self.rejection_reason}},
                },
            })
        if path.endswith('/hpp_link_qr'):
            status = self.qr_status.pop(0) if len(self.qr_status) > 1 else self.qr_status[0]
            return FakeResponse(status, content=QR_PNG if status == 200 else b"")
        if method == 'POST' and path == '/api/v2/checkout/%s/send_hpp_link' % SESSION_ID:
            if self.link_status != 200:
                return FakeResponse(self.link_status, {'error': 'bad_data', 'message': 'product is not chosen'})
            return FakeResponse(200, {'status': 'ok'})
        if method == 'POST' and path == '/api/v2/checkout/%s/cancel' % SESSION_ID:
            if self.cancel_status != 200:
                return FakeResponse(self.cancel_status, {'error': 'session is finalized'})
            self.payment['status'] = 'EXPIRED'
            return FakeResponse(200, {})
        if method == 'GET' and path == '/api/v2/payments/%s' % PAYMENT_ID:
            if self.get_status != 200:
                return FakeResponse(self.get_status, {'error': 'nope'})
            return FakeResponse(200, json.loads(json.dumps(self.payment)))
        if method == 'POST' and path == '/api/v2/payments/%s/captures' % PAYMENT_ID:
            if self.capture_status != 200:
                return FakeResponse(self.capture_status, {'error': 'capture failed'})
            self.payment['captures'].append({'id': new_uuid(), 'amount': body['amount'], 'reference_id': body['reference_id']})
            self.payment['status'] = 'CLOSED'
            return FakeResponse(200, self.payment)
        if method == 'POST' and path == '/api/v2/payments/%s/refunds' % PAYMENT_ID:
            if self.refund_status != 200:
                return FakeResponse(self.refund_status, {'error': 'refund failed'})
            if not self.refund_listed:
                return FakeResponse(200, {'id': PAYMENT_ID, 'status': 'CLOSED'})
            self.payment['refunds'].append({'id': new_uuid(), 'amount': body['amount'], 'reference_id': body['reference_id']})
            return FakeResponse(200, self.payment)
        return FakeResponse(404, {'error': 'not found'})

    def count(self, method, path_end):
        return sum(1 for m, p, *_ in self.calls if m == method and p.endswith(path_end))


@tagged('post_install', '-at_install', 'pos_tabby')
class TestPosTabby(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.currency_id = cls.env.ref('base.AED')
        cls.currency = cls.company.currency_id
        journal = cls.env['account.journal'].search(
            [('type', '=', 'bank'), ('company_id', '=', cls.company.id)], limit=1)
        cls.method = cls.env['pos.payment.method'].create({
            'name': 'Tabby test',
            'journal_id': journal.id,
            'use_payment_terminal': 'tabby',
            'tabby_secret_key': 'sk_' + 'test_00000000-0000-4000-8000-000000000000',
        })
        cls.cash = cls.env['pos.payment.method'].create({
            'name': 'Cash test',
            'journal_id': cls.env['account.journal'].create(
                {'name': 'Tabby test cash', 'code': 'TBCS', 'type': 'cash', 'company_id': cls.company.id}).id,
        })
        cls.config = cls.env['pos.config'].create({
            'name': 'Tabby till',
            'payment_method_ids': [(6, 0, (cls.method | cls.cash).ids)],
            'tabby_merchant_code': 'ae',
            'tabby_terminal_id': 'till-1',
            'tabby_latitude': 25.2,
            'tabby_longitude': 55.27,
        })
        cls.user = cls.env.ref('base.user_admin')
        cls.product = cls.env['product.product'].create({'name': 'Sunglasses', 'available_in_pos': True,
                                                         'list_price': 50, 'taxes_id': [(5, 0, 0)]})

    def setUp(self):
        super().setUp()
        self.fake = FakeTabby()
        patcher = patch('odoo.addons.pos_tabby.models.tabby_api.requests.request', side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pm = self.method.with_user(self.user)
        self.order_uuid = new_uuid()
        self.line_uuid = new_uuid()

    def data(self, **extra):
        return {'config_id': self.config.id, 'order_uuid': self.order_uuid, 'line_uuid': self.line_uuid,
                'line_uuids': [self.line_uuid], **extra}

    def create(self, amount=100.0, phone=None):
        return self.pm.tabby_create_session(self.data(amount=amount, order_reference='Order 1', items=[], buyer={},
                                                      **({'phone': phone} if phone is not None else {})))

    def attempt(self):
        return self.env['pos.tabby.attempt'].search([('line_uuid', '=', self.line_uuid)])

    # -- Access ------------------------------------------------------------

    def test_access_checks(self):
        outsider = self.env['res.users'].create({'name': 'Outsider', 'login': 'outsider_tabby',
                                                 'groups_id': [(6, 0, [self.env.ref('base.group_user').id])]}) \
            if 'groups_id' in self.env['res.users']._fields else self.env['res.users'].create(
                {'name': 'Outsider', 'login': 'outsider_tabby', 'group_ids': [(6, 0, [self.env.ref('base.group_user').id])]})
        with self.assertRaises(AccessError):
            self.method.with_user(outsider).tabby_create_session(self.data(amount=10))
        other = self.env['pos.config'].create({'name': 'Other till', 'payment_method_ids': [(5, 0, 0)]})
        with self.assertRaises(AccessError):
            self.pm.tabby_create_session({**self.data(amount=10), 'config_id': other.id})
        with self.assertRaises(ValidationError):
            self.pm.tabby_payment_status({**self.data(), 'line_uuid': '../payments/x'})
        self.assertFalse(self.fake.calls)

    def test_config_required(self):
        self.config.tabby_latitude = self.config.tabby_longitude = 0
        self.assertIn('coordinates', self.create()['error'])
        self.assertFalse(self.fake.calls)

    # -- Create, resume, status, cancel ------------------------------------

    def test_create_records_attempt_and_status_uses_it(self):
        res = self.create()
        self.assertEqual(res['payment_id'], PAYMENT_ID)
        self.assertTrue(res['qr_image'].startswith('data:image/png;base64,'))
        attempt = self.attempt()
        self.assertEqual((attempt.state, attempt.session_id, attempt.amount), ('pending', SESSION_ID, 100.0))
        sent = next(c for c in self.fake.calls if c[1] == '/api/v2/checkout')
        self.assertEqual(sent[2]['payment']['description'], 'till-1')
        self.assertEqual(json.loads(sent[2]['payment']['attachment']['body'])['latitude'], 25.2)
        self.assertTrue(sent[3]['User-Agent'].startswith('TabbyOdooPOS/'))
        # A client-supplied payment_id is ignored: the line names the attempt.
        res = self.pm.tabby_payment_status(self.data(payment_id='33333333-3333-4333-8333-333333333333'))
        self.assertEqual(res['outcome'], 'pending')
        self.assertEqual(self.fake.count('GET', PAYMENT_ID), 1)
        self.assertEqual(self.pm.tabby_payment_status({**self.data(), 'line_uuid': new_uuid()})['outcome'], 'failed')

    def test_create_resumes_open_attempt(self):
        self.create()
        res = self.create()
        self.assertTrue(res['resumed'])
        self.assertEqual(self.fake.count('POST', '/api/v2/checkout'), 1)
        # After a reload the line can be new: an orphan of the same amount moves to it.
        old_line, self.line_uuid = self.line_uuid, new_uuid()
        res = self.create()
        self.assertTrue(res['resumed'])
        self.assertEqual(self.attempt().payment_id, PAYMENT_ID)
        self.assertEqual(self.fake.count('POST', '/api/v2/checkout'), 1)
        # A different amount does not take the open payment over.
        self.line_uuid = new_uuid()
        self.assertIn('not resolved', self.create(amount=40)['error'])
        self.assertEqual(self.pm.tabby_resume({**self.data(), 'line_uuid': old_line}), {'found': False})

    def test_qr_retry_and_fallback(self):
        self.fake.qr_status = [500, 200]
        self.assertTrue(self.create()['qr_image'])
        self.assertEqual(self.fake.count('GET', 'hpp_link_qr'), 2)
        self.line_uuid, self.order_uuid = new_uuid(), new_uuid()
        self.fake.qr_status = [500]
        res = self.create()
        self.assertTrue(res['qr_image'].startswith('data:image/png;base64,'))  # built from web_url

    def test_no_qr_cancels_session(self):
        self.fake.qr_url = None
        self.fake.web_url = None
        res = self.create()
        self.assertIn('QR code', res['error'])
        self.assertEqual(self.attempt().state, 'cancelled')

    def test_rejection_reason(self):
        self.fake.session_status = 'rejected'
        self.fake.rejection_reason = 'order_amount_too_low'
        self.assertIn('below the Tabby minimum', self.create()['error'])
        self.assertFalse(self.attempt())

    def test_cancel_then_paid(self):
        self.create()
        self.fake.cancel_status = 400
        self.fake.payment.update(status='AUTHORIZED')
        res = self.pm.tabby_cancel_session(self.data())
        self.assertEqual(res['outcome'], 'paid')
        self.assertEqual(self.fake.payment['captures'][0]['reference_id'], '%s-pos' % PAYMENT_ID)
        self.assertEqual(self.attempt().state, 'paid')

    def test_cancel_confirmed(self):
        self.create()
        self.assertEqual(self.pm.tabby_cancel_session(self.data()), {'cancelled': True})
        self.assertEqual(self.attempt().state, 'cancelled')
        self.assertEqual(self.pm.tabby_payment_status(self.data())['outcome'], 'failed')

    # -- Settle --------------------------------------------------------------

    def settle(self, **payment):
        self.create()
        self.fake.payment.update(payment)
        return self.pm.tabby_payment_status(self.data())

    def test_settle_created_is_pending(self):
        self.assertEqual(self.settle(status='CREATED')['outcome'], 'pending')

    def test_settle_authorized_is_captured(self):
        res = self.settle(status='AUTHORIZED')
        self.assertEqual(res['outcome'], 'paid')
        self.assertEqual(self.fake.payment['captures'][0]['amount'], '100.00')

    def test_settle_capture_error_stays_pending(self):
        self.fake.capture_status = 500
        res = self.settle(status='AUTHORIZED')
        self.assertEqual(res['outcome'], 'pending')
        self.assertIn('capture_error', res)

    def test_settle_closed_paths(self):
        self.assertEqual(self.settle(status='CLOSED', captures=[{'amount': '100.00'}])['outcome'], 'paid')
        self.order_uuid, self.line_uuid = new_uuid(), new_uuid()
        self.assertEqual(self.settle(status='CLOSED', captures=[])['outcome'], 'failed')  # voided
        self.order_uuid, self.line_uuid = new_uuid(), new_uuid()
        res = self.settle(status='CLOSED', captures=[{'amount': '40.00'}], refunds=[])
        self.assertEqual(res['outcome'], 'partial')
        self.fake.payment['refunds'] = [{'amount': '40.00'}]
        self.assertEqual(self.pm.tabby_payment_status(self.data())['outcome'], 'failed')  # refunded in the dashboard

    def test_settle_rejected_expired(self):
        self.assertEqual(self.settle(status='REJECTED')['outcome'], 'failed')
        self.order_uuid, self.line_uuid = new_uuid(), new_uuid()
        self.assertEqual(self.settle(status='EXPIRED')['outcome'], 'failed')

    # -- Orders and refunds ----------------------------------------------------

    def make_order(self, payments, order_uuid=None):
        session = self.env['pos.session'].search([('config_id', '=', self.config.id), ('state', '!=', 'closed')], limit=1) \
            or self.env['pos.session'].create({'config_id': self.config.id, 'user_id': self.env.uid})
        return self.env['pos.order'].create({
            'session_id': session.id,
            'uuid': order_uuid or new_uuid(),
            'lines': [(0, 0, {'product_id': self.product.id, 'qty': 2, 'price_unit': 50,
                              'price_subtotal': 100, 'price_subtotal_incl': 100})],
            'payment_ids': [(0, 0, p) for p in payments],
            'amount_tax': 0, 'amount_total': 100, 'amount_paid': 100, 'amount_return': 0,
        })

    def test_order_check_requires_captured_attempt(self):
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': self.line_uuid}],
                                self.order_uuid)
        with self.assertRaises(UserError):
            order._tabby_check_payments()  # Tabby line without any Tabby payment
        self.create()
        with self.assertRaises(UserError):
            order._tabby_check_payments()  # not paid yet
        self.fake.payment.update(status='AUTHORIZED')
        order._tabby_check_payments()  # captured on the way
        self.assertEqual(self.attempt().pos_payment_id, order.payment_ids)
        # The same Tabby payment cannot pay a second line.
        order2 = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': new_uuid()}])
        with self.assertRaises(UserError):
            order2._tabby_check_payments()

    def test_order_check_open_attempt_without_line(self):
        self.create()
        order = self.make_order([{'payment_method_id': self.cash.id, 'amount': 100}], self.order_uuid)
        order._tabby_check_payments()  # the open session is cancelled
        self.assertEqual(self.attempt().state, 'cancelled')

    def test_order_check_paid_attempt_without_line(self):
        self.create()
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '100.00'}])
        self.pm.tabby_payment_status(self.data())
        order = self.make_order([{'payment_method_id': self.cash.id, 'amount': 100}], self.order_uuid)
        with self.assertRaises(UserError):
            order._tabby_check_payments()  # the customer paid with Tabby and would pay cash too
        self.fake.payment['refunds'] = [{'amount': '100.00'}]
        order._tabby_check_payments()  # refunded in the dashboard: no money held

    def paid_order(self):
        """An order paid through the till and checked at sync; the return gets new uuids."""
        self.create()
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '100.00'}])
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': self.line_uuid,
                                  'transaction_id': PAYMENT_ID}], self.order_uuid)
        order._tabby_check_payments()
        self.order_uuid, self.line_uuid = new_uuid(), new_uuid()
        return order

    def make_return(self, original, qty, payments):
        return self.env['pos.order'].create({
            'session_id': original.session_id.id, 'uuid': self.order_uuid,
            'lines': [(0, 0, {'product_id': self.product.id, 'qty': -qty, 'price_unit': 50, 'price_subtotal': -50 * qty,
                              'price_subtotal_incl': -50 * qty, 'refunded_orderline_id': original.lines.id})],
            'payment_ids': [(0, 0, p) for p in payments],
            'amount_tax': 0, 'amount_total': -50 * qty, 'amount_paid': sum(p['amount'] for p in payments), 'amount_return': 0,
        })

    def test_sync_overwrites_transaction_id(self):
        self.create()
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '100.00'}])
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': self.line_uuid,
                                  'transaction_id': '33333333-3333-4333-8333-333333333333'}], self.order_uuid)
        order._tabby_check_payments()
        self.assertEqual(order.payment_ids.transaction_id, PAYMENT_ID)

    def test_refund_needs_checked_payment(self):
        # A Tabby line no attempt confirmed (a draft, or a transaction id the till wrote) is no refund target.
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'transaction_id': PAYMENT_ID}])
        self.assertIn('No Tabby payment found', self.refund(order.lines)['error'])
        self.assertFalse(self.fake.calls)

    def test_refunded_payment_does_not_pay(self):
        self.create()
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '100.00'}], refunds=[{'amount': '100.00'}])
        self.assertEqual(self.pm.tabby_payment_status(self.data())['outcome'], 'failed')
        # Seen paid by the till, refunded in the dashboard before the sale synced.
        self.order_uuid, self.line_uuid = new_uuid(), new_uuid()
        self.create()
        self.fake.payment.update(refunds=[])
        self.assertEqual(self.pm.tabby_payment_status(self.data())['outcome'], 'paid')
        self.fake.payment['refunds'] = [{'amount': '100.00'}]
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': self.line_uuid}],
                                self.order_uuid)
        with self.assertRaises(UserError):
            order._tabby_check_payments()

    def test_edited_amount_cancels_old_session(self):
        self.create(amount=100)
        res = self.create(amount=50)
        self.assertFalse(res.get('resumed'))
        self.assertEqual(self.fake.count('POST', '/api/v2/checkout'), 2)
        self.assertEqual(self.fake.count('POST', '/cancel'), 1)
        states = self.env['pos.tabby.attempt'].search([('line_uuid', '=', self.line_uuid)]).mapped('state')
        self.assertEqual(sorted(states), ['cancelled', 'pending'])
        # A paid session is never dropped for a new amount.
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '50.00'}], amount='50.00')
        self.pm.tabby_payment_status(self.data())
        self.assertIn('Set the line back', self.create(amount=40)['error'])

    def refund(self, line, qty=1, amount=-50.0, line_uuid=None, order_uuid=None):
        self.line_uuid = line_uuid or self.line_uuid
        self.order_uuid = order_uuid or self.order_uuid
        return self.pm.tabby_refund(self.data(amount=amount, refunded_lines=[{'id': line.id, 'qty': qty}]))

    def test_refund_and_replay(self):
        original = self.paid_order()
        res = self.refund(original.lines, qty=1)
        self.assertTrue(res['refund_id'])
        self.assertEqual(self.fake.count('POST', '/refunds'), 1)
        self.assertEqual(self.fake.payment['refunds'][0]['reference_id'], '%s-%s' % (self.order_uuid, self.line_uuid))
        # Replay of the same line: the same refund, nothing new sent.
        self.assertEqual(self.refund(original.lines, qty=1)['refund_id'], res['refund_id'])
        self.assertEqual(self.fake.count('POST', '/refunds'), 1)
        # A different amount on the same line is refused.
        self.assertIn('do not refund this line again', self.refund(original.lines, qty=1, amount=-40)['error'])

    def test_refund_capped_by_returned_items(self):
        original = self.paid_order()
        self.assertIn('more than the value', self.refund(original.lines, qty=1, amount=-60)['error'])
        first = self.line_uuid
        self.refund(original.lines, qty=1, amount=-50)
        # A second Tabby line on the same return cannot take more than the items left.
        self.line_uuid = new_uuid()
        res = self.pm.tabby_refund({**self.data(amount=-60, refunded_lines=[{'id': original.lines.id, 'qty': 1}]),
                                    'line_uuids': [first, self.line_uuid]})
        self.assertIn('more than the value', res['error'])
        self.assertEqual(self.fake.count('POST', '/refunds'), 1)

    def test_refund_capped_by_tabby(self):
        original = self.paid_order()
        self.fake.payment['refunds'] = [{'amount': '80.00', 'reference_id': 'dashboard'}]
        self.assertIn('Only 20.00 AED', self.refund(original.lines, qty=1, amount=-50)['error'])

    def test_refund_uncertain_keeps_reference(self):
        original = self.paid_order()
        self.fake.refund_status = 429
        res = self.refund(original.lines)
        self.assertTrue(res['uncertain'])
        self.assertEqual(self.attempt().state, 'pending')
        self.fake.refund_status = 200
        res = self.refund(original.lines)
        self.assertTrue(res['refund_id'])
        self.assertEqual(len({r['reference_id'] for r in self.fake.payment['refunds']}), 1)

    def test_refund_requires_method_on_pos(self):
        original = self.paid_order()
        other = self.env['pos.payment.method'].create({
            'name': 'Tabby other', 'journal_id': self.method.journal_id.id, 'use_payment_terminal': 'tabby',
            'tabby_secret_key': 'sk_' + 'test_00000000-0000-4000-8000-000000000001'})
        till2 = self.config.copy({'name': 'Till 2', 'payment_method_ids': [(6, 0, other.ids)]})
        res = other.with_user(self.user).tabby_refund({
            **self.data(amount=-50, refunded_lines=[{'id': original.lines.id, 'qty': 1}]), 'config_id': till2.id})
        self.assertIn('not enabled on this POS', res['error'])

    def test_refund_lookup_failure_stays_uncertain(self):
        original = self.paid_order()
        self.fake.refund_status = 503
        self.assertTrue(self.refund(original.lines)['uncertain'])
        self.fake.get_status = 401  # e.g. a key change while the refund is unconfirmed
        res = self.refund(original.lines)
        self.assertTrue(res['uncertain'])
        self.assertEqual(self.attempt().state, 'pending')

    def test_refund_cap_across_returns(self):
        original = self.paid_order()  # 2 items, 100
        self.refund(original.lines, qty=1, amount=-50)  # return A, not synced yet
        self.order_uuid, self.line_uuid = new_uuid(), new_uuid()  # return B of the same item
        self.assertIn('at most 50.00', self.refund(original.lines, qty=2, amount=-100)['error'])
        self.assertTrue(self.refund(original.lines, qty=1, amount=-50)['refund_id'])

    def test_refund_draft_return_not_counted(self):
        original = self.paid_order()
        session = original.session_id
        self.env['pos.order'].create({
            'session_id': session.id, 'uuid': self.order_uuid, 'state': 'draft',
            'lines': [(0, 0, {'product_id': self.product.id, 'qty': -2, 'price_unit': 50, 'price_subtotal': -100,
                              'price_subtotal_incl': -100, 'refunded_orderline_id': original.lines.id})],
            'amount_tax': 0, 'amount_total': -100, 'amount_paid': 0, 'amount_return': 0,
        })
        self.assertTrue(self.refund(original.lines, qty=2, amount=-100)['refund_id'])

    def test_sync_refuses_unconfirmed_refresh(self):
        self.create()
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '100.00'}])
        self.assertEqual(self.pm.tabby_payment_status(self.data())['outcome'], 'paid')
        self.fake.get_status = 503  # Tabby cannot confirm it still holds the money
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': self.line_uuid}],
                                self.order_uuid)
        with self.assertRaises(UserError):
            order._tabby_check_payments()
        self.assertFalse(self.attempt().pos_payment_id)

    def test_validated_order_takes_no_new_tabby_line(self):
        original = self.paid_order()
        self.refund(original.lines, qty=1, amount=-50)
        ret = self.make_return(original, qty=1, payments=[{'payment_method_id': self.method.id, 'amount': -50,
                                                           'uuid': self.line_uuid}])
        ret._tabby_check_payments()  # the return synced: its refund is linked
        ret.state = 'paid'
        res = self.refund(original.lines, qty=1, amount=-50, line_uuid=new_uuid())
        self.assertIn('already validated', res['error'])
        self.assertIn('already validated', self.create()['error'])
        self.assertEqual(self.fake.count('POST', '/refunds'), 1)

    # -- Final review (26.11) --------------------------------------------------

    def user_with(self, login, *xmlids):
        groups = [(6, 0, [self.env.ref(x).id for x in ('base.group_user',) + xmlids])]
        field = 'groups_id' if 'groups_id' in self.env['res.users']._fields else 'group_ids'
        return self.env['res.users'].create({'name': login, 'login': login, field: groups})

    def test_paid_transition_checks_tabby(self):
        # A draft order with a Tabby line no Tabby payment confirmed cannot be set paid directly.
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': self.line_uuid}],
                                self.order_uuid)
        self.assertEqual(order.state, 'draft')
        cashier = self.user_with('tabby_cashier', 'point_of_sale.group_pos_user')
        with self.assertRaises(UserError):
            order.with_user(cashier).action_pos_order_paid()
        self.assertEqual(order.state, 'draft')
        self.create()
        self.fake.payment.update(status='AUTHORIZED')
        order.action_pos_order_paid()
        self.assertEqual(order.state, 'paid')
        self.assertEqual(self.attempt().pos_payment_id, order.payment_ids)

    def test_sync_refusal_reaches_till(self):
        # Odoo 18 logs and swallows errors of action_pos_order_paid during the
        # sync: the Tabby refusal must still fail it.
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': self.line_uuid}],
                                self.order_uuid)
        with self.assertRaises(UserError):
            order._process_saved_order(False)
        self.assertEqual(order.state, 'draft')
        self.create()
        self.fake.payment.update(status='AUTHORIZED')
        gets = self.fake.count('GET', PAYMENT_ID)
        order._process_saved_order(False)
        self.assertEqual(order.state, 'paid')
        self.assertEqual(self.fake.count('GET', PAYMENT_ID) - gets, 2)  # one refresh + the read after the capture

    def test_cancel_draft_holding_tabby(self):
        self.create()
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '100.00'}])
        self.pm.tabby_payment_status(self.data())
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': self.line_uuid}],
                                self.order_uuid)
        with self.assertRaises(UserError):
            order.action_pos_order_cancel()
        self.assertEqual(order.state, 'draft')
        # Refunded in the dashboard: no money held, the draft can go.
        self.fake.payment['refunds'] = [{'amount': '100.00'}]
        order.action_pos_order_cancel()
        self.assertEqual(order.state, 'cancel')

    def test_cancel_draft_with_open_session_cancels_it(self):
        self.create()
        order = self.make_order([{'payment_method_id': self.cash.id, 'amount': 100}], self.order_uuid)
        order.action_pos_order_cancel()
        self.assertEqual((order.state, self.attempt().state), ('cancel', 'cancelled'))

    def test_session_close_reports_unlinked_payment(self):
        self.create()
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '100.00'}])
        self.pm.tabby_payment_status(self.data())  # paid at the till, then the order was lost
        other = self.make_order([{'payment_method_id': self.cash.id, 'amount': 100}])
        other.state = 'paid'
        session = other.session_id
        self.assertFalse(session._cannot_close_session())  # closing is not blocked
        held = session._tabby_report_unlinked()
        self.assertEqual(held, self.attempt())
        self.assertIn(PAYMENT_ID, session.message_ids[:1].body)
        self.fake.payment['refunds'] = [{'amount': '100.00'}]
        self.assertFalse(session._tabby_report_unlinked())  # refunded in the dashboard: released
        self.assertEqual(self.attempt().state, 'failed')

    def test_session_close_survives_tabby_outage(self):
        self.captured()
        session = self.make_order([{'payment_method_id': self.cash.id, 'amount': 100}]).session_id
        self.fake.get_status = 503
        session._tabby_report_unlinked()  # no exception: the close goes on

    def test_session_close_cancels_open_session(self):
        self.create()
        session = self.make_order([{'payment_method_id': self.cash.id, 'amount': 100}]).session_id
        session._tabby_report_unlinked()
        self.assertEqual(self.attempt().state, 'cancelled')

    def test_tabby_settings_manager_only(self):
        cashier = self.user_with('tabby_cashier2', 'point_of_sale.group_pos_user')
        with self.assertRaises(AccessError):
            self.config.with_user(cashier).write({'tabby_merchant_code': 'other-branch'})
        self.assertEqual(self.config.tabby_merchant_code, 'ae')
        self.config.with_user(cashier).write({'name': 'Renamed till'})  # other fields as before
        manager = self.user_with('tabby_manager', 'point_of_sale.group_pos_manager')
        self.config.with_user(manager).write({'tabby_merchant_code': '  BRANCH-17 '})
        self.assertEqual(self.config.tabby_merchant_code, 'BRANCH-17')

    def test_unknown_merchant_code_message(self):
        self.fake.create_status = 401
        error = self.create()['error']
        self.assertIn('Merchant code "ae"', error)
        self.assertIn('not set up in Tabby', error)
        self.assertFalse(self.attempt())

    def test_rejection_without_reason_mentions_setup(self):
        self.fake.session_status = 'rejected'
        self.assertIn('merchant code is active', self.create()['error'])

    def test_refund_without_listed_reference_stays_pending(self):
        original = self.paid_order()
        self.fake.refund_listed = False
        res = self.refund(original.lines)
        self.assertTrue(res['uncertain'])
        self.assertEqual(self.attempt().state, 'pending')
        # Tabby lists it a moment later: the next poll confirms it, nothing is sent again.
        self.fake.payment['refunds'].append({'id': new_uuid(), 'amount': '50.00', 'reference_id': self.attempt().reference})
        self.assertTrue(self.refund(original.lines)['refund_id'])
        self.assertEqual(self.attempt().state, 'refunded')
        self.assertEqual(self.fake.count('POST', '/refunds'), 1)

    def test_sync_refuses_refund_beyond_return(self):
        original = self.paid_order()
        self.assertTrue(self.refund(original.lines, qty=2, amount=-100)['refund_id'])
        # The return that syncs gives back only one item.
        ret = self.make_return(original, qty=1, payments=[{'payment_method_id': self.method.id, 'amount': -100,
                                                           'uuid': self.line_uuid}])
        with self.assertRaises(UserError):
            ret._tabby_check_payments()

    # -- Adversarial pass and Codex re-review (26.11) ---------------------------

    def captured(self, amount=100.0):
        self.create(amount=amount)
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '%.2f' % amount}], amount='%.2f' % amount)
        self.pm.tabby_payment_status(self.data())

    def test_direct_state_write_checks_tabby(self):
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': self.line_uuid}],
                                self.order_uuid)
        cashier = self.user_with('tabby_cashier3', 'point_of_sale.group_pos_user')
        with self.assertRaises(UserError):
            order.with_user(cashier).with_context(tabby_checked=True).write({'state': 'paid'})
        self.assertEqual(order.state, 'draft')

    def test_attempt_of_other_branch_does_not_pay(self):
        branch_b = self.config.copy({'name': 'Branch B', 'tabby_merchant_code': 'branch-b'})
        self.pm.tabby_create_session({**self.data(amount=100, order_reference='x', items=[], buyer={}),
                                      'config_id': branch_b.id})
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '100.00'}])
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'uuid': self.line_uuid}],
                                self.order_uuid)
        with self.assertRaises(UserError):
            order._tabby_check_payments()

    def test_tabby_more_than_total_refused(self):
        self.captured(150.0)
        order = self.make_order([{'payment_method_id': self.method.id, 'amount': 150, 'uuid': self.line_uuid},
                                 {'payment_method_id': self.cash.id, 'amount': -50}], self.order_uuid)
        with self.assertRaises(UserError):
            order._tabby_check_payments()

    def test_tabby_refund_plus_cash_return_refused(self):
        original = self.paid_order()
        tabby_return_uuid, tabby_line = self.order_uuid, self.line_uuid
        self.assertTrue(self.refund(original.lines, qty=2, amount=-100)['refund_id'])
        # Meanwhile another till takes the same two items back in cash, and syncs first.
        self.order_uuid = new_uuid()
        cash_return = self.make_return(original, qty=2, payments=[{'payment_method_id': self.cash.id, 'amount': -100}])
        cash_return.state = 'paid'
        self.order_uuid, self.line_uuid = tabby_return_uuid, tabby_line
        ret = self.make_return(original, qty=2, payments=[{'payment_method_id': self.method.id, 'amount': -100,
                                                           'uuid': self.line_uuid}])
        with self.assertRaises(UserError):
            ret._tabby_check_payments()

    def test_confirmed_tabby_line_is_locked(self):
        order = self.paid_order()
        cashier = self.user_with('tabby_cashier4', 'point_of_sale.group_pos_user')
        line = order.payment_ids.with_user(cashier)
        with self.assertRaises(UserError):
            line.write({'payment_method_id': self.cash.id})
        with self.assertRaises(UserError):
            line.unlink()

    def test_cancel_guard_covers_remove_from_ui(self):
        self.captured()
        order = self.make_order([{'payment_method_id': self.cash.id, 'amount': 100}], self.order_uuid)
        with self.assertRaises(UserError):
            self.env['pos.order'].remove_from_ui([order.id])
        self.assertTrue(order.exists())

    def test_posted_refund_is_not_sent_again_nor_released(self):
        original = self.paid_order()
        self.fake.refund_listed = False
        self.assertTrue(self.refund(original.lines)['uncertain'])
        self.assertTrue(self.attempt().refund_posted)
        self.assertTrue(self.refund(original.lines)['uncertain'])
        self.assertEqual(self.fake.count('POST', '/refunds'), 1)  # polled, not re-sent
        # Tabby does not list it yet: the order sync / session close must not release it.
        self.assertTrue(self.attempt()._tabby_resolve_unlinked())
        self.assertEqual(self.attempt().state, 'pending')

    def test_manager_reconcile(self):
        self.captured()
        session = self.make_order([{'payment_method_id': self.cash.id, 'amount': 100}]).session_id
        cashier = self.user_with('tabby_cashier5', 'point_of_sale.group_pos_user')
        with self.assertRaises(AccessError):
            self.attempt().with_user(cashier).action_tabby_reconcile()
        manager = self.user_with('tabby_manager2', 'point_of_sale.group_pos_manager')
        self.attempt().with_user(manager).action_tabby_reconcile()
        self.assertFalse(session._tabby_report_unlinked())  # reconciled: no longer reported

    def test_no_new_session_while_closing(self):
        session = self.make_order([{'payment_method_id': self.cash.id, 'amount': 100}]).session_id
        session.state = 'closing_control'
        self.assertIn('closing', self.create()['error'])
        self.assertFalse(self.fake.calls)

    def test_refund_counts_refunds_made_outside_pos(self):
        original = self.paid_order()
        # 60 of the 100 were refunded in the dashboard (or by a dispute) after the sale synced.
        self.fake.payment['refunds'] = [{'id': 'dash-1', 'amount': '60.00', 'reference_id': None}]
        res = self.refund(original.lines, qty=1, amount=-50)
        self.assertIn('Only 40.00 AED', res['error'])
        self.assertIn('outside the POS', res['error'])
        self.assertEqual(self.fake.count('POST', '/refunds'), 0)
        sale = self.env['pos.tabby.attempt'].search([('kind', '=', 'payment'), ('payment_id', '=', PAYMENT_ID)])
        self.assertEqual((sale.captured, sale.refunded), (100.0, 60.0))  # the sale shows Tabby's state now
        # What is left can still be refunded from the POS.
        self.order_uuid, self.line_uuid = new_uuid(), new_uuid()
        self.assertTrue(self.refund(original.lines, qty=1, amount=-40)['refund_id'])

    # -- Payment link by SMS ---------------------------------------------------

    def test_phone_sends_link(self):
        res = self.create(phone='050 123 4567')
        self.assertEqual(res['sms'], 'sent')
        self.assertEqual(res['sms_to'], '+971***567')
        self.assertTrue(res['qr_image'])
        sent = next(c for c in self.fake.calls if c[1] == '/api/v2/checkout')
        self.assertEqual(sent[2]['payment']['buyer']['phone'], '+971501234567')
        self.assertEqual(self.fake.count('POST', '/send_hpp_link'), 1)
        self.assertTrue(self.attempt().link_sent)
        # Resuming the line does not text the customer again.
        self.assertTrue(self.create(phone='050 123 4567')['resumed'])
        self.assertEqual(self.fake.count('POST', '/send_hpp_link'), 1)

    def test_phone_formats(self):
        currency = self.config.currency_id
        for raw in ('501234567', '0501234567', '+971501234567', '00971 50 123 4567', '971-50-123-4567'):
            self.assertEqual(self.method._tabby_phone(currency, raw), '+971501234567', raw)
        for raw in ('12345', '0412345678', '+966501234567x1', '9715012345678', '+508551234', '00508551234',
                    '+966501234567', '(+508) 41 23 45', '+', 'abc', '50x1234567', '++971501234567'):
            self.assertIs(self.method._tabby_phone(currency, raw), False, raw)
        self.assertIsNone(self.method._tabby_phone(currency, '  '))
        self.assertIs(self.method._tabby_phone(currency, 501234567), False)
        self.assertEqual(self.method._tabby_phone(currency, '(+971) 50-123-4567'), '+971501234567')
        sar = self.env.ref('base.SAR')
        self.assertEqual(self.method._tabby_phone(sar, '0551234567'), '+966551234567')

    def test_no_phone_no_link(self):
        res = self.create()
        self.assertNotIn('sms', res)
        self.assertFalse(self.fake.count('POST', '/send_hpp_link'))

    def test_invalid_phone_creates_nothing(self):
        self.assertIn('mobile number', self.create(phone='12345')['error'])
        self.assertFalse(self.fake.calls)

    def test_link_failure_keeps_qr(self):
        self.fake.link_status = 400
        res = self.create(phone='501234567')
        self.assertEqual(res['sms'], 'failed')
        self.assertIn('scan the QR code', res['sms_error'])
        self.assertTrue(res['qr_image'])
        self.assertEqual(self.attempt().state, 'pending')
        self.assertFalse(self.attempt().link_sent)
