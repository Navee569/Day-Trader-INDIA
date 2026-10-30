"""Paper-only India news monitor: NSE disclosures, headlines, and price context.

Inspired by the MIT-licensed upstream trader-news workflow. No broker actions.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import email.utils
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .core import IST, symbol

NSE_RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_FEEDS = {
    "announcements": NSE_RSS,
    "financial_results": "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml",
    "board_meetings": "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml",
    "corporate_actions": "https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml",
    "insider_trading": "https://nsearchives.nseindia.com/content/RSS/InsiderTrading.xml",
}
NSE_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-announcements"
YAHOO_RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={}&region=IN&lang=en-IN"
GOOGLE_RSS = "https://news.google.com/rss/search?{}"
CHART = "https://{}.finance.yahoo.com/v8/finance/chart/{}?range=1d&interval=5m"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; IndiaTraderPaperResearch/0.2.1)",
           "Accept": "application/rss+xml, application/xml, application/json, */*",
           "Accept-Encoding": "gzip"}
MAX_FEED = 2_000_000
KEEP_DAYS = 30
MOVE_PCT = 3.0
KEYWORDS = [
    ("earnings", r"\bearnings\b|\bresults\b|\bEPS\b|\bguidance\b|\bforecast\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|price target|\boutperform\b"),
    ("legal", r"\blawsuit\b|\bprobe\b|\binvestigation\b|\bSEBI\b|\bcourt\b"),
    ("deal", r"\bacquisit|\bacquir|\bmerger\b|\bbuyout\b|\btakeover\b"),
    ("capital", r"\bdividend\b|\bbuyback\b|\brepurchase\b|\bsplit\b|\bissue\b"),
    ("people", r"\bCEO\b|\bCFO\b|\bresign|\bappoint|\bdirector\b"),
    ("risk", r"\bbankrupt|\bdefault\b|\bdelist|\bbreach\b|\bfraud\b"),
    ("halt", r"\bhalt\b|trading suspen|trading pause"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|\bapproval\b|\bpenalty\b"),
    ("macro", r"\bCPI\b|\binflation\b|\brepo rate\b|\btariff"),
]
IMPORTANT = re.compile(r"financial results|bankrupt|default|fraud|auditor|resign|trading suspens|"
                       r"acquisit|merger|delist|insolvency|material event", re.I)
EVERGREEN = re.compile(r"stock price\s*[-,:]|stock price.*(?:chart|quote)|share price\s*[-,:].*chart", re.I)


class NewsError(Exception):
    pass


def load_json(path):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def normalize(raw):
    s = str(raw).strip().upper()
    return symbol(s, "BSE" if s.endswith(".BO") or s.isdigit() else "NSE")


def watch_from_files(portfolio, watchlist, plan, universe):
    out = {}
    for path, kind in ((watchlist, "hits"), (plan, "plan"), (portfolio, "positions")):
        doc = load_json(path) or {}
        entries = doc.get("hits", []) if kind == "hits" else (
            doc.get("positions", []) if kind == "positions" else [doc])
        for entry in entries:
            raw = entry.get("symbol") or entry.get("ticker")
            if raw:
                ticker = normalize(raw)
                out[ticker] = entry.get("name") or out.get(ticker)
    p = Path(universe)
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            parts = line.split("|")
            if len(parts) >= 2 and parts[0] in out and not out[parts[0]]:
                out[parts[0]] = parts[1].strip()
    return out


def names_from_universe(path, tickers):
    names = {}
    p = Path(path)
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            parts = line.split("|")
            if len(parts) >= 2 and parts[0] in tickers:
                names[parts[0]] = parts[1].strip()
    return names


def parse_time(value, nse=False):
    value = (value or "").strip()
    if not value:
        return None
    if nse:
        try:
            return datetime.strptime(value, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST).astimezone(timezone.utc)
        except ValueError:
            return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d.replace(tzinfo=timezone.utc).astimezone(timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def parse_rss(data, source, ticker=None):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError(f"invalid {source} RSS XML: {exc}") from exc
    out = []
    for it in root.findall("./channel/item"):
        title = (it.findtext("title") or "").strip()
        summary = re.sub(r"<[^>]+>", " ", it.findtext("description") or "").strip()
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        stamp = parse_time(it.findtext("pubDate"), nse=source.startswith("NSE "))
        if not title or not stamp:
            continue
        if not guid:
            guid = hashlib.sha256((title + "|" + summary + "|" + stamp.isoformat()).encode()).hexdigest()
        out.append({"id": source + ":" + guid, "source": source,
                    "kind": "filing" if source.startswith("NSE ") else "headline",
                    "ticker": ticker, "title": title, "summary": summary, "link": link,
                    "time": stamp, "important": bool(IMPORTANT.search(title + " " + summary))
                    or source == "NSE financial_results"})
    return out


def parse_chart(data, ticker):
    try:
        meta = json.loads(data)["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
        prev = float(meta.get("chartPreviousClose") or meta["previousClose"])
        when = datetime.fromtimestamp(int(meta["regularMarketTime"]), timezone.utc)
        returned = meta.get("symbol", "").upper()
        currency = meta.get("currency", "")
        if returned and returned != ticker or currency and currency != "INR" or price <= 0 or prev <= 0:
            raise ValueError("symbol, currency, or price mismatch")
    except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
        raise NewsError(f"unusable chart for {ticker}") from exc
    return {"ticker": ticker, "price": round(price, 2), "prev_close": round(prev, 2),
            "pct": round((price / prev - 1) * 100, 2), "time": when}


class Fetcher:
    def __init__(self, offline=None, pause=0.15):
        self.offline = Path(offline) if offline else None
        self.pause = pause
        self.last = 0.0

    def get(self, url, name):
        if self.offline:
            p = self.offline / name
            if not p.is_file():
                raise NewsError(f"offline file missing: {p}")
            return p.read_bytes()
        gap = self.pause - (time.monotonic() - self.last)
        if gap > 0:
            time.sleep(gap)
        self.last = time.monotonic()
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=18) as response:
                data = response.read(MAX_FEED + 1)
                if len(data) > MAX_FEED:
                    raise NewsError(f"oversized feed: {url}")
                if response.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                    if len(data) > MAX_FEED:
                        raise NewsError(f"oversized feed: {url}")
                return data
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NewsError(f"could not fetch {url}: {exc}") from exc


def company_terms(watch):
    patterns = {}
    for ticker, name in watch.items():
        bare = ticker.removesuffix(".NS").removesuffix(".BO")
        aliases = [r"(?<![A-Z0-9])" + re.escape(bare) + r"(?![A-Z0-9])"]
        if name and len(name) >= 4:
            aliases.append(re.escape(name).replace(r"\ ", r"\s+"))
            short = re.sub(r"\s+(?:LIMITED|LTD\.?|PRIVATE|PVT\.?)\s*$", "", name, flags=re.I)
            if len(short) >= 6 and short != name:
                aliases.append(re.escape(short).replace(r"\ ", r"\s+"))
        patterns[ticker] = re.compile("|".join(aliases), re.I)
    return patterns


def tag_and_match(items, watch):
    patterns = company_terms(watch)
    filing_patterns = {}
    for ticker, name in watch.items():
        if name and len(name) >= 4:
            short = re.sub(r"\s+(?:LIMITED|LTD\.?)\s*$", "", name, flags=re.I)
            filing_patterns[ticker] = re.compile(re.escape(short).replace(r"\ ", r"\s+"), re.I)
        else:
            bare = ticker.removesuffix(".NS").removesuffix(".BO")
            filing_patterns[ticker] = re.compile(r"\b(?:NSE|SYMBOL)\s*:\s*" + re.escape(bare) + r"\b", re.I)
    out = []
    for it in items:
        hay = it["title"] + " " + it["summary"]
        filing = it["source"].startswith("NSE ")
        if not filing and EVERGREEN.search(it["title"]):
            continue
        relevant = filing_patterns if filing else patterns
        touches = {t for t, rx in relevant.items() if rx.search(hay)}
        if it["ticker"] and not filing:
            ticker = it["ticker"]
            name = watch.get(ticker)
            bare = ticker.removesuffix(".NS").removesuffix(".BO")
            contextual = re.search(r"\b(?:NSE|BSE|India|Indian|Tata)\b|\.NS\b|\.BO\b", hay, re.I)
            if ticker in touches and len(bare) <= 4 and not contextual and name:
                short = re.sub(r"\s+(?:LIMITED|LTD\.?)\s*$", "", name, flags=re.I)
                if not re.search(re.escape(short).replace(r"\ ", r"\s+"), hay, re.I):
                    touches.remove(ticker)
        if filing and not touches:
            continue
        if not filing and not touches:
            continue
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if filing and not it["link"]:
            first = it["touches"][0].removesuffix(".NS")
            it["link"] = NSE_PAGE + "?symbol=" + urllib.parse.quote(first)
            it["link_type"] = "company_search_not_individual_filing"
        out.append(it)
    return out


def dedupe(items):
    seen, out = {}, []
    for it in items:
        title_key = re.sub(r"\W+", " ", it["title"].lower()).strip() + "|" + it["time"].date().isoformat()
        if it["kind"] == "filing" and it.get("link_type") == "company_search_not_individual_filing":
            title_key += "|" + re.sub(r"\W+", " ", it["summary"].lower()).strip()
        keys = [it["id"], it["link"] if it.get("link_type") != "company_search_not_individual_filing" else "", title_key]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit:
            hit["touches"] = sorted(set(hit["touches"] + it["touches"]))
            continue
        for key in keys:
            if key:
                seen[key] = it
        out.append(it)
    return out


def fingerprint(item):
    return hashlib.sha256(item["id"].encode()).hexdigest()[:20]


def collect(fetcher, watch, include_nse=True, include_headlines=True, include_moves=True,
            cutoff=None, nse_feeds=None):
    items, moves, notes = [], [], []
    sources = {}
    if include_nse:
        for feed in nse_feeds or NSE_FEEDS:
            url = NSE_FEEDS[feed]
            try:
                filings = parse_rss(fetcher.get(url, f"nse-{feed.replace('_', '-')}.xml"),
                                    "NSE " + feed)
                items += filings
                sources["nse_" + feed] = {"status": "ok", "feed_items": len(filings),
                                          "oldest": min((i["time"] for i in filings), default=None)}
            except NewsError as exc:
                notes.append(f"NSE {feed}: {exc}")
                sources["nse_" + feed] = {"status": "error"}
    for ticker, name in sorted(watch.items()):
        if include_headlines:
            yahoo = []
            try:
                yahoo = parse_rss(fetcher.get(YAHOO_RSS.format(urllib.parse.quote(ticker)),
                                              f"yahoo-{ticker}.xml"), "Yahoo Finance", ticker)
            except NewsError as exc:
                notes.append(f"Yahoo headlines {ticker}: {exc}")
            relevant_yahoo = tag_and_match(yahoo, {ticker: name})
            items += relevant_yahoo
            recent_yahoo = any(i["time"] >= cutoff for i in relevant_yahoo) if cutoff else bool(relevant_yahoo)
            if not recent_yahoo:
                query = f'"{name}" stock NSE' if name else f'"{ticker.removesuffix(".NS")}" stock NSE'
                url = GOOGLE_RSS.format(urllib.parse.urlencode({"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}))
                try:
                    google = parse_rss(fetcher.get(url, f"google-{ticker}.xml"), "Google News", ticker)
                    items += google
                    sources[f"headlines:{ticker}"] = {"status": "ok", "provider": "Google News", "items": len(google)}
                except NewsError as exc:
                    notes.append(f"Google headlines {ticker}: {exc}")
                    sources[f"headlines:{ticker}"] = {"status": "error", "items": 0}
            else:
                sources[f"headlines:{ticker}"] = {"status": "ok", "provider": "Yahoo Finance", "items": len(relevant_yahoo)}
        if include_moves:
            try:
                try:
                    data = fetcher.get(CHART.format("query2", urllib.parse.quote(ticker)), f"chart-{ticker}.json")
                except NewsError:
                    if fetcher.offline:
                        raise
                    data = fetcher.get(CHART.format("query1", urllib.parse.quote(ticker)), f"chart-{ticker}.json")
                moves.append(parse_chart(data, ticker))
            except NewsError as exc:
                notes.append(f"price {ticker}: {exc}")
    return tag_and_match(items, watch), moves, notes, sources


def select(items, state, now, hours, show_all):
    seen = state.get("seen", {})
    cutoff = now - timedelta(hours=hours)
    out = []
    for item in items:
        if item["time"] < cutoff or item["time"] > now + timedelta(minutes=10):
            continue
        item["new"] = fingerprint(item) not in seen
        if item["new"] or show_all:
            out.append(item)
    return sorted(out, key=lambda i: i["time"], reverse=True)


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    for item in items:
        seen.setdefault(fingerprint(item), now.isoformat())
    cutoff = now - timedelta(days=KEEP_DAYS)
    state["seen"] = {k: v for k, v in seen.items() if (parse_time(v) or now) >= cutoff}
    state["last_check"] = now.isoformat()


def json_ready(item):
    return dict(item, time=item["time"].astimezone(IST).isoformat())


def render(result):
    lines = []
    for move in result["moves"]:
        flag = " MOVING" if move["moving"] else ""
        if move["prior_session"]:
            flag += " PRIOR SESSION"
        lines.append(f"{move['ticker']} Yahoo (unofficial) ₹{move['price']:,.2f} {move['pct']:+.2f}% "
                     f"vs previous close ₹{move['prev_close']:,.2f} at {move['time']}{flag}")
    if lines:
        lines.append("")
    if not result["items"]:
        lines.append("Nothing new in the available feeds.")
    for item in result["items"]:
        mark = "!" if item["important"] else " "
        lines.append(f"{mark} {item['time']}  {','.join(item['touches'])}  {item['source']}: {item['title']}")
        lines.append("  " + ", ".join(item["tags"]) + ("  " if item["tags"] else "") + item["link"])
        if item.get("link_type"):
            lines.append("  NSE company search link; open the actual filing before relying on it.")
    for note in result["notes"]:
        lines.append("note: " + note)
    lines.append(f"Checked {result['checked']}. Headlines are leads; read the story or filing. Paper research only.")
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m india_trader news", description="India news monitor, paper research only")
    p.add_argument("--tickers", nargs="+", help="NSE symbols (default: scanner watchlist + plan + portfolio)")
    p.add_argument("--portfolio", default="trader/portfolio.json")
    p.add_argument("--watchlist", default="trader/watchlist.json")
    p.add_argument("--plan", default="trader/plan.json")
    p.add_argument("--universe", default="trader/universe.txt")
    p.add_argument("--state", default="trader/state/news.json")
    p.add_argument("--hours", type=float, default=48)
    p.add_argument("--all", action="store_true", help="include previously seen items")
    p.add_argument("--no-nse", action="store_true", help="headlines only")
    p.add_argument("--feeds", default=",".join(NSE_FEEDS),
                   help="comma-separated NSE feed categories: " + ", ".join(NSE_FEEDS))
    p.add_argument("--no-headlines", action="store_true", help="NSE disclosures only")
    p.add_argument("--no-moves", action="store_true")
    p.add_argument("--move-pct", type=float, default=MOVE_PCT)
    p.add_argument("--offline", metavar="DIR", help="saved RSS/chart fixture folder")
    p.add_argument("--now", help="ISO 8601 time; naive values interpreted as IST")
    p.add_argument("--dry-run", action="store_true", help="do not update seen-item state")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.hours <= 0 or a.move_pct <= 0:
        p.error("--hours and --move-pct must be positive")
    feeds = [f.strip().lower() for f in a.feeds.split(",") if f.strip()]
    if not feeds or any(f not in NSE_FEEDS for f in feeds):
        p.error("--feeds must list one or more of: " + ", ".join(NSE_FEEDS))
    try:
        known = watch_from_files(a.portfolio, a.watchlist, a.plan, a.universe)
        if a.tickers:
            tickers = [normalize(raw) for raw in a.tickers]
            names = names_from_universe(a.universe, tickers)
            watch = {t: known.get(t) or names.get(t) for t in tickers}
        else:
            watch = known
        if not watch:
            p.error("no tickers: pass --tickers or run the scanner to write trader/watchlist.json")
        now = datetime.fromisoformat(a.now.replace("Z", "+00:00")) if a.now else datetime.now(IST)
        now = (now.replace(tzinfo=IST) if now.tzinfo is None else now).astimezone(timezone.utc)
        state = load_json(a.state) or {}
        raw, moves, notes, sources = collect(Fetcher(a.offline), watch, not a.no_nse,
                                             not a.no_headlines, not a.no_moves,
                                             now - timedelta(hours=a.hours), feeds)
        nse_oldest = sources.get("nse_announcements", {}).get("oldest")
        if nse_oldest and nse_oldest > now - timedelta(hours=a.hours):
            notes.append("NSE RSS begins at " + nse_oldest.astimezone(IST).isoformat() +
                         "; it does not cover the full requested window. Check NSE filings page for older notices.")
        items = dedupe(raw)
        shown = select(items, state, now, a.hours, a.all)
        payload = {"checked": now.astimezone(IST).isoformat(), "watch": watch,
                   "sources": {k: dict(v, oldest=v["oldest"].astimezone(IST).isoformat()) if v.get("oldest") else v
                               for k, v in sources.items()},
                   "moves": [dict(m, time=m["time"].astimezone(IST).isoformat(),
                                  prior_session=m["time"].astimezone(IST).date() < now.astimezone(IST).date(),
                                  moving=abs(m["pct"]) >= a.move_pct and
                                  m["time"].astimezone(IST).date() == now.astimezone(IST).date())
                             for m in moves],
                   "items": [json_ready(i) for i in shown], "notes": notes,
                   "partial": bool(notes) or any(v.get("status") == "error" for v in sources.values())}
        print(json.dumps(payload, indent=2) if a.json else render(payload))
        if not a.dry_run:
            remember(items, state, now)
            target = Path(a.state)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        return 0 if any(i["new"] for i in shown) else 1
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        p.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
