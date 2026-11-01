"""Public NSE PIT disclosures for paper research. No broker operations."""
from __future__ import annotations

import argparse
from datetime import datetime, date, timedelta
import http.cookiejar
import json
from pathlib import Path
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .core import IST
from . import bse_insiders

PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-insider-trading"
API = "https://www.nseindia.com/api/"
HOST = "nsearchives.nseindia.com"
MAX_INDEX = 5_000_000
MAX_XML = 2_000_000
DISCLAIMER = ("Public disclosures describe past transactions; they do not predict future trades or returns. "
              "Paper research only; inspect the original filing before drawing conclusions.")


class InsiderError(Exception):
    pass


def _date(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(str(s), fmt).date()
        except ValueError:
            pass
    raise InsiderError(f"invalid date: {s}")


def _stamp(s):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(str(s), fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    raise InsiderError(f"invalid filing timestamp: {s}")


def _cutoff(s):
    if s is None:
        return datetime.now(IST)
    try:
        value = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InsiderError("--as-of requires an offset-aware ISO timestamp") from exc
    if value.tzinfo is None:
        raise InsiderError("--as-of requires an offset-aware ISO timestamp")
    return value.astimezone(IST)


def _symbol(s):
    value = str(s).strip().upper().removesuffix(".NS")
    if not re.fullmatch(r"[A-Z0-9&_.-]{1,30}", value):
        raise InsiderError(f"invalid NSE symbol: {s}")
    return value


def _official_xml(url):
    p = urllib.parse.urlsplit(str(url or ""))
    if p.scheme != "https" or p.hostname != HOST or not p.path.startswith("/corporate/xbrl/") \
            or not re.fullmatch(r"[A-Za-z0-9_.-]+\.xml", p.path.rsplit("/", 1)[-1]) or p.query or p.fragment:
        raise InsiderError("filing has no safe official NSE XBRL URL")
    return p.geturl()


def _official_view(url):
    p = urllib.parse.urlsplit(str(url or ""))
    if p.scheme == "https" and p.hostname == HOST and p.path.startswith("/corporate/ixbrl/") \
            and re.fullmatch(r"[A-Za-z0-9_.-]+\.html?", p.path.rsplit("/", 1)[-1]) and not p.query:
        return p.geturl()
    return None


class Fetcher:
    def __init__(self, offline=None):
        self.offline = Path(offline) if offline else None
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.ready = False
        self.last = 0.0

    def get(self, url, filename, limit):
        if self.offline:
            path = self.offline / filename
            if not path.is_file() or path.stat().st_size > limit:
                raise InsiderError(f"offline source missing or too large: {path}")
            return path.read_bytes()
        if not self.ready:
            self._request(PAGE, MAX_INDEX)
            self.ready = True
        return self._request(url, limit)

    def _request(self, url, limit):
        gap = 0.2 - (time.monotonic() - self.last)
        if gap > 0:
            time.sleep(gap)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; IndiaTraderPaperResearch/0.2.1)",
            "Accept": "application/json, application/xml, */*", "Referer": PAGE})
        try:
            with self.opener.open(req, timeout=20) as response:
                body = response.read(limit + 1)
        except (OSError, TimeoutError) as exc:
            raise InsiderError(f"public NSE source unavailable: {url}: {exc}") from exc
        self.last = time.monotonic()
        if len(body) > limit:
            raise InsiderError(f"public NSE response too large: {url}")
        return body

    def index(self, archive=False, symbol=None, start=None, end=None):
        endpoint = "corporates-pit" if archive else "corporates-pit-gg"
        params = {"index": "equities"}
        if symbol:
            params["symbol"] = _symbol(symbol)
        if start or end:
            if not archive or not symbol or not start or not end or start > end:
                raise InsiderError("dated NSE archive search needs a symbol and valid date range")
            params["from_date"] = start.strftime("%d-%m-%Y")
            params["to_date"] = end.strftime("%d-%m-%Y")
        url = API + endpoint + "?" + urllib.parse.urlencode(params)
        filename = (("archive" if archive else "current") + ("-" + _symbol(symbol) if symbol else "") +
                    (f"-{start.isoformat()}-{end.isoformat()}" if start else "") + ".json")
        try:
            doc = json.loads(self.get(url, filename, MAX_INDEX))
        except (ValueError, UnicodeDecodeError) as exc:
            raise InsiderError(f"invalid NSE {endpoint} response") from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("data"), list):
            raise InsiderError(f"invalid NSE {endpoint} filing list")
        return doc["data"]

    def xml(self, url):
        safe = _official_xml(url)
        return self.get(safe, "xbrl/" + safe.rsplit("/", 1)[-1], MAX_XML)


def _current(raw):
    if not isinstance(raw, dict) or not re.fullmatch(r"\d+", str(raw.get("appId", ""))):
        return None
    if str(raw.get("regulation", "")).replace(" ", "").lower() not in ("regulation7(2)", "regulation7(3)"):
        return None
    try:
        filed = _stamp(raw["broadcastDateTime"])
        sym = _symbol(raw["symbol"])
        xml = _official_xml(raw["xmlFileName"])
    except (KeyError, InsiderError):
        return None
    return {"id": "current:" + str(raw["appId"]), "source": "NSE current PIT XBRL",
            "symbol": sym, "company": raw.get("companyName"), "filed_at": filed.isoformat(),
            "regulation": raw.get("regulation"), "submission": raw.get("typeOfSubmission"),
            "previous_id": raw.get("prevAppId"), "revision_remark": raw.get("revisionRemark"),
            "xbrl_url": xml, "view_url": _official_view(raw.get("ixbrl"))}


def _archive(raw):
    if not isinstance(raw, dict) or not re.fullmatch(r"\d+", str(raw.get("did", ""))):
        return None
    try:
        filed = _stamp(raw["date"])
        sym = _symbol(raw["symbol"])
        xml = _official_xml(raw["xbrl"])
    except (KeyError, InsiderError):
        return None
    return {"id": "archive:" + str(raw["did"]), "source": "NSE PIT archive", "symbol": sym,
            "company": raw.get("company"), "filed_at": filed.isoformat(), "regulation": raw.get("anex"),
            "submission": None, "previous_id": None, "revision_remark": None,
            "xbrl_url": xml, "view_url": None, "archive_record": raw}


def index(fetch, symbols=(), people=(), since=None, year=None, kind=None, as_of=None, archive=True):
    cutoff = as_of or datetime.now(IST)
    symbols = [_symbol(x) for x in symbols]
    people = [str(x).strip().casefold() for x in people if str(x).strip()]
    sources, errors = [], []
    for old in ((False, True) if archive else (False,)):
        try:
            if symbols:
                for sym in symbols:
                    sources.extend((old, r) for r in fetch.index(old, sym))
                    if old and ((year and year < 2026) or (since and since < date(2026, 5, 1))):
                        first = max(date(year, 1, 1) if year else since, since or date.min)
                        last = min(date(year, 12, 31) if year else cutoff.date(),
                                   cutoff.date(), date(2026, 4, 30))
                        day = first
                        while day <= last:
                            next_month = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
                            stop = min(last, next_month - timedelta(days=1))
                            sources.extend((old, r) for r in fetch.index(old, sym, day, stop))
                            day = stop + timedelta(days=1)
            else:
                sources.extend((old, r) for r in fetch.index(old))
        except InsiderError as exc:
            errors.append(str(exc))
    if not sources:
        raise InsiderError("no NSE filing index available; " + "; ".join(errors))
    out, seen = [], set()
    for old, raw in sources:
        row = (_archive if old else _current)(raw)
        if not row or row["id"] in seen:
            continue
        filed = datetime.fromisoformat(row["filed_at"])
        if filed > cutoff or (since and filed.date() < since) or (year and filed.year != year):
            continue
        if symbols and row["symbol"] not in symbols:
            continue
        if kind and re.sub(r"\s+", "", kind.casefold()) not in \
                re.sub(r"\s+", "", str(row["regulation"]).casefold()):
            continue
        # Current index has no person's name. Read XBRL when filtering people.
        if people:
            try:
                transactions = parse_xml(fetch.xml(row["xbrl_url"]), row)
            except InsiderError as exc:
                errors.append(f'{row["id"]}: {exc}')
                continue
            transactions = [t for t in transactions if any(p in t["person"].casefold() for p in people)]
            if not transactions:
                continue
            row["transactions"] = transactions
        seen.add(row["id"])
        out.append(row)
    out.sort(key=lambda x: (x["filed_at"], x["id"]), reverse=True)
    return out, errors


def parse_xml(data, filing=None):
    if len(data) > MAX_XML or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise InsiderError("unsafe or oversized XBRL document")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise InsiderError("invalid NSE XBRL document") from exc
    facts = {}
    units = {}
    for node in root.iter():
        ref = node.attrib.get("contextRef")
        if ref:
            tag = node.tag.rsplit("}", 1)[-1]
            facts.setdefault(ref, {})[tag] = (node.text or "").strip()
            if tag == "SecuritiesAcquiredOrDisposedValueOfSecurity":
                units[ref] = node.attrib.get("unitRef")
    main = facts.get("MainI") or facts.get("OneI") or {}
    expected = filing.get("symbol") if filing else None
    sym = main.get("Symbol") or main.get("NSESymbol")
    if expected and sym != expected or not sym:
        raise InsiderError("XBRL company symbol does not match filing index")
    reg = main.get("DisclosureUnderRegulation") or main.get("RegulationOfInsiderTrading")
    if filing and filing.get("regulation") and reg:
        if re.sub(r"\W", "", str(filing["regulation"]).lower()).replace("regulation", "") != \
                re.sub(r"\W", "", reg.lower()).replace("regulation", ""):
            raise InsiderError("XBRL regulation does not match filing index")
    records = []
    for ref, f in facts.items():
        person = f.get("NameOfThePerson", "")
        if not person:
            continue
        from_day = _date(f.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate"))
        to_day = _date(f.get("DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate"))
        filed_day = _date(main.get("DateOfFiling")) or (_date(filing["filed_at"][:10]) if filing else None)
        lag = (filed_day - to_day).days if filed_day and to_day else None
        records.append({"symbol": sym, "context": ref, "person": person, "category": f.get("CategoryOfPerson"),
                        "instrument": f.get("TypeOfInstrument"), "transaction_type": f.get("SecuritiesAcquiredOrDisposedTransactionType"),
                        "quantity": f.get("SecuritiesAcquiredOrDisposedNumberOfSecurity"),
                        "value_reported": f.get("SecuritiesAcquiredOrDisposedValueOfSecurity"),
                        "value_unit_ref": units.get(ref),
                        "trade_from": from_day.isoformat() if from_day else None,
                        "trade_to": to_day.isoformat() if to_day else None,
                        "mode": f.get("ModeOfAcquisitionOrDisposal"),
                        "intimated_to_company": f.get("DateOfIntimationToCompany"),
                        "exchange": f.get("ExchangeOnWhichTheTradeWasExecuted"),
                        "held_before": f.get("SecuritiesHeldPriorToAcquisitionOrDisposalNumberOfSecurity"),
                        "held_after": f.get("SecuritiesHeldPostAcquistionOrDisposalNumberOfSecurity"),
                        "disclosure_lag_calendar_days": lag,
                        "derivatives": {k: v for k, v in f.items() if "Derivative" in k or "Contract" in k or "Option" in k},
                        "other_filed_facts": {k: v for k, v in f.items() if k not in {
                            "NameOfThePerson", "CategoryOfPerson", "TypeOfInstrument",
                            "SecuritiesAcquiredOrDisposedTransactionType", "SecuritiesAcquiredOrDisposedNumberOfSecurity",
                            "SecuritiesAcquiredOrDisposedValueOfSecurity", "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyFromDate",
                            "DateOfAllotmentAdviceOrAcquisitionOfSharesOrSaleOfSharesSpecifyToDate", "ModeOfAcquisitionOrDisposal",
                            "DateOfIntimationToCompany", "ExchangeOnWhichTheTradeWasExecuted",
                            "SecuritiesHeldPriorToAcquisitionOrDisposalNumberOfSecurity",
                            "SecuritiesHeldPostAcquistionOrDisposalNumberOfSecurity"} and
                            ("Derivative" not in k and "Contract" not in k and "Option" not in k)}})
    if not records:
        raise InsiderError("XBRL has no named disclosure transactions")
    return records


def _portfolio(path):
    p = Path(path)
    if not p.is_file():
        return set()
    doc = json.loads(p.read_text(encoding="utf-8"))
    positions = doc.get("positions", []) if isinstance(doc, dict) else []
    return {_symbol(x.get("symbol") or x.get("ticker")) for x in positions if isinstance(x, dict)
            and (x.get("symbol") or x.get("ticker"))}


def _read(fetch, row, portfolio=(), xml_dir=None):
    public = {k: v for k, v in row.items() if k != "archive_record"}
    if "transactions" not in public:
        if row["source"] == "BSE PIT XBRL filing":
            data = bse_insiders.get_filing(fetch.opener, row["view_url"], fetch.offline)
            public["transactions"] = bse_insiders.parse_ixbrl(data, row)
            filename = row["view_url"].rsplit("/", 1)[-1]
        else:
            data = fetch.xml(row["xbrl_url"])
            public["transactions"] = parse_xml(data, row)
            filename = row["xbrl_url"].rsplit("/", 1)[-1]
        if xml_dir:
            path = Path(xml_dir) / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            public["saved_xml"] = str(path)
    public["held_in_portfolio"] = row["symbol"] in portfolio
    public["notice"] = DISCLAIMER
    return public


def _save(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _render(rows, errors):
    for row in rows:
        print(f'{row["id"]} {row["filed_at"]} {row["symbol"]} {row["regulation"]} {row["submission"] or ""}')
        print("  Official filing: " + (row.get("xbrl_url") or row.get("view_url") or "local file"))
        for t in row.get("transactions", []):
            print(f'  {t["person"]} ({t["category"]}): {t["transaction_type"]} '
                  f'{t["quantity"]} {t["instrument"]}, reported value {t["value_reported"]} '
                  f'({t["value_unit_ref"]}), '
                  f'trade {t["trade_from"]} to {t["trade_to"]}, '
                  f'filed {t["disclosure_lag_calendar_days"]} calendar days after end')
        if row.get("held_in_portfolio"):
            print("  HELD IN PAPER PORTFOLIO")
    for error in errors:
        print("Source warning: " + error, file=sys.stderr)
    print(DISCLAIMER)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m india_trader insider-tracker",
                                description="Public NSE/BSE insider disclosures, paper research only")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("index", "new", "fetch"):
        q = sub.add_parser(name)
        if name == "fetch":
            q.add_argument("id")
        q.add_argument("--symbol", action="append", default=[], help="NSE symbol or six-digit BSE scrip, repeatable")
        q.add_argument("--exchange", choices=("NSE", "BSE"), default="NSE")
        q.add_argument("--member", action="append", default=[], help="insider/person name fragment, repeatable")
        q.add_argument("--type", help="regulation text, e.g. 7(2)")
        q.add_argument("--since", help="filing date YYYY-MM-DD")
        q.add_argument("--year", type=int)
        q.add_argument("--as-of", help="offset-aware ISO filing cutoff")
        q.add_argument("--offline", metavar="DIR", help="saved NSE JSON/XML or BSE HTML responses")
        q.add_argument("--current-only", action="store_true", help="omit pre-May-2026 archive")
        q.add_argument("--limit", type=int, default=50)
        q.add_argument("--portfolio", default="trader/portfolio.json")
        q.add_argument("--tickers", nargs="*", default=[], help="also flag these paper/watchlist symbols")
        q.add_argument("--xml-dir", help="save successfully parsed official XBRL here")
        q.add_argument("--json", action="store_true")
        q.add_argument("--read", action="store_true", help="parse matching XBRL transactions")
    n = sub.choices["new"]
    n.add_argument("--state", default="trader/state/insider-tracker.json")
    n.add_argument("--dry-run", action="store_true")
    n.add_argument("--today", help="YYYY-MM-DD first-check date; --as-of remains the filing cutoff")
    q = sub.add_parser("parse", help="parse saved official NSE XML or BSE inline XBRL HTML")
    q.add_argument("xml")
    q.add_argument("--symbol", help="verify company symbol")
    q.add_argument("--exchange", choices=("NSE", "BSE"), default="NSE")
    q.add_argument("--portfolio", default="trader/portfolio.json")
    q.add_argument("--tickers", nargs="*", default=[])
    q.add_argument("--filed", help="YYYY-MM-DD, used for lag if XML has no filing date")
    q.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        if args.command == "parse":
            row = {"symbol": (bse_insiders.bse_code(args.symbol) + ".BO" if args.exchange == "BSE"
                              else _symbol(args.symbol))} if args.symbol else None
            if args.filed:
                row = (row or {}) | {"filed_at": _date(args.filed).isoformat()}
            data = Path(args.xml).read_bytes()
            records = bse_insiders.parse_ixbrl(data, row) if args.exchange == "BSE" else parse_xml(data, row)
            held = _portfolio(args.portfolio) | {_symbol(x) for x in args.tickers}
            result = {"source_file": str(args.xml), "symbol": records[0]["symbol"],
                      "held_in_portfolio": records[0]["symbol"] in held,
                      "transactions": records, "notice": DISCLAIMER}
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                _render([{"id": "local", "filed_at": "local", "symbol": records[0]["symbol"],
                          "regulation": "from-XBRL", "submission": "", "xbrl_url": str(args.xml),
                          "transactions": records, "held_in_portfolio": result["held_in_portfolio"]}], [])
            return 0
        if not 1 <= args.limit <= 500:
            raise InsiderError("--limit must be 1..500")
        cutoff = _cutoff(args.as_of)
        since = _date(args.since) if args.since else None
        if args.command == "new":
            state = Path(args.state)
            prior = json.loads(state.read_text(encoding="utf-8")) if state.is_file() else {}
            seen = set(prior.get("seen", []))
            today = _date(args.today) if args.today else cutoff.date()
            if not since:
                since = _date(prior["baseline_date"]) if prior.get("baseline_date") else today - timedelta(days=45)
        fetch = Fetcher(args.offline)
        if args.exchange == "BSE":
            if not args.symbol:
                raise InsiderError("BSE PIT search needs --symbol with a six-digit scrip code")
            start = since or (date(args.year, 1, 1) if args.year else cutoff.date() - timedelta(days=45))
            end = min(cutoff.date(), date(args.year, 12, 31)) if args.year else cutoff.date()
            if end < start:
                raise InsiderError("BSE search date range is empty")
            rows, errors = [], []
            for raw in args.symbol:
                code = bse_insiders.bse_code(raw)
                try:
                    rows.extend(bse_insiders.index(fetch.opener, code, start, end, args.offline))
                except bse_insiders.BseError as exc:
                    errors.append(str(exc))
            if not rows and errors:
                raise InsiderError("no BSE filing index available; " + "; ".join(errors))
            rows = [r for r in rows if datetime.fromisoformat(r["filed_at"]) <= cutoff and
                    (not args.type or re.sub(r"\s+", "", args.type.casefold()) in "7(2)")]
            if args.member:
                filtered = []
                for row in rows:
                    try:
                        records = bse_insiders.parse_ixbrl(
                            bse_insiders.get_filing(fetch.opener, row["view_url"], fetch.offline), row)
                        records = [t for t in records if any(p.casefold() in t["person"].casefold()
                                                               for p in args.member)]
                        if records:
                            row["transactions"] = records
                            filtered.append(row)
                    except bse_insiders.BseError as exc:
                        errors.append(f'{row["id"]}: {exc}')
                rows = filtered
            rows.sort(key=lambda r: (r["filed_at"], r["id"]), reverse=True)
        else:
            rows, errors = index(fetch, args.symbol, args.member, since, args.year, args.type,
                                 cutoff, not args.current_only)
        if args.command == "fetch":
            if not args.id:
                raise InsiderError("fetch needs a filing ID from index")
            selected = [x for x in rows if x["id"] == args.id or x["id"].split(":")[-1] == args.id]
            if not selected:
                raise InsiderError("filing ID not in current NSE index; use --symbol/--year if needed")
            rows = selected[:1]
            args.read = True
        elif args.command == "new":
            rows = [x for x in rows if x["id"] not in seen]
        rows = rows[:args.limit]
        if args.read:
            portfolio = _portfolio(args.portfolio) | {_symbol(x) for x in args.tickers}
            read_rows = []
            for row in rows:
                try:
                    read_rows.append(_read(fetch, row, portfolio, args.xml_dir))
                except (InsiderError, bse_insiders.BseError) as exc:
                    errors.append(f'{row["id"]}: {exc}')
                    read_rows.append({k: v for k, v in row.items() if k != "archive_record"} |
                                     {"read_error": str(exc)})
            rows = read_rows
        else:
            rows = [{k: v for k, v in x.items() if k != "archive_record"} for x in rows]
        if args.command == "new" and not args.dry_run:
            # Only mark successful reads as seen; failed XML can be retried.
            prior["seen"] = sorted(seen | {x["id"] for x in rows if "read_error" not in x})
            prior["checked_at"] = cutoff.isoformat()
            prior.setdefault("baseline_date", since.isoformat())
            _save(args.state, prior)
        if args.json:
            print(json.dumps({"filings": rows, "errors": errors, "count": len(rows),
                              "mode": "PAPER_RESEARCH_ONLY", "notice": DISCLAIMER}, indent=2))
        else:
            _render(rows, errors)
        return 0 if rows and not errors else 1 if not rows and not errors else 2
    except (InsiderError, bse_insiders.BseError, OSError, ValueError, TypeError, KeyError) as exc:
        print("insider-tracker: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
