"""Independent re-audit regressions, 29 October 2026. All tests are offline."""
from __future__ import annotations
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from india_trader.core import Bar, DataError, IST, orb_signal, parse_bars, plan, read_csv, read_json_bars
from india_trader.backtest import backtest_orb
from india_trader.data import fetch_bars
from india_trader.scan import scan_watchlist


class Response:
    def __init__(self, payload):
        self.payload = payload
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass
    def read(self, n=-1):
        return json.dumps(self.payload).encode()


def clean_bars():
    origin = datetime(2026, 10, 28, 9, 15, tzinfo=IST)
    return [Bar(origin + timedelta(minutes=5*i), 100.0, 100.5, 99.5, 100.0, 50) for i in range(72)]


class TestIndependentAudit(unittest.TestCase):
    def test_caller_created_nan_bar_rejected(self):
        bars = clean_bars()
        bars[0] = Bar(bars[0].timestamp, float('nan'), 100.5, 99.5, 100, 50)
        with self.assertRaises(DataError):
            orb_signal(bars, as_of='2026-10-28T10:00:00+05:30')

    def test_caller_created_negative_volume_rejected(self):
        bars = clean_bars()
        bars[1] = Bar(bars[1].timestamp, 100, 100.5, 99.5, 100, -8)
        with self.assertRaises(DataError):
            parse_bars(bars)

    def test_caller_created_naive_time_rejected(self):
        bars = clean_bars()
        bars[0] = Bar(datetime(2026,10,28,9,15), 100, 100.5, 99.5, 100, 100)
        with self.assertRaises(DataError):
            parse_bars(bars)

    def test_caller_created_off_grid_rejected(self):
        bars = clean_bars()
        bars[0] = Bar(datetime(2026,10,28,9,17, tzinfo=IST), 100, 100.5, 99.5, 100, 100)
        with self.assertRaises(DataError):
            parse_bars(bars)

    def test_max_entry_applies_to_actual_fill_not_signal_start(self):
        bars = clean_bars()
        i = (11*60+30 - (9*60+15))//5
        bars[i] = Bar(bars[i].timestamp, 100, 102, 99.5, 102, 50)
        row = backtest_orb(bars, max_entry='11:30')['sessions'][0]
        self.assertEqual(row['status'], 'SKIP')
        self.assertEqual(row['reason'], 'NO_LONG_TRIGGER')

    def test_max_entry_allows_entry_exactly_at_deadline(self):
        bars = clean_bars()
        i = (11*60+25 - (9*60+15))//5
        bars[i] = Bar(bars[i].timestamp, 100, 102, 99.5, 102, 50)
        row = backtest_orb(bars,max_entry='11:30')['sessions'][0]
        self.assertEqual(row['entry_time'], '2026-10-28T11:30:00+05:30')
        self.assertEqual(row['status'], 'SIMULATED')

    def test_extreme_fee_rejected_cleanly(self):
        with self.assertRaises(ValueError):
            plan('100','99','100000','0.5','2','20','0.01','0','1e100')

    def test_extremely_small_tick_rejected_cleanly(self):
        with self.assertRaises(ValueError):
            plan('100','99','100000','0.5','2','20','1e-999999')

    def test_fee_within_cap_but_over_risk_blocks(self):
        self.assertEqual(plan('100','99','100000','0.5','2','20','0.01','0','600')['status'],
                         'BLOCK_ZERO_QUANTITY')

    def test_extra_csv_cells_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bars.csv'
            p.write_text('timestamp,open,high,low,close\n2026-10-28T09:15:00+05:30,100,101,99,100,unexpected\n')
            with self.assertRaises(DataError):read_csv(p)

    def test_watchlist_duplicates_after_normalization(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'watch.txt'
            p.write_text('reliance\nRELIANCE.NS\n')
            with self.assertRaises(DataError):scan_watchlist(str(p))

    def test_invalid_watchlist_symbol_itemized_error(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'watch.txt'
            p.write_text('nonsense-symbol-with-too-many-letters-to-be-valid\n')
            result = scan_watchlist(str(p))
            self.assertEqual(result['results'][0]['status'], 'DATA_ERROR')

    def test_json_wrong_root_does_not_silently_become_no_data(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bars.json'
            p.write_text('{"ticker":"RELIANCE.NS"}')
            with self.assertRaises(DataError): read_json_bars(p)

    def _fetch(self, payload):
        with patch('urllib.request.urlopen', return_value=Response(payload)):
            return fetch_bars('RELIANCE.NS')

    def test_malformed_indicators_rejected_cleanly(self):
        payload={'chart':{'result':[{'meta':{'currency':'INR'},'timestamp':[1793159100], 'indicators':'abc'}]}}
        with self.assertRaises(DataError):self._fetch(payload)

    def test_malformed_quote_structure_rejected_cleanly(self):
        payload={'chart':{'result':[{'meta':{'currency':'INR'},'timestamp':[1793159100], 'indicators':{'quote': {'open':[100]}}}]}}
        with self.assertRaises(DataError):self._fetch(payload)

    def test_malformed_result_dictionary_rejected_cleanly(self):
        payload={'chart':{'result':{'some':'object'}}}
        with self.assertRaises(DataError):self._fetch(payload)

    def test_mismatched_provider_ticker_rejected(self):
        payload={'chart':{'result':[{'meta':{'currency':'INR','symbol':'FAKE.NS'},'timestamp':[1793159100], 'indicators':{'quote':[{'open':[100],'high':[101],'low':[99],'close':[100]}]}}]}}
        with self.assertRaises(DataError):self._fetch(payload)

    def test_nontyped_provider_ticker_rejected(self):
        payload={'chart':{'result':[{'meta':{'currency':'INR','symbol':123},'timestamp':[1793159100], 'indicators':{'quote':[{'open':[100],'high':[101],'low':[99],'close':[100]}]}}]}}
        with self.assertRaises(DataError):self._fetch(payload)


if __name__ == '__main__':
    unittest.main()
