# Stage 5 — Autonomous paper-trading lifecycle

Working copy: `C:\Users\prave\Documents\Codex\Day-Trader-INDIA-Stage2`
Branch: `codex/stage2-provider-integration`
Original `Day-Trader-INDIA` checkout: unchanged
Stage checkpoint: uncommitted; included in the later Stage 7 branch commit.

## What Stage 5 does

`python -m india_trader paper-lifecycle run` polls in the foreground. It reads the Stage 4 committee and Stage 3 engine state files, takes only current AI4 `SELECT` decisions, revalidates the original Stage 3 broker signal and its expiry, and waits for the **next completed one-minute bar** to reconfirm the opening-range breakout. It also requires a current completed five-minute broker bar, positive volume, a fresh broker quote and bid/ask spread, and a normal NSE session. The Yahoo and daily-report fallbacks never qualify for simulated entry. Every candidate passes the existing independent `risk_manager.review` before the local paper broker can receive a proposal. It automatically processes ranked candidates, including multiple stocks; no user stock selection or ticket approval is needed for this separate paper simulator.

The entry plan uses the first 15-minute opening range midpoint as a hypothetical stop, a configurable reward/risk multiple as target, and explicit configured limits for cash, position size, portfolio exposure, daily risk, daily realized loss, open positions, spread and price movement. It reserves paper cash for pending buys. The local paper broker records order submission, partial or complete simulated fills, cancellations and confirmed holdings in one atomically replaced JSON state file. A restart cannot submit the same signal twice; an operating-system file lock prevents two simultaneous controllers from editing the ledger. A fill uses a fresh broker quote with configured adverse slippage and is capped to a fraction of the latest completed one-minute bar's volume. Repeated polls cannot reuse the same bar's volume capacity.

Open paper positions are checked **before** any new entries, without invoking AI. Fresh one-minute broker bars and quotes drive stops, targets, a ratcheting trailing stop and a configurable 15:10 IST flat-by. Pending partial exits continue over later bars. The simulator records the exact exit reason, each fill's modeled costs, net realized P&L and a fresh-quote unrealized estimate. If a quote or bar is unavailable, it records `exit_data_unavailable` and leaves the position visibly open; no fictitious fill is created. It cannot guarantee an exit during outages, illiquidity, a market closure or after the continuous session ends.

The default example uses **hypothetical ₹100,000 capital**, a ₹500 maximum planned loss per entry, ₹1,500 daily loss cap, 10 bps adverse slippage each side and 10% one-minute volume participation. These are paper assumptions, not verified broker terms or a forecast. Prices are rounded to paise; current instrument tick size is not verified. The Stage 5 accounting supplement adds a configurable line-item charge schedule and reconciled portfolio reporting; see `PAPER_ENGINE_ACCOUNTING.md`.

## Commands

```text
python -m india_trader market-engine run
python -m india_trader ai-committee run
python -m india_trader paper-lifecycle run
python -m india_trader paper-lifecycle once
python -m india_trader paper-lifecycle status
```

The three foreground processes must be run separately at this stage, with a current, locally configured read-only Upstox or FYERS data account for broker-grade candles, quotes and market status. Later deployment work will provide central orchestration. `once` is safe outside market hours and does not call an AI model. The lifecycle state defaults to `trader/state/paper-lifecycle.json`; configuration is `examples/paper_lifecycle_config.json`. Use `--state`, `--config`, `--engine-state` and `--committee-state` to use other paths. Keep state and credentials local. The existing manual `broker` CLI is untouched and stores a different paper ledger; do not mix the two portfolios.

## Validation and limits

On Windows/Python 3.11.2, all **275 offline tests passed**. Syntax compilation and `git diff --check` passed. A Sunday `paper-lifecycle once` smoke check returned zero fills, zero open positions, `orders_possible: false` and the unchanged hypothetical ₹100,000 cash balance. The offline tests use synthetic broker bars and quotes and exercise paper entry, multi-stock selection, risk rejection, duplicate prevention after restart, partial buy and sell fills, one-bar participation limits, stop, trailing stop, target, time cutoff, net P&L, unverified feeds, expired decisions, closed markets, and exit independence from AI. They cannot validate real Upstox/FYERS responses, open-market latency, actual liquidity, current NSE tick sizes, actual charges/taxes or profitable performance. This stage has no real broker connector or live-money order path.
