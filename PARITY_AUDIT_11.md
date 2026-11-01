# Workflow 11: India Insider Tracker parity audit

Compared with the original `trader-pelosi` skill and `ptr.py` in [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) on 1 November 2026. This is the eleventh and final **numbered workflow** in the upstream pack. India has no equivalent transaction-by-transaction MP feed, so the public-disclosure subject is company insiders under SEBI PIT Regulation 7(2).

| Original tracker behavior | India adaptation |
| --- | --- |
| House Clerk PTR year index, DocID, filing date/type, member, district | NSE current PIT XBRL index (`corporates-pit-gg`) and older archive (`corporates-pit`), filing ID, date, regulation, issuer symbol and source link. No Indian MP/district analog. |
| `index` with year/member/type/since/limit | `index` with `--year`, `--member`, `--type`, `--since`, `--limit`, `--symbol`, and `--as-of`. Named-person filtering reads XBRL because the current index has no person field. |
| `new` once-only seen-ID state; first 45 days; dry run; empty exit 1 | Implemented with a local state JSON, 45-day first check and persisted baseline, `--today`, `--dry-run`, `--read`, exit 1 when none, and retry of failed XML reads. Current and archive IDs are namespaced. |
| `fetch` PDF / `parse` extracted PDF text | `fetch` retrieves the indexed official XBRL; `parse` reads saved official XML, with `--filed` override. `--xml-dir` can save the source XML. The current filing's iXBRL web view is linked. No PDF or `pdftotext` is needed. |
| Every transaction, owner, buy/sell, instrument, option details, amount range, trade date, lag | Every named XBRL disclosure context is parsed. Person/category, transaction type, instrument, quantity, **reported exact field plus unit reference**, date range, disclosure lag, market mode, before/after holdings, derivative and remaining facts are preserved. The different Indian disclosure fields are kept as filed. |
| Holdings/watchlist match, public-source quote, caution about stale disclosures | Paper portfolio or `--tickers` flag, official NSE links, explicit past-data caveat. No inference of insider information or profitable signal. |
| Senate manual consent route | BSE-only disclosures require a separately checked official BSE route. The NSE implementation does not automate BSE search or imply national completeness. |
| US 45-day late-filing flag | **Not copied**: Indian rules and calendar differ. Calendar lag is shown without a legal-lateness verdict. |

The two NSE indexes are finite public windows, not a proven complete historical database. A `--member` search across every issuer downloads and parses each indexed XBRL and can be slow; use `--symbol` and a date range when possible. Some filed XBRL has questionable unit references (the sampled older RELIANCE XML labels a value field `shares`); the output preserves the unit and never silently converts it to INR. Filing revisions are distinct records, not merged as if they were original transactions. BSE-only filings, NSE special filing shapes, market-hour timeliness, and independent cross-exchange reconciliation remain unverified. No live orders or return claims are included.
