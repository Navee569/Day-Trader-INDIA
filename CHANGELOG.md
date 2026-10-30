# Changelog

## Post-audit development state — 29–30 October 2026

- Added the sixth upstream-style workflow, India paper Budget Manager. `budget-manager` sizes one exit plan by the minimum of max-loss, position, max-order and buffered-cash limits; it reports the binding cap, uses an optional fresh dated quote or limit reference, checks plan date/blocked list/minimum order/reward:risk, and saves a paper proposal collection for the existing risk checker. INR weighted allocation supports inline/file targets, normalization, caps, minimums, local dated prices and whole shares. Allocations without per-ticker stops remain ineligible for risk approval. A dedicated Codex skill and INR example config were added.
- A saved historical RELIANCE plan, using a hypothetical ₹100,000 budget and pinned 29 October clock, produced a 17-share paper proposal (position cap binding) with ₹239.38 modeled loss under the assumed ₹500 limit; the existing risk checker accepted the exact saved proposal on that replay clock. It was neither sent nor filled. 160 offline tests passed on Windows Python 3.11.2 after this work.
- Added the fifth upstream-style workflow, Time Machine, for NSE cash-equity paper plans: complete 60-day five-minute session pool, 10,000 seeded same-time resampled paths, four odds, exit-time quantiles and R, 2,000-path bands, 160 sample paths, histogram, historical ORB rule replay, break-even-aware verdict, saved JSON and ASCII chart. Partial/gapped prior sessions are excluded; at least 20 full sessions and a completed entry bar are required. An ATR/fixed-price stop uses a labeled percentage-risk proxy in historical replay. No order or sizing is performed.
- A saved historical RELIANCE plan and cached Yahoo chart ran 10,000 paths with seed 19 from 58 full prior sessions, and replayed the historical rule on those sessions. Yahoo candles and volume are unofficial; the result is not a forecast or current signal. This local state remains uncommitted and unpushed.
- The full offline suite passed 151/151 on Windows Python 3.11.2 after Time Machine work; compilation and offline CLI run/chart smoke passed. Manual sell-side paper replay and automatic opening-range stop inference were added to match the original Time Machine options; the India exit planner and budget still accept long plans only.
- Added upstream-style NSE paper exit planner with `plan` and `show`, manual plan-ahead or entry-file input, five stop rules, R/fixed-price targets, one-/five-minute ATR through entry time, explicit tick rounding, IST flat-by/config checks, and an exit-plan Codex skill. Plan-ahead output is marked unconfirmed; paper risk now blocks its missing trigger cleanly.
- A saved historical RELIANCE entry produced a midpoint stop and 2R target, plus a separate `atr:1.5` plan from five saved completed bars. The ATR plan was saved and shown; chart volume warnings and historical replay status were preserved. No broker action occurred.
- 143 offline tests pass on Windows Python 3.11.2 after exit-planner work. This local state remains uncommitted and unpushed.
- Added upstream-style entry timer for NSE cash equities: `check`, bounded `watch`, and scanner `watchlist` modes; one-/five-minute ORB or fixed-level completed-close rules; optional volume multiple; IST 2026 calendar; offline simulated clock; once-only trigger state; exit-plan-compatible paper entry file; and a dedicated Codex skill.
- A read-only RELIANCE Yahoo historical check worked for both one-minute and five-minute bars. A saved-chart 29 October replay printed each completed five-minute bar and saved one paper entry at 09:40 IST; rerunning reported `ALREADY_RECORDED`. Yahoo's opening volume was zero, so price-only replay carried a volume warning; an explicit volume filter blocks that data. Live during-session polling and exchange reconciliation remain unverified.
- 134 offline tests pass on Windows Python 3.11.2 after entry-timer work. No commit or push was made.
- Added an upstream-style news monitor adapted to India: five public NSE RSS categories, Yahoo India headlines with Google News fallback, unofficial price moves, watchlist/plan/portfolio inputs, 30-day seen state, offline replay, text/JSON output, and a dedicated Codex skill. The monitor exposes partial coverage and missing individual filing links.
- Live read-only RELIANCE/TCS smoke reached the NSE feeds and Yahoo/Google feeds. The general NSE announcements feed contained 162 latest entries but started only at 00:19 IST on 30 October, shorter than the requested 48 hours. No complete filing-history coverage is claimed.
- 126 offline tests pass on Windows Python 3.11.2 after news-monitor work. This local development state remains uncommitted and unpushed.
- Added the upstream-style three-command scanner (`universe`, `daily`, `orb`) adapted to NSE EQ and IST, with two-stage marketwide ORB, offline replay, watchlist handoff, explicit partial counts, and a dedicated Codex scanner skill.
- A live NSE list returned 2,368 EQ symbols. A full Yahoo daily scan fetched 2,366 charts and scanned 1,027; a historical 10:15 ORB replay found 949 candidates and confirmed the configured 200 most traded. Yahoo remains unofficial and the 749 unconfirmed candidates were reported as partial.
- Added an optional official BSE security-master reader; its 29 October 2026 ZIP yielded 4,126 filtered cash equities. Yahoo did not return usable history for the sampled BSE codes, so BSE chart scans are not claimed as validated.
- 119 offline tests pass on Windows Python 3.11.2 after scanner work. No commit or push was made for this local development state.
- Added official NSE final Bhavcopy reader and marketwide daily scanner with the original five daily setup rules.
- Added read-only Upstox Analytics Token five-minute candle adapter with labeled Yahoo fallback. Upstox requires a locally entered token and has not been account-tested.
- Added completed-bar entry and paper exit, budget, risk, and approval-ticket handoff. No simulated fill or broker order is created.
- Added NSE CM Market Live quote connector with market-closed status; an in-session quote has not been validated.
- Full 11-workflow parity remains incomplete.

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
