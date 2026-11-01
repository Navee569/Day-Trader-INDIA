"""Optional read-only broker market-data WebSockets for watched equities.

The sockets deliver quotes, not order or portfolio events. Five-minute candles
continue to come from the validated historical/intraday data manager. No SDK
is imported until a stream is started with a locally supplied data token.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import math
import os
from threading import Event, Thread

from .core import DataError, IST
from .market_data_manager import DEFAULT_AUDIT, MarketDataManager, _clock, _valid_ticker


def _stamp(value):
    if isinstance(value, str) and not value.isdigit():
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise DataError("stream quote timestamp has no timezone")
        return parsed.astimezone(IST)
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise DataError("stream quote timestamp is invalid")
    return datetime.fromtimestamp(number / (1000 if number > 1e11 else 1), IST)


def _quote(ticker, provider, price, when, now):
    px = float(price)
    if not math.isfinite(px) or px <= 0:
        raise DataError("stream quote price is invalid")
    at = _stamp(when)
    age = (now - at).total_seconds()
    if at.date() != now.date() or not 0 <= age <= 30:
        raise DataError("stream quote is stale or future-dated")
    return {"ticker": ticker, "provider": provider, "price": px,
            "time": at.isoformat(), "age_seconds": round(age, 1),
            "orders_possible": False}


def parse_upstox_message(message, key_to_ticker, now=None):
    """Decode the official SDK's V3 dictionary; ignore market-info packets."""
    now = _clock(now)
    if not isinstance(message, dict):
        raise DataError("Upstox stream message must be decoded by the official SDK")
    feeds = message.get("feeds") or {}
    if not isinstance(feeds, dict):
        raise DataError("Upstox stream feed has invalid shape")
    result = []
    for key, value in feeds.items():
        if key not in key_to_ticker:
            raise DataError("Upstox stream instrument key was not subscribed")
        if not isinstance(value, dict):
            raise DataError("Upstox stream feed item is invalid")
        full = value.get("fullFeed") or value.get("ff") or {}
        market = full.get("marketFF") or full.get("market_ff") or {} if isinstance(full, dict) else {}
        ltpc = value.get("ltpc") or market.get("ltpc")
        if ltpc is None:
            continue
        if not isinstance(ltpc, dict):
            raise DataError("Upstox stream LTPC is invalid")
        result.append(_quote(key_to_ticker[key], "UPSTOX", ltpc["ltp"], ltpc["ltt"], now))
    return result


def parse_fyers_message(message, subscribed, now=None):
    """Normalize FYERS SymbolUpdate with an exact equity-symbol match."""
    now = _clock(now)
    if not isinstance(message, dict):
        raise DataError("FYERS stream message must be a dictionary")
    code = message.get("symbol")
    if code is None:
        return []  # connect, heartbeat and subscription acknowledgements
    ticker = subscribed.get(code)
    if ticker is None:
        raise DataError("FYERS stream symbol was not subscribed")
    when = message.get("last_traded_time", message.get("timestamp"))
    return [_quote(ticker, "FYERS", message["ltp"], when, now)]


class ReadOnlyStreamSupervisor:
    """Run one watched-symbol socket, switching Upstox → FYERS on failure.

    `run` is blocking. Call it from a dedicated service thread or process. A
    callback only receives validated, dated quotes. If no eligible live socket
    remains, the supervisor reports unavailable and waits; it never promotes
    delayed NSE/Yahoo research data into a live stream.
    """
    def __init__(self, symbols, on_quote, on_status=None, now=None, audit_path=None,
                 max_silence_seconds=45, retry_seconds=5):
        if not callable(on_quote):
            raise ValueError("on_quote callback is required")
        self.symbols = list(dict.fromkeys(symbols))
        if not self.symbols:
            raise ValueError("at least one watched cash-equity symbol is required")
        for ticker in self.symbols:
            _valid_ticker(ticker)
        self.on_quote = on_quote
        self.on_status = on_status or (lambda event: None)
        self.now = now
        self.manager = MarketDataManager(audit_path=DEFAULT_AUDIT if audit_path is None else audit_path,
                                         now=now)
        if max_silence_seconds <= 0 or retry_seconds <= 0:
            raise ValueError("stream watchdog and retry durations must be positive")
        self.max_silence_seconds = max_silence_seconds
        self.retry_seconds = retry_seconds
        self.stop_event = Event()
        self._socket = None
        self._last_tick = None
        self._disconnected = Event()
        self._had_quote = False
        self._active_provider = None

    def _status(self, provider, status, reason=None):
        event = {"at": _clock(self.now).isoformat(), "kind": "stream_status",
                 "provider": provider, "status": status}
        if reason:
            event["reason"] = reason
        self.manager._audit(event)
        self.on_status(event)

    def _message(self, provider, message, identities):
        if self._active_provider not in (None, provider):
            return
        try:
            now = _clock(self.now)
            rows = (parse_upstox_message(message, identities, now) if provider == "UPSTOX"
                    else parse_fyers_message(message, identities, now))
            for row in rows:
                self._last_tick = now
                self._had_quote = True
                self.manager._selected(row["ticker"], provider, now, capability="stream")
                self.manager.health.record(provider, "OK", now)
                self.on_quote(row)
        except (DataError, KeyError, TypeError, ValueError, OverflowError):
            self.manager.health.record(provider, "FAILED", _clock(self.now), "INVALID_DATA")
            self._status(provider, "INVALID_DATA")

    def _socket_error(self, provider, error=None):
        if self._active_provider not in (None, provider):
            return
        code = (getattr(error, "status", None) or getattr(error, "code", None))
        if isinstance(error, dict):
            code = error.get("status") or error.get("code")
        reason = "AUTH" if code in (401, 403, "401", "403") else \
                 "RATE_LIMIT" if code in (429, "429") else "SOCKET_ERROR"
        self.manager.health.record(provider, "FAILED", _clock(self.now), reason)
        self._status(provider, "DISCONNECTED", reason)
        self._disconnected.set()

    def _socket_closed(self, provider):
        if self._active_provider == provider:
            self._status(provider, "DISCONNECTED", "SOCKET_CLOSED")
            self._disconnected.set()

    def _upstox_socket(self):
        token = os.environ.get("UPSTOX_ANALYTICS_TOKEN", "").strip()
        if not token:
            raise DataError("Upstox data token not configured")
        import upstox_client  # optional official market-data SDK
        from .intraday_sources import upstox_instrument
        identities = {}
        for ticker in self.symbols:
            segment = "NSE_EQ" if ticker.endswith(".NS") else "BSE_EQ"
            identities[segment + "|" + upstox_instrument(ticker)] = ticker
        cfg = upstox_client.Configuration()
        cfg.access_token = token
        stream = upstox_client.MarketDataStreamerV3(
            upstox_client.ApiClient(cfg), instrumentKeys=list(identities), mode="ltpc")
        stream.auto_reconnect(True, 5, 3)
        stream.on("message", lambda message: self._message("UPSTOX", message, identities))
        stream.on("open", lambda: self._status("UPSTOX", "CONNECTED"))
        stream.on("error", lambda error=None, *_: self._socket_error("UPSTOX", error))
        stream.on("close", lambda *_: self._socket_closed("UPSTOX"))
        return stream

    def _fyers_socket(self):
        app_id = os.environ.get("FYERS_APP_ID", "").strip()
        token = os.environ.get("FYERS_ACCESS_TOKEN", "").strip()
        if not app_id or not token:
            raise DataError("FYERS data token not configured")
        if any(not ticker.endswith(".NS") for ticker in self.symbols) or len(self.symbols) > 100:
            raise DataError("FYERS standard data socket cannot cover this watched-symbol set")
        from fyers_apiv3.FyersWebsocket import data_ws  # optional official data SDK
        identities = {"NSE:" + ticker[:-3] + "-EQ": ticker for ticker in self.symbols}
        stream = None

        def on_connect():
            self._status("FYERS", "CONNECTED")
            stream.subscribe(symbols=list(identities), data_type="SymbolUpdate")
            stream.keep_running()

        stream = data_ws.FyersDataSocket(
            access_token=app_id + ":" + token, log_path="", litemode=False,
            write_to_file=False, reconnect=True, on_connect=on_connect,
            on_message=lambda message: self._message("FYERS", message, identities),
            on_error=lambda error=None, *_: self._socket_error("FYERS", error),
            on_close=lambda *_: self._socket_closed("FYERS"))
        return stream

    def stop(self):
        self.stop_event.set()
        self._disconnected.set()
        if self._socket is not None:
            try:
                self._socket.disconnect()
            except (OSError, AttributeError):
                pass

    def run(self):
        while not self.stop_event.is_set():
            for provider, factory in (("UPSTOX", self._upstox_socket),
                                      ("FYERS", self._fyers_socket)):
                if self.stop_event.is_set():
                    break
                self._disconnected.clear()
                self._last_tick = _clock(self.now)
                try:
                    self._active_provider = provider
                    socket = factory()
                    self._socket = socket
                    def connect_worker():
                        try:
                            socket.connect()
                        except Exception:
                            self._disconnected.set()
                    worker = Thread(target=connect_worker, daemon=True)
                    worker.start()
                    self._status(provider, "CONNECTING")
                    while not self.stop_event.is_set() and not self._disconnected.wait(1):
                        if not worker.is_alive() or _clock(self.now) - self._last_tick > \
                                timedelta(seconds=self.max_silence_seconds):
                            self._status(provider, "DISCONNECTED", "HEARTBEAT_OR_SOCKET_LOST")
                            break
                    try:
                        self._socket.disconnect()
                    except (OSError, AttributeError):
                        pass
                    worker.join(timeout=2)
                except (ImportError, OSError, ValueError, AttributeError, DataError):
                    self._status(provider, "UNAVAILABLE", "SDK_TOKEN_OR_CONNECTION")
                finally:
                    self._active_provider = "NONE"
                    self._socket = None
                if self.stop_event.is_set():
                    break
            if not self._had_quote and not self.stop_event.is_set():
                self._status("ALL", "NO_LIVE_STREAM", "NEW_PAPER_ENTRIES_BLOCKED")
            self._had_quote = False
            self.stop_event.wait(self.retry_seconds)
