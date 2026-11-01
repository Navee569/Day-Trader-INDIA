import os
import json
from datetime import date, datetime
import unittest
from unittest.mock import patch

from india_trader.core import DataError
from india_trader.intraday_sources import fetch_preferred, parse_upstox_candles, upstox_bars, upstox_quote, fyers_bars


CANDLE = ['2026-10-29T09:15:00+05:30', 100, 101, 99, 100.5, 1000, 0]


class IntradaySourcesTests(unittest.TestCase):
    def test_upstox_schema_and_start_time(self):
        rows = parse_upstox_candles({'status': 'success', 'data': {'candles': [CANDLE]}})
        self.assertEqual(rows[0]['timestamp'], CANDLE[0])
        self.assertEqual(rows[0]['volume'], 1000)

    def test_upstox_rejects_wrong_shape(self):
        with self.assertRaises(DataError):
            parse_upstox_candles({'status': 'success', 'data': {'candles': [['bad']]}})

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

    def test_one_minute_validation_and_bse_historical_chunks(self):
        one = ["2026-10-29T09:16:00+05:30", 100, 101, 99, 100.5, 1000]
        self.assertEqual(parse_upstox_candles({"status": "success", "data": {"candles": [one]}}, 1)[0]
                         ["timestamp"], one[0])
        with self.assertRaises(DataError):
            parse_upstox_candles({"status": "success", "data": {"candles": [one, one]}}, 1)
        with patch('india_trader.intraday_sources._upstox_get', return_value=[{
            "timestamp": CANDLE[0], "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000
        }]) as get:
            rows = upstox_bars('500325.BO', 'INE002A01018', 'token', '60d', 5, date(2026, 10, 29))
        self.assertEqual(len(rows), 1)
        self.assertEqual(get.call_count, 3)
        self.assertTrue(all('BSE_EQ%7CINE002A01018' in call.args[0] for call in get.call_args_list))
        self.assertTrue(all('/intraday/' not in call.args[0] for call in get.call_args_list))

    def test_quote_identity_timestamp_and_previous_close(self):
        good = {"status": "success", "data": {"NSE_EQ:RELIANCE": {
            "instrument_token": "NSE_EQ|INE002A01018", "last_price": 1200,
            "prev_close_price": 1190, "last_trade_time": "1793271300000"}}}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _): return json.dumps(good).encode()
        with patch('india_trader.intraday_sources.upstox_instrument', return_value='INE002A01018'), \
                patch('india_trader.intraday_sources.urllib.request.urlopen', return_value=Response()):
            quote = upstox_quote('RELIANCE.NS', 'token')
        self.assertEqual(quote['prev_close'], 1190)
        self.assertIn('Upstox', quote['source'])
        good['data']['NSE_EQ:RELIANCE']['instrument_token'] = 'NSE_EQ|OTHER'
        with patch('india_trader.intraday_sources.upstox_instrument', return_value='INE002A01018'), \
                patch('india_trader.intraday_sources.urllib.request.urlopen', return_value=Response()):
            with self.assertRaises(DataError):
                upstox_quote('RELIANCE.NS', 'token')

    def test_fyers_candles_use_data_endpoint_and_identity(self):
        epoch = int(datetime.fromisoformat(CANDLE[0]).timestamp())
        doc = {"s": "ok", "candles": [[epoch, 100, 101, 99, 100.5, 1000]]}
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _): return json.dumps(doc).encode()
        with patch('india_trader.intraday_sources.urllib.request.urlopen', return_value=Response()) as get:
            rows = fyers_bars('RELIANCE.NS', 'APP-100', 'testtoken', '5d', 5, date(2026, 10, 29))
        self.assertEqual(rows[0]['timestamp'], CANDLE[0])
        request = get.call_args.args[0]
        self.assertIn('symbol=NSE%3ARELIANCE-EQ', request.full_url)
        self.assertEqual(request.get_header('Authorization'), 'APP-100:testtoken')
        self.assertNotIn('testtoken', request.full_url)
        with self.assertRaises(DataError):
            fyers_bars('500325.BO', 'APP-100', 'testtoken')

    @patch.dict(os.environ, {'UPSTOX_ANALYTICS_TOKEN': '', 'FYERS_APP_ID': 'APP-100',
                             'FYERS_ACCESS_TOKEN': 'testtoken'})
    @patch('india_trader.intraday_sources.fyers_bars')
    def test_fyers_is_third_independent_fallback(self, fyers):
        fyers.return_value = [{'timestamp': CANDLE[0]}]
        result = fetch_preferred('RELIANCE.NS')
        self.assertIn('FYERS', result['source'])
        self.assertEqual(result['attempts'][0]['source'], 'Upstox')
        self.assertNotIn('testtoken', str(result))
