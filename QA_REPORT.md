# Independent re-audit — India Day Trader v0.2.1

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
