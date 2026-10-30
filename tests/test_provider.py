import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import URLError
from india_trader.data import fetch_bars
from india_trader.core import DataError


class Response:
    def __init__(self,payload): self.payload=payload
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,n):return self.payload[:n]


def good_payload():
    stamp=int(datetime.fromisoformat('2026-10-28T09:15:00+05:30').timestamp())
    return {'chart':{'error':None,'result':[{'meta':{'currency':'INR'},'timestamp':[stamp,stamp+300],
        'indicators':{'quote':[{'open':[100,100], 'high':[101,102],
            'low':[99,98], 'close':[100,101], 'volume':[1500,2000]}]}}]}}


class TestYahooProvider(unittest.TestCase):
    def run_payload(self,payload):
        with patch('india_trader.data.urllib.request.urlopen',return_value=Response(json.dumps(payload).encode())):
            return fetch_bars('RELIANCE.NS')
    def test_valid_response(self):
        bars=self.run_payload(good_payload())
        self.assertEqual(len(bars),2)
        self.assertEqual(bars[0]['timestamp'],'2026-10-28T09:15:00+05:30')
    def test_trailing_in_progress_quote_update_is_not_a_five_minute_bar(self):
        p=good_payload(); r=p['chart']['result'][0]
        r['timestamp'].append(r['timestamp'][-1]+86)
        q=r['indicators']['quote'][0]
        for key in ('open','high','low','close','volume'): q[key].append(q[key][-1])
        self.assertEqual(len(self.run_payload(p)),2)

    def test_interior_irregular_timestamp_still_fails(self):
        p=good_payload(); r=p['chart']['result'][0]
        r['timestamp'].insert(1,r['timestamp'][0]+86)
        q=r['indicators']['quote'][0]
        for key in ('open','high','low','close','volume'): q[key].insert(1,q[key][0])
        with self.assertRaisesRegex(DataError,'aligned'): self.run_payload(p)
    def test_currency_mismatch(self):
        p=good_payload();p['chart']['result'][0]['meta']['currency']='USD'
        with self.assertRaisesRegex(DataError,'currency'):self.run_payload(p)
    def test_data_gap_placeholder(self):
        p=good_payload();p['chart']['result'][0]['indicators']['quote'][0]['close'][1]=None
        self.assertEqual(len(self.run_payload(p)),1)
    def test_bad_ohlc(self):
        p=good_payload();p['chart']['result'][0]['indicators']['quote'][0]['high'][0]=50
        with self.assertRaises(DataError):self.run_payload(p)
    def test_no_chart(self):
        with self.assertRaises(DataError):self.run_payload({'chart':{'result':None,'error':None}})
    def test_provider_error(self):
        with self.assertRaises(DataError):self.run_payload({'chart':{'error':{'code':'Too Many Requests'}}})
    def test_wrong_lengths(self):
        p=good_payload();p['chart']['result'][0]['indicators']['quote'][0]['close']=[100]
        with self.assertRaises(DataError):self.run_payload(p)
    def test_fetch_restrict_foreign_and_intervals(self):
        with self.assertRaises(ValueError): fetch_bars('TSLA')
        with self.assertRaises(ValueError): fetch_bars('RELIANCE.NS','1m','5d')
        with self.assertRaises(ValueError): fetch_bars('RELIANCE.NS','5m','6mo')
    def test_dns_fails_closed(self):
        with patch('india_trader.data.urllib.request.urlopen',side_effect=URLError('blocked')):
            with self.assertRaisesRegex(DataError,'unavailable'):fetch_bars('RELIANCE.NS')
    def test_second_yahoo_host_used_after_transport_failure(self):
        payload=Response(json.dumps(good_payload()).encode())
        with patch('india_trader.data.urllib.request.urlopen',side_effect=[URLError('first down'),payload]) as fetch:
            self.assertEqual(len(fetch_bars('RELIANCE.NS')),2)
            self.assertEqual(fetch.call_count,2)
    def test_invalid_json(self):
        with patch('india_trader.data.urllib.request.urlopen',return_value=Response(b'no json')):
            with self.assertRaisesRegex(DataError,'invalid JSON'):fetch_bars('RELIANCE.NS')
    def test_response_too_large(self):
        with patch('india_trader.data.urllib.request.urlopen',return_value=Response(b'A'* (8*1024*1024+1))):
            with self.assertRaisesRegex(DataError,'too large'):fetch_bars('RELIANCE.NS')

if __name__=='__main__':unittest.main()
