---
name: india-trader-scanner
description: Scan NSE EQ cash equities for daily chart setups or completed five-minute opening-range breakouts; build a paper-research watchlist. Use when asked to scan the Indian market, find breakouts or bull flags, or prepare a watchlist.
---

# India Trader Scanner

This is the India adaptation of the upstream `trader-scanner` workflow. Its three commands are `universe`, `daily`, and `orb`. Run them from the repository root with `python -m india_trader scanner COMMAND`. The scanner reads charts and writes `trader/watchlist.json`; it does not select a trade, size an order, or place one.

- `universe` downloads the official NSE EQ security list into `trader/universe.txt`. Check the `retrieved_at` time. `--exchange BSE` reads BSE's public security-master ZIP and filters active one-share-lot cash equities; `--exchange BOTH` combines the lists. `--offline EQUITY_L.csv` and `--bse-zip SCRIP.ZIP` use saved exchange files. NSE is the default. BSE Yahoo chart coverage is poor in the tested symbols, so never imply that a BSE security list means a usable marketwide BSE price scan.
- `daily` reads three months of unofficial Yahoo daily closes in batches of 20. It checks 20-day closing breakouts, bull flags, 20-day average reclaim, RSI under 30, and oversold bounce. It excludes a forming daily candle. Run before the market opens or after the daily close. Use `--symbols RELIANCE,TCS`, `--watchlist FILE`, or `--limit N` to narrow the scan. The separate `daily-scan` command uses official NSE final Bhavcopy reports instead.
- `orb` scans completed five-minute bars from 09:15 IST. Stage one checks batched closes; stage two confirms candidates against full Yahoo OHLCV charts, most traded first. `--at 10:15` replays a completed cutoff; `--max-confirm 0` requests confirmation of every candidate. The default 200-confirmation cap can produce `PARTIAL`; always report `unconfirmed` and the fetched, usable, and scanned counts. `--offline PATH` supports saved Yahoo chart/spark JSON or `.json.gz` fixtures.

Default outputs include the source, exchange coverage, as-of time in IST, exact counts, and hits. `trader/watchlist.json` uses `hits[].symbol` for downstream workflows. `--no-write` inspects without replacing it; `--json` returns machine-readable results. A no-hit exit code is 1, so inspect the output rather than interpreting every nonzero code as a software crash.

Report a chart only as a **rule match**. Yahoo is unofficial, can be delayed or incomplete, and two Yahoo hosts are one provider. The scanner excludes missing five-minute slots and zero/missing volume from confirmed ORB hits. A 2026 NSE holiday list is built in; future calendars, Muhurat timing, BSE-specific calendar differences, BSE candle coverage, and exchange reconciliation are not verified. Do not describe a historical replay as a live signal or promise returns.
