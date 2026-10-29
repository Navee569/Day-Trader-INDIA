"""One-shot, paper-only watchlist research. Never creates or sends orders."""
from __future__ import annotations

from datetime import datetime, timedelta

from .core import IST, as_datetime, plan
from .scan import scan_watchlist


def paper_cycle(watchlist, as_of, tick_size, capital="100000", risk_pct="0.5",
                max_alloc_pct="20", slippage_bps="10", fee_buffer="50"):
    cutoff = datetime.now(IST) if as_of is None else as_datetime(as_of)
    scan = scan_watchlist(watchlist, as_of=cutoff.isoformat())
    results = []
    for row in scan["results"]:
        item = {"ticker": row["ticker"], "signal_status": row["status"],
                "data_origin": row.get("data_origin"),
                "provider_attempts": row.get("provider_attempts", []),
                "decision": "NO_PAPER_PLAN"}
        if row["status"] == "TRIGGERED_LONG":
            trigger_end = as_datetime(row["trigger_bar_end"])
            last_complete_end = as_datetime(row["last_complete_bar"]) + timedelta(minutes=5)
            if row.get("volume_missing_in_some_bars"):
                item["reason"] = "VOLUME_NOT_VERIFIED"
            elif trigger_end != last_complete_end or cutoff - trigger_end > timedelta(minutes=5):
                item["reason"] = "OLD_SIGNAL"
            else:
                item["paper_plan"] = plan(row["entry_reference"], row["range_low"], capital,
                                          risk_pct, 2, max_alloc_pct, tick_size,
                                          slippage_bps, fee_buffer)
                item["decision"] = "HYPOTHETICAL_PLAN_ONLY"
        else:
            item["reason"] = row.get("detail", row["status"])
        results.append(item)
    return {"mode": "PAPER_RESEARCH_ONLY", "as_of": cutoff.isoformat(),
            "source": "Per-symbol source in results; no source is exchange reconciled",
            "orders_possible": False, "count": len(results), "results": results}
