# Live mode (no store)

`company_corpus.live` reads the SEC on demand and returns, for one issuer, the same rows the
corpus and `store-batches` would build, without an on-disk corpus, a vault or an open-data-store
instance. It is a library API (no CLI command); myopenfund-mcp's no-vault mode is its first
caller.

```
pip install "myopenfund-company-corpus[live]"
```

The `[live]` extra adds the open-data-store contract schemas (same range as `[store]`, without
the writer) and MarkItDown (filing documents). Both are imported on first use: without them,
`issuer_tables` raises `StoreUnavailable` and the document conversion raises
`MarkItDownUnavailable`, each with the `pip install 'myopenfund-company-corpus[live]'` hint.

## API

| Module | Entry points | Requests |
|---|---|---|
| `live.sec` | `SecClient(contact, *, max_rps=5.0, timeout_s=30.0, retries=4, retry_after_max_s=30.0)`, the `SecGet` protocol (`get_bytes(url, *, max_age_s)`), the error taxonomy below | — |
| `live.resolver` | `resolver_rows(sec, *, max_age_s)` (tickers file), `former_name_rows(archive)` | 1 |
| `live.filings` | `fetch_submissions(cik, sec, *, max_age_s, now)`, `list_filings(archive, *, forms, period_from, period_to)` -> `FilingRef` | 1 + pages |
| `live.issuer` | `issuer_tables(cik, sec, *, config, now, positions_from, positions_to, facts_max_age_s, submissions_max_age_s, positions_forms=POSITIONS_FORMS)` -> `IssuerTables` (`entity_observations`, `fundamentals`, `filings`, `filing_statements` as Arrow tables typed by the contracts, `latest_filing`, `counters`) | 2 + pages + 3 per filing |
| `live.positions` | `filing_positions(ref, sec, *, counters=None, max_schemas=4)`: the FSDS-shaped statement rows of one filing | 3 (+ schemas) |
| `live.documents` | `filing_document(ref, sec, *, max_html_bytes=20_000_000)` -> `FilingDocument` (Markdown + Item `sections`), `html_to_markdown`, `find_sections`, `priority_sections` | 1 or 2 |

`SecClient` keeps no cache: `max_age_s` is a hint for a caching wrapper the caller puts in front
of it (any object with `get_bytes`). Filing files are immutable and always fetched with
`max_age_s=None`; companyfacts, submissions and the tickers file take the caller's TTL.

## The SEC contact

The SEC asks every automated client to declare a contact in its `User-Agent`. `SecClient` takes
it only from the caller: there is no default and no environment variable fallback. A blank
contact, or one with a line break, raises `SecContactMissing` at construction. The contact is
sent in the `User-Agent` and is never logged nor put in an error message.

## Request rate and retries

`max_rps` defaults to 5 requests per second, half the SEC's 10/s ceiling, shared by every thread
using the client and across SEC hosts (`www.sec.gov` and `data.sec.gov` count as one). It is
configurable up to 10 (`Config` refuses more). Transient failures (connection errors, timeouts,
HTTP 429 and 5xx) are retried `retries` times with a jittered exponential backoff. A server
`Retry-After` is honoured up to `retry_after_max_s` (30 s by default; `None` removes the cap);
above it the request fails at once with `SecUnavailable` instead of holding the caller for
minutes. A non-finite or negative `Retry-After` is ignored (the backoff applies).

## Errors

Every error is a `SecError`, and each keeps a built-in base:

| Error | Base | When | What the issuer tables do |
|---|---|---|---|
| `SecContactMissing` | `ValueError` | blank contact, at construction | — |
| `SecRefused` | `RuntimeError` | 403 "undeclared automated tool" page | propagates (every request would fail) |
| `SecUnavailable` | `RuntimeError` | connection errors, timeouts, 429, 5xx after the retries; `Retry-After` above the cap | per filing: `positions:fetch_failed`, the filing borrows earlier positions |
| `SecNotFound` | `LookupError` | 404 | per filing: `positions:fetch_failed`; companyfacts 404 propagates (no XBRL facts) |
| `SecHttpError` | `RuntimeError` | any other non-2xx (400, 401, another 403, 410...), after one request; `.status` holds it | per filing: `positions:fetch_failed` |

`filing_positions` never raises on a missing or malformed file: it returns `[]` and counts one
`positions:<reason>` (see its module docstring). `filing_document` raises
`PrimaryDocumentNotFound`, `UnsupportedDocument` (not HTML, or a `primary_document` holding `/`
or `..`) and `DocumentTooLarge`.

## Costs

* **Cold `fetch_submissions`**: one request for the main submissions file plus one per
  `filings.files` page, made one after the other under the rate cap. An issuer with a long
  history has several pages (the main file holds only its most recent filings), so its first
  call takes several seconds at 5 requests per second before transfer time; the companyfacts
  file of a large issuer is several MB on top. Put a cache in front of `SecClient` for repeated
  calls.
* **Statement positions**: three requests per report filing in the window (`index.json`,
  `FilingSummary.xml`, the `*_pre.xml`), plus one per schema read when the presentation is
  embedded in the `.xsd` (at most `max_schemas`, default 4). A ten-year window of 10-K and 10-Q
  filings is about 120 requests, about 25 s at 5 requests per second.
* **Documents**: the primary document only (plus its filing index when `primary_document` is
  unknown). Documents over `max_html_bytes` (20 MB by default) are refused before parsing.
  Measured on real 10-Ks (eight issuers, 2.0 to 12.9 MB of HTML): 0.6 to 5.5 s and at most
  320 MB peak RSS per document; the synthetic worst case under the 20 MB cap (nested layout
  tables) is about 45 s and over 1 GB. Run conversions off the request thread and size the
  cap to the host's memory.
