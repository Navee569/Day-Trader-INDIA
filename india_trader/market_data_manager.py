"""Read-only market-data selection and validation. No trading API imports.

Five-minute candles are start-stamped in IST. NSE public daily reports and the
delayed CM quote service are never represented as intraday candles. A Yahoo
candle can support labeled research, but cannot authorize a new paper entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import json
import math
import os
from pathlib import Path
import urllib.error

from .core import DataError, IST

PROVIDER_ORDER = ("UPSTOX", "FYERS", "NSE", "YAHOO")
OPEN = time(9, 15)
CLOSE = time(15, 15)
DEFAULT_AUDIT = Path("trader/state/data-provider-events.jsonl")


class FeedUnavailable(DataError):
    """No provider passed the requested quality gate; entries must stop."""

    def __init__(self, attempts):
        self.attempts = attempts
        super().__init__("No validated intraday candle provider is available")


def _category(exc):
    if isinstance(exc, FeedUnavailable):
        return "MARKET_STATUS_OR_FEED_UNAVAILABLE"
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "AUTH"
        if exc.code == 429:
            return "RATE_LIMIT"
        if exc.code >= 500:
            return "OUTAGE"
        return "HTTP_ERROR"
    if isinstance(exc, (TimeoutError, urllib.error.URLError, ConnectionError)):
        return "OUTAGE"
    if isinstance(exc, (DataError, ValueError, TypeError, KeyError, OverflowError)):
        return "INVALID_DATA"
    return "UNAVAILABLE"


def _clock(value):
    now = datetime.now(IST) if value is None else value
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("market-data clock must be timezone-aware")
    return now.astimezone(IST)


def _valid_ticker(ticker):
    from .core import symbol
    if not isinstance(ticker, str) or not ticker.endswith((".NS", ".BO")):
        raise DataError("market data needs an explicit NSE/BSE cash-equity ticker")
    exchange = "NSE" if ticker.endswith(".NS") else "BSE"
    if symbol(ticker, exchange) != ticker:
        raise DataError("invalid or noncanonical cash-equity ticker")
    return exchange


def validate_candles(rows, ticker, interval=5, now=None, end_day=None):
    """Return completed, contiguous session bars and a conservative quality verdict.

    Historical bars are valid research data. Entry readiness additionally needs
    a current-session, recent, broker-sourced completed bar and a verified normal
    market session; the provider-specific origin gate is applied by the manager.
    """
    exchange = _valid_ticker(ticker)
    if interval not in (1, 5) or not isinstance(rows, list) or not rows:
        raise DataError("missing or unsupported one/five-minute candles")
    clock = _clock(now)
    last_allowed = end_day or clock.date()
    if isinstance(last_allowed, str):
        last_allowed = date.fromisoformat(last_allowed)
    if not isinstance(last_allowed, date):
        raise DataError("invalid requested session date")
    selected, seen = [], set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise DataError("candle must be an object")
        try:
            at = datetime.fromisoformat(str(raw["timestamp"]).replace("Z", "+00:00"))
            if at.tzinfo is None:
                raise ValueError("missing timezone")
            at = at.astimezone(IST)
            o, h, l, c = (float(raw[k]) for k in ("open", "high", "low", "close"))
            v = float(raw["volume"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise DataError("candle lacks valid timestamp or OHLCV") from exc
        if (at.second or at.microsecond or (at.minute - 15) % interval or
                not all(math.isfinite(x) for x in (o, h, l, c, v)) or
                l <= 0 or l > min(o, c) or h < max(o, c) or v <= 0):
            raise DataError("invalid candle alignment, OHLC, or volume")
        if at in seen:
            raise DataError("duplicate candle timestamp")
        seen.add(at)
        if at.date() > last_allowed or at > clock + timedelta(seconds=30):
            raise DataError("future or wrong-session candle")
        if OPEN <= at.time() < CLOSE:
            if at + timedelta(minutes=interval) <= clock:
                selected.append({"timestamp": at.isoformat(), "open": o, "high": h,
                                 "low": l, "close": c, "volume": v})
    if not selected:
        raise DataError("no completed normal-session candles")
    selected.sort(key=lambda b: b["timestamp"])
    by_day = {}
    for row in selected:
        at = datetime.fromisoformat(row["timestamp"])
        by_day.setdefault(at.date(), []).append(at)
    for stamps in by_day.values():
        if any(right - left != timedelta(minutes=interval)
               for left, right in zip(stamps, stamps[1:])):
            raise DataError("missing or discontinuous intraday candle")
    latest = datetime.fromisoformat(selected[-1]["timestamp"])
    latest_end = latest + timedelta(minutes=interval)
    age = (clock - latest_end).total_seconds()
    live = latest.date() == clock.date() and 0 <= age <= 120 and OPEN <= clock.time() < CLOSE
    reasons = [] if live else ["NOT_RECENT_COMPLETED_BAR"]
    if live:
        from .market_scanner import trading_day
        session_ok, _ = trading_day(clock.date())
        if not session_ok:
            live = False
            reasons.append("SESSION_NOT_NORMAL")
        elif by_day[clock.date()][0].time() != OPEN:
            live = False
            reasons.append("OPENING_BAR_MISSING")
    if exchange == "BSE" and live:
        from .calendar_source import status
        if status(clock.date(), "BSE")["state"] != "NORMAL":
            live = False
            reasons.append("BSE_SESSION_UNVERIFIED")
    return {"bars": selected, "last_bar_start": latest.isoformat(),
            "last_bar_end": latest_end.isoformat(), "age_seconds": max(0, round(age, 1)),
            "fresh_completed_bar": live, "reasons": reasons, "exchange": exchange,
            "interval_minutes": interval}


@dataclass
class ProviderHealth:
    state: dict = field(default_factory=dict)

    def available(self, provider, now):
        item = self.state.get(provider, {})
        until = item.get("cooldown_until")
        return until is None or now >= until

    def record(self, provider, outcome, now, category=None):
        item = self.state.setdefault(provider, {"failures": 0})
        item["last_checked_at"] = now.isoformat()
        item["status"] = outcome
        if outcome == "OK":
            item["failures"] = 0
            item.pop("cooldown_until", None)
            item.pop("reason", None)
        else:
            item["failures"] += 1
            item["reason"] = category or outcome
            wait = {"AUTH": 300, "RATE_LIMIT": 60, "OUTAGE": 15}.get(category, 0)
            if wait:
                item["cooldown_until"] = now + timedelta(seconds=wait)

    def snapshot(self):
        return {name: {k: (v.isoformat() if isinstance(v, datetime) else v)
                       for k, v in item.items()} for name, item in self.state.items()}


class MarketDataManager:
    def __init__(self, audit_path=DEFAULT_AUDIT, now=None):
        self.audit_path = None if audit_path is None else Path(audit_path)
        self.now = now
        self.health = ProviderHealth()
        self.quote_health = ProviderHealth()
        self.status_health = ProviderHealth()
        self.daily_health = ProviderHealth()
        self.last_source = {}
        self.last_candle = {}
        self.status_cache = {}
        self._nse_daily_index = {}
        self._restore_sources()

    def _restore_sources(self):
        if self.audit_path is None or not self.audit_path.is_file():
            return
        # Read only a bounded tail. A truncated first line is safely ignored.
        with self.audit_path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - 4 * 1024 * 1024))
            chunk = stream.read().decode("utf-8", errors="replace")
        for line in chunk.splitlines():
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict) and event.get("kind") in ("provider_selected", "provider_switch") and \
                    event.get("to") in PROVIDER_ORDER and isinstance(event.get("ticker"), str):
                capability = event.get("capability", "candles")
                self.last_source[(event["ticker"], capability)] = event["to"]
                if capability == "candles" and event.get("last_bar_start") and \
                        isinstance(event.get("last_close"), (int, float)) and \
                        math.isfinite(event["last_close"]) and event["last_close"] > 0:
                    self.last_candle[event["ticker"]] = {
                        "provider": event["to"], "last_bar_start": event["last_bar_start"],
                        "last_close": event["last_close"]}
                elif capability == "candles":
                    self.last_candle.pop(event["ticker"], None)

    def _audit(self, event):
        if self.audit_path is None:
            return
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True, allow_nan=False) + "\n")

    def _selected(self, ticker, provider, now, quality=None, capability="candles"):
        previous = self.last_source.get((ticker, capability))
        if previous != provider:
            event = {"at": now.isoformat(), "kind": "provider_selected" if previous is None
                     else "provider_switch", "ticker": ticker, "from": previous, "to": provider,
                     "capability": capability}
            if quality is not None:
                event.update({"last_bar_start": quality["last_bar_start"],
                              "last_close": quality["last_close"]})
            self._audit(event)
        self.last_source[(ticker, capability)] = provider
        if quality is not None and capability == "candles":
            self.last_candle[ticker] = {"provider": provider,
                                        "last_bar_start": quality["last_bar_start"],
                                        "last_close": quality["last_close"]}
        elif capability == "candles":
            self.last_candle.pop(ticker, None)

    def _reconcile(self, ticker, provider, quality):
        previous = self.last_candle.get(ticker)
        if not previous or previous["provider"] == provider:
            return
        try:
            prior_at = datetime.fromisoformat(previous["last_bar_start"])
            current_at = datetime.fromisoformat(quality["last_bar_start"])
            if prior_at.tzinfo is None or current_at.tzinfo is None:
                raise ValueError("timezone missing")
        except (ValueError, TypeError) as exc:
            raise DataError("previous provider audit has invalid timestamp") from exc
        if current_at.date() == prior_at.date() and current_at < prior_at:
            raise DataError("replacement provider candle timestamp regressed")
        if current_at == prior_at and abs(quality["last_close"] / previous["last_close"] - 1) > 0.02:
            raise DataError("replacement provider disagrees on same-bar close")

    def fetch_candles(self, ticker, range_="5d", interval=5, end_day=None,
                      require_entry_ready=False, allow_yahoo=True):
        """Try Upstox, FYERS, eligible NSE, then Yahoo; return no executable signal.

        `require_entry_ready` is for a future paper-only entry controller. It
        rejects stale and unofficial feeds rather than falling back silently.
        """
        _valid_ticker(ticker)
        if range_ not in ("1d", "5d", "1mo", "60d") or interval not in (1, 5):
            raise DataError("unsupported candle interval or range")
        now = _clock(self.now)
        attempts = []
        from . import intraday_sources as source
        for provider in PROVIDER_ORDER:
            if provider == "NSE":
                attempts.append({"source": provider, "status": "NOT_SUPPORTED_FOR_INTRADAY_CANDLES"})
                continue
            if provider == "YAHOO" and not allow_yahoo:
                attempts.append({"source": provider, "status": "DISABLED"})
                continue
            if provider == "UPSTOX":
                token = os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip()
                configured = bool(token)
            elif provider == "FYERS":
                app_id = os.environ.get("FYERS_APP_ID", "").strip()
                token = os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
                configured = ticker.endswith(".NS") and bool(app_id and token)
            else:
                configured = interval == 5 and end_day in (None, now.date(), now.date().isoformat())
            if not configured:
                attempts.append({"source": provider, "status": "NOT_CONFIGURED" if provider != "YAHOO"
                                 else "NOT_SUPPORTED_FOR_REQUEST"})
                continue
            if not self.health.available(provider, now):
                attempts.append({"source": provider, "status": "COOLDOWN"})
                continue
            try:
                if provider == "UPSTOX":
                    isin = source.upstox_instrument(ticker)
                    rows = source.upstox_bars(ticker, isin, token, range_, interval, end_day)
                    label = "Upstox V3 read-only candles"
                elif provider == "FYERS":
                    rows = source.fyers_bars(ticker, app_id, token, range_, interval, end_day)
                    label = "FYERS read-only historical candles"
                else:
                    rows = source.yahoo_bars(ticker, range_=range_)
                    label = "Yahoo Finance unofficial five-minute candles"
                quality = validate_candles(rows, ticker, interval, now, end_day)
                quality["last_close"] = quality["bars"][-1]["close"]
                self._reconcile(ticker, provider, quality)
                broker_bar_ready = quality["fresh_completed_bar"] and provider in ("UPSTOX", "FYERS")
                entry_ready = False
                if broker_bar_ready and require_entry_ready:
                    self.fetch_market_status(quality["exchange"], require_open=True)
                    entry_ready = True
                if not broker_bar_ready and require_entry_ready:
                    raise DataError("provider is stale or not authorized for a fresh paper entry")
                quality["broker_bar_ready"] = broker_bar_ready
                quality["market_status_verified"] = entry_ready
                self.health.record(provider, "OK", now)
                self._selected(ticker, provider, now, quality)
                return {"source": label, "provider": provider, "bars": quality.pop("bars"),
                        "quality": quality, "entry_ready": entry_ready,
                        "exchange_verified": False, "attempts": attempts,
                        "orders_possible": False}
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                    ValueError, TypeError, KeyError, OverflowError, DataError) as exc:
                category = _category(exc)
                self.health.record(provider, "FAILED", now, category)
                attempts.append({"source": provider, "status": "FAILED", "reason": category})
                self._audit({"at": now.isoformat(), "kind": "provider_failed", "ticker": ticker,
                             "capability": "candles", "provider": provider, "reason": category})
        raise FeedUnavailable(attempts)

    def fetch_market_status(self, exchange="NSE", require_open=False):
        """Check normal cash session in priority order; public calendar is not live status."""
        if exchange not in ("NSE", "BSE"):
            raise DataError("unsupported cash-equity exchange")
        now = _clock(self.now)
        cached = self.status_cache.get(exchange)
        if cached and (now - cached["checked_at"]).total_seconds() < 15:
            result = cached["result"]
            if require_open and not result["entry_ready"]:
                raise FeedUnavailable(result["attempts"])
            return result
        attempts = []
        from . import intraday_sources as source
        for provider in ("UPSTOX", "FYERS"):
            configured = (bool(os.environ.get("UPSTOX_ANALYTICS_TOKEN")) if provider == "UPSTOX"
                          else exchange == "NSE" and bool(os.environ.get("FYERS_APP_ID") and
                                                           os.environ.get("FYERS_ACCESS_TOKEN")))
            if not configured:
                attempts.append({"source": provider, "status": "NOT_CONFIGURED_OR_INELIGIBLE"})
                continue
            if not self.status_health.available(provider, now):
                attempts.append({"source": provider, "status": "COOLDOWN"})
                continue
            try:
                status = (source.upstox_market_status(exchange) if provider == "UPSTOX"
                          else source.fyers_market_status(exchange))
                if status.get("exchange") != exchange or not isinstance(status.get("open"), bool):
                    raise DataError("market-status identity or state mismatch")
                changed = status.get("updated_at")
                if status["open"] and changed:
                    updated = datetime.fromisoformat(changed)
                    if updated.tzinfo is None or updated.astimezone(IST).date() != now.date():
                        raise DataError("broker market status is stale")
                from .market_scanner import trading_day
                calendar_ok = trading_day(now.date())[0] if exchange == "NSE" else False
                if exchange == "BSE":
                    from .calendar_source import status as calendar_status
                    calendar_ok = calendar_status(now.date(), "BSE")["state"] == "NORMAL"
                ready = bool(status["open"] and calendar_ok and OPEN <= now.time() < CLOSE)
                result = {**status, "provider": provider, "entry_ready": ready,
                          "checked_at": now.isoformat(), "attempts": attempts,
                          "orders_possible": False}
                self.status_cache[exchange] = {"checked_at": now, "result": result}
                self.status_health.record(provider, "OK", now)
                if require_open and not ready:
                    raise FeedUnavailable(attempts + [{"source": provider, "status": "MARKET_NOT_OPEN"}])
                return result
            except FeedUnavailable:
                raise
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                    ValueError, TypeError, KeyError, OverflowError, DataError) as exc:
                category = _category(exc)
                self.status_health.record(provider, "FAILED", now, category)
                attempts.append({"source": provider, "status": "FAILED", "reason": category})
                self._audit({"at": now.isoformat(), "kind": "provider_failed",
                             "capability": "market_status", "provider": provider, "reason": category})
        from .market_scanner import trading_day
        public_ok = trading_day(now.date())[0] if exchange == "NSE" else False
        result = {"exchange": exchange, "state": "CALENDAR_ONLY" if public_ok else "UNVERIFIED",
                  "open": False, "entry_ready": False, "provider": "NSE",
                  "source": "NSE public session calendar (not live exchange status)",
                  "checked_at": now.isoformat(), "attempts": attempts,
                  "orders_possible": False}
        self.status_cache[exchange] = {"checked_at": now, "result": result}
        if require_open:
            raise FeedUnavailable(attempts + [{"source": "NSE", "status": "NOT_LIVE_STATUS"},
                                            {"source": "YAHOO", "status": "NOT_SUPPORTED"}])
        return result

    def _nse_daily(self, ticker, end_day):
        if end_day not in self._nse_daily_index:
            from .daily_scan import fetch_recent_reports
            self._nse_daily_index[end_day] = {}
            reports, _ = fetch_recent_reports(end_day.isoformat(), 35)
            index = {}
            for report in reports:
                for row in report["rows"]:
                    index.setdefault(row["symbol"] + ".NS", []).append({
                        "date": row["date"], "open": row["open"], "high": row["high"],
                        "low": row["low"], "close": row["close"], "volume": row["volume"]})
            self._nse_daily_index[end_day] = index
        return sorted(self._nse_daily_index[end_day].get(ticker, []), key=lambda row: row["date"])

    def fetch_daily(self, ticker, end_day=None):
        """Completed daily data in priority order; Yahoo batch is scanner-managed."""
        _valid_ticker(ticker)
        now = _clock(self.now)
        end = end_day or now.date()
        if isinstance(end, str):
            end = date.fromisoformat(end)
        if not isinstance(end, date) or end > now.date():
            raise DataError("daily scan date is invalid or in the future")
        attempts = []
        from . import intraday_sources as source
        for provider in PROVIDER_ORDER:
            if provider == "YAHOO":
                attempts.append({"source": provider, "status": "BATCH_FALLBACK_ONLY"})
                continue
            if provider == "UPSTOX":
                token = os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip()
                configured = bool(token)
            elif provider == "FYERS":
                app_id = os.environ.get("FYERS_APP_ID", "").strip()
                token = os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
                configured = ticker.endswith(".NS") and bool(app_id and token)
            elif provider == "NSE":
                configured = ticker.endswith(".NS")
            if not configured:
                attempts.append({"source": provider, "status": "NOT_CONFIGURED_OR_INELIGIBLE"})
                continue
            if not self.daily_health.available(provider, now):
                attempts.append({"source": provider, "status": "COOLDOWN"})
                continue
            try:
                if provider == "UPSTOX":
                    rows = source.upstox_daily_bars(ticker, source.upstox_instrument(ticker),
                                                    token, end)
                    label = "Upstox V3 completed daily candles"
                elif provider == "FYERS":
                    rows = source.fyers_daily_bars(ticker, app_id, token, end)
                    label = "FYERS completed daily candles"
                else:
                    rows = self._nse_daily(ticker, end)
                    label = "NSE official final CM-UDiFF Bhavcopy"
                if not isinstance(rows, list) or len(rows) < 30:
                    raise DataError("fewer than 30 completed daily candles")
                dates = [date.fromisoformat(row["date"]) for row in rows]
                if dates != sorted(set(dates)) or dates[-1] > end or (end - dates[-1]).days > 7:
                    raise DataError("daily candles are stale, duplicated or out of order")
                for row in rows:
                    o, h, l, c, v = (float(row[k]) for k in
                                     ("open", "high", "low", "close", "volume"))
                    if not all(math.isfinite(x) for x in (o, h, l, c, v)) or \
                            l <= 0 or l > min(o, c) or h < max(o, c) or v < 0:
                        raise DataError("invalid daily OHLCV")
                self.daily_health.record(provider, "OK", now)
                self._selected(ticker, provider, now, capability="daily")
                return {"provider": provider, "source": label, "bars": rows,
                        "attempts": attempts, "entry_ready": False, "orders_possible": False}
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                    ValueError, TypeError, KeyError, OverflowError, DataError) as exc:
                category = _category(exc)
                self.daily_health.record(provider, "FAILED", now, category)
                attempts.append({"source": provider, "status": "FAILED", "reason": category})
                self._audit({"at": now.isoformat(), "kind": "provider_failed", "ticker": ticker,
                             "capability": "daily", "provider": provider, "reason": category})
        raise FeedUnavailable(attempts)

    def fetch_quote(self, ticker, require_fresh=False, max_age_seconds=30,
                    allow_nse_raw=False):
        """Read one quote in provider order; delayed fallbacks remain research-only.

        A fresh broker quote is still only market data. It is never a completed
        candle or an executable fill, and independent bar/risk gates remain due.
        """
        _valid_ticker(ticker)
        now = _clock(self.now)
        attempts = []
        from . import intraday_sources as source
        for provider in PROVIDER_ORDER:
            if provider == "UPSTOX":
                configured = bool(os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip())
            elif provider == "FYERS":
                configured = ticker.endswith(".NS") and bool(os.environ.get("FYERS_APP_ID") and
                                                               os.environ.get("FYERS_ACCESS_TOKEN"))
            elif provider == "NSE":
                configured = ticker.endswith(".NS")
            else:
                configured = True
            if not configured:
                attempts.append({"source": provider, "status": "NOT_CONFIGURED_OR_INELIGIBLE"})
                continue
            if not self.quote_health.available(provider, now):
                attempts.append({"source": provider, "status": "COOLDOWN"})
                continue
            try:
                if provider == "UPSTOX":
                    quote = source.upstox_quote(ticker)
                elif provider == "FYERS":
                    quote = source.fyers_quote(ticker)
                elif provider == "NSE":
                    from .nse_sources import cm_live_quote
                    public = cm_live_quote(ticker[:-3], now)
                    if public.get("status") != "OK" or not public.get("quote"):
                        raise DataError("NSE public quote unavailable")
                    if require_fresh or not allow_nse_raw:
                        raise DataError("NSE public quote is delayed or unverified")
                    self.quote_health.record(provider, "OK", now)
                    self._selected(ticker, provider, now, capability="quotes")
                    return {"provider": provider, "source": public["source"], "ticker": ticker,
                            "price": None, "time": None, "research_payload": public["quote"],
                            "entry_ready": False, "attempts": attempts, "orders_possible": False}
                else:
                    rows = source.yahoo_bars(ticker, range_="5d")
                    quality = validate_candles(rows, ticker, 5, now)
                    valid_bars = quality["bars"]
                    last = valid_bars[-1]
                    last_day = date.fromisoformat(last["timestamp"][:10])
                    previous = next((bar["close"] for bar in reversed(valid_bars[:-1])
                                     if date.fromisoformat(bar["timestamp"][:10]) < last_day), None)
                    if require_fresh:
                        raise DataError("Yahoo close is not a fresh verified quote")
                    quote = {"ticker": ticker, "price": last["close"],
                             "prev_close": previous,
                             "time": quality["last_bar_end"], "currency": "INR",
                             "source": "Yahoo Finance unofficial completed-candle close"}
                if quote.get("ticker") != ticker or quote.get("currency") != "INR":
                    raise DataError("quote symbol or currency mismatch")
                price = float(quote["price"])
                stamp = datetime.fromisoformat(quote["time"])
                if not math.isfinite(price) or price <= 0 or stamp.tzinfo is None:
                    raise DataError("quote price or timestamp invalid")
                age = (now - stamp.astimezone(IST)).total_seconds()
                broker_quote_fresh = (provider in ("UPSTOX", "FYERS") and 0 <= age <= max_age_seconds and
                         stamp.astimezone(IST).date() == now.date() and OPEN <= now.time() < CLOSE)
                if broker_quote_fresh:
                    from .market_scanner import trading_day
                    broker_quote_fresh = trading_day(now.date())[0]
                if require_fresh and not broker_quote_fresh:
                    raise DataError("quote is stale or not eligible for fresh paper research")
                if require_fresh:
                    self.fetch_market_status("NSE" if ticker.endswith(".NS") else "BSE",
                                             require_open=True)
                self.quote_health.record(provider, "OK", now)
                self._selected(ticker, provider, now, capability="quotes")
                return {**quote, "provider": provider, "age_seconds": max(0, round(age, 1)),
                        "broker_quote_fresh": broker_quote_fresh,
                        "entry_ready": bool(require_fresh and broker_quote_fresh),
                        "attempts": attempts, "orders_possible": False}
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                    ValueError, TypeError, KeyError, OverflowError, DataError) as exc:
                category = _category(exc)
                self.quote_health.record(provider, "FAILED", now, category)
                attempts.append({"source": provider, "status": "FAILED", "reason": category})
                self._audit({"at": now.isoformat(), "kind": "provider_failed", "ticker": ticker,
                             "capability": "quote", "provider": provider, "reason": category})
        raise FeedUnavailable(attempts)


_default_manager = MarketDataManager()


def fetch_candles(ticker, range_="5d", **kwargs):
    return _default_manager.fetch_candles(ticker, range_, **kwargs)


def provider_health():
    return {"candles": _default_manager.health.snapshot(),
            "daily": _default_manager.daily_health.snapshot(),
            "quotes": _default_manager.quote_health.snapshot(),
            "market_status": _default_manager.status_health.snapshot()}
