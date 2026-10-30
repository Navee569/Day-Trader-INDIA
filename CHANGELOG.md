# Changelog

## Deep audit of implemented workflow areas 1–7 — 30 October 2026

- Risk now independently recomputes order notional and planned loss, and rejects malformed state, invalid side, manipulated allocation, or a recomputed fingerprint hiding inconsistent terms. Budget and exit reject extreme numbers before arithmetic can overflow.
- Entry refuses old trigger and level data; risk, sandbox entry and replay block the published 2026 NSE EQ holiday dates. The calendar is a static snapshot, not a verified live session service.
- Broker CLI transactions use an OS file lock. Ledger load/save rejects duplicate IDs, mismatched fills, and invalid positions. Approved tickets carry a terms digest; attempted sandbox sends/cancels can be reconciled after process interruption without retrying.
- Sandbox placement accepts only the exact validated market-delivery payload; instrument master validation requires a matching ISIN and rejects malformed keys, quantities, lots, freeze limits and compressed files with controlled errors. Read-only Upstox requests refuse redirects and malformed candle responses.
- Deep offline checks include adversarial forged orders, concurrent CLI access, and 500 randomized paper round trips. Live five-minute Yahoo fetch/entry were rechecked during the market session; NSE MCP still returned HTTP 404 after initialization. No account-backed Upstox or sandbox transaction was attempted.

## Local live-session validation — 30 October 2026

- Corrected Yahoo's trailing in-progress off-grid quote update so it cannot invalidate the entire five-minute candle series. Kept rejection of irregular timestamps inside the series; added two focused regressions.
- Market-session validation results are reported separately from the offline suite; a retrieved Yahoo candle is unofficial and not an exchange-verified fill.
- Corrected BSE Yahoo examples to use a trading symbol; a numeric BSE scrip code that Yahoo does not resolve now returns a more specific error.

## Feature 7 paper broker

- Added a paper-only broker ledger: exact proposal approval, fresh risk recheck, duplicate and cash reservation guard, manual simulated fills, position close, cancellation and event history.
- The CLI requires explicit `YES` for submitting a paper proposal and `SIMULATE` for manually entered fills. No brokerage endpoint is connected.
- Added offline tests for the ledger lifecycle, stale/changed/duplicate approvals, limits, persistence and the CLI confirmation gate.
- Added decline, fingerprinted sell-to-close approval, plain ticket review, and an optional Upstox V3 sandbox-only place/cancel handoff with a separate ALLOW ONCE gate. Uncertain outcomes are persisted and require manual orderbook reconciliation. No live host or automatic fill.
- Require cancellation/reconciliation of an outstanding entry remainder before a paper exit; require the exact approved exit ticket ID when one exists. Cross-check optional ISIN and security classification in the local instrument master.

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
