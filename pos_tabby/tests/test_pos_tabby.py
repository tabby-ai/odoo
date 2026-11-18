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

    def __call__(self, method, url, headers=None, data=None, timeout=None, allow_redirects=True):
        path = re.sub(r'^https://[^/]+', '', url)
        body = json.loads(data) if data else None
        self.calls.append((method, path, body, headers))
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

    def create(self, amount=100.0):
        return self.pm.tabby_create_session(self.data(amount=amount, order_reference='Order 1', items=[], buyer={}))

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
        self.fake.payment.update(status='CLOSED', captures=[{'amount': '100.00'}])
        return self.make_order([{'payment_method_id': self.method.id, 'amount': 100, 'transaction_id': PAYMENT_ID}])

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
