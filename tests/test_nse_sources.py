import io
import urllib.error
import unittest
import zipfile
from datetime import datetime
from unittest.mock import patch

from india_trader.nse_sources import bhavcopy_day, cm_live_quote
from india_trader.core import IST


class _Response:
    def __init__(self, body):
        self.body = body
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def read(self, size):
        return self.body[:size]


def _archive(date='2026-10-28'):
    text = ('TradDt,Sgmt,Src,FinInstrmTp,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,TtlTradgVol\n'
            f'{date},CM,NSE,STK,RELIANCE,EQ,100,110,95,105,10000\n'
            f'{date},CM,NSE,STK,OTHER,BE,100,110,95,105,10000\n')
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        archive.writestr('BhavCopy.csv', text)
    return out.getvalue()


class NSESourcesTests(unittest.TestCase):
    @patch('india_trader.nse_sources.urllib.request.urlopen')
    def test_bhavcopy_filters_eq_and_symbol(self, open_url):
        open_url.return_value = _Response(_archive())
        result = bhavcopy_day('2026-10-28', 'RELIANCE')
        self.assertEqual(result['count'], 1)
        self.assertEqual(result['rows'][0]['close'], 105)
        self.assertEqual(result['frequency'], 'daily')

    @patch('india_trader.nse_sources.urllib.request.urlopen')
    def test_bhavcopy_rejects_mismatched_date(self, open_url):
        open_url.return_value = _Response(_archive('2026-10-27'))
        with self.assertRaisesRegex(ValueError, 'trade date'):
            bhavcopy_day('2026-10-28')

    @patch('india_trader.nse_sources._mcp_post')
    def test_live_session_failure_is_explicit(self, post):
        post.side_effect = [({'result': {}}, 'session'), urllib.error.HTTPError('url', 404, 'Session not found', {}, None)]
        result = cm_live_quote('RELIANCE', datetime(2026, 10, 29, 10, 0, tzinfo=IST))
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertIsNone(result['quote'])

    @patch('india_trader.nse_sources._mcp_post')
    def test_live_quote_tool_discovery(self, post):
        post.side_effect = [
            ({'result': {}}, 'session'),
            ({}, None),
            ({'result': {'tools': [{'name': 'get_stock_quote', 'inputSchema': {'properties': {'symbol': {'type': 'string'}}, 'required': ['symbol']}}]}}, None),
            ({'result': {'content': [{'type': 'text', 'text': 'example'}]}}, None),
        ]
        result = cm_live_quote('RELIANCE', datetime(2026, 10, 29, 10, 0, tzinfo=IST))
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(post.call_args.args[1]['arguments']['symbol'], 'RELIANCE')

    @patch('india_trader.nse_sources._mcp_post')
    def test_after_hours_does_not_request_live_quote(self, post):
        result = cm_live_quote('RELIANCE', datetime(2026, 10, 29, 20, 0, tzinfo=IST))
        self.assertEqual(result['status'], 'MARKET_CLOSED')
        self.assertIsNone(result['quote'])
        post.assert_not_called()
