---
name: india-trader-risk
description: Check saved NSE/BSE cash-equity paper proposals against per-trade, daily, cash, position, price, time, and duplicate limits before a paper ticket is shown.
---

# India Trader Risk Manager

Run `python -m india_trader risk-manager` after `$india-trader-budget` saves `trader/orders.json`. It reads optional `trader/config.json`, `trader/portfolio.json`, `trader/prices.json`, and `trader/state/broker.json`, checks proposals in file order, and writes `trader/state/risk.json` with a fingerprint for each exact order. Use `--no-write` for a read-only view, `--json` for full details, or `--now ISO_TIME` for a historical IST replay. `paper-ticket --order trader/orders.json --risk trader/state/risk.json` can read a one-order PASS; approval still requires its own fresh same-day check. A changed order needs a new verdict.

If daily counters are absent, risk review reconstructs the same-day counts and planned loss from the paper fill log and uses the greater of logged and saved values. When a fresh dated quote exists, the position cap uses that quote to value existing paper holdings; otherwise it falls back to cost basis. `--today YYYY-MM-DD` must match the IST clock and is useful with a pinned `--now` replay.

For entries, check format and whole shares, long NSE/BSE cash side, stop and target, current plan trigger, normal-session calendar and IST entry window, fresh higher quote, maximum loss after configured slippage and fee buffer, reward:risk, today's cumulative planned loss, trade and order counts, duplicate ticker, blocked list, minimum/maximum order, position cap, and buffered paper cash. The four limits in `budget-manager` are recalculated here rather than trusted. Price files need offset-aware timestamps; an old price is ignored. Review each passed order as if it filled solely to catch later batch entries that exceed combined limits. The result does not record a fill or alter broker state.

A `SELL_LONG_CASH` paper exit may only reduce a verified paper holding. Its shape, fingerprint, ticker and quantity are checked. Entry limits such as the window, blocked list and daily loss do not prevent a position-reducing exit. A sell larger than the holding is blocked; short sales, derivatives, margin and crypto are unsupported. The current India app has no live broker connector.

Report PASS/BLOCK and each reason, the recomputed risk and reward:risk, as-of IST time, whether the input price was dated, and the remaining calendar and feed limitations. PASS is a rule check only. The 2026 normal-session holiday list excludes known closures; future and special sessions, BSE trading hours, instrument restrictions and actual broker cash are not verified. Never infer an order was placed, approved or filled.
