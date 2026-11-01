"""Offline checks for NSE XBRL quarterly earnings and its paper-only CLI."""
import contextlib
from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest

from india_trader import earnings_analyst as a
from india_trader.core import IST


NOW = datetime(2026, 11, 1, 12, tzinfo=IST)


def filing(end, name="RELIANCE", bank=False):
    return {"symbol": "HDFCBANK" if bank else "RELIANCE", "name": "Fixture Ltd", "period_end": end,
            "filed_at": "2026-10-30T21:00:00+05:30", "basis": "consolidated", "audited": "Unaudited",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/" + name + ".xml", "bank": bank}


def xml(start, end, revenue, net, eps, sym="RELIANCE", annual=False, bank=False):
    rev_tag = "Income" if bank else "RevenueFromOperations"
    net_tag = "ProfitLossForThePeriod" if bank else "ProfitLossForPeriod"
    eps_tag = ("DilutedEarningsPerShareAfterExtraordinaryItems" if bank else
               "DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations")
    op_tag = "OperatingProfitBeforeProvisionAndContingencies" if bank else "ProfitBeforeExceptionalItemsAndTax"
    extra = "" if bank else '<f:FinanceCosts contextRef="OneD" unitRef="INR">10</f:FinanceCosts><f:OtherIncome contextRef="OneD" unitRef="INR">5</f:OtherIncome>'
    return (f'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:f="urn:fixture">'
            f'<xbrli:context id="OneD"><xbrli:entity/><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>'
            f'<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>'
            f'<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>'
            f'<xbrli:unit id="INRPerShare"><xbrli:divide><xbrli:unitNumerator><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unitNumerator>'
            f'<xbrli:unitDenominator><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unitDenominator></xbrli:divide></xbrli:unit>'
            f'<f:Symbol contextRef="OneD">{sym}</f:Symbol>'
            f'<f:DescriptionOfPresentationCurrency contextRef="OneD">INR</f:DescriptionOfPresentationCurrency>'
            f'<f:NatureOfReportStandaloneConsolidated contextRef="OneD">Consolidated</f:NatureOfReportStandaloneConsolidated>'
            f'<f:{rev_tag} contextRef="OneD" unitRef="INR">{revenue}</f:{rev_tag}>'
            f'<f:{net_tag} contextRef="OneD" unitRef="INR">{net}</f:{net_tag}>'
            f'<f:{op_tag} contextRef="OneD" unitRef="INR">30</f:{op_tag}>'
            f'{extra}<f:{eps_tag} contextRef="OneD" unitRef="INRPerShare">{eps}</f:{eps_tag}>'
            f'</xbrli:xbrl>').encode()


class EarningsTests(unittest.TestCase):
    def test_index_filters_basis_company_type_future_and_revision(self):
        url = filing("2026-09-30")["xbrl"]
        rows = [{"symbol": "RELIANCE", "type": "Integrated Filing- Financials",
                 "consolidated": "Consolidated", "qe_Date": "30-SEP-2026",
                 "broadcast_Date": "30-Oct-2026 21:00:00", "xbrl": url,
                 "cmName": "Reliance Industries"},
                {"symbol": "RELIANCE", "type": "Integrated Filing- Governance",
                 "consolidated": "Consolidated", "qe_Date": "30-SEP-2026",
                 "broadcast_Date": "30-Oct-2026 21:01:00", "xbrl": url},
                {"symbol": "RELIANCE", "type": "Integrated Filing- Financials",
                 "consolidated": "Standalone", "qe_Date": "30-SEP-2026",
                 "broadcast_Date": "30-Oct-2026 21:02:00", "xbrl": url},
                {"symbol": "TCS", "type": "Integrated Filing- Financials",
                 "consolidated": "Consolidated", "qe_Date": "30-SEP-2026",
                 "broadcast_Date": "30-Oct-2026 21:03:00", "xbrl": url}]
        self.assertEqual(len(a.filings(rows, "RELIANCE", "consolidated", "integrated", NOW)), 1)
        earlier = datetime(2026, 10, 29, 12, tzinfo=IST)
        self.assertEqual(a.filings(rows, "RELIANCE", "consolidated", "integrated", earlier), [])
        self.assertEqual(len(a.filings(rows, "RELIANCE", "standalone", "integrated", NOW)), 1)
        revised = dict(rows[0], revised_Date="02-Nov-2026 09:00:00", type_Sub="Revised")
        self.assertEqual(a.filings([revised], "RELIANCE", "consolidated", "integrated", NOW), [])

    def test_xbrl_rejects_mismatch_and_ignores_segment_context(self):
        f = filing("2026-09-30")
        data = xml("2026-07-01", "2026-09-30", 100, 20, 2)
        parsed = a.parse_xbrl(data, f)
        self.assertEqual(parsed[0]["facts"]["revenue"], 100)
        with self.assertRaises(a.EarningsError):
            a.parse_xbrl(data.replace(b"RELIANCE", b"TCS"), f)
        with self.assertRaises(a.EarningsError):
            a.parse_xbrl(b"<!DOCTYPE x [<!ENTITY bad 'x'>]>" + data, f)
        with self.assertRaises(a.EarningsError):
            a.parse_xbrl(data.replace(b">INR</f:Description", b">USD</f:Description"), f)
        with self.assertRaises(a.EarningsError):
            a.parse_xbrl(data, dict(f, expected_isin="INE002A01018"))

    def test_same_quarter_growth_ttm_missing_gross_and_bank_fields(self):
        dates = [("2025-10-01", "2025-12-31", 90, 9, 0.9),
                 ("2026-01-01", "2026-03-31", 95, 10, 1),
                 ("2026-04-01", "2026-06-30", 110, 11, 1.1),
                 ("2026-07-01", "2026-09-30", 120, 12, 1.2),
                 ("2025-07-01", "2025-09-30", 100, 10, 1)]
        rows = []
        for start, end, rev, net, eps in dates:
            rows += a.parse_xbrl(xml(start, end, rev, net, eps), filing(end))
        result = a.analyse("RELIANCE", rows, quarters=5, as_of=NOW)
        self.assertEqual(result["rows"][0]["revenue_yoy_pct"], 20)
        self.assertEqual(result["rows"][0]["net_margin_pct"], 10)
        self.assertIsNone(result["rows"][0]["gross_margin_pct"])
        self.assertEqual(result["ttm"]["revenue_inr"], 415)
        self.assertIn("derived_ebit_proxy", result["rows"][0]["operating_income_source"])
        bank = a.parse_xbrl(xml("2026-07-01", "2026-09-30", 120, 12, 1.2,
                                sym="HDFCBANK", bank=True), filing("2026-09-30", "BANK", True))
        bank_result = a.analyse("HDFCBANK", bank, as_of=NOW)
        self.assertEqual(bank_result["rows"][0]["revenue_definition"], "bank_total_income")
        self.assertEqual(bank_result["rows"][0]["operating_income_source"],
                         "filed_bank_pre_provision_operating_profit")

    def test_derive_fourth_quarter_from_full_year_only_with_three_quarters(self):
        rows = []
        for start, end, rev in (("2025-04-01", "2025-06-30", 100),
                                ("2025-07-01", "2025-09-30", 110),
                                ("2025-10-01", "2025-12-31", 120),
                                ("2025-04-01", "2026-03-31", 460)):
            rows += a.parse_xbrl(xml(start, end, rev, rev / 10, 1), filing(end))
        result = a.analyse("RELIANCE", rows, quarters=4, as_of=NOW)
        self.assertTrue(result["rows"][0]["derived"])
        self.assertEqual(result["rows"][0]["revenue_inr"], 130)
        self.assertIn("approximate", " ".join(result["notes"]))
        with self.assertRaises(a.EarningsError):
            a.analyse("RELIANCE", [rows[-1]], as_of=NOW)
        with self.assertRaises(a.EarningsError):
            a.analyse("RELIANCE", [dict(rows[0], facts={"other_income": 5})], as_of=NOW)

    def test_release_link_requires_same_symbol_cycle_and_official_pdf(self):
        good = {"symbol": "RELIANCE", "an_dt": "30-Oct-2026 19:43:24",
                "desc": "Press Release", "attchmntText": "Results for quarter ended September 2026",
                "attchmntFile": "https://nsearchives.nseindia.com/corporate/results.pdf"}
        wrong = dict(good, symbol="TCS")
        unsafe = dict(good, attchmntFile="https://example.com/results.pdf")
        links = a.release_links([wrong, unsafe, good], "RELIANCE", filing("2026-09-30"), NOW)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["kind"], "earnings_press_release_candidate")

    def test_cli_offline_watchlist_json_and_metrics_merge(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "xbrl").mkdir()
            records = []
            for start, end, rev in (("2025-07-01", "2025-09-30", 100),
                                    ("2025-10-01", "2025-12-31", 105),
                                    ("2026-01-01", "2026-03-31", 110),
                                    ("2026-04-01", "2026-06-30", 115),
                                    ("2026-07-01", "2026-09-30", 120)):
                name = "R" + end + ".xml"
                (root / "xbrl" / name).write_bytes(xml(start, end, rev, rev / 10, 1))
                records.append({"symbol": "RELIANCE", "cmName": "Fixture Ltd",
                    "type": "Integrated Filing- Financials", "consolidated": "Consolidated",
                    "qe_Date": a._day(end).strftime("%d-%b-%Y"), "broadcast_Date": "30-Oct-2026 21:00:00",
                    "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/" + name})
            (root / "integrated-RELIANCE.json").write_text(json.dumps({"data": records}))
            (root / "quarterly-RELIANCE.json").write_text("[]")
            (root / "annual-RELIANCE.json").write_text("[]")
            (root / "announcements-RELIANCE.json").write_text(json.dumps([{
                "symbol": "RELIANCE", "an_dt": "30-Oct-2026 19:43:24", "desc": "Press Release",
                "attchmntText": "Financial results for quarter ended September 2026",
                "attchmntFile": "https://nsearchives.nseindia.com/corporate/results.pdf"}]))
            watch = root / "watchlist.json"
            watch.write_text(json.dumps({"hits": [{"symbol": "RELIANCE.NS"}]}))
            metrics = root / "metrics.json"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = a.main(["--watchlist", str(watch), "--offline", str(root),
                             "--as-of", NOW.isoformat(), "--quarters", "5",
                             "--metrics", str(metrics), "--json"])
            self.assertEqual(rc, 0)
            doc = json.loads(output.getvalue())
            self.assertEqual(doc["results"][0]["rows"][0]["revenue_yoy_pct"], 20)
            self.assertEqual(json.loads(metrics.read_text())["companies"][0]["revenue_ttm_inr"], 450)
            self.assertFalse(doc["results"][0]["orders_possible"])
            self.assertEqual(len(doc["results"][0]["release_links"]), 1)

    def test_isin_lookup_uses_saved_nse_universe(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "universe.txt"
            source.write_text("RELIANCE.NS|Reliance Industries|INE002A01018\n")
            self.assertEqual(a._ticker_for_isin("INE002A01018", source), "RELIANCE.NS")
            with self.assertRaises(a.EarningsError):
                a._ticker_for_isin("INE002A01019", source)


if __name__ == "__main__":
    unittest.main()
