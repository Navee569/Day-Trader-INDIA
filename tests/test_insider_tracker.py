"""Offline checks for official NSE PIT indexes and transaction XBRL."""
import contextlib
from datetime import datetime, date
import io
import json
from pathlib import Path
import tempfile
import unittest

from india_trader import insider_tracker as it
from india_trader.core import IST


CURRENT_URL = "https://nsearchives.nseindia.com/corporate/xbrl/IT_current.xml"
OLD_URL = "https://nsearchives.nseindia.com/corporate/xbrl/IT_old.xml"
NOW = datetime(2026, 11, 1, 12, tzinfo=IST)


def xbrl(old=False, two=False):
    main = "OneI" if old else "MainI"
    symbol = "NSESymbol" if old else "Symbol"
    regulation = "RegulationOfInsiderTrading" if old else "DisclosureUnderRegulation"
    regval = "7(2)" if old else "Regulation 7 (2)"
    names = ["A. Person", "B. Relative"] if two else ["A. Person"]
    contexts = "".join(f'<xbrli:context id="Disclosure{n}"><xbrli:entity><xbrli:identifier '
                       f'scheme="x">RELIANCE</xbrli:identifier></xbrli:entity></xbrli:context>'
                       for n in range(1, len(names)+1))
    rows = "".join(
        f'<i:NameOfThePerson contextRef="Disclosure{n}">{name}</i:NameOfThePerson>'
        f'<i:CategoryOfPerson contextRef="Disclosure{n}">Director</i:CategoryOfPerson>'
        f'<i:TypeOfInstrument contextRef="Disclosure{n}">Equity</i:TypeOfInstrument>'
        f'<i:SecuritiesAcquiredOrDisposedTransactionType contextRef="Disclosure{n}">{"Sell" if n==2 else "Buy"}</i:SecuritiesAcquiredOrDisposedTransactionType>'
        f'<i:SecuritiesAcquiredOrDisposedNumberOfSecurity contextRef="Disclosure{n}">100</i:SecuritiesAcquiredOrDisposedNumberOfSecurity>'
        f'<i:SecuritiesAcquiredOrDisposedValueOfSecurity contextRef="Disclosure{n}" unitRef="INR">5000</i:SecuritiesAcquiredOrDisposedValueOfSecurity>'
        f'<i:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate contextRef="Disclosure{n}">2026-10-27</i:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate>'
        f'<i:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate contextRef="Disclosure{n}">2026-10-28</i:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate>'
        f'<i:TypeOfDerivativeContract contextRef="Disclosure{n}">Call</i:TypeOfDerivativeContract>'
        for n, name in enumerate(names, 1))
    return (f'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:i="urn:fixture">'
            f'<i:{symbol} contextRef="{main}">RELIANCE</i:{symbol}>'
            f'<i:{regulation} contextRef="{main}">{regval}</i:{regulation}>'
            f'<i:DateOfFiling contextRef="{main}">2026-10-31</i:DateOfFiling>'
            f'{contexts}{rows}</xbrli:xbrl>').encode()


def current(appid="100", stamp="31-Oct-2026 10:00:00", url=CURRENT_URL):
    return {"appId": appid, "broadcastDateTime": stamp, "symbol": "RELIANCE",
            "companyName": "Reliance", "regulation": "Regulation 7 (2)", "typeOfSubmission": "Original",
            "xmlFileName": url, "ixbrl": "https://nsearchives.nseindia.com/corporate/ixbrl/IT_current.html"}


def archive():
    return {"did": "901", "date": "01-May-2026 10:00", "symbol": "RELIANCE",
            "company": "Reliance", "anex": "7(2)", "xbrl": OLD_URL}


class InsiderTests(unittest.TestCase):
    def test_both_xbrl_layouts_multiple_people_and_lag(self):
        for old in (False, True):
            filing = it._archive(archive()) if old else it._current(current())
            rows = it.parse_xml(xbrl(old, two=True), filing)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["person"], "B. Relative")
            self.assertEqual(rows[1]["transaction_type"], "Sell")
            self.assertEqual(rows[0]["disclosure_lag_calendar_days"], 3)
            self.assertEqual(rows[0]["value_unit_ref"], "INR")
            self.assertEqual(rows[0]["derivatives"]["TypeOfDerivativeContract"], "Call")

    def test_rejects_unsafe_url_wrong_symbol_and_doctype(self):
        self.assertIsNone(it._current(current(url="https://example.com/file.xml")))
        with self.assertRaises(it.InsiderError):
            it.parse_xml(xbrl().replace(b"RELIANCE", b"TCS"), it._current(current()))
        with self.assertRaises(it.InsiderError):
            it.parse_xml(b"<!DOCTYPE x [<!ENTITY a 'b'>]>" + xbrl())

    def test_filters_revision_and_as_of(self):
        class Fake:
            def index(self, old, symbol=None):
                return [archive()] if old else [current(),
                    dict(current("101", "01-Nov-2026 15:00:00"), typeOfSubmission="Revised", prevAppId="100")]

            def xml(self, url):
                return xbrl(url == OLD_URL)

        rows, errors = it.index(Fake(), ["RELIANCE"], (), None, 2026, "7 (2)", NOW)
        self.assertEqual([x["id"] for x in rows], ["current:100", "archive:901"])
        self.assertFalse(errors)
        people, _ = it.index(Fake(), ["RELIANCE"], ["relative"], None, None, None, NOW)
        self.assertEqual(people, [])
        people, _ = it.index(Fake(), ["RELIANCE"], ["person"], None, None, None, NOW)
        self.assertEqual(len(people), 2)

    def test_dated_archive_months_and_offline_name(self):
        calls = []
        class Fake:
            def index(self, old, symbol=None, start=None, end=None):
                calls.append((old, symbol, start, end))
                if old and start == date(2025, 9, 1):
                    return [dict(archive(), date="12-Sep-2025 19:02")]
                return []
        rows, errors = it.index(Fake(), ["RELIANCE"], since=date(2025, 9, 1),
                                year=2025, as_of=NOW)
        self.assertFalse(errors)
        self.assertEqual(rows[0]["filed_at"], "2025-09-12T19:02:00+05:30")
        self.assertIn((True, "RELIANCE", date(2025, 9, 1), date(2025, 9, 30)), calls)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            name = "archive-RELIANCE-2025-09-01-2025-09-30.json"
            (root / name).write_text(json.dumps({"data": [dict(archive(), date="12-Sep-2025 19:02")]}))
            self.assertEqual(len(it.Fetcher(root).index(True, "RELIANCE",
                           date(2025, 9, 1), date(2025, 9, 30))), 1)

    def test_offline_new_once_only_and_dry_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "xbrl").mkdir()
            (root / "current-RELIANCE.json").write_text(json.dumps({"data": [current()]}))
            (root / "archive-RELIANCE.json").write_text(json.dumps({"data": [archive()]}))
            (root / "xbrl" / "IT_current.xml").write_bytes(xbrl())
            (root / "xbrl" / "IT_old.xml").write_bytes(xbrl(True))
            state = root / "state.json"
            argv = ["new", "--offline", str(root), "--symbol", "RELIANCE", "--state", str(state),
                    "--as-of", NOW.isoformat(), "--read", "--json"]
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(it.main(argv + ["--dry-run"]), 0)
            self.assertEqual(json.loads(out.getvalue())["count"], 1)  # first 45 days
            self.assertFalse(state.exists())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(it.main(argv), 0)
            self.assertEqual(json.loads(out.getvalue())["filings"][0]["transactions"][0]["person"], "A. Person")
            self.assertEqual(json.loads(state.read_text())["seen"], ["current:100"])
            self.assertEqual(json.loads(state.read_text())["baseline_date"], "2026-09-17")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(it.main(argv), 1)
            self.assertEqual(json.loads(out.getvalue())["count"], 0)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(it.main(["fetch", "archive:901", "--offline", str(root), "--symbol", "RELIANCE", "--json"]), 0)
            self.assertEqual(json.loads(out.getvalue())["filings"][0]["transactions"][0]["person"], "A. Person")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(it.main(["parse", str(root / "xbrl" / "IT_old.xml"), "--symbol", "RELIANCE",
                                          "--filed", "2026-10-31", "--tickers", "RELIANCE", "--json"]), 0)
            self.assertTrue(json.loads(out.getvalue())["held_in_portfolio"])


if __name__ == "__main__":
    unittest.main()
