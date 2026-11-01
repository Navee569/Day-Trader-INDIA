# Independent re-audit — India Day Trader v0.2.1

## Scheduled paper monitoring and calendar pass — 1 November 2026

- Added a no-token public holiday-calendar refresh and status command. A read-only refresh returned 22 current-year events; `market-calendar status --date 2026-11-08 --exchange NSE` reported the Diwali special session from 14:20 to 15:30 IST. Ordinary opening-range scanning refuses special sessions. The source is Upstox's public schedule, not an independently reconciled exchange notice. The task refreshes it daily while running; stale or unknown future-year status fails closed.
- Added a paper monitor for calendar, alert, news, NSE/BSE insider and NSE earnings checks, with separate polling intervals and local event/error logs. One real `paper-monitor once --symbol RELIANCE --json` smoke completed on the closed Sunday with zero events. The Windows weekday 09:00 IST task was registered for the signed-in user and initially showed Ready with next run 2 November 2026 09:00 IST. Actual unattended market-hour delivery remains untested. No orders, broker credentials or notifications were added.
- A later Sunday monitor check recorded an insider-source error: the public NSE host could not be resolved in this run. The monitor now logs `public source unavailable` for that case without copying raw provider stderr into its local event log. This source failure is not a successful filing check; the next scheduled interval will retry.
- Scanner stage one now retries missing Yahoo five-minute charts through optional configured Upstox/FYERS adapters, with an optional retry cap; offline tests exercise a BSE numeric-code fallback. Public BSE Financial Results category 22 was identified, but a valid result row/linked XBRL has not been obtained, so BSE earnings parsing is still missing. Initial BSE queries for two codes returned no rows. A further read-only query was temporarily rejected because automatic approval review hit its usage limit; after the stated reset time, a retry completed and returned zero rows for `500325` and `539289` over July–November 2026. The missing sample prevents a trustworthy financial-results parser.
- The entry timer now checks the public BSE schedule when its cache is fresh. A known BSE holiday or special session blocks normal-session entry; offline regression covers this. When the BSE schedule is unavailable, paper output keeps the explicit NSE-calendar proxy warning.
- Full offline Windows Python 3.11.2 suite: **217 tests passed**. The current pass also runs module compilation, paper-only doctor, bounded monitor and checksum checks. Account-based candle feeds and in-session scanner/alert behavior remain pending a live market and account access.

## Remaining paper-coverage gaps pass — 1 November 2026

- Added Upstox V3 candle/quote routing in the entry timer, Time Machine, ORB confirmation and alerts, plus an optional FYERS historical candle fallback for NSE cash equities, followed by labeled Yahoo fallback. Neither account token was provided, so account behavior and in-session latency remain unverified. The scanner's broad first-stage batch remains Yahoo. Three independent five-minute provider adapters are coded, but only Yahoo is usable without an account; NSE Bhavcopy and CM Market Live do not provide five-minute candles.
- Added official BSE PIT history lookup by six-digit scrip code and inline XBRL parsing. A read-only live query for code `539289`, 30 October 2026, parsed one promoter purchase of 10,000 equity shares and linked the official BSE filing. NSE's public date filters returned two RELIANCE records for September 2025; older symbol searches now query monthly windows. Neither venue's archive is proven complete.
- Added a foreground alert watcher, local event log and Windows launcher. On the closed 1 November 2026 session, bounded CLI smoke returned `checks: 0` as expected. Offline tests exercise a ring and quote fallback; no in-session continuous delivery was observed.
- The full offline suite passed **212 tests** on Windows Python 3.11.2 after using a temp directory that permits atomic state-file renames. `python -m compileall -q india_trader`, `git diff --check`, `doctor` (`orders_possible: false`), and all **84** `SHA256SUMS.txt` file hashes passed. No broker order path was added.

## Eleventh workflow validation — 1 November 2026

- The new Insider Tracker is mapped to the upstream Congress PTR tracker in `PARITY_AUDIT_11.md`. Four new offline tests cover current/archived NSE XBRL layouts, multiple named transaction contexts, date and name filters, a future/revision cutoff, once-only state, dry run, offline fetch, bad XBRL and untrusted filing URLs.
- `python -m unittest discover -s tests -v`: **199 tests passed** on Windows Python 3.11.2. A live read-only `insider-tracker index --current-only --since 2026-10-30 --read --limit 1 --json` parsed NSE filing `current:4108` for GRADIENTE, posted 31 October 2026 16:48:46 IST, with one promoter/director transaction, trade date 29 October and filing date 31 October. A separate `fetch archive:563850 --symbol RELIANCE --json` parsed two people from an older NSE XBRL filing. The older XML marked its reported-value field with unitRef `shares`; output preserves this mismatch instead of calling it INR.
- The two public NSE index endpoints returned data, but their full history, completeness and market-hour latency are unverified. BSE-only disclosures and a legal-lateness verdict are not supported. The tool is read-only except its local once-only state, has no scheduled delivery, and does not create orders or claim a trading advantage.

## Tenth workflow validation — 1 November 2026

- The new NSE Earnings Analyst is documented in `PARITY_AUDIT_10.md`. Offline tests cover issuer/basis/filing cutoff filters, INR XBRL units and identity, annual-minus-three-quarter derivation, same-quarter growth, TTM completeness, bank fields, release link matching, ISIN lookup and metrics export.
- Read-only public NSE smoke at a pinned 1 November 2026 12:00 IST cutoff parsed RELIANCE consolidated six-quarter results and HDFCBANK consolidated six-quarter results. RELIANCE's September 2026 filing was published 30 October 2026 21:32:55 IST. Its XBRL yielded revenue INR 332,861 crore, net profit INR 29,250 crore and diluted EPS INR 18.88; the output linked a separate official NSE press-release PDF candidate. HDFCBANK yielded filed total income INR 129,358.69 crore, net profit INR 20,700.03 crore and diluted EPS INR 13.41, with gross margin blank. These are filing reads, not trading signals. `examples/METRICS_INDIA.json` records a dated two-company metrics snapshot.
- NSE public endpoints and some taxonomy shapes may change; BSE-only filings, automatic press-release narrative parsing, call transcripts, next-report dates, and independent source reconciliation were not validated. `--isin INE002A01018` resolved RELIANCE through the public NSE EQ universe and produced the same latest consolidated quarter. A separate `--basis standalone` run yielded RELIANCE's September 2026 standalone revenue INR 174,668 crore and net profit INR 15,354 crore without mixing it with consolidated figures.
- `python -m unittest discover -s tests -v`: **195 tests passed** on Windows Python 3.11.2. `python -m compileall -q india_trader`: passed. The bundled skill-creator `quick_validate.py` could not run because this Python environment lacks PyYAML (`ModuleNotFoundError: yaml`); the skill frontmatter, discoverable repo path, and references were inspected directly. A fresh-session Codex discovery check was not run.

## Nine-workflow parity pass — 31 October 2026

- Compared the original workflows 1–9 at the command, state, handoff, and example level. The detailed mapping and the India-specific limits are in `PARITY_AUDIT_1_TO_9.md`.
- Added offline regression cases for BSE entry paper charts and watch pre-open behavior; daily risk-counter recovery and fresh-quote holding valuation; broker partial fills, failed paper results and command handoff; alert source/output/config options and example files.
- `python -m unittest discover -s tests -q`: **188 tests passed** on Windows Python 3.11.2. Expected invalid-input diagnostics from negative cases appeared on stderr. `python -m compileall -q india_trader`: passed. `git diff --check`: passed before committing.
- Paper-only smoke: `python -m india_trader doctor` returned `status: OK`, `orders_possible: false`, and `market_data_connectivity: NOT_CHECKED`. `python -m india_trader alerts clock --now 2026-10-31T10:00:00+05:30 --json` returned `session_open: false`, `reason: weekend`, and next normal open 2 November 09:15 IST; its exit code **1 is expected for a closed market**. The file-based broker entry-to-exit/partial-result CLI path is exercised offline in `tests/test_paper_broker.py`.
- This pass did not contact a live broker, submit an order, or verify live in-session BSE/Yahoo/Upstox/NSE quote coverage. Missing official filing archive, unsupported BSE candle aliases, unverified special sessions, and lack of a continuous notification service remain explicit limits. Earnings analyst and insider-disclosure tracker are original workflows 10–11, outside this pass.

## Post-audit Windows validation — Paper Alerts, 31 October 2026

- The ninth original workflow is adapted as `alerts clock|add|quote|check|list|reset`. Six new offline tests cover NSE IST normal-session/holiday/unknown-calendar behavior, plan-derived stop/target/flat-by rules, custom percent/direction/time/opening-bell rules, once-only state and rearming, malformed/foreign-currency/NaN/stale quote refusal, dated quote handoff to budget sizing, and file-based CLI replay. The full suite passed **177/177** on Windows Python 3.11.2; module compilation passed.
- The CLI tests use saved Yahoo-shaped quote fixtures. A separate read-only public quote smoke on 31 October returned unofficial `RELIANCE.NS` INR 1164.90, previous close INR 1167.40, with market timestamp **30 October 15:28:17 IST**; no price file was written. The live clock smoke on Saturday 31 October returned `session_open: false`, `reason: weekend`, and next normal open 2 November 09:15 IST. Query1 fallback, a live in-session alert run, and third-party notification delivery were not validated. The check returns a ring in JSON/CLI output only when invoked. A missed poll can miss a crossed level. The 2026 NSE normal-session calendar does not verify special/early sessions, BSE hours or future years. No broker order, stop order, automatic flatten, or profit claim is made.

## Post-audit Windows validation — Paper Broker, 31 October 2026

- The eighth upstream workflow now has paper ticket display, one-ticket exact yes/no records, a local simulated-fill ledger, paper cash/whole-share holdings, gross realized P&L, and a flatten proposal routed back through Risk Manager. The implementation has no broker network client or live-order function. A supplied INR fill price is a simulation input, not an observed exchange fill.
- `python -m unittest discover -s tests -v` passed **171/171** on Windows Python 3.11.2. `python -m compileall -q india_trader` passed. Five broker tests cover changed/stale verdicts, one-order approval, decline, explicit-price fill, duplicate/daily-limit refusal, cash/holding changes, late-session exit, and the complete file-based CLI handoff through flatten and realized loss. A sixth integration regression checks that prior-day risk counters reset. These tests used temporary files and hypothetical prices; no live broker or market feed was involved.
- The paper ledger does not deduct Indian broker fees or taxes, mark holdings to current market prices, place working stop/target orders, or automatically close at the flat-by time. No actual trading or profitability is validated. The original Alpaca/IBKR/Claude approval-card path is intentionally absent because this India workflow is restricted to local paper simulation.

## Post-audit Windows validation — Risk Manager, 31 October 2026

- `risk-manager` adds batch paper checks and recorded exact-order verdicts. Five new tests cover sequential daily loss, changed order fingerprints, fresh-price reward:risk, duplicates, closed entry windows, unreviewed allocations, position-reducing exits and CLI save/no-write. The full offline suite passed **165/165** on Windows Python 3.11.2 and module compilation passed.
- A saved historical RELIANCE paper proposal checked at a pinned 29 October 09:41 IST clock returned PASS: recalculated maximum loss Rs.239.38 and reward:risk 1.475. The saved verdict opened an `AWAITING_APPROVAL` paper ticket with `fill: null` and `orders_possible: false`. No live price, broker connection, fill or user approval was used.
- The 2026 normal-session holiday list is checked. Future dates, special sessions, BSE hours, broker cash and actual fills are unverified. The batch assumes earlier passed proposals filled only while calculating later limits; it records no simulated fill or broker state.

## Post-audit Windows validation — Budget Manager, 30 October 2026

- The sixth upstream workflow is implemented locally as `budget-manager`, plus a repo-local Codex skill and INR example config. The one-plan mode uses four explicit whole-share limits, a dated higher-price adjustment (15-minute cutoff), limit-price option, minimum order and reward:risk gates, blocked ticker/date checks, and a saved paper proposal collection. Weighted mode normalizes and caps allocations, drops small slices, reports idle INR, and supports priced whole-share references; no-stop allocations cannot pass the existing risk checker.
- `python -m unittest discover -s tests -v` passed **160/160** on Windows Python 3.11.2, including nine budget manager cases for each binding limit, fresh/stale prices, refusals, cap redistribution, whole shares, no-stop blocking, targets-file CLI, and saved-collection handoff. `python -m compileall -q india_trader` passed.
- An offline historical RELIANCE exit plan (entry Rs.1174.30, stop Rs.1165.50, target Rs.1191.90) with hypothetical Rs.100,000 capital and pinned 29 October 09:41 IST produced **17 paper shares**. The four limits allowed 40 (max loss), 17 (position), 21 (max order) and 76 (buffered cash); the position cap bound. Modeled notional was Rs.19,982.99 and modeled loss Rs.239.38 using assumed 10 bps slippage on each side and a Rs.50 fee buffer. The saved collection passed the existing paper risk review on the pinned historical clock. This is a replayed rules check, not a current signal, fill, or profit estimate. Weighted allocation was smoke-checked separately with explicit inline weights and no prices; it produced indicative amounts only. No broker connection or live order was used.
- The example fee/slippage settings do not calculate all Indian charges, tax, spread, gap risk or market impact. The one-share lot is an explicit assumption; actual security series/lot, instrument tick, cash balance, broker execution and user funds were not verified. The budget never places an order.
- The skill-creator `quick_validate.py` could not run because the available Python environment has no PyYAML (`ModuleNotFoundError: yaml`). The new skill's frontmatter name, description, closing marker and repository path were checked directly. Fresh-session Codex skill discovery was not tested.

## Post-audit Windows validation — Time Machine, 30 October 2026

- The fifth upstream workflow is implemented locally as `time-machine run|chart`, plus a Codex skill. It builds 10,000 seeded same-time five-minute paths from full sessions strictly before the plan date, replays the ORB rule on those real sessions, and saves the fan-chart data. The algorithm is adapted to the conservative NSE 09:15–15:15 IST window and cash-equity paper research. `tests/test_time_machine.py` covers seed repeatability, prior-session exclusion, same-bar stop-first ordering, incomplete entry rejection, session-gap minimum, historical replay and the CLI/save/chart path.
- `python -m unittest discover -s tests -v` passed **151/151** on Windows Python 3.11.2. `python -m compileall -q india_trader` passed. An invalid offline chart exits cleanly with an error. Manual sell-side scenario and historical stop-first checks passed. The real cached-chart run and `chart --from` both completed without network or broker access.
- A read-only offline replay used a cached unofficial Yahoo 60-day RELIANCE chart as of **29 October 2026, 15:29 IST**, and a historical 29 October 09:40 paper exit plan (entry Rs.1174.30, stop Rs.1165.50, target Rs.1191.90; assumed Rs.0.05 tick). It used **58 complete prior sessions**, excluding one incomplete session and 56 missing/zero-volume bars. With **10,000 paths, seed 19**, target-first was **6.29%**, stop-first **43.11%**, neither **50.60%**, and green **40.54%**. The exit-time median was Rs.1172.89, 5–95% range Rs.1156.24–1189.94, average R **−0.123**. The historical percentage-risk proxy ORB rule fired 27 of 58 sessions: 2 targets, 9 stops, 16 neither, average R **+0.018**. Synthetic and historical averages disagree. The printed verdict said boundary hits strongly favored the stop. These are retrospective, gross-of-cost calculations, not calibrated odds, a current signal, or evidence of profit.
- The Yahoo chart was not reconciled to NSE. The simulated stop assumes a stop-price exit on a gap; actual losses can exceed 1R. Tick, fees, taxes, spread, slippage, impact, corporate actions, and out-of-sample strategy validation remain unverified. No order, broker access, commit, or push occurred.

## Local scanner development check — 30 October 2026 (Windows; market data from 29 October)

This section covers the new NSE adaptation of the upstream `trader-scanner` workflow; the historical v0.2.1 audit below describes the earlier ZIP.

- The new `scanner universe` command downloaded the official NSE EQ list: **2,368** symbols. It wrote `trader/universe.txt` locally. The generated `trader/` folder is ignored by Git.
- A full `scanner daily --no-write` run fetched **2,366/2,368** Yahoo charts, scanned **1,027** after liquidity/history filters, and found **322** rule matches. Two symbols had no chart. The scanner reports the failed count; Yahoo results are unofficial.
- A historical `scanner orb --at 10:15 --no-write` run fetched **2,366/2,368**, found **1,367** usable charts and **949** stage-one candidates. It confirmed the configured maximum of **200**, found **120** matches among those, and explicitly marked **749 candidates unconfirmed**. This was a historical replay of completed bars, not a live-session test.
- The official BSE security-master ZIP dated 29 October 2026 yielded **4,126** active one-share-lot cash-equity codes in groups A/B/X/T/XT. Combining it with the NSE list yielded **6,494 exchange-specific entries**. Yahoo returned no chart for two sampled official numeric BSE codes; a BSE trading-symbol alias had insufficient daily history. BSE price-scan coverage is therefore unverified.
- `python -m unittest discover -s tests -v` passed **119/119** tests on Windows Python 3.11.2. `python -m compileall -q india_trader` passed. Offline scanner fixtures cover IST session rules, an NSE holiday, staged confirmation limits, gap/volume rejection, currency and symbol checks, watchlist handoff, and NSE/BSE security-list filtering.
- The skill-creator's `quick_validate.py` could not run because neither available Python runtime has PyYAML. The scanner skill frontmatter and links were inspected manually. Codex automatic discovery of the new skill has not been tested in a fresh session.
- NSE EQ is the default universe. BSE Yahoo candles, special-session timing, exchange reconciliation of Yahoo five-minute bars, Upstox account data, and profitability remain unverified. The scanner never places an order.

**Audit:** 2026-10-29, second-pass review of the supplied `Day_Trader_India_ChatGPT_Codex_v0.2_AUDITED.zip` and corrected v0.2.1.  
**Verdict:** **PASS for the tested offline Python-only paper research subset**. **FAIL / NOT READY** as a complete 11-skill port, live Indian stock-market trading system, production scanner, brokerage integration, or credible trading signal provider. No profit/strategy edge verified.

## Verified facts

- Re-extracted the previous v0.2 ZIP into a new directory and checked its ZIP CRC. It contained 61 unit tests, all passing on Linux Python **3.13.5**.
- Reproduced defects not covered by those tests:
  1. Direct construction of `Bar` with `float('nan')` bypassed validation and returned an ORB status rather than rejecting bad OHLC.
  2. A triggering candle STARTING at 11:30 was allowed to yield a simulated ENTRY at 11:35 despite `max_entry='11:30'`.
  3. `plan(..., fee_buffer='1e100')` raised uncaught `decimal.InvalidOperation` rather than rejecting input.
  4. Malformed Yahoo `indicators` and `quote` structures produced `AttributeError` / `KeyError` in direct API calls; a response with a different `meta.symbol` was accepted.
- Corrected those cases, plus CSV rows with extraneous fields, invalid JSON root objects, and case/suffix-normalized duplicate watchlist tickers.
- A fresh expanded test run now has **79/79 tests passing**, including **18 new independent regression tests**. Python compilation succeeds.
- Package is **not** a patch applied to Jake Schincariol's 11-skill repository. It is a standalone Indian cash-market subset with Codex instructions. Do not claim 11-skill equivalence.

## Post-audit Windows validation — exit planner, 30 October 2026

- `python -m unittest discover -s tests -v`: 143 passed on Windows Python 3.11.2. Nine new cases cover midpoint/ATR and other stop rules, tick/target rounding, tight-risk rejection, plan-ahead status, flat-by cutoffs, config warnings, gap rejection, saved-plan display, and budget/risk handoff.
- `python -m compileall -q india_trader`: passed.
- Using the saved **historical** RELIANCE entry from 29 October 09:40 IST, a read-only plan with an assumed Rs.0.05 tick produced an opening-range midpoint stop Rs.1163.35 and 2R target Rs.1196.20 from the Rs.1174.30 entry. An offline `atr:1.5` plan used five completed bars through 09:40 and produced stop Rs.1165.50 and target Rs.1191.90. It was saved and displayed with `show`; a separate direct Yahoo ATR fetch returned the same stop and target. The tick was an illustrative input, not independently verified for the instrument.
- A manual plan ahead keeps `trigger_at: null`. The paper budget can estimate size, but the risk review returns `UNTRIGGERED_PLAN` and blocks it. No order or fill was created. Yahoo prices and ATR bars were not reconciled to NSE.
- No commit or push occurred.

## Post-audit Windows validation — entry timer, 30 October 2026

- `python -m unittest discover -s tests -v`: 134 passed on Python 3.11.2. Eight new tests cover completed-bar timing, one-minute rule, optional volume filter, gaps, wrong symbol/interval, NSE holiday, live metadata, watch replay/once-only state, watchlist, and paper exit handoff.
- `python -m compileall -q india_trader`: passed.
- Historical read-only RELIANCE Yahoo checks returned 5-minute and 1-minute paper signals for 29 October at a 09:40 IST cutoff. The five-minute chart's first eligible close was 09:40 at Rs.1174.30, 0.325% over the Rs.1170.50 opening-range high. The one-minute chart's first eligible close was 09:34 at Rs.1171.30. Yahoo volume included zero/missing data, which the price-only checks disclosed; a requested volume threshold blocks unverified volume.
- A saved Yahoo chart was replayed bar by bar without waiting. It wrote `trader/replay_entry.json` once; a second replay reported `ALREADY_RECORDED`. This is historical paper research, not a current signal or fill. A live market-hours watch has not been run.
- No brokerage operation, commit, or push occurred.

## Post-audit Windows validation — news monitor, 30 October 2026

- `python -m unittest discover -s tests -v`: 126 passed on Python 3.11.2. Seven new cases cover NSE IST dates, company filtering, deduplication, price validation, scanner/plan/portfolio inputs, offline fallback, seen-state behavior, dry run, and partial failures.
- `python -m compileall -q india_trader`: passed.
- A read-only `news --tickers RELIANCE TCS --dry-run --json --hours 48` run reached all five public NSE feeds, Yahoo India RSS and Google News India RSS. NSE announcements returned 162 entries, with the oldest at 00:19:10 IST on 30 October, so the requested 48-hour filing window was **partial**. Yahoo chart metadata returned two prices; these were not exchange reconciled or live trade signals.
- Official NSE RSS often has no direct announcement-document link. The command labels those URLs as company-search pages. No claim is made that empty RSS output means no filing.
- No broker connectivity or orders were exercised. No commit or push was made for this local update.

## Corrections in v0.2.1

| Category | Change |
| --- | --- |
| Input validation | Revalidate `Bar` instances via the same parse rules as dictionaries (finite positive OHLC, aligned offset-aware timestamps, nonnegative volume). |
| Replay cutoff | Entry-time limit checks the actual next-bar hypothetical entry timestamp, not the triggering bar start. |
| Risk sizing | Requires sensible tick floor and fee buffer <= capital; excessive fee examples raise controlled errors. |
| Yahoo responses | Validate `result`, `quote`, and `indicators` data shapes; reject mismatched provider ticker when metadata provides one. |
| CSV / JSON | Reject CSV rows with more cells than headers; reject object JSON missing `bars` instead of silently returning no data. |
| Scanner | Detect duplicate tickers after NSE/BSE normalization; preserve per-ticker error reporting for invalid entries. |
| QA | Independent regression tests added to the original 61. |

## Smoke tests (executed on Linux Python 3.13.5, offline)

```shell
python -m unittest discover -s tests -v
python -m compileall -q india_trader
python -m india_trader doctor
python -m india_trader plan --entry 1500 --stop 1485 --capital 100000 --tick 0.10 --slippage-bps 10 --fee-buffer 50
python -m india_trader orb --csv examples/SYNTHETIC_TEST_BARS.csv --date 2026-10-28 --as-of 2026-10-28T09:40:00+05:30
python -m india_trader scan --folder examples --date 2026-10-28 --as-of 2026-10-28T09:40:00+05:30
python -m india_trader backtest --csv examples/SYNTHETIC_TEST_BARS.csv
```

All samples use **synthetic invented candles**, NOT actual NSE/BSE prices or evidence of profit.

## NOT verified / major product gaps

1. Windows 10/11 and Python 3.11 integration, or Codex GUI/CLI skill discovery. Agent skill folder format is consistent with the documented `.agents/skills` convention; live invocation was not performed.
2. Live Yahoo Finance availability, correctness, completeness, timestamp conventions, exchange-price reconciliation, or provider usage limits. Yahoo is an unofficial feed, not an exchange-approved execution feed.
3. Instrument master: current security identifiers, exchange/series, circuit limits, price bands, rolling tick sizes, CAS applicability, and corporate actions.
4. Indian exchange holidays, Muhurat, special market sessions, illiquid call auctions, or order book/volume-at-price behavior.
5. Full NSE/BSE/Nifty 500 universe scanner; sentiment/news/filings; alerts; original 10,000-outcome Time Machine; all 11 upstream skill equivalents.
6. Realistic strategy validation (out-of-sample, walk forward, realistic impact, commissions, STT/GST/exchange charges, survivorship bias) and live paper feed reconciliation.
7. Broker connectivity and automated trading. Deliberately NOT implemented. No claim of profitability.

**Research window:** Conservative 09:15–15:15 IST. The NSE CAS applies to certain derivative-eligible cash equities with a transition beginning 15:15, while other equities generally continue continuous trading to 15:30. The software uniformly truncates continuous-bar research at 15:15, intentionally skipping additional tradable minutes and all auction mechanics. This is a documented simplification, not a full market-hours implementation.

**Reference sources:**
- NSE CAS official: https://www.nseindia.com/static/products-services/closing-auction-session
- NSE tick-size circular: https://nsearchives.nseindia.com/content/circulars/CMTR67133.pdf
- OpenAI Codex repo-local skills: https://developers.openai.com/blog/skills-agents-sdk
- Upstream original: https://github.com/Jakeschincariol/day-trader-skill

**Approval decision:** Accept only as an **offline paper-research prototype** pending independent Windows, market-data, financial, security and capability acceptance testing. Previous v0.2 offline PASS should not be interpreted as proof of full correctness.
