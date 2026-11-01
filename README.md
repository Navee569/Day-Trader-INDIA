# Day Trader India — ChatGPT + Codex (v0.2.1)

An **independent, defensive Python implementation** inspired by the [MIT-licensed `day-trader-skill` by Jake Schincariol](https://github.com/Jakeschincariol/day-trader-skill). You do **not** need Claude. All 11 numbered upstream workflows now have Indian paper-research adaptations, with documented source and execution differences. This is **not** a validated profitable strategy or a live broker system.

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

You can give the app a ticker directly; you do **not** have to prepare candle files. It fetches five-minute bars through configured read-only Upstox or FYERS account sources, then unofficial Yahoo fallback, and runs the analysis. Account feeds have not been live-tested here; Yahoo may be delayed or incomplete, and none is reconciled against NSE/BSE.

```powershell
python -m india_trader orb RELIANCE --as-of 2026-10-29T10:30:00+05:30
```

For a list of tickers, put one symbol per line in `watchlist.txt`, then run `python -m india_trader scan --watchlist watchlist.txt --as-of 2026-10-29T10:30:00+05:30`. The watchlist is limited to 10 symbols per run. Use CSV or JSON input when you want to analyze your own verified bars or work offline.

### Fetch and save bars for later (requires Internet)

```powershell
python -m india_trader fetch RELIANCE --out trader\reliance.json
python -m india_trader orb --json trader\reliance.json --out trader\reliance_orb.json
```

The public Yahoo Finance chart endpoint is **unofficial**: intermittent HTTP errors, delays, data gaps, historical limits and rate limiting can occur. `fetch` only allows 5-minute bars with `--range 1d`, `5d` or `1mo`. The program refuses many invalid candles and does not silently fill missing trading bars. It does not verify prices against NSE/BSE. Validate symbols, exchange, freshness, lot type, calendar, and corporate actions separately; do not assume the latest session is today's session.

The candle fetcher tries locally configured read-only Upstox, then optional FYERS historical candles for NSE cash equities, then Yahoo's `query2` host and `query1` after a transport failure. The Yahoo hosts are **one provider**, giving three independent five-minute adapter paths, but only Yahoo has been exercised without account credentials. Upstox and FYERS need their own accounts/tokens and are not live-tested in this installation. NSE's official one- and five-minute snapshot feed is a subscription product. The app does not silently substitute a quote or a daily bar for a missing five-minute candle.

### Automatic paper checks on Windows

`python -m india_trader market-calendar refresh` caches the current year's public NSE/BSE holiday and special-session schedule from Upstox without a token. Check a date with `python -m india_trader market-calendar status --date 2026-11-08 --exchange NSE`. A normal-session scan refuses a known special session; future years require a fresh calendar. This public calendar is a research source, so verify exchange notices before relying on a schedule.

`python -m india_trader paper-monitor once --symbol RELIANCE --json` makes one paper-only check. `RUN_PAPER_MONITOR_WINDOWS.bat` keeps the monitor visible. `INSTALL_PAPER_MONITOR_TASK_WINDOWS.ps1` installs a weekday 09:00 IST Windows task while the user is signed in; run it with `-Remove` to uninstall. The monitor refreshes the calendar daily, checks saved alert rules every 30 seconds during a normal session, news every 15 minutes, issuer insider disclosures every 30 minutes, and NSE earnings every six hours. It writes events to `trader/state/paper-monitor-events.jsonl`. Add rules with `python -m india_trader alerts add`; an empty alert-rule file produces no price rings. It does not send desktop or chat notifications, guarantee delivery, or place orders. A logged error or missing public source must be reviewed manually.

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

The original scanner's three workflows now have an NSE EQ adaptation. Run this to create the official NSE EQ list, scan daily closes across that list, and scan five-minute opening-range candidates:

```powershell
python -m india_trader scanner universe
python -m india_trader scanner daily --show 20
python -m india_trader scanner orb --at 10:15 --show 20
```

`daily` uses the original five setup rules on unofficial Yahoo batch closes. `orb` uses the original two-stage method: batch closes first, then full OHLCV charts for candidates. Its default `--max-confirm 200` verifies the 200 most traded candidates; `--max-confirm 0` requests all of them. A partial result reports the exact number left unconfirmed. Both write `trader/watchlist.json` with `hits[].symbol` for the next workflow. Use `--no-write` to inspect without changing that file, `--json` for complete counts, `--symbols RELIANCE,TCS` or `--watchlist FILE` for a smaller run, and `--offline PATH --now "2026-10-28 10:15"` to replay saved Yahoo JSON. The source is unofficial and a historical `--at` replay is not a live signal.

On 29 October 2026, the official list contained 2,368 NSE EQ names. A full Yahoo daily run fetched 2,366, scanned 1,027 after quality/liquidity checks, and found 322 setup matches. A 10:15 IST intraday replay fetched 2,366, found 1,367 usable charts and 949 stage-one candidates; it confirmed 200, found 120 matches among those, and labeled the other 749 candidates **unconfirmed**. These are observed coverage counts for that run, not a performance measure. NSE daily Bhavcopy is also available through `daily-scan`, described below. The scanner has no exchange-verified five-minute feed.

For optional BSE security-master coverage, `python -m india_trader scanner universe --exchange BSE --no-write --json` reads [BSE's public SCRIP.ZIP](https://www.bseindia.com/downloads/Help/file/scrip.zip) in memory and keeps active one-share-lot cash equities in groups A/B/X/T/XT. `--exchange BOTH` combines the NSE and BSE lists. The 29 October 2026 BSE master yielded 4,126 filtered codes; both lists together yielded 6,494 exchange-specific entries. **BSE chart coverage is not validated:** Yahoo returned no chart for the official numeric codes `500325.BO` and `532540.BO` in a two-symbol check, and the `RELIANCE.BO` alias had too little daily history. A BSE-inclusive list may therefore produce many failed or unusable charts. Use the default NSE universe for the validated scanner path. Neither exchange has a verified five-minute feed in this app.

The older `scan --watchlist` command below remains a ten-symbol single-stage research check; use `scanner` for the marketwide workflow.

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

Default limit for this older command: **10 tickers per run**. Avoid rapidly polling the public endpoint; check the provider's terms and applicable market data permissions.

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

The installed app tries an Upstox **read-only Analytics Token** first, if one is supplied locally. It then tries FYERS historical candles for NSE shares when `FYERS_APP_ID` and `FYERS_ACCESS_TOKEN` are set, followed by Yahoo. It never combines bars from different sources in one series. [Upstox says the token and its market-data APIs are free](https://upstox.com/developer/api-documentation/analytics-token/); you need an Upstox account and must check any account charges separately. To generate it, sign in to [Upstox Developer Apps](https://account.upstox.com/developer/apps), open **Analytics**, and generate a token. Do not send any token in a chat, save it in this repository, or include it in screenshots. On Windows, double-click `RUN_PAPER_CYCLE_WITH_UPSTOX_WINDOWS.bat` and enter the token at the hidden prompt; the token stays in that run's process environment and is not saved.

[FYERS Historical Data](https://fyers.in/api-docs/v3/api/market-data/historical-data) documents one- and five-minute candles, but requires a FYERS account, an API app with Market Data permission, and a daily [app ID/access token](https://fyers.in/api-docs/v3/api/authentication/). For an optional local run, set `FYERS_APP_ID` and `FYERS_ACCESS_TOKEN` in that PowerShell process before starting the app; never add them to source files or send them here. The FYERS adapter is offline-tested but has not been checked with a real account. [Dhan](https://dhanhq.co/docs/v2/authentication/) documents minute candles and says its Data APIs have extra charges. NSE Bhavcopy and CM Market Live provide daily bars and delayed quotes, respectively; neither is a five-minute candle fallback.

### Entry and exit handoff

`entry` checks completed five-minute bars for an opening-range or explicit level close. It can enforce a trigger-bar volume multiple, reports the trigger time and age, and never places an order. `exit` accepts a saved triggered entry and computes an opening-range midpoint/low, percentage, explicit-price, or ATR stop plus an R-multiple or explicit-price target. A tick assumption is required. Example with the bundled synthetic bars:

```powershell
python -m india_trader entry --csv examples/SYNTHETIC_TEST_BARS.csv --as-of 2026-10-28T09:35:00+05:30 --out trader/entry.json
python -m india_trader exit --from-entry trader/entry.json --stop or-mid --target 2R --tick 0.05 --out trader/exit.json
```

The example produced a hypothetical entry ₹103, stop ₹100, and target ₹109. The synthetic dataset is for checking code, not a real stock signal. The separate `entry-timer watch` command provides a bounded foreground watch.

### Paper budget, risk, and ticket

`budget` sizes a proposed whole-share, long-cash paper position from a saved exit plan using capital, per-trade loss, allocation, cash buffer, slippage, and fee assumptions. `risk` checks the exact proposal fingerprint, IST entry window, freshness, daily loss, trade count, duplicate symbol, and paper cash. `paper-ticket` shows the reviewed ticket; `--approve` prompts for an exact `YES` and records approval only after a fresh same-day risk review. It never sends an order or assumes a fill. An offline synthetic end-to-end check produced a 140-share proposal with ₹498.42 planned loss under a ₹500 example limit, and an `AWAITING_APPROVAL` ticket. This is not a real RELIANCE price or recommendation.

```powershell
python -m india_trader budget --plan trader/exit.json --out trader/order.json
python -m india_trader risk --order trader/order.json --out trader/risk.json
python -m india_trader paper-ticket --order trader/order.json --risk trader/risk.json --out trader/ticket.json
```

### Budget Manager: sixth upstream workflow

The expanded `budget-manager` command reads the saved `trader/plan.json` from `exit-planner`, an optional `trader/config.json`, and optional dated local prices and paper cash. `examples/BUDGET_CONFIG_INDIA.json` shows the INR settings. Supply hypothetical capital with `--budget INR` or `budget_inr` in your config; the example has no prefilled capital. `--now` pins an IST replay clock. This remains paper research, even if a historical risk check passes on a pinned clock.

```powershell
python -m india_trader budget-manager --budget 100000 --write
python -m india_trader risk --order trader/orders.json --out trader/risk.json
python -m india_trader budget-manager --budget 100000 --limit --write
python -m india_trader budget-manager --budget 100000 --weights RELIANCE=40,TCS=60
```

Single-trade size is the smallest whole-share count allowed by **four limits**: maximum assumed loss after a fee buffer, maximum position percentage, maximum single order, and cash after a reserved buffer. The report names the binding limit and refuses zero shares, a below-minimum order, insufficient reward:risk, wrong-day/blocked/short plans, or a target already passed. `--limit` uses the plan price; `--limit PRICE` sizes at that hypothetical limit. For market-reference sizing, a higher local price is used only when its file has an offset-aware timestamp no more than 15 minutes old. The default assumptions are a ₹500 max loss, 20% position cap, ₹25,000 max order, 10% cash buffer, 10 bps per-side slippage and a ₹50 fee buffer; edit the config for your own **paper** scenario. These are assumptions, not a complete Indian charges or fill model.

`--write` saves a paper proposal collection at `trader/orders.json`. The existing `risk` and `paper-ticket` commands accept a collection containing exactly one stop-and-target proposal. A refusal overwrites the saved collection with zero proposals, so a stale proposal cannot be reused accidentally. Weights mode accepts `--weights` or a `--targets` JSON file; it renormalizes after blocked/zero/top-N filters, caps overweight slices, drops undersized slices, and reports idle cash. Without a dated `--prices` file it shows **indicative INR allocations only**. With prices it rounds to whole shares but marks every allocation `UNREVIEWED_NO_STOP`; each ticker needs its own exit plan before paper risk approval. The calculation assumes a one-share market lot; verify the security's series and lot before relying on it. [NSE's normal-market trading-system description](https://www.nseindia.com/static/products-services/equity-market-trading-system) says the market lot is one for shares in compulsory dematerialised mode. No fractional cash shares, broker orders, or simulated fills are created.

### Risk Manager: seventh upstream workflow

`risk-manager` reads `trader/orders.json`, optional config, prices, portfolio and paper broker state, and checks each order in sequence. It recalculates stop loss and reward:risk from a higher fresh dated price, then checks the blocked list, order size, position cap, cash buffer, daily planned loss, trade and order counts, duplicates, completed paper trigger and IST entry window. It saves each PASS/BLOCK verdict with the exact order fingerprint. A later order in a batch is checked against earlier passed proposals as if they filled for the **limit calculation only**. A paper exit may reduce a verified holding even after the entry window or a daily limit, but cannot sell more shares than held.

Risk review also reconstructs daily counts and planned loss from logged paper fills if saved counters are missing, and uses fresh dated prices to value existing paper holdings for the position cap. `--today YYYY-MM-DD` must match the IST clock.

```powershell
python -m india_trader risk-manager --orders trader/orders.json
python -m india_trader risk-manager --orders trader/orders.json --no-write --json
python -m india_trader paper-ticket --order trader/orders.json --risk trader/state/risk.json
```

The saved risk result is a paper rules check, not approval or a fill. This reviewer uses the known 2026 normal-session NSE holiday list plus a fresh current-year public calendar when available; known special sessions fail closed. BSE execution rules remain unverified. It has no broker connection or margin/derivatives/short-selling path.

### News monitor (upstream workflow 2, India adaptation)

```powershell
python -m india_trader news --tickers RELIANCE TCS --dry-run
python -m india_trader news --json
```

The first command checks two examples without saving seen-item state. The second reads scanner watchlist hits, a saved plan, and portfolio positions; `trader/universe.txt` supplies company names. The monitor reads public NSE RSS feeds for announcements, financial results, board meetings, corporate actions, and insider trading. It checks Yahoo India headlines and uses Google News India RSS when Yahoo has no recent company-matched result. Yahoo chart metadata provides an unofficial price-versus-previous-close line, with a 3% move marker. There is no order placement or broker login.

By default it shows only new items from the last 48 hours and remembers fingerprints for 30 days in `trader/state/news.json`. Use `--all` to include seen items, `--hours N` for the lookback, `--feeds announcements,corporate_actions` to select filing categories, `--no-nse`, `--no-headlines`, or `--no-moves` to omit a source, and `--offline DIR --now ISO_TIME` to replay saved feeds. `--json` reports each feed's status and a `partial` flag. Exit code 1 means there are no new items. The free NSE announcements RSS can cover much less than 48 hours and may omit individual filing links; a company-search link is **not** the filing itself. Open the actual NSE document or news article before relying on an item. A quiet or partial result cannot establish that no company event occurred. [NSE's RSS directory](https://www.nseindia.com/static/rss-feed) lists the public feeds.

### Entry timer (upstream workflow 3, India adaptation)

```powershell
python -m india_trader entry-timer check RELIANCE
python -m india_trader entry-timer check RELIANCE --setup level --level 1200 --interval 1m
python -m india_trader entry-timer watch RELIANCE --until 15:10
python -m india_trader entry-timer watchlist
```

The timer uses NSE cash-equity bars from unofficial Yahoo charts; it accepts saved chart JSON for offline replay. Its default ORB level is the high of completed bars from 09:15–09:30 IST. A completed close above the level is a paper signal; a wick or forming bar does not count. `--interval 1m` and `5m`, a fixed `--setup level --level PRICE`, `--or-minutes N`, and optional `--min-volume-x N` work in `check`, `watch`, and `watchlist`. `watchlist` reads scanner hits; a level setup may read `hits[].level`. `watch` polls while the program and computer are awake, stops at the configured cutoff, saves a trigger to `trader/entry.json`, and remembers that setup so it does not save it twice. `check --write` saves a single result. `--offline PATH --now ISO_TIME` replays a saved chart on a simulated clock.

The normal research window ends at 15:15 IST to avoid NSE closing-auction ambiguity; the last new-entry cutoff is 15:10. The timer validates the completed-bar grid, exchange symbol, currency, gaps, holiday date, provider last-trade time, and a one-minute live settling delay. It reports missing/zero volume. With a requested volume threshold, missing volume blocks the setup. A historical signal, even if the command says `TRIGGERED`, is **not** a current entry, fill, or order. Current Yahoo bars are unofficial and may lag. The built-in NSE holiday list covers 2026; the refreshed public calendar also closes known special sessions. BSE entry timing checks a fresh public BSE calendar; if unavailable, it uses an explicitly labeled NSE proxy. See [NSE market timings](https://www.nseindia.com/static/market-data/market-timings) and [NSE closing auction](https://www.nseindia.com/static/products-services/closing-auction-session).

### Exit planner (upstream workflow 4, India adaptation)

```powershell
python -m india_trader exit-planner plan --tick 0.05 --write
python -m india_trader exit-planner show
python -m india_trader exit-planner plan --symbol RELIANCE --entry 1200 --stop pct:1 --tick 0.05 --date 2026-10-30
```

The first command reads `trader/entry.json` from the entry timer, calculates an opening-range midpoint stop and 2R target, and saves `trader/plan.json` for the paper budget step. The second shows that saved plan. The third is a **hypothetical plan ahead**, not a trigger. Replace `0.05` with the instrument's verified current NSE tick size; this example does not establish RELIANCE's tick. NSE [reviews tick size by security](https://nsearchives.nseindia.com/content/circulars/CMTR62174.pdf), so this command requires an explicit assumption.

Stop choices are `or-mid`, `or-low`, `price:X`, `pct:X`, and `atr:K`; targets are `1R`, `1.5R`, `2R`, or `price:X`. ATR uses one-minute or five-minute session bars through the entry close, available from a saved Yahoo chart with `--offline PATH` or a fresh public fetch. It rejects missing/gapped histories and reports when fewer than 14 bars were used. The stop rounds down to the chosen tick and the target rounds half up. `--flat-by HH:MM` and optional `trader/config.json` set a latest exit time no later than 15:10 IST; an entry at or after that time is refused. The plan records risk per share, reward:risk, source, volume quality, and warnings. A plan made ahead has no trigger and cannot pass the paper risk check. This is a plan for a paper exit, not a stop order or guaranteed fill.

## Using ChatGPT (without Claude)

Upload this ZIP and ask ChatGPT to inspect `SKILL.md` and analyze **your verified candle CSV**. A standard chat does not automatically access your Windows filesystem or run this repository locally. To execute programs directly on your Windows PC use Python or Codex CLI/IDE; some ChatGPT workflows can run code in their own environment but aren't a local installation.

## Using Codex (without Claude)

Open the folder in Codex; `AGENTS.md` is the repo policy file. Each numbered workflow has an India skill in `.agents/skills`, including `india-trader-analyst` and `india-trader-insiders`; `.agents/skills/india-day-trader/SKILL.md` covers the other paper research commands. Invoke the matching `$india-trader-*` skill for each workflow. Codex uses the Python installed in its environment; make sure your shell can run `python`.

### Time Machine: fifth upstream workflow

After `exit-planner plan --write` saves `trader/plan.json`, run:

```powershell
python -m india_trader time-machine run --write
python -m india_trader time-machine chart --from trader/timemachine.json
```

The first command fetches an unofficial 60-day Yahoo five-minute chart, uses at least 20 complete prior 09:15–15:15 IST sessions, runs 10,000 seeded scenario paths (seed 19), and replays the opening-range rule over those past sessions. `--offline SAVED_CHART.json` gives a repeatable run; `--now ISO_TIME` pins the completed-bar cutoff. `--replays N`, `--seed N`, `--window N`, and `--until HH:MM` are supported. Manual `--symbol`, `--date`, `--entry`, `--stop`, `--target`, and `--entry-time` flags make a hypothetical plan. A saved result contains four odds, exit-time price quantiles, average R, historical rule results, verdict, percentile bands, sample paths and histogram. The chart command renders the saved bands and histogram in text.

Only the first 72 five-minute bars (through 15:15 IST) are included to avoid closing-auction mechanics. The past-rule replay supports ORB; for non-ORB setups it reports that history is unavailable. ATR, price and percentage stops use the plan's percentage risk as an explicitly named historical proxy. The original's manual `--side sell` paper scenario is supported with `target < entry < stop`; the India exit planner and budget remain long-only, so a short scenario has no sizing handoff. The scenario odds are resampling fractions, not a forecast or profitability proof. The run excludes fees and market frictions, uses an assumed tick from the plan, and never places an order.

### Paper Broker: eighth upstream workflow

Set a hypothetical `budget_inr` and `paper: true` in `trader/config.json` first (see `examples/BUDGET_CONFIG_INDIA.json`). After `budget-manager` proposes an exact order and `risk-manager` records a fresh PASS, run `python -m india_trader broker show`. Ask for one explicit yes or no per ticket; `broker approve --index 1 --reply yes` records that answer. `broker fill --index 1 --price INR --confirm PAPER` records a **simulated** fill at the supplied hypothetical price. `broker portfolio` and `broker log` show local cash, holdings, gross realized P&L, decisions and fills. `broker flatten --ticker SYMBOL` writes a position-reducing exit proposal to `trader/flatten_order.json`; run `risk-manager --orders trader/flatten_order.json --out trader/state/flatten_risk.json` and repeat the ticket/approval/fill steps with `broker --orders trader/flatten_order.json --risk trader/state/flatten_risk.json`. The skill file has the exact commands.

`show --json` returns structured tickets. `fill --qty N` records a partial paper fill; later same-day slices remain bounded by the reviewed quantity and risk. `decline --index N` and `result --index N --status denied|failed` record unfilled outcomes. `log --all` includes past dates. A successful approval needs a risk check no older than five minutes; a fill after that approval uses the same-day verdict and is still a local simulation.

No broker account, exchange order, exchange fill, stop order or automatic flat-by scheduler exists. The user supplies each simulated fill price, and fees/taxes are not charged to the ledger. The portfolio's equity is valued at cost, not at a current quote. A recorded paper stop/target is a plan only; a human must run the exit flow. The original's Alpaca/IBKR/Claude approval-card integration has no Indian equivalent here. Never interpret a paper ledger balance as actual money or validated profit.

### Paper Alerts: ninth upstream workflow

```powershell
python -m india_trader alerts clock
python -m india_trader alerts add --from-plan
python -m india_trader alerts quote --holdings --write
python -m india_trader alerts check
python -m india_trader alerts watch --every 30
python -m india_trader alerts list
```

`add --from-plan` creates date-bound stop, target and flat-by time alerts from `trader/plan.json`. Custom `--above`, `--below`, `--stop`, `--target`, `--pct` with optional up/down direction or reference, `--at HH:MM`, and `--opening-bell` rules are available. Price rules can `--rearm` after crossing back; `reset ID` clears a ring. Time, bell and percent rules ring once per trading day. `check` returns exit code 0 when something rings and 1 when nothing rings, saves once-only state in `trader/state/alerts.json`, and can read saved dated quotes with `--saved` or offline Yahoo response files with `--offline DIR`. `--no-write` is read-only. The `quote` command can merge portfolio/plan tickers into `trader/prices.json` for budget/risk checks.

Output is readable text by default; `--json` gives the full result. `--source saved` reads a previously dated price file without network, while `--source yahoo` is the default. `quote --write PATH` and `check --prices-out PATH` select output files. `add --config PATH --from-plan` takes a fallback `flat_by` when the plan has none. Editable examples are in `examples/ALERTS_INDIA.json` and `examples/TARGETS_INDIA.json`.

With a locally configured Analytics Token, quote and alert checks try Upstox V3 quotes before unofficial Yahoo query2/query1. Each quote shows its provider and market timestamp. A price rule refuses a missing, future, or more-than-15-minute-old quote; no five-minute candle is inferred from a Bhavcopy or CM Market Live value. The built-in NSE clock knows 2026 normal sessions, and a fresh public calendar closes known special sessions; unknown future sessions fail closed. BSE alert-session timing remains unverified. `watch` runs repeated checks in the foreground until 15:30 IST or `--until`, saves ring/error events to `trader/state/alert-events.jsonl`, and sounds a console bell. Double-click `RUN_PAPER_ALERT_WATCH_WINDOWS.bat` to keep a visible watcher open. The separate Windows paper monitor can run on a scheduled task but still does not guarantee desktop notification. A flat-by alert tells the user to check the holding and use the paper Broker flatten path manually.

### Earnings Analyst: tenth upstream workflow

```powershell
python -m india_trader earnings-analyst RELIANCE --quarters 8
python -m india_trader earnings-analyst HDFCBANK --basis consolidated --json
python -m india_trader earnings-analyst --isin INE002A01018 --metrics trader/metrics.json
```

The analyst reads official NSE integrated and older financial-result indexes, then parses linked XBRL. It reports INR quarterly revenue or bank total income, net profit, diluted EPS, same-quarter growth, available margins and four-quarter totals. It keeps consolidated and standalone filings separate, uses the latest available revision, and marks a fourth quarter derived from an annual filing as approximate for EPS. Gross margin stays blank when not filed; a non-bank operating margin calculated from profit before exceptional items, finance costs and other income is labeled a **proxy**. It links the source filing and same-cycle NSE PDF release candidates without treating press-release language as filed fact. `--as-of` supports point-in-time replay; `--offline DIR` supports saved index/XBRL fixtures. `--no-filings` skips announcement lookup, and `--metrics` merges a dated JSON summary. [PARITY_AUDIT_10.md](PARITY_AUDIT_10.md) lists each original behavior and its India equivalent.

The public source pages are [NSE Integrated Filing Financials](https://www.nseindia.com/companies-listing/corporate-integrated-filing) and [NSE Financial Results](https://www.nseindia.com/companies-listing/corporate-filings-financial-results). The example [METRICS_INDIA.json](examples/METRICS_INDIA.json) is a dated public NSE filing snapshot, not a current recommendation. Upcoming earnings dates and call transcripts need separate sources. A result released during an overnight paper holding can gap past a planned stop. The analyst does not place or recommend orders.

### Insider Tracker: eleventh upstream workflow

```powershell
python -m india_trader insider-tracker index --symbol RELIANCE --read
python -m india_trader insider-tracker new --symbol RELIANCE --read --json
python -m india_trader insider-tracker fetch current:384 --symbol RELIANCE --json
python -m india_trader insider-tracker index --exchange BSE --symbol 539289 --since 2026-10-01 --read
python -m india_trader insider-tracker index --symbol RELIANCE --year 2025 --read
```

This reads [NSE's public insider-trading filings](https://www.nseindia.com/companies-listing/corporate-filings-insider-trading) under SEBI PIT Regulation 7(2)/7(3). It combines the current NSE XBRL feed and the older NSE archive, retains original filing links, parses every named transaction context, and shows the person's category, buy/sell/pledge type, instrument, quantity, reported value and unit, trade date range, intimation and disclosure lag. `new` records once-only filing IDs in `trader/state/insider-tracker.json`; `--dry-run` avoids changing state. Use `--member`, `--since`, `--year`, `--type`, `--portfolio`, `--tickers`, `--as-of`, `--offline DIR`, `--current-only`, `--xml-dir`, `--json` and `--limit` as needed. `parse PATH.xml` reads a saved official XBRL file and accepts `--filed` if the filing date is absent from the XML. The named-person search can be slow across the full NSE index because it must read each XBRL file. [PARITY_AUDIT_11.md](PARITY_AUDIT_11.md) maps the original Congress PTR controls to this Indian source.

For NSE symbol searches before May 2026, `--year` or `--since` also queries the older archive month by month using NSE's public date filters; an offline dated response is named `archive-SYMBOL-YYYY-MM-DD-YYYY-MM-DD.json`. BSE searches require a six-digit scrip code and use BSE's public PIT historical XBRL page and linked inline filings, with at most a 366-day window per command. The BSE result is a company-specific search, not a complete marketwide BSE feed. The old NSE archive's XBRL can label a reported value with a `shares` unit; the tracker preserves that unit rather than presenting the field as verified INR. The lag is calendar days, not a legal late-filing verdict. Public disclosures describe past events and cannot justify a present trade without separate market-data, sizing and risk checks. No order is placed.

## Implemented vs NOT implemented

| Feature | Current development state |
| --- | --- |
| NSE/BSE Yahoo-format symbol mapping | Implemented; provider availability unverified |
| Offline 5-minute CSV / JSON validation | Implemented and tested |
| Completed 15-minute ORB + fail-closed gaps/staleness | Implemented and tested |
| Hypothetical INR risk/position/target planner | Implemented and tested |
| Historical session replay and descriptive win rate | Implemented and tested on synthetic data |
| Original scanner workflow: NSE EQ universe, daily setups, two-stage marketwide ORB, watchlist handoff | Implemented for NSE EQ with actual full-list and offline tests; Yahoo five-minute data remains unofficial and can yield partial coverage |
| Older watchlist scanner (up to 10) and offline CSV folder scanner | Implemented and tested offline |
| Codex Agent Skill / ChatGPT workflow documentation | Included |
| NSE EQ / BSE security universes | NSE EQ list and filtered BSE cash-equity master implemented; missing Yahoo five-minute charts can be retried through configured Upstox/FYERS; broad BSE candle coverage is not live validated |
| Independent five-minute candle sources | Upstox, FYERS (NSE cash only) and unofficial Yahoo adapters implemented across fetch, entry timer, Time Machine and ORB confirmation; account-based adapters need live validation |
| Original entry timer: check, bounded watch, scanner watchlist, one-/five-minute ORB/level, once-only paper handoff | Implemented and offline tested; live polling during market hours remains unverified |
| Original exit planner: plan ahead/confirmed entry, five stops, R/price targets, ATR, show/save, tick and flat-by checks | Implemented and offline tested; tick is an explicit assumption and Yahoo ATR data is unofficial |
| Original Time Machine: seeded 10,000-path scenarios, 60-day full-session pool, historical ORB rule replay, verdict, saved fan chart/histogram | Implemented and offline tested for NSE cash-equity paper research, including manual sell-side replay; Yahoo bars are unofficial and outcomes are not calibrated probabilities |
| Original budget manager: four-limit paper sizing, fresh quote, limit, blocked/minimum/reward gates, weighted allocation | Implemented and offline tested in INR; weighted cash shares are whole-share or indicative only, with no-stop allocations blocked by risk review |
| Original risk manager: batch entry checks, fresh-price recomputation, daily limits, exits, exact-order verdicts | Implemented and offline tested for paper NSE/BSE cash research; current-year public calendar is cached but broker exchange rules remain unverified |
| Original broker: exact ticket, per-order yes/no, fill log, local holdings, flatten ticket | Adapted as a paper-only simulation and offline tested; no real broker or exchange fill |
| Original alerts: market clock, plan stop/target/flat-by, custom price/percent/time/bell rules, once-only state, dated quote file | Adapted to IST/NSE paper checks, Upstox/Yahoo quote fallback and foreground polling; offline tested, live delivery unverified |
| Original earnings analyst: filed quarterly facts, YoY, margins, TTM, Q4 derivation, links, metrics export | Adapted to NSE XBRL and announcement links, with bank-specific fields and missing/derived values labeled |
| Original Congress-trades tracker: filing index, once-only new alert, detailed transaction parse, owner/date/amount, holdings flag | Adapted to public NSE and BSE issuer insider PIT filings, with XBRL transaction rows and paper-portfolio flag; Indian issuer insiders replace US Congress members |
| Real-time exchange-approved live market feed | **Not implemented** |
| Original news monitor: NSE RSS filings, India headlines, price moves, deduplication, state, offline replay | Implemented and offline tested; public RSS horizon is short and filing links can be missing |
| Complete financial filing archive, company guidance NLP and automatic transcript retrieval | **Not implemented**; earnings analyst reads available NSE XBRL and links candidate release PDFs |
| Marketwide sentiment and guaranteed notification delivery | **Not implemented**; a scheduled Windows paper monitor writes local events, without guaranteed notifications |
| Indian exchange holiday + Muhurat + special-session calendar | Current-year public NSE/BSE schedule cache and special-session refusal implemented; exchange notices and BSE ORB timing are not independently verified |
| Broker API, order placement, broker fees, tax engine | **Not implemented (safety boundary)** |
| Original 11 numbered workflows | All have India paper adaptations; source, live-broker and legal-timing differences are documented in the parity audits |
| Graphical UI | **Not implemented** |

**Why not claim exact behavior?** US SEC/EDGAR, House Congress disclosures, Alpaca MCP and US-specific trading rules differ from Indian public filings and paper simulation. The eleven workflow controls are adapted, while live order execution, a graphical UI, live validation of Upstox/FYERS, guaranteed notification delivery, BSE financial-result parsing, and complete filing coverage remain open gaps.

## QA and provenance

See `PARITY_AUDIT_1_TO_9.md`, `PARITY_AUDIT_10.md`, `PARITY_AUDIT_11.md`, `QA_REPORT.md`, `CHANGELOG.md`, and `TESTER_PROMPT.txt`. The prior v0.2 release passed its original 61 tests but contained independently reproduced validation and replay bugs; the v0.2.1 re-audit passed 79. The complete offline suite now passes **219 tests** on Windows Python 3.11.2. NSE XBRL earnings reads were checked for RELIANCE and HDFCBANK; current and archived NSE public insider filings, one BSE inline filing, and a dated NSE 2025 archive query were also checked. Earlier passes checked NSE daily Bhavcopy, NSE/BSE security masters, Yahoo scanner, Yahoo paper-cycle, and public NSE/India news feeds. The public calendar returned a special 8 November 2026 session. Upstox account candles, FYERS, CM Market Live during market hours, actual order execution and profitability have **not** been validated. The supplied tests are Python standard-library `unittest`. `python -m compileall -q india_trader` checks syntax. Any mention of a `TRIGGERED_LONG` status means a **completed-bar historical condition** only, not a recommendation or market-order fill.

Sources: [NSE tick-size circular](https://nsearchives.nseindia.com/content/circulars/CMTR67133.pdf), [2026 closing-auction timing reference](https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/what-are-the-market-timings), [NSE market timings](https://www.nseindia.com/static/market-data/market-timings), [OpenAI skills documentation](https://learn.chatgpt.com/docs/build-skills), [upstream project](https://github.com/Jakeschincariol/day-trader-skill).
