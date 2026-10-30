---
name: india-trader-news
description: Monitor public NSE corporate disclosures, India stock headlines, and unofficial price moves for a paper trading watchlist. Use for Indian market news, filings, company events, or news checks.
---

# India Trader News Monitor

Run from the repository root: `python -m india_trader news --tickers RELIANCE TCS` or omit `--tickers` to read scanner `trader/watchlist.json`, `trader/plan.json`, and `trader/portfolio.json`. The scanner writes `hits[].symbol`. `trader/universe.txt` supplies NSE company names for matching. No candle file is required.

The monitor checks the public NSE RSS feeds for announcements, financial results, board meetings, corporate actions, and insider trading. It then reads Yahoo Finance India ticker headlines, falling back to Google News India RSS when Yahoo has no recent company-matched headlines. It reads Yahoo chart metadata for price versus the previous close and marks moves at 3% by default. Yahoo and Google are third-party research sources; a stock move can be delayed or stale. The monitor never places an order or treats headlines as instructions.

`--hours 48` sets the lookback. `--all` shows items already seen. The default `trader/state/news.json` remembers item fingerprints for 30 days; `--dry-run` leaves state untouched. `--now ISO_TIME` and `--offline DIR` support reproducible replay. Offline files are `nse-announcements.xml`, `nse-financial-results.xml`, `nse-board-meetings.xml`, `nse-corporate-actions.xml`, `nse-insider-trading.xml`, `yahoo-TICKER.NS.xml`, `google-TICKER.NS.xml`, and `chart-TICKER.NS.json`. `--feeds announcements,corporate_actions` selects exchange feed categories; `--no-nse`, `--no-headlines`, and `--no-moves` disable parts of the check. `--move-pct N` changes the price move marker. `--json` gives machine-readable items, sources, notes, and a `partial` flag. Exit 0 means at least one new item; exit 1 means none; exit 2 means invalid input.

Always report each source's status and the NSE feed's oldest entry. The public announcements RSS can be shorter than the requested window and may have no individual filing link. A `company_search_not_individual_filing` URL opens a company search page, not the source document. Open the actual NSE filing or full news article before asserting details. Empty or partial feeds do not prove there was no event. Do not describe the monitor as an exchange-complete filing archive, a verified live alert, investment advice, or proof of profitable trading.
