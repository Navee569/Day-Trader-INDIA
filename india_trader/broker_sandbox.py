"""Upstox sandbox order handoff. This module contains no live broker endpoint."""
from __future__ import annotations

import json
import re
import gzip
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener

SANDBOX_PLACE_URL = "https://api-sandbox.upstox.com/v3/order/place"
SANDBOX_CANCEL_URL = "https://api-sandbox.upstox.com/v3/order/cancel"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("Sandbox redirect refused")


def verify_instrument(master_path: str, ticker: str, instrument_key: str,
                      quantity: int) -> dict:
    """Cross-check the chosen key against a local Upstox BOD JSON master."""
    if (not isinstance(instrument_key, str) or
            not re.fullmatch(r"(?:NSE|BSE)_EQ\|IN[A-Z0-9]{10}", instrument_key) or
            isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1):
        raise ValueError("Supply a valid equity ISIN key and positive whole-share quantity")
    path = Path(master_path)
    if path.stat().st_size > 50_000_000:
        raise ValueError("Instrument master exceeds 50 MB")
    try:
        stream = gzip.open(path, "rb") if path.suffix.lower() == ".gz" else path.open("rb")
        with stream:
            raw = stream.read(50_000_001)
    except (gzip.BadGzipFile, EOFError) as exc:
        raise ValueError("Invalid compressed Upstox instrument master") from exc
    if len(raw) > 50_000_000:
        raise ValueError("Instrument master exceeds 50 MB uncompressed")
    try:
        rows = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid Upstox instrument JSON") from exc
    if not isinstance(rows, list):
        raise ValueError("Upstox instrument master must be a JSON array")
    exchange = "NSE" if ticker.endswith(".NS") else "BSE" if ticker.endswith(".BO") else None
    if exchange is None:
        raise ValueError("Ticket needs an NSE/BSE ticker")
    matches = [row for row in rows if isinstance(row, dict) and row.get("instrument_key") == instrument_key]
    if len(matches) != 1:
        raise ValueError("Instrument key is absent or duplicated in the master")
    row = matches[0]
    freeze = row.get("freeze_quantity")
    if (row.get("segment") != exchange + "_EQ" or row.get("exchange") != exchange or
        row.get("trading_symbol") != ticker[:-3] or
        isinstance(row.get("lot_size"), bool) or row.get("lot_size") != 1 or
        row.get("isin") != instrument_key.split("|", 1)[1] or
        (row.get("security_type") is not None and row.get("security_type") != "NORMAL") or
        (exchange == "NSE" and row.get("instrument_type") != "EQ") or
        (freeze is not None and
         (isinstance(freeze, bool) or not isinstance(freeze, int) or freeze < quantity))):
        raise ValueError("Instrument master does not match the paper ticket or quantity")
    return {"instrument_key": instrument_key, "trading_symbol": row["trading_symbol"],
            "segment": row["segment"], "source": str(path),
            "note": "Local BOD file must be refreshed for the intended trading day"}


def order_payload(order: dict, instrument_key: str) -> dict:
    """Build only an NSE/BSE EQ delivery payload from an approved paper ticket."""
    if order.get("mode") != "PAPER_ONLY" or order.get("status") not in ("APPROVED_UNFILLED", "EXIT_APPROVED"):
        raise ValueError("Sandbox handoff requires an approved, unfilled paper ticket")
    ticker = order.get("ticker", "")
    exchange = "NSE" if ticker.endswith(".NS") else "BSE" if ticker.endswith(".BO") else None
    if exchange is None or not re.fullmatch(r"(?:NSE|BSE)_EQ\|IN[A-Z0-9]{10}", instrument_key or ""):
        raise ValueError("Supply a verified NSE/BSE EQ instrument key")
    if not instrument_key.startswith(exchange + "_EQ|"):
        raise ValueError("Instrument key exchange does not match the ticket")
    qty = order.get("quantity")
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        raise ValueError("Invalid whole-share quantity")
    side = "BUY" if order.get("side") == "BUY_LONG_CASH" else "SELL" if order.get("side") == "SELL_TO_CLOSE" else None
    if side is None:
        raise ValueError("Unsupported paper order side")
    if side == "BUY" and order.get("filled_quantity") != 0:
        raise ValueError("Sandbox entry must have no manually simulated fills")
    return {"quantity": qty, "product": "D", "validity": "DAY", "price": 0,
            "instrument_token": instrument_key, "order_type": "MARKET",
            "transaction_type": side, "disclosed_quantity": 0, "trigger_price": 0,
            "is_amo": False, "slice": False, "market_protection": -1}


def place_sandbox(payload: dict, token: str, *, opener=None) -> str:
    """Send once to the fixed sandbox host; return order ID, never a fill."""
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Set UPSTOX_SANDBOX_TOKEN locally; do not paste it into chat")
    fields = {"quantity", "product", "validity", "price", "instrument_token", "order_type",
              "transaction_type", "disclosed_quantity", "trigger_price", "is_amo",
              "slice", "market_protection"}
    if (not isinstance(payload, dict) or set(payload) != fields or
            isinstance(payload.get("quantity"), bool) or not isinstance(payload.get("quantity"), int) or
            payload["quantity"] < 1 or payload.get("product") != "D" or
            payload.get("validity") != "DAY" or payload.get("order_type") != "MARKET" or
            payload.get("transaction_type") not in ("BUY", "SELL") or
            not isinstance(payload.get("instrument_token"), str) or
            not re.fullmatch(r"(?:NSE|BSE)_EQ\|IN[A-Z0-9]{10}", payload["instrument_token"]) or
            payload.get("price") != 0 or payload.get("trigger_price") != 0 or
            payload.get("disclosed_quantity") != 0 or payload.get("is_amo") is not False or
            payload.get("slice") is not False or payload.get("market_protection") != -1):
        raise ValueError("Invalid sandbox payload")
    request = Request(SANDBOX_PLACE_URL,
                      data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                      headers={"Accept": "application/json", "Content-Type": "application/json",
                               "Authorization": "Bearer " + token.strip()}, method="POST")
    transport = opener or build_opener(_NoRedirect, HTTPSHandler)
    try:
        with transport.open(request, timeout=15) as response:
            data = response.read(64_001)
            if len(data) > 64_000:
                raise ValueError("Sandbox response too large")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        # The request may have reached the sandbox on a timeout. Caller marks it unknown.
        raise ValueError("Sandbox request failed or outcome is unknown; do not retry automatically") from exc
    try:
        doc = json.loads(data)
        ids = doc["data"].get("order_ids") or ([doc["data"]["order_id"]] if "order_id" in doc["data"] else [])
        if (doc.get("status") != "success" or len(ids) != 1 or
                not isinstance(ids[0], str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", ids[0])):
            raise ValueError("Sandbox did not return one accepted order ID")
    except (TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("Unexpected sandbox response; outcome unknown") from exc
    return ids[0]


def cancel_sandbox(order_id: str, token: str, *, opener=None) -> str:
    """Request cancellation of one sandbox order; returned ID is an acknowledgement."""
    if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,80}", order_id):
        raise ValueError("Invalid sandbox order ID")
    if not isinstance(token, str) or not token.strip():
        raise ValueError("Set UPSTOX_SANDBOX_TOKEN locally")
    request = Request(SANDBOX_CANCEL_URL + "?order_id=" + order_id,
                      headers={"Accept": "application/json",
                               "Authorization": "Bearer " + token.strip()}, method="DELETE")
    transport = opener or build_opener(_NoRedirect, HTTPSHandler)
    try:
        with transport.open(request, timeout=15) as response:
            data = response.read(64_001)
            if len(data) > 64_000:
                raise ValueError("Sandbox response too large")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ValueError("Sandbox cancellation outcome unknown; do not retry automatically") from exc
    try:
        doc = json.loads(data)
        acknowledged = doc["data"]["order_id"]
        if doc.get("status") != "success" or acknowledged != order_id:
            raise ValueError("Sandbox cancellation not acknowledged for this order")
    except (TypeError, KeyError, AttributeError, json.JSONDecodeError) as exc:
        raise ValueError("Unexpected sandbox cancellation response") from exc
    return acknowledged
