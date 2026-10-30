"""Behavior checks for the NSE adaptation of the upstream three-part scanner."""
import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

from india_trader import market_scanner as m
from india_trader.core import IST
from india_trader.universe import parse_bse_scrip_zip


DAY = date(2026, 10, 28)


def bars(closes, volume=1000):
    base = datetime(2026, 10, 28, 9, 15, tzinfo=IST)
    return {
        "t": [int((base + timedelta(minutes=i * 5)).timestamp()) for i in range(len(closes))],
        "c": closes,
        "h": [x + 1 for x in closes],
        "l": [x - 1 for x in closes],
        "v": [volume] * len(closes),
        "meta": {"symbol": "RELIANCE.NS", "currency": "INR"},
    }


def chart(ticker, series):
    return {"chart": {"result": [{
        "meta": {"symbol": ticker, "currency": "INR"},
        "timestamp": series["t"],
        "indicators": {"quote": [{"close": series["c"], "high": series["h"],
                                   "low": series["l"], "volume": series["v"]}]},
    }], "error": None}}


class MarketScannerTests(unittest.TestCase):
    def test_ist_window_and_holiday(self):
        self.assertEqual(m.to_ist(datetime(2026, 10, 28, 4, 0)), datetime(2026, 10, 28, 9, 30))
        self.assertFalse(m.trading_day(date(2026, 10, 20))[0])
        self.assertFalse(m.trading_day(date(2026, 11, 8))[0])
        self.assertFalse(m.trading_day(date(2027, 1, 4))[0])
        with self.assertRaises(m.ScanError):
            m.orb_window(m.parse_now("2026-10-20 10:00"), m.parse_hhmm("10:00"), 15,
                         m.parse_hhmm("15:10"))
        with self.assertRaises(m.ScanError):
            m.orb_window(m.parse_now("2026-10-28 09:25"), None, 15,
                         m.parse_hhmm("15:10"))
        with self.assertRaises(m.ScanError):
            m.orb_window(m.parse_now("2026-10-28 16:00"), None, 15,
                         m.parse_hhmm("15:10"))

    def test_two_stage_limit_reports_unconfirmed(self):
        cutoff = datetime(2026, 10, 28, 9, 40)
        symbols = ["A.NS", "B.NS", "C.NS"]
        # A and C pass the necessary close-only test; A has higher turnover.
        stage1 = {"A.NS": bars([100, 100, 100, 105, 106], 1000),
                  "B.NS": bars([100, 100, 100, 99, 99], 1000),
                  "C.NS": bars([100, 100, 100, 105, 106], 10)}
        confirmed = []
        def confirm(ticker):
            confirmed.append(ticker)
            return stage1[ticker]
        hits, counts, failed = m.scan_orb(stage1, symbols, DAY, cutoff, 15,
                                          min_price=20, confirm=confirm, max_confirm=1)
        self.assertEqual(confirmed, ["A.NS"])
        self.assertEqual([h["symbol"] for h in hits], ["A.NS"])
        self.assertEqual(counts["unconfirmed"], 1)
        self.assertEqual(counts["scanned"], 3)
        self.assertEqual(failed, {})

    def test_incomplete_or_gap_does_not_confirm(self):
        s = bars([100, 100, 100, 105, 106])
        s["t"].pop(1)
        for key in ("c", "h", "l", "v"):
            s[key].pop(1)
        hits, counts, _ = m.scan_orb({"A.NS": s}, ["A.NS"], DAY,
                                     datetime(2026, 10, 28, 9, 40), 15)
        self.assertEqual(hits, [])
        self.assertEqual(counts["unusable"], 1)

    def test_zero_volume_blocks_confirmation(self):
        s = bars([100, 100, 100, 105, 106])
        s["v"][3] = 0
        hits, counts, failed = m.scan_orb({"A.NS": s}, ["A.NS"], DAY,
                                           datetime(2026, 10, 28, 9, 40), 15)
        self.assertEqual(hits, [])
        self.assertEqual(counts["zero_or_missing_volume"], 1)
        self.assertIn("A.NS", failed)

    def test_zero_confirmation_cap_checks_every_candidate(self):
        cutoff = datetime(2026, 10, 28, 9, 40)
        s = bars([100, 100, 100, 105, 106])
        stage1 = {"A.NS": s, "B.NS": s}
        called = []
        def confirm(ticker):
            called.append(ticker)
            return s
        hits, counts, _ = m.scan_orb(stage1, list(stage1), DAY, cutoff, 15,
                                     confirm=confirm, max_confirm=0)
        self.assertEqual(len(called), 2)
        self.assertEqual(len(hits), 2)
        self.assertEqual(counts["unconfirmed"], 0)

    def test_daily_meta_volume_must_match_completed_day(self):
        days = []
        cursor = date(2026, 10, 29)
        while len(days) < 30:
            if cursor.weekday() < 5:
                days.append(cursor)
            cursor -= timedelta(days=1)
        days.reverse()
        stamps = [int(datetime.combine(d, time(9, 15), tzinfo=IST).timestamp()) for d in days]
        data = {"RELIANCE.NS": {"t": stamps, "c": [100] * 29 + [110],
                                 "v": None, "meta": {"symbol": "RELIANCE.NS", "currency": "INR",
                                                    "regularMarketTime": stamps[-1],
                                                    "regularMarketVolume": 200000}}}
        after = m.scan_daily(data, ["RELIANCE.NS"], m.parse_now("2026-10-29 16:00"), 20, 100000)
        intraday = m.scan_daily(data, ["RELIANCE.NS"], m.parse_now("2026-10-29 12:00"), 20, 100000)
        self.assertEqual(after[1]["scanned"], 1)
        self.assertEqual(intraday[1]["scanned"], 0)

    def test_wrong_currency_or_symbol_rejected(self):
        s = bars([100, 100, 100, 105, 106])
        wrong = chart("A.NS", s)
        wrong["chart"]["result"][0]["meta"]["currency"] = "USD"
        with self.assertRaises(m.ScanError):
            m.parse_chart(wrong, "A.NS")
        wrong["chart"]["result"][0]["meta"] = {"symbol": "B.NS", "currency": "INR"}
        with self.assertRaises(m.ScanError):
            m.parse_chart(wrong, "A.NS")

    def test_offline_orb_writes_handoff(self):
        s = bars([100, 100, 100, 105, 106])
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "RELIANCE.NS.json"
            out = Path(tmp) / "watchlist.json"
            source.write_text(json.dumps(chart("RELIANCE.NS", s)), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = m.main(["orb", "--offline", str(source), "--symbols", "RELIANCE",
                               "--now", "2026-10-28 09:40", "--at", "09:40",
                               "--out", str(out), "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["mode"], "orb")
            self.assertEqual(payload["hits"][0]["symbol"], "RELIANCE.NS")
            self.assertEqual(payload["as_of"], "2026-10-28 09:40 IST")
            self.assertEqual(payload["status"], "OK")
            self.assertEqual(payload["clock_mode"], "HISTORICAL_REPLAY")

    def test_offline_universe_uses_nse_eq_only(self):
        content = ("SYMBOL,NAME OF COMPANY,SERIES,DATE OF LISTING,PAID UP VALUE,MARKET LOT,ISIN NUMBER,FACE VALUE\n"
                   "RELIANCE,Reliance Industries,EQ,01-JAN-2000,10,1,INE002A01018,10\n"
                   "RELIANCE,Reliance Industries,BE,01-JAN-2000,10,1,INE002A01018,10\n")
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "EQUITY_L.csv"
            out = Path(tmp) / "universe.txt"
            csv_path.write_text(content, encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = m.main(["universe", "--offline", str(csv_path), "--out", str(out), "--json"])
            self.assertEqual(code, 0)
            self.assertIn("RELIANCE.NS|", out.read_text(encoding="utf-8"))
            self.assertEqual(len(out.read_text(encoding="utf-8").splitlines()), 1)

    def test_bse_universe_filters_cash_equity_and_duplicate_hash_symbol(self):
        csv_text = ("FinInstrmId,TckrSymb,SctySrs,FinInstrmNm,ISIN,NewBrdLotQty,SctyTpFlg,Sts\n"
                    "500325,RELIANCE,A,Reliance Industries,INE002A01018,1,EQ,A\n"
                    "100325,RELIANCE#,A,Reliance Industries,INE002A01018,1,EQ,A\n"
                    "501479,SPRINGFORM,P,Springform,INE000A01011,50,EQ,A\n"
                    "500111,EXAMPLE,A,Example,INE001A01012,1,EQ,S\n")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("SCRIP/BSE_EQ_SCRIP_29102026.csv", csv_text)
        rows, source_date = parse_bse_scrip_zip(buf.getvalue())
        self.assertEqual([r["ticker"] for r in rows], ["500325.BO"])
        self.assertEqual(source_date, "2026-10-29")


if __name__ == "__main__":
    unittest.main()
