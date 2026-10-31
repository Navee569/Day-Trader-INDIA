---
name: india-trader-broker
description: Show exact NSE/BSE paper tickets, record one user's yes or no, simulate an explicitly priced fill, inspect the local INR portfolio and log, and prepare a risk-reviewed position-reducing flatten ticket.
---

# India Trader Broker (paper only)

This workflow stores **local simulations** in `trader/portfolio.json` and `trader/state/broker.json`. It has no NSE broker connection, no order API, no credentials, and no exchange fills. Do not call a simulated fill an executed trade. Never type `yes` on a user's behalf. One explicit reply applies to one exact ticket.

Set a hypothetical `budget_inr` and `paper: true` in `trader/config.json` (see `examples/BUDGET_CONFIG_INDIA.json`). After `$india-trader-budget` writes `trader/orders.json`, run `$india-trader-risk` so `trader/state/risk.json` contains a fresh exact-order PASS. Then:

```powershell
python -m india_trader broker show
python -m india_trader broker approve --index 1 --reply yes
python -m india_trader broker fill --index 1 --price 1200.00 --confirm PAPER
python -m india_trader broker portfolio
python -m india_trader broker log
```

The supplied price is a **hypothetical INR fill price**, never inferred from an entry reference or a stale quote. The fill command checks the exact fingerprint, same-day risk PASS no older than five minutes, same-day per-ticket approval, duplicate fill, whole-share quantity, cash buffer, position cap, size/risk limits, and stop/target range. It updates local cash, average cost, holdings, realized gross P&L and daily risk counters. The portfolio equity is marked at cost, not a current market valuation; no fees or taxes are deducted. Stop and target are recorded planning levels, not working exchange orders. The computer must be on and someone must explicitly run the exit step; `flat_by_ist` is not an automated close.

To prepare a flatten ticket from a recorded holding:

```powershell
python -m india_trader broker flatten --ticker RELIANCE
python -m india_trader risk-manager --orders trader/flatten_order.json --out trader/state/flatten_risk.json
python -m india_trader broker --orders trader/flatten_order.json --risk trader/state/flatten_risk.json show
python -m india_trader broker --orders trader/flatten_order.json --risk trader/state/flatten_risk.json approve --index 1 --reply yes
python -m india_trader broker --orders trader/flatten_order.json --risk trader/state/flatten_risk.json fill --index 1 --price 1190.00 --confirm PAPER
```

The risk checker lets a verified holding-reducing exit through after the entry window and daily entry limits. It blocks an exit larger than the local holding. A flatten ticket does not itself close anything. `--now ISO_TIME` is only for an explicitly labeled historical/offline replay; use actual current IST otherwise. Keep `trader/` local and uncommitted.
