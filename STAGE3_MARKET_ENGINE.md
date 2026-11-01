# Stage 3 — Continuous NSE market engine

Working copy: `C:\Users\prave\Documents\Codex\Day-Trader-INDIA-Stage2`
Branch: `codex/stage2-provider-integration` (the same branch as Stage 2)
Original `Day-Trader-INDIA` checkout: no Stage 3 edits
Stage checkpoint: uncommitted; included in the later Stage 7 branch commit.

## What runs

`python -m india_trader market-engine run` is a foreground process. While it is running, it waits through weekends, published NSE holidays, special sessions and off-hours, and starts scanning during eligible normal cash-equity sessions. `once` performs one bounded cycle; `status` reads saved state without network access. An OS startup service and hosted deployment belong to Stage 7; the process will not start itself after a computer restart at this stage.

The broad scanner rotates through the supplied NSE EQ universe in groups of **10** on each completed five-minute slot after 09:35 IST. It reuses the scanner's complete-bar, opening-range, price and volume rules and Stage 2 source order. A Yahoo chart may create a **research candidate**, but cannot make it live verified. The detailed scanner checks up to **2** candidates per minute from a pool of at most **8**, so monitoring continues while other candidates or paper positions exist. Candidates expire after 45 minutes. A first breakout older than five minutes is not emitted as a new event.

Detailed review requires current, validated broker-sourced **one-minute and five-minute candles**, a fresh broker quote, verified normal market status, positive volume, enough rupee turnover, an in-range intraday price range and a verified bid/ask spread. It calculates VWAP, intraday range, and, when enough completed bars exist, MA20, RSI14 and ATR14. A one-minute close must still be above the five-minute opening range. Quote/bar disagreement over 2%, missing depth, stale data, data gaps, wide spreads, broker outages, and unverified exchange state block a new signal. Signals are once-only `AWAITING_AI_REVIEW` records for Stage 4; they are neither AI selections nor executable paper orders.

The JSON state at `trader/state/market-engine.json` stores the rotating cursors, candidates, explicit source and quality statuses, latest broad-scan counts and signal IDs across normal restarts. Each cycle reports universe size, attempted and usable broad charts, live-verified detailed symbols, symbols outside the current broad batch, and symbols **not proven** to have reliable live data. The last category includes both unattempted and failed symbols; it does not claim real-time coverage of the full NSE universe. The latest completed broad-scan count remains visible between five-minute slots.

The defaults are intentionally bounded for FYERS Standard. Its published Data API limits are [5 requests/second, 50/minute and 5,000/day](https://support.fyers.in/portal/en/kb/articles/what-are-the-fyers-api-rate-limits-for-standard-and-prime-plans). The controller spaces FYERS requests and rejects configured batch sizes whose conservative full-session estimate exceeds 4,500 data requests. This estimate leaves room for other use but cannot know requests made by another process or account. It uses the [documented FYERS bid/ask quote fields](https://github.com/FyersDev/fyers-skills/blob/master/skills/fyers-trading/references/market-data.md) and [Upstox V3 top-of-book depth](https://upstox.com/developer/api-documentation/get-full-market-quote-v3/) when available. Missing depth fails closed.

## Commands

```text
python -m india_trader scanner universe
python -m india_trader market-engine once --symbols RELIANCE,TCS
python -m india_trader market-engine run --universe trader/universe.txt
python -m india_trader market-engine status
```

`run` stays in the foreground and checks the clock every 30 seconds. It does not submit a simulated fill or broker order. It requires locally supplied read-only Upstox or FYERS data credentials for live-verified candidates. Tokens are not saved in the engine state or output. Without a broker account and fresh bid/ask depth, the controller can still count research coverage, but produces no entry-ready handoff.

## Validation and limits

On Windows/Python 3.11.2, the full offline suite passed **248 tests**. This includes Stage 3 checks for session gating, bounded rotation, multi-symbol coverage, stale candidate expiry, spread and feed failure, once-only signals after restart, and FYERS pace limits. Syntax compilation and `git diff --check` passed. A Sunday `market-engine once --symbols RELIANCE,TCS` smoke check returned `MARKET_CLOSED`, `weekend`, zero fetched symbols and `orders_possible: false`, without fetching data. Test temporary files were placed inside this writable working copy because the system temp folder denied rename operations.

No actual Upstox or FYERS token, live socket, or open-market session was available. Provider payloads, live latency, achievable coverage and real account rate behavior remain unvalidated. The Stage 2 WebSocket adapter is available, but this Stage 3 controller uses completed REST candles and quotes; it does not claim a live socket connection. Stage 4 AI review and Stage 5 simulated entry/exit are not part of this Stage 3 output.
