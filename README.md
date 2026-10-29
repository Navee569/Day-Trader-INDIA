# Day Trader India — ChatGPT + Codex (v0.2.1)

An **independent, defensive Python implementation** inspired by the [MIT-licensed `day-trader-skill` by Jake Schincariol](https://github.com/Jakeschincariol/day-trader-skill). You do **not** need Claude. This archive **does not contain the upstream 11 Claude skills**, does not claim to be a complete port and is **not** a validated profitable strategy.

**Safety:** paper-only; no broker order connector or autonomous trade execution. An optional locally entered Upstox Analytics Token is read-only market-data access. Do not use Yahoo quotes for live execution. No investment advice.

## Getting started — Windows 10/11

Install **Python 3.11 or newer**. No pip dependencies required. Extract the ZIP. In File Explorer, open the `Day_Trader_India_ChatGPT_Codex_v0.2.1` folder; click the address bar, type `powershell`, press Enter, and run:

```powershell
python -m unittest discover -s tests -v
python -m india_trader doctor
python -m india_trader orb --csv examples\SYNTHETIC_TEST_BARS.csv --date 2026-10-28 --as-of 2026-10-28T09:40:00+05:30
python -m india_trader backtest --csv examples\SYNTHETIC_TEST_BARS.csv
python -m india_trader scan --folder examples --date 2026-10-28 --as-of 2026-10-28T09:40:00+05:30
```

`SYNTHETIC_TEST_BARS.csv` is **invented test data**, not actual Indian market history. Sample successes do not demonstrate returns.

### Analyze a ticker (automatically fetches bars; requires Internet)

You can give the app a ticker directly; you do **not** have to prepare candle files. It fetches five-minute bars from Yahoo Finance and runs the analysis. The feed is unofficial, may be delayed or incomplete, and is not checked against NSE/BSE.

```powershell
python -m india_trader orb RELIANCE --as-of 2026-10-29T10:30:00+05:30
python -m india_trader orb 500325 --exchange BSE --as-of 2026-10-29T10:30:00+05:30
```

For a list of tickers, put one symbol per line in `watchlist.txt`, then run `python -m india_trader scan --watchlist watchlist.txt --as-of 2026-10-29T10:30:00+05:30`. The watchlist is limited to 10 symbols per run. Use CSV or JSON input when you want to analyze your own verified bars or work offline.

### Fetch and save bars for later (requires Internet)

```powershell
python -m india_trader fetch RELIANCE --out trader\reliance.json
python -m india_trader orb --json trader\reliance.json --out trader\reliance_orb.json
python -m india_trader fetch 500325 --exchange BSE --out trader\bse.json
```

The public Yahoo Finance chart endpoint is **unofficial**: intermittent HTTP errors, delays, data gaps, historical limits and rate limiting can occur. `fetch` only allows 5-minute bars with `--range 1d`, `5d` or `1mo`. The program refuses many invalid candles and does not silently fill missing trading bars. It does not verify prices against NSE/BSE. Validate symbols, exchange, freshness, lot type, calendar, and corporate actions separately; do not assume the latest session is today's session.

The candle fetcher tries locally configured read-only Upstox first, then Yahoo's `query2` host and `query1` after a transport failure. The Yahoo hosts are **one provider**, not two independent data sources. Upstox needs a locally entered account token and is not live-tested in this installation. FYERS is not connected. NSE's official one- and five-minute snapshot feed is a subscription product. The app does not silently substitute a quote or a daily bar for a missing five-minute candle.

### Paper position plan (INR examples, not recommendations)

```powershell
python -m india_trader plan --entry 1500 --stop 1485 --capital 100000 --risk-pct 0.5 --max-alloc-pct 20 --target-r 2 --tick 0.10 --slippage-bps 10 --fee-buffer 50 --out trader\plan.json
```

The `--tick 0.10` is **only an example assumption**. NSE cash-equity tick sizes are price-dependent and reviewed monthly (e.g., according to circular NSE/CMTR/67133: ₹1,000–₹5,000 uses ₹0.10; subject to current security master); they can vary by stock/exchange; verify it with your broker. The `--tick` argument is **mandatory** to prevent silent rounding with an unverified universal tick. The entered fee buffer is **not** a substitute for calculating brokerage, exchange charges, STT, GST, stamp duty, SEBI charges or slippage. A stop loss can slip beyond the assumed loss. No order is generated or sent.

### Offline historical analysis

To inspect your own real OHLCV candles (CSV), use columns `timestamp,open,high,low,close,volume` (volume optional). Each timestamp must include its offset, for example `2026-10-28T09:15:00+05:30`; timestamp is **start of the five-minute candle**. No naive local times, repeated timestamps, missing opening bars, or malformed OHLC accepted. Zero-volume bars block the signal/replay; missing volume is disclosed as unverified liquidity, not treated as positive volume. Use `--csv ...` instead of live fetch.

```powershell
python -m india_trader backtest --csv your_verified_history.csv --out trader\replay.json
python -m india_trader orb --csv your_verified_history.csv --date 2026-10-28 --as-of 2026-10-28T10:30:00+05:30
```

The backtest only uses **complete 72-bar, gap-free sessions ending at 15:15 IST (conservative pre-auction cutoff)**. On a close above the opening range, it pretends to buy at the **next five-minute bar's OPEN**, not retrospectively at the breakout bar's close. If stop and target could both be hit in one candle, it counts the stop first. **Session cutoff:** Since a 2026 closing auction applies to certain stocks, the adapter uniformly stops continuous-bar analysis by **15:15 IST**. Non-auction stocks may continue through 15:30; this tool intentionally skips that segment. It does **not** simulate auction orders, matching or execution. **It assumes zero fees, zero slippage, 100% fill availability, no liquidity problem, no corporate actions and no verified instrument calendar.** Results are exploratory, not accurate real-world P&L, and not Monte Carlo forecasting.

### Scanner

To build an NSE cash-equity `EQ` symbol list from NSE's public CSV, run:

```powershell
python -m india_trader universe
```

This writes `trader/universe.txt` with symbol, company name and ISIN. It does not itself run a marketwide scan, and it excludes BSE-only listings and non-EQ series. The source is [NSE's securities available for trading list](https://www.nseindia.com/static/market-data/securities-available-for-trading); check its retrieved time before using it.

Offline scanner accepts a folder of CSV files (one symbol per file). It returns every data error explicitly:

```powershell
python -m india_trader scan --folder verified_csv_folder --as-of 2026-10-28T10:30:00+05:30
```

To inspect a small live *research* watchlist, create `watchlist.txt` with one NSE/BSE symbol per line and use:

```powershell
python -m india_trader scan --watchlist watchlist.txt --out trader\scan.json
```

Default limit: **10 tickers per run**. This is **not** an NSE-wide or Nifty 500 real-time scanner. Avoid rapidly polling the public endpoint; check the provider's terms and applicable market data permissions.

### One-command paper research cycle

`paper-cycle` fetches the watchlist, checks completed five-minute bars, rejects old signals or missing volume, and saves hypothetical risk plans. It never submits an order or records a fill. Replace the illustrative watchlist with your own symbols and supply a tick assumption that is valid for **every** symbol in that run; split the list if tick sizes differ.

```powershell
python -m india_trader paper-cycle --watchlist watchlist.txt --tick 0.10 --out trader\paper_cycle.json
```

The `--tick 0.10` value is only an example. Verify each instrument's tick size before using its plan. `--as-of` can pin a historical IST cutoff; without it, the command uses the current IST time. This is a single run, not an unattended scheduler. The unofficial Yahoo feed, missing market calendar, and unverified costs make it unsuitable for live order placement.

For an initial paper example, double-click `RUN_PAPER_CYCLE_WINDOWS.bat`. It uses the illustrative `paper_watchlist.txt` (RELIANCE only), a hypothetical ₹100,000 balance and the same unverified tick/cost assumptions. It writes `trader/paper_cycle.json`; a `NO_PAPER_PLAN` result is expected whenever data checks block a plan.

## Official NSE daily and delayed quote sources (installed update)

```powershell
python -m india_trader nse-bhavcopy --date 2026-10-28 --symbol RELIANCE
python -m india_trader nse-live RELIANCE
```

`nse-bhavcopy` reads NSE's final CM-UDiFF daily archive, validates the trade date and OHLCV, and selects NSE `EQ` shares. The report appears after the trading day; it cannot supply five-minute candles. `nse-live` checks normal weekday market hours first and returns `MARKET_CLOSED` without a quote outside them; the app does not yet account for holidays and special sessions. During normal hours it tries NSE's official CM Market Live MCP quote service, whose prices NSE says lag by about 1–3 minutes. If the service rejects its session or a quote is unavailable, the command returns `UNAVAILABLE` with no price. An after-hours direct probe on 29 October 2026 initialized but returned HTTP 404, “Session not found”, when the tool list was requested. An in-session quote has not yet been validated. Both sources are for informational paper research under [NSE's MCP terms](https://www.nseindia.com/nse-mcp); they do not enable broker orders. The five-minute ORB uses locally configured read-only Upstox candles, unofficial Yahoo fallback, or user-provided candles.

### Marketwide daily scan

`python -m india_trader daily-scan --as-of 2026-10-28 --reports 30 --max-hits 50 --out trader/daily_scan.json` reads 30 completed NSE reports and applies the original repo's five daily close setup rules: 20-day breakout, bull flag, 20-day moving-average reclaim, RSI oversold, and oversold bounce. It reports how many symbols were scanned, skipped for short history or low liquidity, and whether any report dates were unavailable. A 29 October 2026 run on the 28 October close scanned 1,111 eligible NSE stocks and found 318 setup hits. This is end-of-day research, not intraday entry timing.

### Independent five-minute candle source

The installed app now tries an Upstox **read-only Analytics Token** first, if one is supplied locally, and falls back to Yahoo. It never combines bars from different sources in one series. [Upstox says the token and its market-data APIs are free](https://upstox.com/developer/api-documentation/analytics-token/); you need an Upstox account and must check any account charges separately. To generate it, sign in to [Upstox Developer Apps](https://account.upstox.com/developer/apps), open **Analytics**, and generate a token. Do not send the token in a chat, save it in this repository, or include it in screenshots. On Windows, double-click `RUN_PAPER_CYCLE_WITH_UPSTOX_WINDOWS.bat` and enter the token at the hidden prompt; the token stays in that run's process environment and is not saved. If Upstox is unavailable, the result names the failed source and uses Yahoo if Yahoo responds.

[FYERS Standard](https://fyers.in/notice-board/changes-to-fyers-api-rate-limits-and-usage) is another account-based five-minute source, but is not connected to this app yet. [Dhan](https://dhanhq.co/docs/v2/authentication/) documents minute candles and says its Data APIs have extra charges. NSE Bhavcopy and CM Market Live provide daily bars and delayed quotes, respectively; neither is a five-minute candle fallback.

### Entry and exit handoff

`entry` checks completed five-minute bars for an opening-range or explicit level close. It can enforce a trigger-bar volume multiple, reports the trigger time and age, and never places an order. `exit` accepts a saved triggered entry and computes an opening-range midpoint/low, percentage, explicit-price, or ATR stop plus an R-multiple or explicit-price target. A tick assumption is required. Example with the bundled synthetic bars:

```powershell
python -m india_trader entry --csv examples/SYNTHETIC_TEST_BARS.csv --as-of 2026-10-28T09:35:00+05:30 --out trader/entry.json
python -m india_trader exit --from-entry trader/entry.json --stop or-mid --target 2R --tick 0.05 --out trader/exit.json
```

The example produced a hypothetical entry ₹103, stop ₹100, and target ₹109. The synthetic dataset is for checking code, not a real stock signal. There is no continuous background entry watch yet.

### Paper budget, risk, and ticket

`budget` sizes a proposed whole-share, long-cash paper position from a saved exit plan using capital, per-trade loss, allocation, cash buffer, slippage, and fee assumptions. `risk` checks the exact proposal fingerprint, IST entry window, freshness, daily loss, trade count, duplicate symbol, and paper cash. `paper-ticket` shows the reviewed ticket; `--approve` prompts for an exact `YES` and records approval only after a fresh same-day risk review. It never sends an order or assumes a fill. An offline synthetic end-to-end check produced a 140-share proposal with ₹498.42 planned loss under a ₹500 example limit, and an `AWAITING_APPROVAL` ticket. This is not a real RELIANCE price or recommendation.

```powershell
python -m india_trader budget --plan trader/exit.json --out trader/order.json
python -m india_trader risk --order trader/order.json --out trader/risk.json
python -m india_trader paper-ticket --order trader/order.json --risk trader/risk.json --out trader/ticket.json
```

## Using ChatGPT (without Claude)

Upload this ZIP and ask ChatGPT to inspect `SKILL.md` and analyze **your verified candle CSV**. A standard chat does not automatically access your Windows filesystem or run this repository locally. To execute programs directly on your Windows PC use Python or Codex CLI/IDE; some ChatGPT workflows can run code in their own environment but aren't a local installation.

## Using Codex (without Claude)

Open the extracted folder in Codex; `AGENTS.md` is the repo policy file and `.agents/skills/india-day-trader/SKILL.md` is a valid repository-local Agent Skill. Codex supports repo skills in `.agents/skills` and explicit `$india-day-trader` invocation. Ask: **“Use $india-day-trader; run tests, then analyze the provided CSV and explain the 15-minute ORB. Keep it paper-only.”** If skills aren't listed, restart Codex and point it directly to the skill file. Codex uses the Python installed in its environment; make sure your shell can run `python`.

## Implemented vs NOT implemented

| Feature | Current development state |
| --- | --- |
| NSE/BSE Yahoo-format symbol mapping | Implemented; provider availability unverified |
| Offline 5-minute CSV / JSON validation | Implemented and tested |
| Completed 15-minute ORB + fail-closed gaps/staleness | Implemented and tested |
| Hypothetical INR risk/position/target planner | Implemented and tested |
| Historical session replay and descriptive win rate | Implemented and tested on synthetic data |
| Watchlist scanner (up to 10) and offline folder scanner | Implemented and tested offline |
| Codex Agent Skill / ChatGPT workflow documentation | Included |
| NSE EQ daily universe / BSE universe | NSE EQ list and official daily marketwide scan implemented; BSE not implemented |
| Independent five-minute candle sources | Read-only Upstox adapter prepared but not account-tested; unofficial Yahoo fallback tested |
| Entry, exit, budget, risk, paper ticket | Basic handoff implemented and tested offline; no background watch or simulated fill ledger |
| Real-time exchange-approved live market feed | **Not implemented** |
| Indian corporate filings/news/financial statements NLP | **Not implemented** |
| Marketwide sentiment/realtime alerts | **Not implemented** |
| Indian exchange holiday + Muhurat + special-auction calendar | **Not implemented** |
| Broker API, order placement, broker fees, tax engine | **Not implemented (safety boundary)** |
| Original 11 Claude skills, full parity / Monte Carlo 10,000 | **Not implemented** |
| Graphical UI | **Not implemented** |

**Why not claim full parity?** US SEC/EDGAR, Pelosi disclosures, Alpaca MCP and US-specific trading rules are not Indian market components and cannot be truthfully relabeled as NSE/BSE integrations. Real equivalents require different audited feeds, policies, connectors and independent acceptance tests.

## QA and provenance

See `QA_REPORT.md`, `CHANGELOG.md`, and `TESTER_PROMPT.txt`. The prior v0.2 release passed its original 61 tests but contained independently reproduced validation and replay bugs; the v0.2.1 re-audit passed 79. This later working state passed 109 offline tests on Windows Python 3.11. NSE daily Bhavcopy and Yahoo paper-cycle smoke checks ran; Upstox, CM Market Live during market hours, exchange holidays, actual order execution and profitability have **not** been validated. The supplied tests are Python standard-library `unittest`. `python -m compileall -q india_trader` checks syntax. Any mention of a `TRIGGERED_LONG` status means a **completed-bar historical condition** only, not a recommendation or market-order fill.

Sources: [NSE tick-size circular](https://nsearchives.nseindia.com/content/circulars/CMTR67133.pdf), [2026 closing-auction timing reference](https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/what-are-the-market-timings), [NSE market timings](https://www.nseindia.com/static/market-data/market-timings), [OpenAI skills documentation](https://learn.chatgpt.com/docs/build-skills), [upstream project](https://github.com/Jakeschincariol/day-trader-skill).
