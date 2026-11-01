"""BSE public PIT search and inline XBRL replay, with no network."""
import contextlib
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from india_trader import bse_insiders as b
from india_trader import insider_tracker as it


URL = "https://www.bseindia.com/XBRLFILES/PITXBRLDataXML/PIT_539289_30102026072235_PIT.html"
PAGE = ("<html><body>You are viewing page 1 of 1"
        '<table id="ContentPlaceHolder1_grdxbrdetails">'
        '<tr class="TTRow"><td>539289</td><td>Aurum PropTech Ltd</td><td>2026-2027</td>'
        '<td></td><td></td><td><a href="' + URL + '">XBRL</a></td>'
        '<td>30/10/2026 19:22:35</td></tr></table></body></html>').encode()
FACTS = ("<html><body>"
         "<ix:nonNumeric name='in-bse-co:ScripCode' contextRef='MainI'>539289</ix:nonNumeric>"
         "<ix:nonNumeric name='in-bse-co:Symbol' contextRef='MainI'>AURUM</ix:nonNumeric>"
         "<ix:nonNumeric name='in-bse-co:DateOfFiling' contextRef='MainI'>30-10-2026</ix:nonNumeric>"
         "<ix:nonNumeric name='in-bse-co:NameOfThePerson' contextRef='Disclosure1'>Aurum RealEstate Developers</ix:nonNumeric>"
         "<ix:nonNumeric name='in-bse-co:CategoryOfPerson' contextRef='Disclosure1'>Promoter</ix:nonNumeric>"
         "<ix:nonNumeric name='in-bse-co:TypeOfInstrument' contextRef='Disclosure1'>Equity</ix:nonNumeric>"
         "<ix:nonFraction name='in-bse-co:SecuritiesAcquiredOrDisposedNumberOfSecurity' contextRef='Disclosure1' unitRef='shares'>10000</ix:nonFraction>"
         "<ix:nonFraction name='in-bse-co:SecuritiesAcquiredOrDisposedValueOfSecurity' contextRef='Disclosure1' unitRef='INR' scale='0'>20,27,042.00</ix:nonFraction>"
         "<ix:nonNumeric name='in-bse-co:SecuritiesAcquiredOrDisposedTransactionType' contextRef='Disclosure1'>Buy</ix:nonNumeric>"
         "<ix:nonNumeric name='in-bse-co:DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate' contextRef='Disclosure1'>30-10-2026</ix:nonNumeric>"
         "</body></html>").encode()


class BseInsiderTests(unittest.TestCase):
    def test_index_link_identity_and_inline_transaction(self):
        rows, pages = b.parse_index_html(PAGE, "539289")
        self.assertEqual(pages, 1)
        self.assertEqual(rows[0]["symbol"], "539289.BO")
        self.assertEqual(rows[0]["view_url"], URL)
        found = b.parse_ixbrl(FACTS, rows[0])
        self.assertEqual(found[0]["person"], "Aurum RealEstate Developers")
        self.assertEqual(found[0]["value_reported"], "2027042.00")
        self.assertEqual(found[0]["value_unit_ref"], "INR")
        self.assertEqual(found[0]["disclosure_lag_calendar_days"], 0)
        with self.assertRaises(b.BseError):
            b.parse_ixbrl(FACTS.replace(b"539289", b"590003"), rows[0])
        with self.assertRaises(b.BseError):
            b.filing_url("https://example.com/PIT_539289_bad.html")

    def test_pagination_splits_period_instead_of_omitting_rows(self):
        with patch.object(b, "_one_index", side_effect=[([], 2), ([{"id": "a"}], 1),
                                                       ([{"id": "b"}], 1)]) as one:
            rows = b._range(None, "539289", date(2026, 10, 30), date(2026, 10, 31), None)
        self.assertEqual([r["id"] for r in rows], ["a", "b"])
        self.assertEqual(one.call_count, 3)
        with patch.object(b, "_one_index", return_value=([], 2)):
            with self.assertRaises(b.BseError):
                b._range(None, "539289", date(2026, 10, 30), date(2026, 10, 30), None)

    def test_offline_cli_index_read_and_once_only_new(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "bse-ixbrl").mkdir()
            (root / "bse-539289-2026-10-30-2026-10-31.html").write_bytes(PAGE)
            (root / "bse-ixbrl" / URL.rsplit("/", 1)[-1]).write_bytes(FACTS)
            base = ["--exchange", "BSE", "--symbol", "539289", "--since", "2026-10-30",
                    "--as-of", "2026-10-31T23:00:00+05:30", "--offline", str(root), "--read", "--json"]
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(it.main(["index", *base]), 0)
            self.assertEqual(json.loads(out.getvalue())["filings"][0]["transactions"][0]["quantity"], "10000")
            state = root / "state.json"
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(it.main(["new", *base, "--state", str(state)]), 0)
            self.assertEqual(json.loads(state.read_text())["seen"], ["bse:PIT_539289_30102026072235_PIT"])
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(it.main(["new", *base, "--state", str(state)]), 1)
            self.assertEqual(json.loads(out.getvalue())["count"], 0)


if __name__ == "__main__":
    unittest.main()
