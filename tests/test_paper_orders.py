import unittest
from datetime import datetime

from india_trader.paper_orders import _fingerprint, paper_ticket, risk_review, size_paper_order
from india_trader.core import IST


PLAN = {'mode': 'PAPER_PLAN_ONLY', 'status': 'PLANNED', 'ticker': 'RELIANCE.NS',
        'entry_reference': 103, 'stop': 100, 'target': 109,
        'trigger_at': '2026-10-28T09:35:00+05:30', 'flat_by_ist': '15:10',
        'data_origin': 'synthetic_test'}


class PaperOrdersTests(unittest.TestCase):
    def test_budget_risk_and_ticket_handoff(self):
        order = size_paper_order(PLAN)
        self.assertEqual(order['status'], 'PROPOSED')
        self.assertLessEqual(order['planned_loss_inr'], 500)
        review = risk_review(order, '2026-10-28T09:36:00+05:30')
        self.assertEqual(review['status'], 'PASS')
        ticket = paper_ticket(order, review, approval=True,
                              now=datetime(2026, 10, 28, 9, 37, tzinfo=IST))
        self.assertEqual(ticket['status'], 'APPROVED_UNFILLED')
        self.assertIsNone(ticket['fill'])
        self.assertFalse(ticket['orders_possible'])

    def test_changed_order_is_blocked(self):
        order = size_paper_order(PLAN)
        order['quantity'] += 1
        review = risk_review(order, '2026-10-28T09:36:00+05:30')
        self.assertIn('ORDER_CHANGED', review['reasons'])
        with self.assertRaises(ValueError):
            paper_ticket(order, review)

    def test_closed_market_blocks_new_ticket(self):
        order = size_paper_order(PLAN)
        review = risk_review(order, '2026-10-28T20:00:00+05:30')
        self.assertIn('OUTSIDE_NORMAL_ENTRY_WINDOW', review['reasons'])

    def test_zero_share_budget_blocks(self):
        result = size_paper_order(PLAN, capital='100', max_loss='1', fee_buffer='0')
        self.assertEqual(result['status'], 'BLOCK')

    def test_recomputed_fingerprint_cannot_hide_million_share_order(self):
        order = size_paper_order(PLAN)
        order.update(quantity=1_000_000, planned_notional_inr=1, planned_loss_inr=1)
        order['fingerprint'] = _fingerprint(order)
        review = risk_review(order, '2026-10-28T09:36:00+05:30')
        self.assertEqual(review['status'], 'BLOCK')
        self.assertIn('ORDER_MATH_MISMATCH', review['reasons'])
        with self.assertRaisesRegex(ValueError, 'sizing arithmetic'):
            paper_ticket(order, {'status': 'PASS', 'order_fingerprint': order['fingerprint']})

    def test_recomputed_fingerprint_cannot_hide_invalid_side_or_allocation(self):
        order = size_paper_order(PLAN)
        order['side'] = 'SELL_SHORT'
        order['fingerprint'] = _fingerprint(order)
        self.assertIn('INVALID_ORDER_TERMS', risk_review(order, '2026-10-28T09:36:00+05:30')['reasons'])
        order = size_paper_order(PLAN)
        order['max_position_pct'] = 1
        order['fingerprint'] = _fingerprint(order)
        self.assertIn('POSITION_CAP', risk_review(order, '2026-10-28T09:36:00+05:30')['reasons'])

    def test_sizer_rejects_extreme_capital_before_decimal_to_int(self):
        with self.assertRaises(ValueError):
            size_paper_order(PLAN, capital='1e1000000')

    def test_negative_and_malformed_portfolio_state_blocks(self):
        order = size_paper_order(PLAN)
        for state in ({'daily_planned_loss_inr': '-5000'}, {'cash_inr': '-1'},
                      {'trades_today': -1}, {'traded_symbols_today': 'RELIANCE.NS'}, []):
            result = risk_review(order, '2026-10-28T09:36:00+05:30', state)
            self.assertEqual(result['status'], 'BLOCK')
            self.assertIn('INVALID_RISK_STATE', result['reasons'])

    def test_invalid_daily_trade_limit_fails_closed(self):
        result = risk_review(size_paper_order(PLAN), '2026-10-28T09:36:00+05:30',
                             max_trades_per_day='many')
        self.assertEqual(result['status'], 'BLOCK')
        self.assertIn('INVALID_RISK_STATE', result['reasons'])

    def test_nse_holiday_and_unknown_year_block_paper_entry(self):
        for when in ('2026-10-20T09:36:00+05:30', '2027-10-28T09:36:00+05:30'):
            order = size_paper_order({**PLAN, 'trigger_at': when})
            result = risk_review(order, when)
            self.assertEqual(result['status'], 'BLOCK')
            self.assertIn('NSE_REGULAR_SESSION_UNVERIFIED_OR_CLOSED', result['reasons'])
        bse = size_paper_order({**PLAN, 'ticker': 'RELIANCE.BO',
                                'trigger_at': '2026-10-20T09:35:00+05:30'})
        result = risk_review(bse, '2026-10-20T09:36:00+05:30')
        self.assertIn('BSE_ENTRY_BLOCKED_BY_NSE_CLOSURE_OR_UNKNOWN_YEAR', result['reasons'])
