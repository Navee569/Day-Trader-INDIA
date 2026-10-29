import unittest
from datetime import date, timedelta

from india_trader.daily_scan import find_setups, rsi, scan_rows


class DailyScanTests(unittest.TestCase):
    def test_upstream_breakout_rule(self):
        closes = [100.0] * 29 + [105.0]
        tags, metrics = find_setups(closes)
        self.assertIn('breakout_20d', tags)
        self.assertEqual(metrics['high20'], 100.0)

    def test_wilder_rsi_needs_history(self):
        self.assertIsNone(rsi([100.0] * 14))
        self.assertEqual(rsi([100.0] * 15), 100.0)

    def test_marketwide_scan_uses_only_complete_30_day_history(self):
        start = date(2026, 9, 1)
        reports = []
        for i in range(30):
            d = (start + timedelta(days=i)).isoformat()
            reports.append({'date': d, 'frequency': 'daily', 'rows': [
                {'symbol': 'RELIANCE', 'date': d, 'close': 100.0 if i < 29 else 105.0, 'volume': 1000000},
                {'symbol': 'SHORT', 'date': d, 'close': 100.0, 'volume': 1000000} if i >= 5 else
                {'symbol': 'OTHER', 'date': d, 'close': 100.0, 'volume': 1000000},
            ]})
        result = scan_rows(reports)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['counts']['scanned'], 1)
        self.assertEqual(result['hits'][0]['symbol'], 'RELIANCE')
        self.assertIn('breakout_20d', result['hits'][0]['setups'])

    def test_duplicate_report_date_rejected(self):
        report = {'date': '2026-10-28', 'frequency': 'daily', 'rows': []}
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            scan_rows([report, report])
