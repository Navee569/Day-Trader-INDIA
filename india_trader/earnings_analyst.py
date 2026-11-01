"""NSE filed XBRL earnings analyst for Indian cash equities; research only."""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import http.cookiejar
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .budget_manager import ticker as canonical_ticker
from .core import DataError, IST
from .universe import load_nse_equities


class EarningsError(Exception):
    pass


API = "https://www.nseindia.com/api/"
PAGE = "https://www.nseindia.com/companies-listing/corporate-integrated-filing"
OLD_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
MAX_INDEX = 3_000_000
MAX_ANNOUNCEMENTS = 6_000_000
MAX_XBRL = 12_000_000
XBRLI = "{http://www.xbrl.org/2003/instance}"
MONEY_TAGS = {
    "revenue": ("RevenueFromOperations", "RevenueFromOperationsNet", "Revenue", "Income"),
    "gross_profit": ("GrossProfit",),
    "operating_income": ("OperatingProfitLoss", "OperatingIncomeLoss",
                         "OperatingProfitBeforeProvisionAndContingencies"),
    "net_income": ("ProfitLossForPeriod", "ProfitLossForPeriodFromContinuingOperations",
                   "ProfitLossForThePeriod"),
    "profit_before_tax": ("ProfitBeforeTax",),
    "finance_costs": ("FinanceCosts",),
    "other_income": ("OtherIncome",),
    "profit_before_exceptional": ("ProfitBeforeExceptionalItemsAndTax",),
}
EPS_TAGS = ("DilutedEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "DilutedEarningsLossPerShareFromContinuingOperations", "DilutedEarningsLossPerShare",
            "DilutedEarningsPerShareAfterExtraordinaryItems")
MONEY_FIELDS = ("revenue", "gross_profit", "operating_income", "net_income")
METRIC_FIELDS = MONEY_FIELDS + ("eps_diluted",)


def _day(value):
    for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            pass
    raise EarningsError(f"invalid filing date: {value!r}")


def _stamp(value):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            d = datetime.strptime(str(value), fmt)
            return d if d.tzinfo else d.replace(tzinfo=IST)
        except ValueError:
            pass
    raise EarningsError(f"invalid filing timestamp: {value!r}")


def _cutoff(value):
    if value is None:
        return datetime.now(IST)
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EarningsError("--as-of needs an offset-aware ISO timestamp") from exc
    if stamp.tzinfo is None:
        raise EarningsError("--as-of needs an offset-aware ISO timestamp")
    return stamp.astimezone(IST)


def _url(url):
    p = urllib.parse.urlsplit(str(url))
    if (p.scheme != "https" or p.hostname != "nsearchives.nseindia.com" or
            not p.path.startswith("/corporate/xbrl/") or not p.path.lower().endswith(".xml") or
            not re.fullmatch(r"[A-Za-z0-9_.-]+", p.path.rsplit("/", 1)[-1])):
        raise EarningsError("filing has no usable official NSE XBRL URL")
    return p.geturl()


def _attachment(url):
    p = urllib.parse.urlsplit(str(url or ""))
    if p.scheme == "https" and p.hostname == "nsearchives.nseindia.com" and \
            p.path.startswith("/corporate/") and not p.path.endswith("/null") and \
            p.path.lower().endswith((".pdf", ".html", ".htm")):
        return p.geturl()
    return None


class Fetcher:
    def __init__(self, offline=None):
        self.offline = Path(offline) if offline else None
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.last = 0.0
        self.ready = False

    def get(self, url, offline_name, limit):
        if self.offline:
            path = self.offline / offline_name
            if not path.is_file():
                raise EarningsError(f"offline source missing: {path}")
            if path.stat().st_size > limit:
                raise EarningsError(f"offline source exceeds size limit: {path}")
            return path.read_bytes()
        if not self.ready:
            self._request(PAGE, MAX_INDEX)
            self.ready = True
        return self._request(url, limit)

    def _request(self, url, limit):
        delay = 0.2 - (time.monotonic() - self.last)
        if delay > 0:
            time.sleep(delay)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; IndiaTraderPaperResearch/0.2.1)",
            "Accept": "application/json, application/xml, text/xml, */*",
            "Referer": ("https://www.nseindia.com/companies-listing/corporate-filings-announcements"
                        if "corporate-announcements" in url else PAGE if "integrated" in url else OLD_PAGE)})
        try:
            with self.opener.open(req, timeout=20) as response:
                data = response.read(limit + 1)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise EarningsError(f"public NSE source unavailable: {url}: {exc}") from exc
        self.last = time.monotonic()
        if len(data) > limit:
            raise EarningsError(f"public NSE response exceeds size limit: {url}")
        return data

    def index(self, sym, kind):
        path = ("integrated-filing-results" if kind == "integrated" else "corporates-financial-results")
        query = {"index": "equities", "symbol": sym}
        if kind != "integrated":
            query["period"] = "Quarterly" if kind == "quarterly" else "Annual"
        url = API + path + "?" + urllib.parse.urlencode(query)
        raw = self.get(url, f"{kind}-{sym}.json", MAX_INDEX)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise EarningsError(f"invalid NSE {kind} filing index") from exc
        rows = data.get("data") if kind == "integrated" and isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise EarningsError(f"invalid NSE {kind} filing list")
        return rows

    def xbrl(self, url):
        url = _url(url)
        name = "xbrl/" + urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        return self.get(url, name, MAX_XBRL)

    def announcements(self, sym):
        url = API + "corporate-announcements?" + urllib.parse.urlencode({"index": "equities", "symbol": sym})
        try:
            rows = json.loads(self.get(url, f"announcements-{sym}.json", MAX_ANNOUNCEMENTS))
        except (ValueError, UnicodeDecodeError) as exc:
            raise EarningsError("invalid NSE announcement index") from exc
        if not isinstance(rows, list):
            raise EarningsError("invalid NSE announcement list")
        return rows


def filings(rows, sym, basis, kind, as_of):
    """Normalize one official index and keep only the requested company and basis."""
    out = []
    for item in rows:
        if not isinstance(item, dict) or str(item.get("symbol", "")).upper() != sym:
            continue
        if kind == "integrated" and item.get("type") != "Integrated Filing- Financials":
            continue
        if kind in ("quarterly", "annual") and str(item.get("period", "")).lower() != kind:
            continue
        reporting = str(item.get("consolidated") or "").lower()
        if basis == "consolidated" and reporting != "consolidated":
            continue
        if basis == "standalone" and reporting not in ("standalone", "non-consolidated"):
            continue
        try:
            end = _day(item.get("qe_Date") if kind == "integrated" else item.get("toDate"))
            published = _stamp((item.get("revised_Date") or item.get("broadcast_Date")) if kind == "integrated" else
                               item.get("broadCastDate") or item.get("filingDate"))
            link = _url(item.get("xbrl"))
        except EarningsError:
            continue
        if published > as_of or end > as_of.date():
            continue
        out.append({"symbol": sym, "name": item.get("cmName") or item.get("companyName") or sym,
                    "period_end": end.isoformat(), "filed_at": published.isoformat(),
                    "basis": basis, "source_kind": kind, "xbrl": link,
                    "revised": bool(item.get("revised_Date") or item.get("revision_Remark") or
                                    str(item.get("type_Sub", "")).lower() == "revised"),
                    "isin": item.get("isin"), "audited": item.get("audited"),
                    "bank": "BANKING" in link.upper() or str(item.get("bank", "")).upper() == "B",
                    "pdf_url": _attachment(item.get("pdf_attach")),
                    "view_url": _attachment(item.get("ixbrl"))})
    # A later amended filing supersedes the earlier copy; integrated wins an exact tie.
    out.sort(key=lambda x: (x["period_end"], x["filed_at"], x["source_kind"] == "integrated"), reverse=True)
    return out


def release_links(items, sym, latest_filing, as_of):
    """Attach likely same-cycle NSE release documents without interpreting their text."""
    filed = _stamp(latest_filing["filed_at"])
    found = []
    for item in items:
        if not isinstance(item, dict) or str(item.get("symbol", "")).upper() != sym:
            continue
        try:
            published = _stamp(item.get("an_dt"))
        except EarningsError:
            continue
        if published > as_of or abs((published - filed).total_seconds()) > 4 * 86400:
            continue
        url = _attachment(item.get("attchmntFile"))
        if not url:
            continue
        category = str(item.get("desc") or "").lower()
        context = str(item.get("attchmntText") or "").lower()
        if "press release" in category and re.search(r"financial results|quarter.*ended|earnings", context):
            kind, rank = "earnings_press_release_candidate", 0
        elif "financial results" in category:
            kind, rank = "financial_results_pdf_candidate", 1
        elif "investor presentation" in category and re.search(r"quarter|financial results|earnings", context):
            kind, rank = "investor_presentation_candidate", 2
        else:
            continue
        found.append({"kind": kind, "published_at": published.isoformat(), "url": url,
                      "source": "NSE corporate announcement", "match": "same symbol and within four days of filing" ,
                      "rank": rank})
    found.sort(key=lambda x: (x["rank"], -_stamp(x["published_at"]).timestamp()))
    unique, seen = [], set()
    for item in found:
        if item["url"] not in seen:
            item.pop("rank")
            unique.append(item)
            seen.add(item["url"])
    return unique[:5]


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _number(text):
    try:
        value = Decimal(str(text).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EarningsError("filed financial value is invalid") from exc
    if not value.is_finite() or abs(value) > Decimal("1e20"):
        raise EarningsError("filed financial value is nonfinite or implausibly large")
    return value


def parse_xbrl(raw, filing):
    """Extract unsegmented three-month and full-year INR facts from one filing."""
    if len(raw) > MAX_XBRL or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise EarningsError("XBRL has an unsafe or oversized XML envelope")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise EarningsError("invalid NSE XBRL XML") from exc
    if root.tag != XBRLI + "xbrl":
        raise EarningsError("not an XBRL instance")
    units = {node.get("id"): node for node in root.findall(XBRLI + "unit")}
    inr = units.get("INR")
    per_share = units.get("INRPerShare")
    if inr is None or inr.findtext(XBRLI + "measure") != "iso4217:INR":
        raise EarningsError("XBRL INR unit is missing or inconsistent")
    if per_share is None or (per_share.findtext(XBRLI + "divide/" + XBRLI + "unitNumerator/" +
                                                  XBRLI + "measure") != "iso4217:INR") or \
            (per_share.findtext(XBRLI + "divide/" + XBRLI + "unitDenominator/" +
                                XBRLI + "measure") != "xbrli:shares"):
        raise EarningsError("XBRL INR-per-share unit is missing or inconsistent")
    contexts = {}
    for node in root.findall(XBRLI + "context"):
        period = node.find(XBRLI + "period")
        if period is None or node.find(XBRLI + "scenario") is not None:
            continue
        start = period.findtext(XBRLI + "startDate")
        end = period.findtext(XBRLI + "endDate")
        if start and end:
            try:
                days = (_day(end) - _day(start)).days + 1
            except EarningsError:
                continue
            if 80 <= days <= 100 or 350 <= days <= 380:
                contexts[node.get("id")] = (start, end, "quarter" if days <= 100 else "annual")
    if not contexts:
        raise EarningsError("XBRL has no unsegmented quarterly or annual duration facts")
    rows = {}
    identities = set()
    isins = set()
    currencies = set()
    bases = set()
    for node in root:
        name = _local(node.tag)
        cref = node.get("contextRef")
        if name == "Symbol" and node.text:
            identities.add(node.text.strip().upper())
        if name == "ISIN" and node.text:
            isins.add(node.text.strip().upper())
        if name == "DescriptionOfPresentationCurrency" and node.text:
            currencies.add(node.text.strip().upper())
        if name == "NatureOfReportStandaloneConsolidated" and node.text:
            bases.add(node.text.strip().lower())
        if cref not in contexts:
            continue
        key = contexts[cref]
        metric = next((k for k, choices in MONEY_TAGS.items() if name in choices), None)
        if name in ("Income", "OperatingProfitBeforeProvisionAndContingencies") and not filing["bank"]:
            continue
        if name in EPS_TAGS:
            metric = "eps_diluted"
        if metric is None or node.text is None:
            continue
        unit = node.get("unitRef", "")
        if metric == "eps_diluted" and unit != "INRPerShare":
            continue
        if metric != "eps_diluted" and unit != "INR":
            continue
        row = rows.setdefault(key, {"start": key[0], "end": key[1], "period_type": key[2],
                                    "facts": {}, "fact_tags": {}, "filing": filing})
        if metric not in row["facts"]:
            row["facts"][metric] = _number(node.text)
            row["fact_tags"][metric] = name
    if filing["symbol"] not in identities:
        raise EarningsError("XBRL symbol does not match the NSE filing index")
    if filing.get("expected_isin") and filing["expected_isin"] not in isins:
        raise EarningsError("XBRL ISIN does not match the NSE equity universe")
    if currencies != {"INR"}:
        raise EarningsError("XBRL reporting currency is not INR")
    if filing["basis"] not in bases and not (filing["basis"] == "standalone" and
                                                     "non-consolidated" in bases):
        raise EarningsError("XBRL reporting basis does not match the NSE filing index")
    if not rows:
        raise EarningsError("XBRL has no usable INR earnings facts")
    return list(rows.values())


def _derive_q4(rows):
    quarterly = {r["end"]: r for r in rows if r["period_type"] == "quarter" and
                 ("revenue" in r["facts"] or "net_income" in r["facts"])}
    for year in (r for r in rows if r["period_type"] == "annual"):
        if year["end"] in quarterly:
            continue
        inside = sorted((r for r in quarterly.values() if year["start"] <= r["start"] and
                         r["end"] < year["end"]), key=lambda r: r["end"])
        if len(inside) != 3:
            continue
        if any((_day(b["start"]) - _day(a["end"])).days != 1 for a, b in zip(inside, inside[1:])):
            continue
        facts, tags = {}, {}
        for metric in METRIC_FIELDS:
            if metric in year["facts"] and all(metric in r["facts"] for r in inside):
                facts[metric] = year["facts"][metric] - sum(r["facts"][metric] for r in inside)
                tags[metric] = "annual_minus_first_three_quarters"
        if "revenue" in facts or "net_income" in facts:
            quarterly[year["end"]] = {"start": (_day(inside[-1]["end"]) + timedelta(days=1)).isoformat(),
                                      "end": year["end"], "period_type": "quarter", "facts": facts,
                                      "fact_tags": tags, "filing": year["filing"], "derived": True,
                                      "components": [r["filing"]["xbrl"] for r in inside]}
    return quarterly


def _pct(a, b):
    return round(float((a - b) / abs(b) * 100), 1) if a is not None and b not in (None, 0) else None


def _margin(part, whole):
    return round(float(part / whole * 100), 1) if part is not None and whole not in (None, 0) else None


def _same_year(series, end):
    target = _day(end) - timedelta(days=365)
    matches = [(abs((_day(e) - target).days), r) for e, r in series.items()
               if abs((_day(e) - target).days) <= 10]
    return min(matches, key=lambda x: x[0])[1] if matches else None


def analyse(symbol, source_rows, quarters=6, as_of=None):
    """Create like-for-like quarter and TTM comparisons without inventing missing facts."""
    if not 1 <= quarters <= 20:
        raise EarningsError("--quarters must be between 1 and 20")
    # The list is already ordered newest first. One filing may contain comparative contexts.
    chosen = {}
    for row in source_rows:
        key = (row["start"], row["end"], row["period_type"])
        old = chosen.get(key)
        if old is None or row["filing"]["filed_at"] > old["filing"]["filed_at"]:
            chosen[key] = row
    q = _derive_q4(list(chosen.values()))
    ends = sorted(q, reverse=True)[:quarters]
    if not ends:
        raise EarningsError("no comparable quarterly facts in the selected filings")
    out = []
    for end in ends:
        row = q[end]
        f = row["facts"]
        rev, net, eps = f.get("revenue"), f.get("net_income"), f.get("eps_diluted")
        prior = _same_year(q, end)
        prior_f = prior["facts"] if prior else {}
        op, op_source = f.get("operating_income"), "filed" if f.get("operating_income") is not None else None
        if op is not None and row["fact_tags"].get("operating_income") == "OperatingProfitBeforeProvisionAndContingencies":
            op_source = "filed_bank_pre_provision_operating_profit"
        if op is None and all(x in f for x in ("profit_before_exceptional", "finance_costs", "other_income")):
            op = f["profit_before_exceptional"] + f["finance_costs"] - f["other_income"]
            op_source = "derived_ebit_proxy_from_pre_exceptional_profit_plus_finance_costs_minus_other_income"
        out.append({"start": row["start"], "end": end,
                    "fiscal": f"FY{(_day(end).year + (1 if _day(end).month >= 4 else 0)) % 100:02d}",
                    "basis": row["filing"]["basis"], "audited": row["filing"]["audited"],
                    "revenue_definition": "bank_total_income" if row["filing"]["bank"] else "revenue_from_operations",
                    "derived": bool(row.get("derived")), "revenue_inr": float(rev) if rev is not None else None,
                    "gross_profit_inr": float(f["gross_profit"]) if "gross_profit" in f else None,
                    "operating_income_inr": float(op) if op is not None else None,
                    "operating_income_source": op_source,
                    "net_income_inr": float(net) if net is not None else None,
                    "eps_diluted_inr": float(eps) if eps is not None else None,
                    "gross_margin_pct": _margin(f.get("gross_profit"), rev),
                    "operating_margin_pct": _margin(op, rev), "net_margin_pct": _margin(net, rev),
                    "revenue_yoy_pct": _pct(rev, prior_f.get("revenue")),
                    "net_income_yoy_pct": _pct(net, prior_f.get("net_income")),
                    "eps_yoy_pct": _pct(eps, prior_f.get("eps_diluted")),
                    "filed_at": row["filing"]["filed_at"], "filing_url": row["filing"]["xbrl"],
                    "filing_pdf_url": row["filing"].get("pdf_url"),
                    "filing_view_url": row["filing"].get("view_url"),
                    "fact_tags": row["fact_tags"], "derivation_sources": row.get("components", [])})
    ttm = {}
    top = [q[e] for e in sorted(q, reverse=True)[:4]]
    if len(top) == 4 and all(65 <= (_day(a["end"]) - _day(b["end"])).days <= 115
                             for a, b in zip(top, top[1:])):
        for metric in ("revenue", "net_income", "eps_diluted"):
            if all(metric in row["facts"] for row in top):
                ttm[metric + "_inr"] = float(sum(row["facts"][metric] for row in top))
    latest = out[0]
    notes = []
    if latest["derived"]:
        notes.append("Latest quarter is annual result minus three earlier quarters; derived EPS is approximate.")
    if latest["net_income_inr"] is not None and latest["operating_income_inr"] is not None and \
            latest["net_income_inr"] > latest["operating_income_inr"]:
        notes.append("Net income exceeds the operating-income proxy; inspect other income, tax and one-offs.")
    if latest["gross_margin_pct"] is None:
        notes.append("Gross margin is unavailable because no comparable gross-profit fact was filed.")
    if latest["revenue_definition"] == "bank_total_income":
        notes.append("For this bank, the revenue column is filed total income; operating margin uses filed pre-provision operating profit when available.")
    if latest["operating_income_source"] and latest["operating_income_source"].startswith("derived"):
        notes.append("Operating margin uses a labeled pre-exceptional EBIT proxy, not a filed operating-profit fact.")
    if len(q) < 4:
        notes.append("Fewer than four complete quarterly periods; TTM cannot be calculated.")
    return {"mode": "PAPER_RESEARCH_ONLY", "ticker": symbol + ".NS", "entity": source_rows[0]["filing"]["name"],
            "basis": source_rows[0]["filing"]["basis"], "as_of": (as_of or datetime.now(IST)).isoformat(),
            "rows": out, "ttm": ttm, "notes": notes, "orders_possible": False}


def _metrics_row(result):
    row = result["rows"][0]
    return {"ticker": result["ticker"], "name": result["entity"], "basis": result["basis"],
            "period_end": row["end"], "revenue_ttm_inr": result["ttm"].get("revenue_inr"),
            "net_income_ttm_inr": result["ttm"].get("net_income_inr"),
            "eps_ttm_inr": result["ttm"].get("eps_diluted_inr"),
            "revenue_growth_yoy_pct": row["revenue_yoy_pct"], "eps_growth_yoy_pct": row["eps_yoy_pct"],
            "net_income_growth_yoy_pct": row["net_income_yoy_pct"],
            "gross_margin_pct": row["gross_margin_pct"],
            "operating_margin_pct": row["operating_margin_pct"], "net_margin_pct": row["net_margin_pct"],
            "source": "NSE filed XBRL", "filing_url": row["filing_url"]}


def _write(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _ticker_for_isin(isin, universe=None, offline=None):
    if not re.fullmatch(r"IN[A-Z0-9]{10}", isin):
        raise EarningsError("--isin needs a 12-character Indian ISIN")
    path = Path(universe) if universe else Path(offline) / "EQUITY_L.csv" if offline else None
    if path and path.suffix.lower() == ".txt":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            parts = line.split("|")
            if len(parts) >= 3:
                rows.append({"ticker": parts[0], "isin": parts[2]})
    else:
        try:
            rows, _ = load_nse_equities(str(path) if path else None)
        except (DataError, OSError, urllib.error.URLError) as exc:
            raise EarningsError("NSE equity universe is unavailable for ISIN lookup") from exc
    matches = [row["ticker"] for row in rows if row.get("isin") == isin]
    if len(matches) != 1:
        raise EarningsError("ISIN did not resolve to one NSE EQ ticker")
    return matches[0]


def _watchlist(path):
    p = Path(path)
    if not p.is_file():
        raise EarningsError("give tickers or a saved scanner watchlist")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        return [x.get("symbol") or x.get("ticker") for x in doc["hits"] if isinstance(x, dict)]
    except (ValueError, KeyError, TypeError) as exc:
        raise EarningsError("watchlist needs hits[].symbol") from exc


def _render(result):
    revenue_label = "total inc" if result["rows"][0]["revenue_definition"] == "bank_total_income" else "revenue"
    lines = [f"{result['entity']} ({result['ticker']}), {result['basis']}. NSE filed XBRL; INR crore except EPS.",
             f"end         {revenue_label:>9}  rev YoY  gross%  oper%  net%   net income  diluted EPS  EPS YoY"]
    def crore(value):
        return "-" if value is None else f"{value / 10_000_000:,.1f}"
    def pct(value):
        return "-" if value is None else f"{value:+.1f}%"
    for row in result["rows"]:
        eps_text = "-" if row["eps_diluted_inr"] is None else f"{row['eps_diluted_inr']:.2f}"
        lines.append(f"{row['end']}  {crore(row['revenue_inr']):>9}  {pct(row['revenue_yoy_pct']):>7}  "
                     f"{pct(row['gross_margin_pct']):>6}  {pct(row['operating_margin_pct']):>5}  "
                     f"{pct(row['net_margin_pct']):>5}  {crore(row['net_income_inr']):>10}  "
                     f"{eps_text:>11}  "
                     f"{pct(row['eps_yoy_pct']):>7}{'  derived Q4' if row['derived'] else ''}")
    if result["ttm"]:
        t = result["ttm"]
        lines.append("Last four quarters: revenue INR " + crore(t.get("revenue_inr")) +
                     " crore; net income INR " + crore(t.get("net_income_inr")) +
                     " crore; diluted EPS INR " +
                     ("-" if t.get("eps_diluted_inr") is None else f"{t['eps_diluted_inr']:.2f}"))
    cur = result["rows"][0]
    lines.append("Latest filed quarter: " + cur["end"] + "; revenue YoY " + pct(cur["revenue_yoy_pct"]) +
                 "; net income YoY " + pct(cur["net_income_yoy_pct"]) +
                 "; EPS YoY " + pct(cur["eps_yoy_pct"]) + ".")
    lines.append("Latest filing: " + cur["filing_url"] + " (published " + cur["filed_at"] + ")")
    if cur["filing_pdf_url"]:
        lines.append("Filed result PDF: " + cur["filing_pdf_url"])
    if cur["filing_view_url"]:
        lines.append("Filing web view: " + cur["filing_view_url"])
    for link in result.get("release_links", []):
        lines.append(link["kind"].replace("_", " ").capitalize() + ": " + link["url"])
    for note in result["notes"]:
        lines.append("NOTE: " + note)
    lines.append("Research only. Verify the filing and upcoming result date before an overnight paper position.")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m india_trader earnings-analyst",
                                description="Quarterly Indian earnings from official NSE XBRL; paper research only")
    p.add_argument("tickers", nargs="*")
    p.add_argument("--isin", help="look up one NSE EQ company by its official Indian ISIN")
    p.add_argument("--universe", help="saved NSE equity CSV or ticker|name|ISIN list for --isin")
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--quarters", type=int, default=6)
    p.add_argument("--basis", choices=("consolidated", "standalone"), default="consolidated")
    p.add_argument("--as-of", help="offset-aware filing cutoff for reproducible research")
    p.add_argument("--offline", metavar="DIR", help="saved NSE index JSON and XBRL XML files")
    p.add_argument("--no-filings", action="store_true", help="suppress historical filing list in JSON")
    p.add_argument("--metrics", metavar="FILE", help="merge latest summary rows into this JSON file")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    try:
        cutoff = _cutoff(a.as_of)
        if a.isin and a.tickers:
            raise EarningsError("use either a ticker or --isin")
        wanted = [_ticker_for_isin(a.isin.upper(), a.universe, a.offline)] if a.isin else \
            a.tickers or _watchlist(a.watchlist)
        tickers = []
        for raw in wanted:
            sym = canonical_ticker(raw)
            if not sym.endswith(".NS"):
                raise EarningsError("NSE filing lookup requires an NSE .NS symbol")
            if sym not in tickers:
                tickers.append(sym)
        if not tickers or len(tickers) > 20:
            raise EarningsError("give 1–20 NSE tickers")
        if not 1 <= a.quarters <= 20:
            raise EarningsError("--quarters must be between 1 and 20")
        fetch = Fetcher(a.offline)
        results, errors, metrics = [], [], []
        for tick in tickers:
            sym = tick[:-3]
            try:
                found = []
                source_errors = []
                for kind in ("integrated", "quarterly", "annual"):
                    try:
                        found.extend(filings(fetch.index(sym, kind), sym, a.basis, kind, cutoff))
                    except EarningsError as exc:
                        source_errors.append(f"{kind}: {exc}")
                if not found:
                    raise EarningsError("no dated NSE XBRL filings for this symbol/basis; " + "; ".join(source_errors))
                found.sort(key=lambda x: (x["period_end"], x["filed_at"]), reverse=True)
                unique, seen = [], set()
                for filing in found:
                    key = (filing["period_end"], filing["source_kind"] == "annual")
                    if key not in seen:
                        unique.append(filing)
                        seen.add(key)
                source_rows = []
                failed = []
                for filing in unique[:max(a.quarters + 5, 10)]:
                    try:
                        if a.isin:
                            filing["expected_isin"] = a.isin.upper()
                        source_rows.extend(parse_xbrl(fetch.xbrl(filing["xbrl"]), filing))
                    except EarningsError as exc:
                        failed.append({"period_end": filing["period_end"], "url": filing["xbrl"],
                                       "error": str(exc)})
                if not source_rows:
                    raise EarningsError("no usable filed earnings facts; " + str(failed[:2]))
                result = analyse(sym, source_rows, a.quarters, cutoff)
                result["filing_status"] = {"found": len(found), "parsed": len(unique[:max(a.quarters + 5, 10)]) - len(failed),
                                           "failed": failed, "index_errors": source_errors,
                                           "partial": bool(failed or source_errors)}
                if not a.no_filings:
                    result["filings"] = unique[:max(a.quarters + 5, 10)]
                    try:
                        result["release_links"] = release_links(fetch.announcements(sym), sym, unique[0], cutoff)
                    except EarningsError as exc:
                        result["release_links"] = []
                        result["filing_status"]["partial"] = True
                        result["filing_status"].setdefault("index_errors", []).append("announcements: " + str(exc))
                    if not result["release_links"]:
                        result["notes"].append("No same-cycle earnings-release PDF was confirmed; inspect the company IR page or exchange announcements.")
                if result["rows"][0]["end"] < max(x["period_end"] for x in found):
                    result["notes"].append("The newest indexed filing was not parsed into a quarter; read it before summarising.")
                if source_errors:
                    result["notes"].append("One filing index was unavailable; historical coverage may be partial.")
                results.append(result)
                metrics.append(_metrics_row(result))
            except EarningsError as exc:
                errors.append({"ticker": tick, "error": str(exc)})
        if a.metrics and metrics:
            pth = Path(a.metrics)
            prior = json.loads(pth.read_text(encoding="utf-8")) if pth.is_file() else {}
            existing = {x["ticker"]: x for x in prior.get("companies", []) if isinstance(x, dict) and
                        isinstance(x.get("ticker"), str)}
            existing.update({x["ticker"]: x for x in metrics})
            _write(pth, {"as_of": cutoff.isoformat(), "mode": "PAPER_RESEARCH_ONLY",
                         "companies": sorted(existing.values(), key=lambda x: x["ticker"])})
        if a.json:
            print(json.dumps({"results": results, "errors": errors, "mode": "PAPER_RESEARCH_ONLY"}, indent=2,
                             allow_nan=False))
        else:
            for result in results:
                print(_render(result))
                print()
            for error in errors:
                print(f"earnings-analyst: {error['ticker']}: {error['error']}", file=sys.stderr)
            if a.metrics and metrics:
                print(f"Wrote {len(metrics)} company metric row(s) to {a.metrics}.")
        return 0 if results and not errors else 1 if results else 2
    except (EarningsError, OSError, ValueError, TypeError, KeyError) as exc:
        print("earnings-analyst: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
