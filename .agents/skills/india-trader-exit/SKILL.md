---
name: india-trader-exit
description: Plan and show a paper NSE cash-equity stop, target, risk per share, and IST flat-by time from an entry trigger or a hypothetical entry. Use for exit planning, stop loss, target, risk/reward, or after the entry timer triggers.
---

# India Trader Exit Planner

Run `python -m india_trader exit-planner plan --tick TICK` after the entry timer saves `trader/entry.json`. The default stop is the opening-range midpoint and the target is 2R. To save the plan for downstream paper workflows, add `--write` (default `trader/plan.json`). `python -m india_trader exit-planner show` displays that file. `--from PATH` reads a different entry file; `--json` prints the plan as data. `--symbol RELIANCE --entry 1200 --stop pct:1 --tick 0.05` makes a plan ahead, explicitly marked unconfirmed. The paper risk gate blocks any plan without a completed trigger.

Stop choices are `or-mid`, `or-low`, `price:X`, `pct:X`, and `atr:K`. `--target 1R|1.5R|2R|price:X` sets the target. `atr:K` needs completed session bars up to the entry close. Use `--offline FILE_OR_DIR` with a saved Yahoo chart for reproducible replay; otherwise the planner fetches an unofficial Yahoo chart for ATR only. It supports both 1-minute and 5-minute entry timers. It rejects missing or gapped ATR histories and requires at least three completed bars; when fewer than 14 exist, it reports the sample size. The stop rounds down to the supplied tick, away from a long entry. The target rounds half up to that tick. The tick is required because NSE reviews cash-equity tick sizes by instrument and period; the program must not guess one.

`trader/config.json` may contain `flat_by` and `min_reward_risk`. The default flat-by is 15:10 IST, before the conservative 15:15 end of this tool's continuous-bar research window. A configured flat-by must remain within that limit. A reward:risk below the configured minimum is a warning; no automatic stop or target adjustment occurs. An entry at or after flat-by is refused. NSE's verified 2026 normal-session holiday list is used; special sessions, future calendars, BSE timing and CAS mechanics remain unverified. `--now ISO_TIME` is for a reproducible replay and never establishes that a signal is current.

The planner writes `mode: PAPER_PLAN_ONLY`, `entry_confirmed`, `trigger_at`, INR risk per share, source, volume quality, warnings and `orders_possible: false`. It is compatible with the paper budget step. A stop is a planned reference, not a guaranteed fill; gaps, spread, slippage, exchange charges and taxes can change realized loss. The planner never sends orders, sizes a position, or widens a stop to make a plan pass. Open the entry source and check the instrument's real tick before interpreting a plan.
