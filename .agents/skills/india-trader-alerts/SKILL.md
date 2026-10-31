---
name: india-trader-alerts
description: Check the NSE normal-session IST clock, create stop/target/flat-by alerts from a paper exit plan, add price/percent/time/opening-bell rules, read dated unofficial INR quotes, and report each alert once without placing orders.
---

# India Trader Alerts

Use after `$india-trader-exit` saves a paper plan, or when the user asks for a price/time alert. Alerts report facts from dated quotes or the verified 2026 normal-session NSE calendar. They do not close a position, place a stop or target order, or send a desktop notification by themselves.

```powershell
python -m india_trader alerts clock
python -m india_trader alerts add --from-plan
python -m india_trader alerts add --ticker RELIANCE --above 1200
python -m india_trader alerts add --ticker TCS --pct 3 --direction down
python -m india_trader alerts add --opening-bell
python -m india_trader alerts add --at 15:10 --label "Flat by" --before-close
python -m india_trader alerts list
python -m india_trader alerts check
python -m india_trader alerts reset reliance-ns-plan-stop
python -m india_trader alerts quote --holdings --write
```

`add --from-plan` creates three date-bound alerts: stop, target and the plan's `flat_by_ist` time. Custom `--below`, `--stop`, `--target`, `--pct`, `--at`, and `--opening-bell` rules are supported. `--date YYYY-MM-DD` limits a custom rule to one session. `--rearm` lets a price rule ring again after the price returns across its level; percent, time and opening-bell rules ring at most once per day. `reset ID` clears a recorded ring. A time alert still only reports a deadline; the paper exit path is `$india-trader-broker` flatten, `$india-trader-risk`, approval and a separately priced simulated fill.

`check` fetches an unofficial Yahoo `.NS`/`.BO` INR quote for each active price rule using query2 with query1 fallback. Each quote must carry its own exchange-market timestamp no older than 15 minutes; missing, future and stale quotes cannot trigger a price rule. `quote --holdings --write` reads portfolio and plan tickers and stores dated values in `trader/prices.json` for budget/risk review. `--offline DIR` reads `yahoo-SYMBOL.json` files instead of the network. `check --saved` uses `trader/prices.json` only; `--no-write` leaves ring state and price files unchanged. `--now ISO_TIME` pins the IST clock for an explicitly labeled replay. Exit code 0 means an alert rang; exit code 1 means none rang or the command failed, so inspect JSON/stderr. Run `check` repeatedly while monitoring; nothing monitors while it is not running.

The 2026 NSE normal-session calendar is built in. `--before-close` validates that a normal-session reminder is no later than 15:10 IST; it cannot adjust to an unverified special or early close. Unknown years, holidays and weekends fail closed for checking; special sessions and BSE hours are not verified. Free Yahoo quotes can be delayed or unavailable; a level can be crossed between checks. Never call the quote live or exchange verified. Do not promise alert delivery, an automatic flat-by exit, or a profit outcome.
