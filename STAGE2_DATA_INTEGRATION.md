# Stage 2 — Read-only market-data integration

Working copy: `C:\Users\prave\Documents\Codex\Day-Trader-INDIA-Stage2`
Branch: `codex/stage2-provider-integration`
Baseline: `8b3980e390a0e6335614dfa67769ed9f4a7e9f18`

This is an independent copy of the India repository. The existing `Day-Trader-INDIA` checkout and the upstream original repository were not edited. Nothing has been committed or pushed.

## Implemented

| Data type | Priority and behavior |
|---|---|
| One- and five-minute completed candles | Upstox V3 → FYERS History → NSE marked unavailable for intraday candles → Yahoo only where supported and labeled unofficial. One-minute Yahoo chart research remains available through `entry-timer`; it cannot pass the live entry gate. |
| Completed daily candles | Upstox V3 → FYERS History → official NSE final CM Bhavcopy for NSE EQ → Yahoo batched research fallback. The scanner uses these sources in that order. |
| Dated quotes | Upstox V3 → FYERS Quotes → delayed NSE CM Market Live research payload → Yahoo completed-candle close for research. NSE/Yahoo cannot pass the fresh broker quote gate. |
| Exchange state | Upstox Exchange Status → FYERS Market Status → public NSE calendar as a non-live research fallback. An unverified or closed state blocks entry readiness. |
| Live quote stream | Optional official Upstox `MarketDataStreamerV3` → FYERS `FyersDataSocket` for watched symbols. Stream errors, stale ticks and disconnections trigger fallback; no NSE/Yahoo source is promoted into a live stream. The stream is a library component for the later controller, not a running service yet. |

The shared manager is `india_trader/market_data_manager.py`. It validates canonical exchange symbols, OHLCV, timestamp alignment, completed bars, positive volume, candle continuity, live freshness and normal session state. It rejects stale, malformed or inconsistent replacement feeds. A source switch compares the newest same-session candle timestamp and, for the same bar, rejects a close difference above 2%. Provider failures and switches are written without tokens or raw exception text to `trader/state/data-provider-events.jsonl`. The last selected source and last candle are restored from a bounded tail of that file after a process restart. Rate-limit and authentication failures place the affected capability on a cooldown.

The manager deliberately has separate health state for candles, daily data, quotes and exchange status. A successful status request therefore does not conceal a candle-feed failure. `python -m india_trader data-status` shows configuration presence and local health without contacting a provider or printing tokens.

The entry timer asks for a fresh broker candle and verified open exchange state during a live check. It can still show a clearly labeled Yahoo research chart, but returns `DATA_UNVERIFIED` instead of treating it as an entry-ready signal. Existing offline replay behavior remains available. The marketwide ORB scanner now tries configured broker candles before Yahoo; a failed Yahoo batch gets a bounded per-symbol chart retry. Online paper alerts now use the same quote manager, including FYERS between Upstox and the research fallbacks.

## Official interface references

- [Upstox V3 intraday candles](https://upstox.com/developer/api-documentation/v3/get-intra-day-candle-data/) and [historical candles](https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/)
- [Upstox V3 market-data WebSocket](https://upstox.com/developer/api-documentation/v3/get-market-data-feed/), [exchange status](https://upstox.com/developer/api-documentation/get-market-status/) and [official Python SDK](https://github.com/upstox/upstox-python)
- [FYERS historical, quote and market-status data](https://github.com/FyersDev/fyers-skills/blob/master/skills/fyers-trading/references/market-data.md) and [FYERS data WebSocket](https://github.com/FyersDev/fyers-skills/blob/master/skills/fyers-trading/references/websocket.md)
- [FYERS Standard/Prime API limits](https://support.fyers.in/portal/en/kb/articles/what-are-the-fyers-api-rate-limits-for-standard-and-prime-plans) and [Upstox rate limits](https://upstox.com/developer/api-documentation/rate-limiting/)

## Validation boundary

On Windows/Python 3.11.2, `python -m unittest discover -s tests -v` passed **238 tests** in 3.997 seconds with `TEMP` and `TMP` set to a writable folder inside this working copy. The first run hit Windows permission errors in the system temporary folder; those disappeared with the local temporary folder. `python -m compileall -q india_trader` and `git diff --check` passed. `python -m india_trader data-status` reported both broker sources unconfigured, live connectivity `NOT_CHECKED`, and `orders_possible: false`. The synthetic offline ORB and backtest CLI smoke checks passed. These tests mock provider responses and socket callbacks; they exercise precedence, history adapters, symbol identity, stale/missing candles, volume, cooldowns, closed status, switch reconciliation, audit recovery, Yahoo entry blocking and socket fallback. The full test log is at `C:\Users\prave\Documents\Codex\2026-10-29\referenced-chatgpt-conversation-this-is-an\work\stage1_tmp\stage2_final_unittest.log`.

No Upstox or FYERS token, optional WebSocket SDK, or live session was available for this run. REST and WebSocket behavior against actual accounts, current rate limits, provider payload variants and live latency remain **unvalidated**. FYERS History does not echo an independent ticker in the documented candle response, so live account validation of the constructed NSE EQ symbol is still required. The tests do not prove that the feeds are profitable or that a live paper entry can occur. Provider charges, subscription limits and account eligibility depend on the user's plan and should be checked at setup time.

The WebSocket SDKs are optional and listed in `requirements-market-data.txt`. No token is written to source or test files. The future hosted service must supply them through a private server-side secret mechanism. No order endpoint is called or configured.

## Stage 3 handoff

The continuous market controller can call `MarketDataManager.fetch_candles(..., require_entry_ready=True)` and `fetch_market_status(..., require_open=True)`, consume `ReadOnlyStreamSupervisor` validated quotes, and use the existing scanner and entry calculations. It must stop new paper entries on `FeedUnavailable`, honor provider rate limits and retain deterministic exits when data is unavailable. The stream supervisor currently has no process lifecycle, database or UI; those belong to later requested stages.
