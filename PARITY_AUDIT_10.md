# Workflow 10: India Earnings Analyst parity audit

Compared with the original `trader-analyst` skill and `facts.py` in [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) on 1 November 2026. This is a paper-research adaptation of a US SEC workflow, using public NSE filings rather than US company facts.

| Original analyst behavior | India implementation |
| --- | --- |
| Ticker lookup or direct CIK | NSE EQ ticker lookup or `--isin` using the official NSE equity universe; `.NS` cash symbols only |
| SEC 10-Q/10-K XBRL facts | NSE Integrated Filing Financials plus older quarterly and annual indexes, with linked NSE XBRL instances |
| Latest amended fact wins | The latest available filing for a given period/basis wins; revised publication time respects `--as-of` |
| Quarterly revenue, net, diluted EPS, gross/operating/net margins | INR filed fields; gross margin stays blank when not filed. Non-bank operating income is a labeled derived pre-exceptional EBIT proxy when no filed fact exists. For banks, revenue means filed total income and operating income means filed pre-provision profit. |
| Same-quarter prior-year growth, quarter sequence, four-quarter totals | Same-quarter year-earlier matching within ten days; TTM only from four consecutive complete quarters |
| Missing fourth quarter derived from annual minus three quarters | Implemented when all three component quarters are available; derived EPS is marked approximate |
| Newest 10-Q and earnings 8-K/EX-99 link | Latest NSE XBRL and iXBRL/PDF links, plus same-symbol, same-cycle official NSE press-release, financial-results and investor-presentation PDF candidates |
| Stale facts warning and one-off note | Warns when the newest indexed filing was not parsed into a quarter; flags net income above operating-income proxy and missing/derived fields |
| Watchlist, `--quarters`, `--json`, `--metrics`, offline replay, `--no-filings` | Implemented; metrics merge into a dated JSON file. `--as-of`, `--basis` and saved-universe options add India replay controls. |
| Call transcript and next report date | The Codex skill can summarize a supplied transcript and requires a separately sourced next report date. The Python tool does not fetch either, as in the original. |

The tool never treats a press release as the source for filed numbers. It links PDF **candidates** for a human to open; it does not claim to have read their narrative. The Indian taxonomy does not always file a gross-profit concept or one universal operating-income concept. BSE-only companies without an NSE EQ ticker, special filing shapes, and future changes to NSE's public endpoints remain unverified. An ISIN lookup uses the current NSE EQ universe, so historic ticker identity is not independently reconstructed by `--as-of`. No order or investment recommendation is produced.
