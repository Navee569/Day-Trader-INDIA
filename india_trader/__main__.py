"""python -m india_trader --help"""
import argparse
import json
import os
import sys
from datetime import datetime
from .core import DataError, IST, orb_signal, plan, read_csv, read_json_bars, save_json, symbol
from .data import fetch_bars
from .backtest import backtest_orb
from .scan import scan_csv_folder, scan_watchlist
from .paper_cycle import paper_cycle
from .universe import load_nse_equities, write_universe
from .nse_sources import bhavcopy_day, cm_live_quote
from .daily_scan import scan_nse_daily
from .intraday_sources import fetch_preferred
from .trade_workflow import entry_check, exit_plan
from .paper_orders import size_paper_order, risk_review, paper_ticket
from . import market_scanner
from . import news_monitor
from . import entry_timer
from . import exit_planner
from . import time_machine
from . import budget_manager
from . import risk_manager
from . import paper_broker
from . import alerts
from . import earnings_analyst
from . import insider_tracker
from . import calendar_source
from . import paper_monitor
from . import market_engine
from . import ai_committee
from . import paper_lifecycle
from . import web_api
from . import deployment


def attach_bars(p, symbol_optional=False):
    p.add_argument("symbol", nargs="?" if symbol_optional else None)
    p.add_argument("--csv", help="CSV with offset-aware 5-minute candles")
    p.add_argument("--json", help="Saved candle JSON from fetch command")
    p.add_argument("--exchange", choices=("NSE", "BSE"), default="NSE")


def load_bars(args):
    if int(bool(args.csv))+int(bool(args.json))+int(bool(args.symbol)) != 1:
        raise ValueError("Supply exactly one of SYMBOL, --csv, or --json")
    if args.csv:
        return read_csv(args.csv), "local_csv"
    if args.json:
        return read_json_bars(args.json), "local_json"
    ticker = symbol(args.symbol, args.exchange)
    fetched = fetch_preferred(ticker)
    return fetched["bars"], fetched["source"]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "scanner":
        return market_scanner.main(argv[1:])
    if argv and argv[0] == "news":
        return news_monitor.main(argv[1:])
    if argv and argv[0] == "entry-timer":
        return entry_timer.main(argv[1:])
    if argv and argv[0] == "exit-planner":
        return exit_planner.main(argv[1:])
    if argv and argv[0] == "time-machine":
        return time_machine.main(argv[1:])
    if argv and argv[0] == "budget-manager":
        return budget_manager.main(argv[1:])
    if argv and argv[0] == "risk-manager":
        return risk_manager.main(argv[1:])
    if argv and argv[0] == "broker":
        return paper_broker.main(argv[1:])
    if argv and argv[0] == "alerts":
        return alerts.main(argv[1:])
    if argv and argv[0] == "earnings-analyst":
        return earnings_analyst.main(argv[1:])
    if argv and argv[0] == "insider-tracker":
        return insider_tracker.main(argv[1:])
    if argv and argv[0] == "market-calendar":
        return calendar_source.main(argv[1:])
    if argv and argv[0] == "paper-monitor":
        return paper_monitor.main(argv[1:])
    if argv and argv[0] == "market-engine":
        return market_engine.main(argv[1:])
    if argv and argv[0] == "ai-committee":
        return ai_committee.main(argv[1:])
    if argv and argv[0] == "paper-lifecycle":
        return paper_lifecycle.main(argv[1:])
    if argv and argv[0] == "web":
        return web_api.main(argv[1:])
    if argv and argv[0] == "deploy":
        return deployment.main(argv[1:])
    p = argparse.ArgumentParser(prog="python -m india_trader", description="NSE/BSE cash-equity research; PAPER ONLY")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scanner", help="Full NSE EQ universe, daily setup, and marketwide ORB scanner")
    sub.add_parser("news", help="NSE disclosures, India headlines, and paper-only price context")
    sub.add_parser("entry-timer", help="Check, watch, or inspect a watchlist for completed-bar paper entries")
    sub.add_parser("exit-planner", help="Plan or show a paper stop, target, and flat-by time")
    sub.add_parser("time-machine", help="Seeded paper scenario replay and historical ORB rule check")
    sub.add_parser("budget-manager", help="Four-limit INR paper trade sizing and weighted allocation")
    sub.add_parser("risk-manager", help="Batch paper risk checks and exact-order verdicts")
    sub.add_parser("broker", help="Paper tickets, approval log, simulated fills and flatten")
    sub.add_parser("alerts", help="IST market clock and once-only paper price/time alerts")
    sub.add_parser("earnings-analyst", help="NSE filed quarterly earnings, comparisons and TTM metrics")
    sub.add_parser("insider-tracker", help="Public NSE insider disclosures, once-only alerts and XBRL transactions")
    sub.add_parser("market-calendar", help="Refresh public NSE/BSE holiday data and inspect session status")
    sub.add_parser("paper-monitor", help="Scheduled foreground paper alerts, news, earnings and insider checks")
    sub.add_parser("market-engine", help="Continuous foreground NSE scanner and multi-stock paper candidate monitor")
    sub.add_parser("ai-committee", help="Paper-only analyst debate and independent AI4 judgment of verified candidates")
    sub.add_parser("paper-lifecycle", help="Autonomous local paper entry, risk, simulated fills and independent exits")
    sub.add_parser("web", help="Authenticated local mobile paper dashboard")
    sub.add_parser("deploy", help="Private Linux paper services, health and checked backups")
    f = sub.add_parser("fetch", help="Fetch validated 5-minute research bars in provider priority order")
    f.add_argument("symbol")
    f.add_argument("--exchange", choices=("NSE", "BSE"), default="NSE")
    f.add_argument("--range", choices=("1d", "5d", "1mo"), default="5d")
    f.add_argument("--out")
    o = sub.add_parser("orb", help="Completed-bar opening-range breakout, long-only")
    attach_bars(o, symbol_optional=True)
    o.add_argument("--date", help="Select YYYY-MM-DD session (default latest)")
    o.add_argument("--as-of", help="ISO 8601 cutoff with UTC offset (default now)")
    o.add_argument("--opening-minutes", type=int, default=15)
    o.add_argument("--out")
    t = sub.add_parser("plan", help="Long cash-only hypothetical risk sizing")
    t.add_argument("--entry", required=True)
    t.add_argument("--stop", required=True)
    t.add_argument("--capital", default="100000")
    t.add_argument("--risk-pct", default="0.5")
    t.add_argument("--max-alloc-pct", default="20")
    t.add_argument("--target-r", default="2")
    t.add_argument("--tick", required=True, help="Mandatory exchange/instrument-specific tick assumption (e.g. 0.01, 0.05, 0.10)")
    t.add_argument("--slippage-bps", default="0")
    t.add_argument("--fee-buffer", default="0", help="User-estimated INR costs both sides")
    t.add_argument("--out")
    b = sub.add_parser("backtest", help="Historical next-bar replay; CSV or saved JSON")
    b.add_argument("--csv")
    b.add_argument("--json")
    b.add_argument("--opening-minutes", type=int, default=15)
    b.add_argument("--reward-r", type=float, default=2)
    b.add_argument("--out")
    sc = sub.add_parser("scan", help="Batch offline CSV scanner, or explicit live research watchlist")
    src = sc.add_mutually_exclusive_group(required=True)
    src.add_argument("--folder", help="Folder of CSV price-bar datasets")
    src.add_argument("--watchlist", help="Text file, one symbol per line (uses unofficial Yahoo)")
    sc.add_argument("--date")
    sc.add_argument("--as-of")
    sc.add_argument("--out")
    d = sub.add_parser("doctor", help="Runtime self-check, no network")
    d.add_argument("--out")
    sub.add_parser("data-status", help="Show configured read-only data sources and local health; no network")
    pc = sub.add_parser("paper-cycle", help="Fetch a short watchlist and make paper-only research plans")
    pc.add_argument("--watchlist", required=True)
    pc.add_argument("--as-of", help="Offset-aware cutoff time (default: current IST time)")
    pc.add_argument("--tick", required=True, help="Explicit instrument tick assumption")
    pc.add_argument("--capital", default="100000")
    pc.add_argument("--risk-pct", default="0.5")
    pc.add_argument("--max-alloc-pct", default="20")
    pc.add_argument("--slippage-bps", default="10")
    pc.add_argument("--fee-buffer", default="50")
    pc.add_argument("--out")
    u = sub.add_parser("universe", help="Download NSE EQ symbol universe from the official public CSV")
    u.add_argument("--csv", help="Use a saved NSE equity-list CSV instead of network")
    u.add_argument("--out", default="trader/universe.txt")
    nb = sub.add_parser("nse-bhavcopy", help="Official NSE final daily CM OHLCV, paper research only")
    nb.add_argument("--date", required=True, help="Trading date YYYY-MM-DD")
    nb.add_argument("--symbol", help="Optional NSE EQ symbol, e.g. RELIANCE")
    nb.add_argument("--out")
    nl = sub.add_parser("nse-live", help="Official NSE delayed CM Market Live quote, if its MCP service responds")
    nl.add_argument("symbol")
    nl.add_argument("--out")
    ds = sub.add_parser("daily-scan", help="NSE-wide daily setups from official final Bhavcopy reports")
    ds.add_argument("--as-of", help="Last report date YYYY-MM-DD (default today)")
    ds.add_argument("--reports", type=int, default=35, help="Number of completed daily reports, 30-60")
    ds.add_argument("--min-price", type=float, default=20, help="Minimum last close in INR")
    ds.add_argument("--min-volume", type=float, default=100000, help="Minimum last daily volume")
    ds.add_argument("--max-hits", type=int, default=50)
    ds.add_argument("--out")
    en = sub.add_parser("entry", help="Check a completed-bar paper entry from ORB or a price level")
    attach_bars(en, symbol_optional=True)
    en.add_argument("--ticker", help="Symbol identity for --csv/--json data, e.g. RELIANCE")
    en.add_argument("--setup", choices=("orb", "level"), default="orb")
    en.add_argument("--level", type=float)
    en.add_argument("--as-of")
    en.add_argument("--opening-minutes", type=int, default=15)
    en.add_argument("--min-volume-x", type=float, default=0)
    en.add_argument("--out")
    ex = sub.add_parser("exit", help="Set stop and target for a triggered paper entry")
    ex.add_argument("--from-entry", required=True, help="JSON result saved by entry --out")
    ex.add_argument("--stop", default="or-mid")
    ex.add_argument("--target", default="2R")
    ex.add_argument("--tick", required=True, help="Instrument-specific tick assumption")
    ex.add_argument("--bars-json", help="Saved five-minute bars for atr:K stop")
    ex.add_argument("--flat-by", default="15:10")
    ex.add_argument("--out")
    bu = sub.add_parser("budget", help="Size one proposed long cash paper ticket")
    bu.add_argument("--plan", required=True, help="Saved exit plan JSON")
    bu.add_argument("--capital", default="100000")
    bu.add_argument("--max-loss", default="500")
    bu.add_argument("--max-position-pct", default="20")
    bu.add_argument("--cash-buffer-pct", default="10")
    bu.add_argument("--slippage-bps", default="10")
    bu.add_argument("--fee-buffer", default="50")
    bu.add_argument("--out")
    ri = sub.add_parser("risk", help="Review an exact paper proposal against IST and budget limits")
    ri.add_argument("--order", required=True)
    ri.add_argument("--state", help="Optional paper portfolio state JSON")
    ri.add_argument("--as-of", help="Offset-aware IST review clock")
    ri.add_argument("--max-daily-loss", default="1500")
    ri.add_argument("--max-trades-per-day", type=int, default=3)
    ri.add_argument("--out")
    pt = sub.add_parser("paper-ticket", help="Show an exact risk-reviewed paper ticket")
    pt.add_argument("--order", required=True)
    pt.add_argument("--risk", required=True)
    pt.add_argument("--approve", action="store_true", help="Prompt for explicit YES; records approval only")
    pt.add_argument("--out")
    args = p.parse_args(argv)
    try:
        if args.cmd == "fetch":
            s = symbol(args.symbol, args.exchange)
            fetched = fetch_preferred(s, range_=args.range)
            rows = fetched["bars"]
            result = {"source":fetched["source"], "provider":fetched["provider"],
                      "provider_attempts":fetched["attempts"], "quality":fetched["quality"],
                      "entry_ready":fetched["entry_ready"],
                      "mode":"RESEARCH_ONLY", "ticker":s, "bar_interval":"5m",
                      "fetched_at":datetime.now(IST).isoformat(), "count":len(rows),
                      "orders_possible":False, "bars":rows}
        elif args.cmd == "data-status":
            from .market_data_manager import DEFAULT_AUDIT, PROVIDER_ORDER, provider_health
            result = {"mode": "READ_ONLY", "provider_priority": PROVIDER_ORDER,
                      "configured": {"UPSTOX": bool(os.environ.get("UPSTOX_ANALYTICS_TOKEN")),
                                     "FYERS": bool(os.environ.get("FYERS_APP_ID") and
                                                   os.environ.get("FYERS_ACCESS_TOKEN")),
                                     "NSE_PUBLIC": True, "YAHOO_UNOFFICIAL": True},
                      "health_this_process": provider_health(),
                      "audit_path": str(DEFAULT_AUDIT),
                      "live_connectivity": "NOT_CHECKED", "orders_possible": False}
        elif args.cmd == "orb":
            rows, source = load_bars(args)
            result = {**orb_signal(rows,args.opening_minutes,args.as_of,args.date), "data_origin":source,
                      "data_verified_against_exchange":False}
        elif args.cmd == "plan":
            result = plan(args.entry,args.stop,args.capital,args.risk_pct,args.target_r,
                          args.max_alloc_pct,args.tick,args.slippage_bps,args.fee_buffer)
        elif args.cmd == "backtest":
            if bool(args.csv)==bool(args.json):
                raise ValueError("Specify exactly one of --csv or --json")
            rows = read_csv(args.csv) if args.csv else read_json_bars(args.json)
            result = backtest_orb(rows,args.opening_minutes,args.reward_r)
        elif args.cmd == "scan":
            if args.folder:
                result = scan_csv_folder(args.folder,args.as_of,args.date)
            else:
                result = scan_watchlist(args.watchlist,args.as_of,args.date)
        elif args.cmd == "paper-cycle":
            result = paper_cycle(args.watchlist, args.as_of, args.tick, args.capital,
                                 args.risk_pct, args.max_alloc_pct, args.slippage_bps,
                                 args.fee_buffer)
        elif args.cmd == "universe":
            rows, origin = load_nse_equities(args.csv)
            result = write_universe(args.out, rows, origin)
        elif args.cmd == "nse-bhavcopy":
            result = bhavcopy_day(args.date, args.symbol)
        elif args.cmd == "nse-live":
            result = cm_live_quote(args.symbol)
        elif args.cmd == "daily-scan":
            result = scan_nse_daily(args.as_of, args.reports, args.min_price,
                                    args.min_volume, args.max_hits)
        elif args.cmd == "entry":
            rows, origin = load_bars(args)
            if args.ticker and args.symbol:
                raise ValueError("Use positional SYMBOL or --ticker with saved bars, not both")
            ticker = symbol(args.symbol or args.ticker, args.exchange) if (args.symbol or args.ticker) else "USER_SUPPLIED_BARS"
            result = {**entry_check(rows, ticker, args.setup, args.level, args.as_of,
                                    args.opening_minutes, args.min_volume_x),
                      "data_origin": origin, "data_verified_against_exchange": False}
        elif args.cmd == "exit":
            with open(args.from_entry, encoding="utf-8") as stream:
                entry = json.load(stream)
            bars = read_json_bars(args.bars_json) if args.bars_json else None
            result = exit_plan(entry, args.stop, args.target, args.tick, bars, args.flat_by)
        elif args.cmd == "budget":
            with open(args.plan, encoding="utf-8") as stream:
                exit_data = json.load(stream)
            result = size_paper_order(exit_data, args.capital, args.max_loss,
                                      args.max_position_pct, args.cash_buffer_pct,
                                      args.slippage_bps, args.fee_buffer)
        elif args.cmd == "risk":
            with open(args.order, encoding="utf-8") as stream:
                order = json.load(stream)
            if isinstance(order, dict) and "orders" in order:
                proposals = order["orders"]
                if not isinstance(proposals, list) or len(proposals) != 1:
                    raise ValueError("Risk review needs exactly one paper proposal")
                order = proposals[0]
            state = None
            if args.state:
                with open(args.state, encoding="utf-8") as stream:
                    state = json.load(stream)
            result = risk_review(order, args.as_of, state, args.max_daily_loss,
                                 args.max_trades_per_day)
        elif args.cmd == "paper-ticket":
            with open(args.order, encoding="utf-8") as stream:
                order = json.load(stream)
            if isinstance(order, dict) and "orders" in order:
                proposals = order["orders"]
                if not isinstance(proposals, list) or len(proposals) != 1:
                    raise ValueError("Paper ticket needs exactly one risk-reviewed proposal")
                order = proposals[0]
            with open(args.risk, encoding="utf-8") as stream:
                review = json.load(stream)
            if isinstance(review, dict) and "verdicts" in review:
                verdicts = review["verdicts"]
                if not isinstance(verdicts, list) or len(verdicts) != 1:
                    raise ValueError("Paper ticket needs one exact risk verdict")
                verdict = verdicts[0]
                review = {"status": verdict.get("status"),
                          "order_fingerprint": verdict.get("order_fingerprint"),
                          "checked_at": review.get("checked_at")}
            approved = args.approve and input("Type YES to approve this exact PAPER ticket (no fill): ").strip() == "YES"
            result = paper_ticket(order, review, approved)
        else:
            result = {"status":"OK", "python":sys.version.split()[0],
                      "timezone":"UTC+05:30 fixed IST", "orders_possible":False,
                      "market_data_connectivity":"NOT_CHECKED",
                      "provider":"Upstox, FYERS, eligible NSE, Yahoo; live connectivity not tested"}
        if getattr(args,"out",None) and args.cmd != "universe":
            save_json(args.out,result)
        print(json.dumps(result,indent=2,allow_nan=False,ensure_ascii=False))
    except (ValueError, OSError, KeyError, TypeError, OverflowError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}",file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
