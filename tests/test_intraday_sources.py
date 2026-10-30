import os
import unittest
from unittest.mock import patch

from india_trader.core import DataError
from india_trader.intraday_sources import (_NoRedirect, _upstox_get, fetch_preferred,
                                           parse_upstox_candles, upstox_bars)


CANDLE = ['2026-10-29T09:15:00+05:30', 100, 101, 99, 100.5, 1000, 0]


class IntradaySourcesTests(unittest.TestCase):
    def test_upstox_schema_and_start_time(self):
        rows = parse_upstox_candles({'status': 'success', 'data': {'candles': [CANDLE]}})
        self.assertEqual(rows[0]['timestamp'], CANDLE[0])
        self.assertEqual(rows[0]['volume'], 1000)

    def test_upstox_rejects_wrong_shape(self):
        with self.assertRaises(DataError):
            parse_upstox_candles({'status': 'success', 'data': {'candles': [['bad']]}})
        with self.assertRaisesRegex(DataError, 'object'):
            parse_upstox_candles({'status': 'success', 'data': 'not-an-object'})

    def test_read_only_token_does_not_follow_redirect_or_other_host(self):
        with self.assertRaisesRegex(DataError, 'official HTTPS endpoint'):
            _upstox_get('https://example.com/v3/historical-candle/redirect', 'test-token')
        with self.assertRaisesRegex(DataError, 'redirect refused'):
            _NoRedirect().redirect_request(None, None, 302, 'Moved', {}, 'https://other.example/')

    @patch('india_trader.intraday_sources._upstox_get')
    def test_upstox_merges_history_and_intraday(self, get):
        first = {"timestamp": "2026-10-28T09:15:00+05:30", "open": 100, "high": 101,
                 "low": 99, "close": 100.5, "volume": 1000}
        second = {**first, "timestamp": "2026-10-29T09:15:00+05:30"}
        get.side_effect = [[first], [second]]
        rows = upstox_bars('RELIANCE.NS', 'INE002A01018', 'token', '5d')
        self.assertEqual(len(rows), 2)

    @patch.dict(os.environ, {'UPSTOX_ANALYTICS_TOKEN': 'secret-for-test'})
    @patch('india_trader.intraday_sources.upstox_bars')
    @patch('india_trader.intraday_sources.load_nse_equities')
    def test_prefers_read_only_upstox(self, universe, upstox):
        universe.return_value = ([{'ticker': 'RELIANCE.NS', 'isin': 'INE002A01018'}], 'NSE')
        upstox.return_value = [{'timestamp': CANDLE[0]}]
        result = fetch_preferred('RELIANCE.NS')
        self.assertIn('Upstox', result['source'])
        self.assertNotIn('secret-for-test', str(result))

    @patch.dict(os.environ, {'UPSTOX_ANALYTICS_TOKEN': ''})
    @patch('india_trader.intraday_sources.yahoo_bars')
    def test_yahoo_fallback_without_account(self, yahoo):
        yahoo.return_value = [{'timestamp': CANDLE[0]}]
        result = fetch_preferred('RELIANCE.NS')
        self.assertIn('Yahoo', result['source'])
        self.assertEqual(result['attempts'][0]['status'], 'NOT_CONFIGURED')
