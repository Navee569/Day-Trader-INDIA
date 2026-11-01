# Stage 1 — Existing source audit

Audit date: 2026-11-01 (Asia/Kolkata)
Baseline: `8b3980e390a0e6335614dfa67769ed9f4a7e9f18` on `main`
Scope: executable local source and offline validation of the baseline; no live orders, credentials, deployment, or commit.

## Result

The baseline is a working **paper-only research CLI**, not yet the autonomous paper-trading application described in the master development prompt. Its scanner, five-minute ORB, sizing, risk checks, simulated-fill calculations, disclosures, and Monte Carlo replay provide substantial reusable code. The current foreground monitor does not connect scanning, AI selection, entry monitoring, risk review, simulated buying, exit monitoring, and simulated selling into a continuous lifecycle.

This conclusion is based on source inspection and executed offline checks, rather than README or prior QA claims. The working tree was clean at the start of the audit; this report is the only new file.

## Validation performed

| Check | Actual result |
|---|---|
| `git rev-parse HEAD` | Exact requested baseline commit above. |
| `python -m unittest discover -s tests -v` | **219 tests passed** in 4.377 seconds on Windows/Python 3.11.2. |
| `python -m compileall -q india_trader` | Passed. |
| `python -m india_trader doctor` | `status: OK`, `orders_possible: false`; market-data connectivity explicitly `NOT_CHECKED`. |
| Offline ORB on `examples/SYNTHETIC_TEST_BARS.csv` at 2026-10-28 09:40 IST | Exit 0; `TRIGGERED_LONG`, completed-bar reference 103.0, explicitly not an executable fill. |
| Offline backtest on the same synthetic file | Exit 0; 2 sessions, 1 simulated trade, 1 skipped; `insufficient_sample: true`, zero fees/slippage assumed. This is a CLI smoke check, not performance evidence. |

Tests use fixtures and mocks. They do not establish current Upstox/FYERS access, data freshness in a live session, server recovery, AI decisions, an autonomous trade lifecycle, or UI behavior.
The full test output is saved at `C:\Users\prave\Documents\Codex\2026-10-29\referenced-chatgpt-conversation-this-is-an\work\stage1_tmp\baseline_unittest.log`.

## Reusable source inventory

The package contains 27 Python modules and the suite contains 27 test modules. The following inventory describes observed code and CLI surfaces; it does not imply production readiness.

| Area | Existing source | Reuse decision |
|---|---|---|
| Market data and session | `universe.py`, `nse_sources.py`, `calendar_source.py`, `data.py`, `intraday_sources.py`, `core.py` | Reuse symbol parsing, NSE/BSE public-data readers, holiday parsing, bar validation, and read-only adapters. Put a provider manager and consistent freshness policy around them. |
| Screening and technical signals | `market_scanner.py`, `daily_scan.py`, `scan.py`, `entry_timer.py`, `trade_workflow.py` | Reuse daily/ORB ranking and completed-bar entry logic. Feed them from a persistent scanner and managed candidates. |
| Sizing, risk, exits | `budget_manager.py`, `risk_manager.py`, `exit_planner.py`, `paper_orders.py` | Reuse calculations and fail-closed paper verdicts; extend controls and connect them at every automatic entry/exit. |
| Paper accounting | `paper_broker.py`, `paper_cycle.py` | Reuse exact ticket/fill calculations and existing paper ledger semantics; add a transactional journal and an automatic paper execution interface. `paper_cycle` itself only returns hypothetical plans. |
| Context and notifications | `news_monitor.py`, `earnings_analyst.py`, `insider_tracker.py`, `bse_insiders.py`, `alerts.py`, `paper_monitor.py` | Reuse source parsers and alert checks. The monitor is a foreground research scheduler, not the final controller. |
| Replay and testing | `backtest.py`, `time_machine.py` | Reuse ORB historical replay and seeded Monte Carlo simulation; add explicit cost models and integrated lifecycle tests. |
| CLI | `__main__.py` | Preserve commands and output contracts while adding service/API entry points. |

## Gaps and defects to carry forward

### Priority 0 — Required before automatic paper entries

1. **No continuous trade controller.** `paper_monitor.py:17-54` schedules calendar, alerts, news, insiders and earnings only. `paper_monitor.py:98-149` records their informational results; it never scans or ranks the full universe, selects candidates, invokes an AI committee, checks entry signals, asks risk for a verdict, simulates a buy, watches stops/targets, or simulates a sell. `paper_cycle.py:10-39` is one-shot watchlist research and returns `HYPOTHETICAL_PLAN_ONLY`.
2. **Paper fills are manual.** `paper_broker.py:181-280` computes a local simulated fill, but the CLI flow at `paper_broker.py:290-376` requires an explicit ticket approval and supplied fill price. An automatic paper execution path and deterministic exit path are absent.
3. **No AI committee.** The executable package has no model-provider calls, analyst debate, separate final judge, decision schema, cost tracking, or fail-closed AI availability gate. Stage 4 must implement these while deterministic exits remain independent of AI.
4. **Paper ledger is not transactionally durable.** `_write` in `paper_broker.py:22-25` writes JSON directly. A fill writes the portfolio and state separately at `paper_broker.py:371-374`; interruption between writes can leave holdings and the order log inconsistent. An idempotent event journal and recovery logic are needed before unattended simulation.

### Priority 1 — Data and risk integration

5. **Scanner provider priority differs from the requested policy.** `intraday_sources.py:249-279` tries configured Upstox, then FYERS, then Yahoo for an individual chart. But broad `market_scanner.py:685-737` fetches Yahoo batches first and calls Upstox/FYERS only for missing five-minute charts. It must use one deliberate priority/health policy across paths. The `except ... continue` at `market_scanner.py:695-699` also skips the later per-symbol fallback when an entire Yahoo batch fails.
6. **Provider health and freshness are not centralized.** The adapters expose attempts/source labels but there is no persistent provider status, switch audit, WebSocket stream, cross-provider quote reconciliation, or uniform stale-data gate for every automatic entry. The `doctor` check reported market connectivity `NOT_CHECKED`. Official NSE daily data and CM Market Live code cannot be assumed to provide five-minute candles.
7. **Risk controls are incomplete for unattended operation.** `risk_manager.py:41-57` and `risk_manager.py:113-285` check paper mode, time, cash, size, position cap, reward/risk, daily planned loss, and trade/order counts. They do not implement configurable sector exposure, maximum concurrent positions, spread/liquidity rejection, or a persistent emergency stop-new-entries switch. Existing exit reduction should remain available when new entries are stopped.
8. **Simulation costs are incomplete.** The offline backtest reports `zero_fees_slippage_assumed: true`; `paper_broker.py:181-280` credits gross realized P&L. Existing risk calculations allow a slippage/fee buffer, but the ledger does not deduct a complete Indian charge/tax model. Return and P&L displays must distinguish gross from modeled net and declare assumptions.

### Priority 2 — Application and operations

9. **No application API, database, or mobile dashboard.** No backend HTTP/WebSocket service, database schema/migration, frontend application, or persistent trade/decision view was found in executable source or repository build files. JSON and JSONL files are used locally.
10. **No server deployment/recovery package.** No Dockerfile or Compose configuration is present. `paper_monitor.py:150+` is a foreground loop with local files, without service supervision or restart reconciliation. Windows task support does not meet the requested hosted service behavior.
11. **Calendar coverage needs live verification.** `calendar_source.py` has a read-only holiday cache and `market_scanner.py:112+` has session logic, but the audit did not prove accurate future special sessions or real-time exchange state. Automatic entries must fail closed when session status cannot be verified.

## Integration plan for the remaining requested stages

1. **Stage 2:** unify read-only data adapters behind one provider manager, enforce Upstox → FYERS → eligible NSE → Yahoo priority, record source switches and freshness, and test failures without credentials. Keep NSE usage limited to supported data types.
2. **Stage 3:** reuse the scanner/ORB and entry logic in a continuously running market-session service; persist candidate state and decisions.
3. **Stage 4:** add configurable 1–3 analyst models and a separate final judge with structured evidence, bounded debate, costs, expiry, and fail-closed entry decisions.
4. **Stage 5:** connect selection → entry → independent risk → atomic simulated buy → deterministic continuous exit → atomic simulated sell. Preserve the existing CLI and paper-only guardrails.
5. **Stages 6–7:** add a mobile-friendly read/write dashboard, API, database migrations, Docker deployment, secrets handling, health checks and recovery.
6. **Stage 8:** test the complete system offline, in historical replay, in restart/failure scenarios, and with live **paper-only** market data after user-supplied read-only credentials are available.

## Boundaries

- No credentials were read or configured, no network market checks were run, and no broker order API was used.
- No source code was changed in Stage 1. This report was later included in the Stage 7 branch commit. The existing passing tests establish only their covered offline behavior.
- The requested autonomous system, live provider behavior, mobile UI, and server operations are **not yet validated or delivered**.
