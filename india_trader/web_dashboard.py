"""Read-only, sanitized dashboard projection of the local paper components."""
from __future__ import annotations

from datetime import datetime, time
import os
from pathlib import Path

from .core import IST
from .market_engine import _read_state as read_market_state
from .market_scanner import trading_day
from .paper_accounting import report as paper_report
from .paper_lifecycle import _state as read_paper_state, load_config as load_paper_config
from .web_settings import load_settings


def _clock(value):
    try:
        at = datetime.fromisoformat(value)
        return at.astimezone(IST) if at.tzinfo else None
    except (TypeError, ValueError):
        return None


def _age(value, now):
    at = _clock(value)
    return round((now - at).total_seconds()) if at else None


def _json_state(path, limit=5_000_000):
    import json
    path = Path(path)
    if not path.is_file():
        return None
    if path.stat().st_size > limit:
        raise ValueError("state file too large")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("orders_possible") is not False:
        raise ValueError("state is not paper-only")
    return doc


def snapshot(root, now=None):
    root = Path(root)
    now = datetime.now(IST) if now is None else now.astimezone(IST)
    day = now.date().isoformat()
    open_day, calendar_reason = trading_day(now.date())
    market_open = open_day and time(9, 15) <= now.time() < time(15, 15)
    market_status = "OPEN" if market_open else "CLOSED"
    errors = []
    try:
        engine = read_market_state(root / "trader/state/market-engine.json")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        engine = {}
        errors.append("scanner state: " + type(exc).__name__)
    try:
        committee = _json_state(root / "trader/state/ai-committee.json") or {}
    except (OSError, ValueError, TypeError) as exc:
        committee = {}
        errors.append("AI state: " + type(exc).__name__)
    try:
        ledger_started = (root / "trader/state/paper-lifecycle.json").is_file()
        config = load_paper_config(root / "trader/settings/paper_lifecycle.json"
                                   if (root / "trader/settings/paper_lifecycle.json").is_file()
                                   else root / "examples/paper_lifecycle_config.json")
        paper = read_paper_state(root / "trader/state/paper-lifecycle.json", config)
        account = paper_report(paper, now=now,
                               max_mark_age_seconds=config["max_quote_age_seconds"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        paper, account, ledger_started = {}, None, False
        errors.append("paper account: " + type(exc).__name__)
    settings = {}
    for kind in ("ai", "paper", "market", "notifications"):
        try:
            settings[kind] = load_settings(root, kind)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            settings[kind] = None
            errors.append(kind + " settings: " + type(exc).__name__)
    candidates = engine.get("candidates") or {}
    if not isinstance(candidates, dict):
        candidates = {}
    candidate_rows = []
    for ticker, row in candidates.items():
        if not isinstance(row, dict):
            continue
        candidate_rows.append({"ticker": ticker, "status": row.get("status"),
                               "setup": row.get("setup"), "metrics": row.get("metrics"),
                               "checks": row.get("checks") or [],
                               "last_verified_at": row.get("last_verified_at"),
                               "five_minute_provider": row.get("five_minute_provider"),
                               "one_minute_provider": row.get("one_minute_provider"),
                               "quote_provider": row.get("quote_provider")})
    candidate_rows.sort(key=lambda row: row.get("last_verified_at") or "", reverse=True)
    decisions = [row for row in committee.get("decisions", []) if isinstance(row, dict)]
    decisions.sort(key=lambda row: row.get("decided_at") or "", reverse=True)
    signals = [row for row in engine.get("signals", []) if isinstance(row, dict)]
    signals.sort(key=lambda row: row.get("recorded_at") or "", reverse=True)
    live_provider = next((row for row in candidate_rows
                          if row.get("quote_provider") in ("UPSTOX", "FYERS") and
                          _age(row.get("last_verified_at"), now) is not None and
                          0 <= _age(row.get("last_verified_at"), now) <= 120), None)
    paper_positions = []
    marks = paper.get("last_marks") or {}
    for position in paper.get("positions", []):
        if not isinstance(position, dict):
            continue
        mark = marks.get(position.get("ticker")) or {}
        age = _age(mark.get("at"), now)
        max_mark_age = config["max_quote_age_seconds"] if account else 30
        paper_positions.append({**position, "mark_price_inr": mark.get("price_inr")
                                if age is not None and 0 <= age <= max_mark_age else None,
                                "mark_at": mark.get("at"),
                                "mark_fresh": age is not None and 0 <= age <= max_mark_age})
    today = next((row for row in (account or {}).get("daily", [])
                  if row["date"] == day), None)
    last_paper = paper.get("last_cycle") or {}
    equity_age = _age(last_paper.get("at"), now)
    portfolio_value = (None if not ledger_started else
                       paper.get("cash_inr") if not paper_positions else
                       last_paper.get("marked_equity_inr") if equity_age is not None and
                       0 <= equity_age <= (config["max_quote_age_seconds"] if account else 30) and
                       last_paper.get("valuation_status") == "FRESH_BROKER_MARK"
                       else None)
    engine_last = engine.get("last_cycle") or {}
    ai_last = committee.get("last_run") or {}
    scanner_age = _age(engine_last.get("at"), now)
    ai_age = _age(ai_last.get("at"), now)
    paper_age = _age(last_paper.get("at"), now)
    def health(age, has_state):
        if not has_state:
            return "NOT_STARTED"
        if market_open and (age is None or age < 0 or age > 120):
            return "STALE"
        return "RUNNING" if market_open else "IDLE_MARKET_CLOSED"
    return {"as_of": now.isoformat(), "paper_only": True,
            "market": {"status": market_status, "reason": calendar_reason if not open_day else None,
                       "timezone": "Asia/Kolkata", "session": "09:15–15:15 IST"},
            "summary": {"portfolio_value_inr": portfolio_value,
                        "ledger_started": ledger_started,
                        "today_realized_pnl_inr": today["realized_net_pnl_inr"] if today else 0.0,
                        "total_realized_pnl_inr": paper.get("realized_pnl_inr", 0.0),
                        "unrealized_pnl_inr": (account or {}).get("unrealized_pnl_inr"),
                        "unrealized_status": (account or {}).get("valuation_status", "UNAVAILABLE"),
                        "open_positions": len(paper_positions)},
            "scanner": {"status": engine_last.get("status", "NOT_STARTED"),
                        "updated_at": engine_last.get("at"),
                        "coverage": engine_last.get("coverage") or {},
                        "candidates": candidate_rows[:50], "signals": signals[:30]},
            "committee": {"status": ai_last.get("status", "NOT_STARTED"),
                          "updated_at": ai_last.get("at"),
                          "analyst_count": sum(bool(x.get("enabled")) for x in
                                               (settings.get("ai") or {}).get("analysts", [])),
                          "agents": (settings.get("ai") or {}).get("analysts", []) +
                                    ([(settings["ai"]["judge"])] if settings.get("ai") else []),
                          "decisions": decisions[:30],
                          "last_reason": ai_last.get("reason"),
                          "usage": committee.get("usage") or {}},
            "trading": {"positions": paper_positions,
                        "orders": (paper.get("orders") or [])[-30:],
                        "trades": (paper.get("trades") or [])[-30:],
                        "daily": (account or {}).get("daily", [])[-14:],
                        "balance": (account or {}).get("balance"),
                        "last_cycle": last_paper},
            "providers": {"active": live_provider.get("quote_provider") if live_provider else None,
                          "upstox_configured": bool(os.environ.get("UPSTOX_ANALYTICS_TOKEN")),
                          "fyers_configured": bool(os.environ.get("FYERS_APP_ID") and
                                                   os.environ.get("FYERS_ACCESS_TOKEN")),
                          "yahoo_research_available": True},
            "settings": settings,
            "health": {"scanner": health(scanner_age, bool(engine_last)),
                       "committee": health(ai_age, bool(ai_last)),
                       "paper": health(paper_age, bool(last_paper)),
                       "errors": errors}}
