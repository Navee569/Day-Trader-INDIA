import unittest
from datetime import datetime, timedelta

from india_trader.core import IST
from india_trader.trade_workflow import entry_check, exit_plan


def bars():
    start = datetime(2026, 10, 28, 9, 15, tzinfo=IST)
    data = []
    for i in range(5):
        data.append({'timestamp': (start + timedelta(minutes=5*i)).isoformat(),
                     'open': 100 if i < 3 else 102, 'high': 102 if i < 3 else 104,
                     'low': 99 if i < 3 else 101, 'close': 100 if i < 3 else 103,
                     'volume': 1000})
    return data


class TradeWorkflowTests(unittest.TestCase):
    def test_entry_waits_for_completed_breakout(self):
        result = entry_check(bars(), 'RELIANCE.NS', as_of='2026-10-28T09:34:59+05:30')
        self.assertEqual(result['status'], 'WAIT')

    def test_entry_exit_handoff(self):
        entry = entry_check(bars(), 'RELIANCE.NS', as_of='2026-10-28T09:35:00+05:30')
        self.assertEqual(entry['status'], 'TRIGGERED')
        self.assertEqual(entry['entry_price'], 103)
        result = exit_plan(entry, tick_size='0.05')
        self.assertEqual(result['status'], 'PLANNED')
        self.assertEqual(result['stop'], 100.5)
        self.assertEqual(result['target'], 108.0)
        self.assertFalse(result['orders_possible'])

    def test_level_requires_level(self):
        with self.assertRaises(ValueError):
            entry_check(bars(), 'RELIANCE.NS', setup='level', as_of='2026-10-28T09:35:00+05:30')

    def test_exit_rejects_untriggered(self):
        with self.assertRaises(ValueError):
            exit_plan({'status': 'WAIT'})

    def test_zero_volume_blocks_entry(self):
        data = bars()
        data[3]['volume'] = 0
        result = entry_check(data, 'RELIANCE.NS', as_of='2026-10-28T09:35:00+05:30')
        self.assertEqual(result['status'], 'ZERO_VOLUME_BAR')

    def test_exit_rejects_extreme_tick_without_decimal_overflow(self):
        entry = entry_check(bars(), 'RELIANCE.NS', as_of='2026-10-28T09:35:00+05:30')
        with self.assertRaisesRegex(ValueError, 'tick must be'):
            exit_plan(entry, tick_size='1e-999999')

    def test_old_breakout_cannot_generate_a_new_exit_plan(self):
        entry = entry_check(bars(), 'RELIANCE.NS', as_of='2026-10-28T09:45:00+05:30')
        self.assertEqual(entry['status'], 'STALE_SIGNAL')
        with self.assertRaisesRegex(ValueError, 'triggered entry'):
            exit_plan(entry, tick_size='0.05')

    def test_level_entry_blocks_stale_quote_series(self):
        entry = entry_check(bars(), 'RELIANCE.NS', setup='level', level=101,
                            as_of='2026-10-28T10:00:00+05:30')
        self.assertEqual(entry['status'], 'STALE_DATA')

    def test_nse_holiday_cannot_trigger_paper_entry(self):
        data = bars()
        for row in data:
            row['timestamp'] = row['timestamp'].replace('2026-10-28', '2026-10-20')
        result = entry_check(data, 'RELIANCE.NS', as_of='2026-10-20T09:35:00+05:30')
        self.assertEqual(result['status'], 'NSE_REGULAR_SESSION_UNVERIFIED_OR_CLOSED')
