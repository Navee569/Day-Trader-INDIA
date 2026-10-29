# Changelog

## Post-audit development state — 29 October 2026

- Added official NSE final Bhavcopy reader and marketwide daily scanner with the original five daily setup rules.
- Added read-only Upstox Analytics Token five-minute candle adapter with labeled Yahoo fallback. Upstox requires a locally entered token and has not been account-tested.
- Added completed-bar entry and paper exit, budget, risk, and approval-ticket handoff. No simulated fill or broker order is created.
- Added NSE CM Market Live quote connector with market-closed status; an in-session quote has not been validated.
- 109 offline tests pass on Windows Python 3.11. Full 11-workflow parity remains incomplete.

## v0.2.1 — 2026-10-29 — Independent re-audit patch
- Reproduced and fixed NaN/invalid directly-constructed `Bar` objects bypassing parse validation.
- Corrected backtest entry cutoff: cutoff applies to the **next-bar fill timestamp**.
- Prevented unhandled decimal failure from extreme `fee_buffer` values; rejected unrealistically small price ticks.
- Protected Yahoo adapter against malformed `result`, `indicators`, `quote` structure; reject mismatched `meta.symbol`.
- Reject CSV rows containing more columns than declared and JSON objects without a `bars` key.
- Detect normalized watchlist duplicates without compromising per-item invalid-symbol error reports.
- Added 18 independent regression cases; 79/79 offline tests pass on Linux Python 3.13.5.
- No claims of live price-feed, Windows, profitability or complete 11-skill implementation.

## v0.2 — 2026-10-29
- Completed-bar ORB, defensive unofficial Yahoo 5-minute bar provider, local CSV/JSON validation.
- Offline 5-minute historical replay, scanner, explicit tick sizing, conservative cutoff, Codex skill instructions.
- 61 tests. v0.2 has subsequently discovered defects; use v0.2.1.
