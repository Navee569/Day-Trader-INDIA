import csv
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from india_trader.core import (IST, DataError, Bar, symbol, as_datetime, parse_bars,
    read_csv, read_json_bars, orb_signal, plan, session_bars, save_json)
from india_trader.backtest import backtest_orb
from india_trader.scan import scan_csv_folder
from india_trader.__main__ import main


def row(date="2026-10-28", minute=0, open_=100, high=101, low=99, close=100, volume=1000):
    ts = datetime.fromisoformat(date+"T09:15:00+05:30")+timedelta(minutes=minute)
    return dict(timestamp=ts.isoformat(),open=open_,high=high,low=low,close=close,volume=volume)


def day(date="2026-10-28", full=False, trigger=True):
    result = [row(date, i*5) for i in range(75 if full else 5)]
    # Opening highs=101, lows=99, future closes above 101
    if trigger:
        result[3].update(open=100,high=104,low=100,close=103)
        if full: result[4].update(open=103,high=108,low=101,close=107)
    return result


class TestSymbol(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(symbol('reliance'), 'RELIANCE.NS')
        self.assertEqual(symbol('500325', 'BSE'), '500325.BO')
        self.assertEqual(symbol('RELIANCE.NS'), 'RELIANCE.NS')
    def test_reject(self):
        for value in ('TSLA.US','../etc/passwd','A/B','', ' A B ', 'A%2F', 'A'*34):
            with self.subTest(value=value), self.assertRaises(ValueError): symbol(value)
    def test_exchange_mismatch(self):
        with self.assertRaises(ValueError): symbol('RELIANCE.NS', 'BSE')
        with self.assertRaises(ValueError): symbol('RELIANCE.BO', 'NSE')


class TestInput(unittest.TestCase):
    def test_normalize_utc(self):
        bar = row(); bar['timestamp'] = '2026-10-28T03:45:00Z'
        self.assertEqual(Bar.parse(bar).timestamp.hour, 9)
    def test_naive(self):
        with self.assertRaises(DataError): Bar.parse(row() | {'timestamp':'2026-10-28T09:15:00'})
    def test_duplicate(self):
        with self.assertRaises(DataError): parse_bars([row(),row()])
    def test_bad_ohlc(self):
        for bad in (dict(low=101), dict(high=99), dict(open=float('nan')),
                    dict(close=-1),dict(volume=-1)):
            with self.subTest(bad=bad), self.assertRaises(DataError): Bar.parse(row()|bad)
    def test_off_grid(self):
        with self.assertRaises(DataError): Bar.parse(row(minute=1))
    def test_date_selection(self):
        self.assertEqual(len(session_bars(day(), '2026-10-28')), 5)
        self.assertEqual(len(session_bars(day(), '2026-10-27')), 0)
    def test_csv(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bars.csv'
            with p.open('w',newline='') as f:
                w=csv.DictWriter(f, fieldnames=row().keys());w.writeheader();w.writerow(row())
            self.assertEqual(read_csv(p)[0]['open'],100.0)
    def test_json(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bars.json';p.write_text(json.dumps({'bars':[row()]}))
            self.assertEqual(len(read_json_bars(p)),1)


class TestORB(unittest.TestCase):
    def test_trigger(self):
        r=orb_signal(day(),as_of='2026-10-28T09:40:00+05:30')
        self.assertEqual(r['status'],'TRIGGERED_LONG')
        self.assertEqual(r['trigger_bar_end'],'2026-10-28T09:35:00+05:30')
    def test_completed_bar_only(self):
        r=orb_signal(day(),as_of='2026-10-28T09:33:00+05:30')
        self.assertEqual(r['status'],'WAIT')
    def test_before_range_close(self):
        self.assertEqual(orb_signal(day(),as_of='2026-10-28T09:27:00+05:30')['status'],'INCOMPLETE_OPEN')
    def test_no_data(self):
        self.assertEqual(orb_signal([])['status'],'NO_DATA')
    def test_missing_first(self):
        self.assertEqual(orb_signal(day()[1:],as_of='2026-10-28T10:00:00+05:30')['status'],'INCOMPLETE_OPEN')
    def test_open_gap(self):
        bars=day();bars.pop(1)
        self.assertEqual(orb_signal(bars,as_of='2026-10-28T10:00:00+05:30')['status'],'INCOMPLETE_OPEN')
    def test_later_gap(self):
        bars=day();bars.pop(3)
        self.assertEqual(orb_signal(bars,as_of='2026-10-28T10:00:00+05:30')['status'],'DATA_GAP')
    def test_future_session(self):
        self.assertEqual(orb_signal(day(),as_of='2026-10-27T11:00:00+05:30')['status'],'FUTURE_SESSION')
    def test_downside(self):
        bars=day(trigger=False)
        bars[3].update(open=100,high=100,low=97,close=98)
        self.assertEqual(orb_signal(bars,as_of='2026-10-28T09:40:00+05:30')['status'],'DOWNSIDE_BREAK_NO_LONG')
    def test_historical_reproducible(self):
        bars=day()
        x=orb_signal(bars,as_of='2026-10-28T09:40:00+05:30')
        y=orb_signal(bars,as_of='2026-10-28T09:40:00+05:30')
        self.assertEqual(x,y)
    def test_timezones_convert(self):
        rows=day();rows[3]['timestamp']='2026-10-28T04:00:00+00:00'
        self.assertEqual(orb_signal(rows,as_of='2026-10-28T09:40:00+05:30')['status'],'TRIGGERED_LONG')


class TestPlan(unittest.TestCase):
    def test_basic(self):
        p=plan('100','95','100000','0.5','2','20',tick_size='0.01')
        self.assertEqual(p['quantity'],100)
        self.assertEqual(p['target'],110.0)
        self.assertEqual(p['planned_loss_inr_including_assumptions'],500)
    def test_tick_rounding(self):
        p=plan('100.001','95.049',tick_size='0.05')
        self.assertEqual(p['entry'],100.05)
        self.assertEqual(p['stop'],95.0)
    def test_slippage_and_fee_reduce_qty(self):
        orig=plan(100,95,tick_size="0.01")
        expensive=plan(100,95,slippage_bps=50,fee_buffer=100,tick_size="0.01")
        self.assertLess(expensive['quantity'],orig['quantity'])
        self.assertLessEqual(expensive['planned_loss_inr_including_assumptions'],expensive['risk_budget_inr'])
    def test_block_zero(self):
        p=plan(100,95,capital=10,tick_size="0.01")
        self.assertEqual(p['status'],'BLOCK_ZERO_QUANTITY')
    def test_bad_values(self):
        for kw in (dict(entry=100,stop=101),dict(entry=100,stop=95,capital=0),
                   dict(entry=100,stop=95,max_risk_pct=100),
                   dict(entry=100,stop=95,slippage_bps=-1),
                   dict(entry=100,stop=95,fee_buffer=-1),
                   dict(entry=100,stop=95,tick_size=0),
                   dict(entry=100,stop=95,capital='NaN')):
            with self.subTest(kw=kw),self.assertRaises(ValueError):plan(**(dict(tick_size="0.01")|kw))
    def test_fee_exceeds_budget(self):
        self.assertEqual(plan(100,95,fee_buffer=1000,tick_size="0.01")['quantity'],0)


class TestBacktest(unittest.TestCase):
    def test_simulates_full_day(self):
        results=backtest_orb(day(full=True))
        self.assertEqual(results['simulated_trades'],1)
        self.assertEqual(results['skipped_sessions'],0)
        self.assertEqual(results['sessions'][0]['entry_time'],'2026-10-28T09:35:00+05:30')
    def test_excludes_partial(self):
        results=backtest_orb(day(full=False))
        self.assertEqual(results['simulated_trades'],0)
        self.assertEqual(results['skipped_sessions'],1)
    def test_excludes_gaps(self):
        rows=day(full=True);rows.pop(10)
        self.assertEqual(backtest_orb(rows)['simulated_trades'],0)
    def test_stop_first_ambiguous(self):
        rows=day(full=True)
        # Signal 09:30; entering next 09:35 open; stop 99 target 2R; both hit
        rows[4].update(open=101,high=110,low=98,close=101)
        r=backtest_orb(rows)['sessions'][0]
        self.assertEqual(r['reason'],'STOP')
    def test_no_long(self):
        self.assertEqual(backtest_orb(day(full=True,trigger=False))['simulated_trades'],0)


class TestCLI(unittest.TestCase):
    def test_doctor(self): self.assertEqual(main(['doctor']),0)
    def test_plan(self): self.assertEqual(main(['plan','--entry','100','--stop','95','--tick','0.01']),0)
    def test_bad_plan_code(self): self.assertEqual(main(['plan','--entry','95','--stop','100','--tick','0.01']),2)
    def test_bad_date(self):
        self.assertRaises(DataError,orb_signal,day(),15,'2026-10-28T10:00:00+05:30','today')
    def test_scanner_offline(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'RELIANCE.csv'
            with p.open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=row().keys());w.writeheader();w.writerows(day())
            r=scan_csv_folder(d,as_of='2026-10-28T09:40:00+05:30')
            self.assertEqual(r['candidates'],1)
    def test_save_json(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'folder'/'test.json'
            save_json(p, {'status':'OK'})
            self.assertEqual(json.loads(p.read_text())['status'],'OK')

if __name__=='__main__': unittest.main()
