"""Foreground, paper-only NSE market scanner and candidate monitor.

The engine schedules work while its process is running. It never places an
order. Stage 4 can consume verified, once-only candidate events from state.
"""
from __future__ import annotations

import argparse
from datetime import datetime, time, timedelta
import json
import math
import os
from pathlib import Path
import sys
import time as clock_time

from .core import IST, DataError
from .entry_timer import evaluate as evaluate_entry, parse_chart as parse_entry_chart
from .intraday_sources import _bars_chart
from .market_data_manager import FeedUnavailable, MarketDataManager, _valid_ticker
from . import market_scanner as scanner

DEFAULT_STATE = Path("trader/state/market-engine.json")
USER_SETTINGS = Path("trader/settings/market_engine.json")
SESSION_OPEN = time(9, 15)
FIRST_BROAD = time(9, 35)
LAST_NEW_SIGNAL = time(15, 10)
SESSION_END = time(15, 15)


class EngineError(ValueError):
    pass


def _settings(**overrides):
    values = {"broad_batch_size": 10, "detailed_batch_size": 2,
              "max_candidates": 8, "candidate_ttl_minutes": 45,
              "min_price": 20.0, "min_broad_volume": 10000.0,
              "min_turnover_inr": 1000000.0, "min_range_pct": 0.1,
              "max_range_pct": 8.0, "max_spread_pct": 0.5,
              "max_signal_age_minutes": 5.0}
    unknown = set(overrides) - set(values)
    if unknown:
        raise EngineError("unknown market-engine settings: " + ", ".join(sorted(unknown)))
    values.update(overrides)
    if any(not isinstance(values[k], int) or isinstance(values[k], bool) or values[k] < 1
           for k in ("broad_batch_size", "detailed_batch_size", "max_candidates",
                     "candidate_ttl_minutes")):
        raise EngineError("market-engine batch sizes, candidate limit and TTL must be positive integers")
    if any(not isinstance(values[k], (int, float)) or isinstance(values[k], bool) or
           not math.isfinite(values[k]) or values[k] < 0
           for k in ("min_price", "min_broad_volume", "min_turnover_inr", "min_range_pct",
                     "max_range_pct", "max_spread_pct", "max_signal_age_minutes")):
        raise EngineError("market-engine numeric filters must be finite and nonnegative")
    if values["max_range_pct"] <= values["min_range_pct"] or values["max_spread_pct"] <= 0:
        raise EngineError("market-engine volatility and spread limits are inconsistent")
    # FYERS Standard currently permits 5,000 Data API requests/day. Count one
    # FYERS request per broad symbol and up to four per detailed symbol-minute
    # (two histories, quote, status), with headroom for manual checks.
    projected_fyers = values["broad_batch_size"] * 68 + values["detailed_batch_size"] * 4 * 336
    if os.environ.get("FYERS_APP_ID") and os.environ.get("FYERS_ACCESS_TOKEN") and \
            projected_fyers > 4500:
        raise EngineError("configured scan pace could exceed FYERS Standard data limits")
    return values


def _empty(day):
    return {"version": 1, "day": day, "broad_cursor": 0, "detail_cursor": 0,
            "last_broad_slot": None, "last_detail_slot": None,
            "last_broad": None, "last_detailed": None,
            "candidates": {}, "signals": [], "last_cycle": None,
            "mode": "PAPER_RESEARCH_ONLY", "orders_possible": False}


def _read_state(path):
    if not path.is_file():
        return _empty(None)
    if path.stat().st_size > 2_000_000:
        raise EngineError("market-engine state exceeds size limit")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise EngineError("market-engine state is corrupt; inspect before restarting") from exc
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("candidates"), dict) or \
            not isinstance(doc.get("signals"), list) or doc.get("orders_possible") is not False:
        raise EngineError("market-engine state has an unexpected schema")
    if any(not isinstance(candidate, dict) for candidate in doc["candidates"].values()) or \
            any(not isinstance(signal, dict) for signal in doc["signals"]) or \
            doc.get("last_broad") is not None and not isinstance(doc["last_broad"], dict):
        raise EngineError("market-engine state has malformed candidate or coverage records")
    return doc


def _write_state(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temp.replace(path)


def _metrics(bars, quote):
    if not bars:
        raise EngineError("no completed five-minute bars")
    day = datetime.fromisoformat(bars[-1]["timestamp"]).date()
    today = [b for b in bars if datetime.fromisoformat(b["timestamp"]).date() == day]
    volume = sum(b["volume"] for b in today)
    turnover = sum(b["close"] * b["volume"] for b in today)
    open_price = today[0]["open"]
    range_pct = 100 * (max(b["high"] for b in today) - min(b["low"] for b in today)) / open_price
    vwap = sum(((b["high"] + b["low"] + b["close"]) / 3) * b["volume"] for b in today) / volume
    closes = [b["close"] for b in bars]
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    rsi14 = None
    if len(closes) >= 15:
        changes = [closes[i] - closes[i - 1] for i in range(len(closes) - 14, len(closes))]
        gain = sum(max(0, x) for x in changes) / 14
        loss = sum(max(0, -x) for x in changes) / 14
        rsi14 = 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    atr14 = None
    if len(bars) >= 15:
        ranges = [max(bars[i]["high"] - bars[i]["low"],
                      abs(bars[i]["high"] - closes[i - 1]),
                      abs(bars[i]["low"] - closes[i - 1]))
                  for i in range(len(bars) - 14, len(bars))]
        atr14 = sum(ranges) / 14
    bid, ask = quote.get("bid"), quote.get("ask")
    spread_pct = None
    if bid is not None and ask is not None:
        bid, ask = float(bid), float(ask)
        if all(math.isfinite(x) and x > 0 for x in (bid, ask)) and ask >= bid:
            spread_pct = 100 * (ask - bid) / ((ask + bid) / 2)
    return {"day_volume": volume, "turnover_inr": round(turnover, 2),
            "range_pct": round(range_pct, 3), "vwap": round(vwap, 3),
            "spread_pct": None if spread_pct is None else round(spread_pct, 4),
            "ma20": None if ma20 is None else round(ma20, 3),
            "rsi14": None if rsi14 is None else round(rsi14, 2),
            "atr14": None if atr14 is None else round(atr14, 3)}


class ContinuousMarketEngine:
    """One bounded scan cycle at a time; no account or order API dependency."""

    def __init__(self, symbols, state_path=DEFAULT_STATE, manager=None, broad_fetch=None,
                 **settings):
        self.symbols = list(dict.fromkeys(symbols))
        if not self.symbols or any(not s.endswith(".NS") for s in self.symbols):
            raise EngineError("provide at least one canonical NSE EQ ticker")
        for ticker in self.symbols:
            _valid_ticker(ticker)
        self.state_path = Path(state_path)
        self.manager = manager or MarketDataManager()
        self.broad_fetch = broad_fetch or self._fetch_broad
        self.settings = _settings(**settings)
        self.state = _read_state(self.state_path)
        self._last_provider_call = None

    def _pace(self):
        if not (os.environ.get("FYERS_APP_ID") and os.environ.get("FYERS_ACCESS_TOKEN")):
            return
        now = clock_time.monotonic()
        if self._last_provider_call is not None:
            clock_time.sleep(max(0, 0.3 - (now - self._last_provider_call)))
        self._last_provider_call = clock_time.monotonic()

    def _fetch_broad(self, symbols, now):
        pause = 0.3 if os.environ.get("FYERS_APP_ID") and os.environ.get("FYERS_ACCESS_TOKEN") else 0
        result = scanner.fetch_spark(symbols, "1d", "5m", pause, day=now.date(), manager=self.manager)
        if pause:
            self._last_provider_call = clock_time.monotonic()
        return result

    def _expire(self, now):
        limit = now - timedelta(minutes=self.settings["candidate_ttl_minutes"])
        removed = []
        for ticker, candidate in list(self.state["candidates"].items()):
            try:
                first = datetime.fromisoformat(candidate["first_seen"])
                expired = first.tzinfo is None or first < limit or first.date() != now.date()
            except (ValueError, KeyError, TypeError):
                expired = True
            if expired:
                removed.append(ticker)
                del self.state["candidates"][ticker]
        return removed

    def _broad(self, now):
        size = min(self.settings["broad_batch_size"], len(self.symbols))
        start = int(self.state["broad_cursor"]) % len(self.symbols)
        batch = [self.symbols[(start + i) % len(self.symbols)] for i in range(size)]
        self.state["broad_cursor"] = (start + size) % len(self.symbols)
        try:
            data, failures = self.broad_fetch(batch, now)
        except (FeedUnavailable, DataError, OSError, ValueError, TypeError, KeyError):
            data, failures = {}, {ticker: "PROVIDER_UNAVAILABLE" for ticker in batch}
        if not isinstance(data, dict) or not isinstance(failures, dict):
            raise EngineError("broad scanner returned an invalid result")
        cutoff = now.replace(tzinfo=None, second=0, microsecond=0)
        counts = {"attempted": len(batch), "fetched": 0, "usable": 0,
                  "shortlisted": 0, "failed": 0,
                  "not_attempted_this_cycle": len(self.symbols) - len(batch)}
        for ticker in batch:
            series = data.get(ticker)
            if not series:
                counts["failed"] += 1
                continue
            counts["fetched"] += 1
            try:
                bars = scanner.session_bars(series, now.date(), cutoff)
                if not scanner.usable(bars, now.date(), cutoff, 3):
                    continue
                volume = sum(b["v"] or 0 for b in bars)
                if any(b["v"] is None or b["v"] <= 0 for b in bars):
                    continue
                counts["usable"] += 1
                if bars[-1]["c"] < self.settings["min_price"] or \
                        volume < self.settings["min_broad_volume"] or not scanner.orb_candidate(bars, 3):
                    continue
                counts["shortlisted"] += 1
                existing = self.state["candidates"].get(ticker)
                if existing is None and len(self.state["candidates"]) < self.settings["max_candidates"]:
                    self.state["candidates"][ticker] = {
                        "ticker": ticker, "first_seen": now.isoformat(), "last_seen": now.isoformat(),
                        "broad_source": (series.get("meta") or {}).get("data_provider", "UNSPECIFIED"),
                        "status": "RESEARCH_CANDIDATE", "orders_possible": False}
                elif existing is not None:
                    existing["last_seen"] = now.isoformat()
            except (KeyError, ValueError, TypeError, IndexError, OSError):
                continue
        return counts

    def _detail_one(self, ticker, now):
        candidate = self.state["candidates"][ticker]
        try:
            self._pace()
            five = self.manager.fetch_candles(ticker, "1d", interval=5,
                                              require_entry_ready=True, allow_yahoo=False)
            self._pace()
            one = self.manager.fetch_candles(ticker, "1d", interval=1,
                                             require_entry_ready=True, allow_yahoo=False)
            self._pace()
            quote = self.manager.fetch_quote(ticker, require_fresh=True)
            if not (five.get("entry_ready") and one.get("entry_ready") and
                    quote.get("entry_ready")):
                raise EngineError("broker data did not pass all entry-readiness gates")
            metrics = _metrics(five["bars"], quote)
            candidate["metrics"] = metrics
            candidate["five_minute_provider"] = five["provider"]
            candidate["one_minute_provider"] = one["provider"]
            candidate["quote_provider"] = quote["provider"]
            candidate["last_verified_at"] = now.isoformat()
            reasons = []
            if metrics["turnover_inr"] < self.settings["min_turnover_inr"]:
                reasons.append("ILLIQUID")
            if not self.settings["min_range_pct"] <= metrics["range_pct"] <= self.settings["max_range_pct"]:
                reasons.append("VOLATILITY_OUTSIDE_LIMIT")
            if metrics["spread_pct"] is None:
                reasons.append("SPREAD_UNVERIFIED")
            elif metrics["spread_pct"] > self.settings["max_spread_pct"]:
                reasons.append("SPREAD_TOO_WIDE")
            raw5, _ = _bars_chart(ticker, five["bars"], 5, five["source"])
            chart5 = parse_entry_chart(raw5, ticker, "5m", five["source"])
            chart5["entry_ready"] = True
            result5 = evaluate_entry(chart5, now, interval="5m", live=True)
            raw1, _ = _bars_chart(ticker, one["bars"], 1, one["source"])
            chart1 = parse_entry_chart(raw1, ticker, "1m", one["source"])
            chart1["entry_ready"] = True
            last1 = max((bar for bar in chart1["bars"] if bar["time"].date() == now.date() and
                         bar["time"] + timedelta(minutes=1) <= now),
                        key=lambda bar: bar["time"], default=None)
            candidate["one_minute_last_bar"] = last1["time"].isoformat() if last1 else None
            candidate["five_minute_status"] = result5["status"]
            candidate["checks"] = reasons
            if not last1 or last1["time"] + timedelta(minutes=1) < now - timedelta(minutes=2):
                reasons.append("ONE_MINUTE_STALE")
            if last1 and abs(float(quote["price"]) / last1["close"] - 1) > 0.02:
                reasons.append("QUOTE_BAR_DIVERGENCE")
            if result5["status"] == "TRIGGERED" and last1 and \
                    last1["close"] <= result5["level"]:
                reasons.append("ONE_MINUTE_BELOW_BREAKOUT")
            if result5["status"] != "TRIGGERED" or reasons:
                candidate["status"] = "MONITORING" if not reasons else "QUALITY_BLOCKED"
                return None
            trigger = datetime.fromisoformat(result5["trigger_at"])
            if (now - trigger).total_seconds() / 60 > self.settings["max_signal_age_minutes"]:
                candidate["status"] = "EXPIRED_TRIGGER"
                return None
            signal_id = f"{now.date()}|{ticker}|orb|{result5['trigger_bar_start']}"
            if any(item.get("id") == signal_id for item in self.state["signals"]):
                candidate["status"] = "SIGNAL_ALREADY_RECORDED"
                return None
            event = {"id": signal_id, "ticker": ticker, "setup": "orb",
                     "trigger_at": result5["trigger_at"], "recorded_at": now.isoformat(),
                     "exchange": "NSE", "instrument_id": "NSE:" + ticker[:-3] + "-EQ",
                     "instrument_id_type": "NSE_EQ_SYMBOL",
                     "five_minute_bar_complete": True,
                     "one_minute_last_bar": candidate["one_minute_last_bar"],
                     "one_minute_last_close": last1["close"],
                     "five_minute_last_close": five["bars"][-1]["close"],
                     "opening_range_high": result5["level"],
                     "quote_at": quote["time"], "quote_price": quote["price"],
                     "market_status_verified": True,
                     "data_gaps": 0, "volume_quality": "POSITIVE",
                     "source_5m": five["provider"], "source_1m": one["provider"],
                     "quote_source": quote["provider"], "metrics": metrics,
                     "status": "AWAITING_AI_REVIEW", "orders_possible": False}
            self.state["signals"].append(event)
            candidate["status"] = "AWAITING_AI_REVIEW"
            return event
        except (FeedUnavailable, DataError, EngineError, OSError, ValueError, TypeError,
                KeyError, IndexError) as exc:
            candidate["status"] = "DATA_UNAVAILABLE"
            candidate["checks"] = ["VERIFIED_FEED_OR_CALCULATION_FAILED"]
            candidate["last_error_type"] = type(exc).__name__
            return None

    def _detail(self, now):
        tickers = sorted(self.state["candidates"])
        if not tickers:
            return {"attempted": 0, "verified_live": 0, "new_signals": []}
        size = min(self.settings["detailed_batch_size"], len(tickers))
        start = int(self.state["detail_cursor"]) % len(tickers)
        batch = [tickers[(start + i) % len(tickers)] for i in range(size)]
        self.state["detail_cursor"] = (start + size) % len(tickers)
        signals = []
        for ticker in batch:
            signal = self._detail_one(ticker, now)
            if signal:
                signals.append(signal)
        verified = sum(self.state["candidates"][s].get("last_verified_at") == now.isoformat()
                       for s in batch)
        return {"attempted": len(batch), "verified_live": verified,
                "new_signals": signals, "not_attempted_this_cycle": len(tickers) - len(batch)}

    def cycle(self, now=None):
        now = datetime.now(IST) if now is None else now
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise EngineError("market-engine clock must include a timezone")
        now = now.astimezone(IST)
        if self.state["day"] != now.date().isoformat():
            self.state = _empty(now.date().isoformat())
        expired = self._expire(now)
        eligible, reason = scanner.trading_day(now.date())
        active = eligible and SESSION_OPEN <= now.time() < SESSION_END
        broad = None
        detail = None
        if active and FIRST_BROAD <= now.time() <= LAST_NEW_SIGNAL:
            broad_slot = int((datetime.combine(now.date(), now.time()) -
                              datetime.combine(now.date(), SESSION_OPEN)).total_seconds() // 300)
            if self.state["last_broad_slot"] != broad_slot:
                broad = self._broad(now)
                self.state["last_broad_slot"] = broad_slot
                self.state["last_broad"] = {"at": now.isoformat(), "counts": broad}
        if active and now.time() <= LAST_NEW_SIGNAL:
            detail_slot = int((datetime.combine(now.date(), now.time()) -
                               datetime.combine(now.date(), SESSION_OPEN)).total_seconds() // 60)
            if self.state["last_detail_slot"] != detail_slot:
                detail = self._detail(now)
                self.state["last_detail_slot"] = detail_slot
                self.state["last_detailed"] = {"at": now.isoformat(),
                                               "attempted": detail["attempted"],
                                               "verified_live": detail["verified_live"]}
        status = ("MARKET_CLOSED" if not eligible else "WAIT_SESSION" if not active else
                  "MONITORING" if now.time() <= LAST_NEW_SIGNAL else "NEW_SIGNALS_CLOSED")
        last_broad = self.state.get("last_broad") or {}
        last_broad_counts = last_broad.get("counts") or {}
        live_now = 0
        for candidate in self.state["candidates"].values():
            try:
                checked = datetime.fromisoformat(candidate["last_verified_at"])
                if active and checked.tzinfo is not None and \
                        0 <= (now - checked).total_seconds() <= 120 and \
                        candidate.get("status") != "DATA_UNAVAILABLE":
                    live_now += 1
            except (KeyError, ValueError, TypeError):
                pass
        coverage = {"universe": len(self.symbols), "attempted_this_broad_cycle":
                    broad["attempted"] if broad else 0,
                    "usable_broad_charts": broad["usable"] if broad else 0,
                    "reliable_live_this_detail_cycle": detail["verified_live"] if detail else 0,
                    "latest_broad_scan_at": last_broad.get("at"),
                    "latest_broad_scanned": last_broad_counts.get("usable", 0),
                    "reliable_live_now": live_now,
                    "not_covered_by_broad_scan_this_cycle": len(self.symbols) -
                    (broad["attempted"] if broad else 0),
                    "not_proven_reliable_live": len(self.symbols) - live_now,
                    "candidate_pool": len(self.state["candidates"])}
        result = {"at": now.isoformat(), "status": status, "reason": reason if not eligible else None,
                  "broad": broad, "detailed": detail, "expired": expired,
                  "coverage": coverage, "orders_possible": False}
        self.state["last_cycle"] = result
        self.state["signals"] = self.state["signals"][-500:]
        _write_state(self.state_path, self.state)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m india_trader market-engine",
                                     description="Continuous NSE paper-research scanner; no orders")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("once", "run", "status"):
        p = sub.add_parser(name)
        p.add_argument("--state", default=str(DEFAULT_STATE))
        if name != "status":
            p.add_argument("--universe", default=scanner.DEFAULT_UNIVERSE)
            p.add_argument("--symbols", help="comma-separated NSE EQ tickers, for bounded research")
            p.add_argument("--broad-batch-size", type=int)
            p.add_argument("--detailed-batch-size", type=int)
            p.add_argument("--max-candidates", type=int)
            p.add_argument("--max-cycles", type=int, default=None)
    a = parser.parse_args(argv)
    try:
        if a.command == "status":
            print(json.dumps(_read_state(Path(a.state)), indent=2))
            return 0
        if a.max_cycles is not None and a.max_cycles < 1:
            raise EngineError("--max-cycles must be positive")
        if a.symbols:
            from .entry_timer import ticker as canonical
            symbols = [canonical(s.strip()) for s in a.symbols.split(",") if s.strip()]
        else:
            symbols = scanner.symbols_from_file(a.universe)
        saved_settings = {}
        if USER_SETTINGS.is_file():
            if USER_SETTINGS.stat().st_size > 64_000:
                raise EngineError("market settings exceed size limit")
            saved_settings = json.loads(USER_SETTINGS.read_text(encoding="utf-8"))
            if not isinstance(saved_settings, dict):
                raise EngineError("market settings must be a JSON object")
        for key, value in (("broad_batch_size", a.broad_batch_size),
                           ("detailed_batch_size", a.detailed_batch_size),
                           ("max_candidates", a.max_candidates)):
            if value is not None:
                saved_settings[key] = value
        effective_settings = _settings(**saved_settings)
        engine = ContinuousMarketEngine(symbols, state_path=a.state,
                                        **effective_settings)
        cycles = 0
        while True:
            result = engine.cycle()
            print(json.dumps(result, allow_nan=False), flush=True)
            cycles += 1
            if a.command == "once" or a.max_cycles is not None and cycles >= a.max_cycles:
                return 0
            clock_time.sleep(30)
    except KeyboardInterrupt:
        return 0
    except (EngineError, scanner.ScanError, DataError, OSError, ValueError, TypeError, KeyError) as exc:
        print("market-engine: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
