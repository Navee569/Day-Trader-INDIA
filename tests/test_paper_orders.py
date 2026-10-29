import unittest
from datetime import datetime

from india_trader.paper_orders import paper_ticket, risk_review, size_paper_order
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
