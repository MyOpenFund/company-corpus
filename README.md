# company-corpus

An exhaustive, replicable, **open-data** corpus of **company** primary-source
documents — the *bottom-up / micro* layer that complements
[`central-bank-corpus`](https://github.com/MyOpenFund/central-bank-corpus). Both feed the
MyOpenFund stack (vault → [`data-orchestrator`](https://github.com/MyOpenFund/data-orchestrator) →
eigenmind/Qdrant). **Today only the SEC EDGAR pillar reaches that chain; the EU
and register pillars are acquired and parsed but not yet exported to the vault**
(tracked in the project notes).

Every document comes from the issuer's **regulator of record** (SEC, AMF, FCA,
CONSOB, …) — public, primary-source disclosures, with provenance recorded per
file. No proprietary datasets, no machine translation, no model-generated text.

## Three pillars

| Pillar | Region | Source of record | Identity | Status |
|---|---|---|---|---|
| **🇺🇸 SEC** | United States | EDGAR | CIK | complete (reports, ownership, XBRL financials) |
| **🇪🇺 EU** | 14 jurisdictions | national OAMs + Euronext + FCA NSM | LEI / ISIN | 13 backend keys / 12 classes (`IE` reuses `GB`) + Euronext for `PT` |
| **Register** | 8 countries | national business registers | local entity ID | 8 registers merged — [`REGISTER_FINANCIALS.md`](docs/REGISTER_FINANCIALS.md) |

All three pillars share the same discipline (official sources only, stable ids,
exhaustive discovery, never silently partial) and feed the same RAG contract. The
SEC and EU pillars cover listed issuers (CIK / GLEIF LEI); the register pillar
targets the credit and private-company universe of non-listed entities.

### Structured financials

Beyond raw filings, a shared engine (`financials.py`) extracts curated metrics into
a unified per-period row schema (see [`docs/FINANCIALS.md`](docs/FINANCIALS.md)):

| Layer | Universe | Source | Status |
|---|---|---|---|
| **SEC XBRL** | US listed issuers | EDGAR `companyfacts` | ✅ done |
| **EU ESEF / IFRS** (Pillar B) | EU listed issuers | `filings.xbrl.org` json_url + Arelle (Tier B) | ✅ done — [`EU_FINANCIALS.md`](docs/EU_FINANCIALS.md) |
| **Register financials** | Private / credit universe (non-listed) | 8 national registers: 🇳🇴 NO · 🇬🇧 UK · 🇧🇪 BE · 🇱🇺 LU · 🇫🇮 FI · 🇩🇰 DK · 🇪🇪 EE · 🇸🇰 SK | ✅ all eight registers merged — [`REGISTER_FINANCIALS.md`](docs/REGISTER_FINANCIALS.md) |

The register pillar targets issuers that never file ESEF — bond obligors, private
companies, bank counterparties. Output lands in `data/financials_register/` (never
merged with `data/financials_eu/`), labelled by `basis` (legal-entity vs.
consolidated). It is governed by a **no-false-data** discipline: any value that
cannot be confirmed from structural anchors is suppressed, not guessed. Leverage
rows carry a `leverage_basis` field (`"borrowings"` or `"total_liabilities"`)
because registers differ — some expose real financial borrowings (BE/LU/SK/DK-ESEF),
others only total liabilities (NO/UK/EE/DK-FSA) — so consumers cannot compare
`debt_to_equity` across registers without knowing the basis.

## Quick start

```bash
pip install -r requirements.txt           # runtime-only
pip install -r requirements-dev.txt       # adds pytest-socket etc. for the test suite
python -m pytest -q                       # network-free test suite

export COMPANY_CORPUS_CONTACT="you@example.com"   # required before any live crawl
```

There is **no default contact** — without it the `User-Agent` carries only the
tool name, so cloning the repo never leaks anyone's address. Regulators ask for a
real contact, so set it before crawling.

```bash
# SEC: build a tiny universe, discover + download
python -m company_corpus build-universe --tickers AAPL,MSFT --name demo --write
python -m company_corpus discover --universe demo --download --since 2015-01-01 --write

# EU: acquire one issuer's regulated filings by ISIN (resolves the LEI via GLEIF);
# dry-run by default (discovery only, nothing written), --write downloads
python -m company_corpus eu-acquire --isins FR0010193052
python -m company_corpus eu-acquire --isins FR0010193052 --write

# Register financials: Norwegian statutory accounts (Brreg, no key required)
python -m company_corpus register-financials --orgnrs 923609016 --write
# UK Companies House bulk iXBRL (Arelle required; --limit for a bounded test run)
python -m company_corpus register-financials --ch-bulk accounts_monthly_2024_01.zip --limit 100
```

## Operations

**Doctrine, in one sentence:** every work command folds its own counters into a
structured run-report before exiting, so a run that did no useful work can
never exit `0`.

### Exit codes

- `0` — clean: real work got done, or nothing was found and zero errors occurred.
- `1` — fatal: an uncaught exception, or a command that itself returned non-zero.
- `3` — degraded: any source reported a `truncated` (partial) result, OR zero
  new documents were produced while errors occurred (save errors and/or fetch
  errors). Recovered transient errors alongside real new documents do **not**
  degrade a run.

### `data/runs.jsonl`

Every work command — `discover`, `discover-index`, `download`, `render-pdf`,
`xbrl`, `ownership`, `enrich-openfigi`, `eu-financials`, `eu-acquire`,
`register-financials` — appends one JSON line (atomic `O_APPEND` write) before
returning:

```json
{"run_id": "...", "tool": "company-corpus", "command": "discover",
 "started_at": "...", "finished_at": "...", "outcome": "ok", "exit_code": 0,
 "totals": {"docs_seen": 120, "docs_new": 4, "docs_failed": 0},
 "sources": [{"source_code": "sec", "docs_seen": 120, "docs_new": 4,
              "docs_failed": 0, "fetch_errors": 0, "truncated": false,
              "error_samples": []}]}
```

A `failed` run also carries a `fatal` field (the exception, capped at 500
chars). The MyOpenFund vault ingests this file **unchanged** — the schema is
deliberately flat and stable, so it feeds a `runs` table with no transform step.

`COMPANY_DATA_DIR` is a test/ops override of the **run-report path only**
(`runs.jsonl`): it wins over everything else for that one file, and nothing
else honours it — the corpus and the error trail below stay under the data
dir. To relocate the corpus *and* the trail, use `--data-dir` (which also
places the report, absent the override); the default is `Config`'s `./data`.

### Unit of useful work, per command

`docs_new` is what a command reports as its actual output; `docs_seen` is what
it examined; `docs_failed` is its error count. A dry run still counts what it
*would* write (except `download`, whose producer has no would-download
counter), so a dry run that found candidates and hit no errors is `ok`, never
a green "nothing to do":

| command | source | docs_new (useful work) |
|---|---|---|
| `discover` (`--download`) | sec | records added (+ files downloaded); both legs fold into the one `sec` row, so exit 3 needs both legs empty and either leg failing |
| `discover-index` | sec | records added |
| `download` | sec | files downloaded |
| `render-pdf` | sec | rendered + would_render |
| `xbrl` | sec | period summaries |
| `ownership` | sec | insider + 13F + passthrough filings (+ would-download on a dry run) |
| `enrich-openfigi` | sec | identifiers mapped (no-match ≠ error) |
| `eu-financials` | xbrlorg | period summaries |
| `eu-acquire` | one row per backend dispatched to | documents kept from that backend |
| `register-financials` | one row per register | period summaries |

Full detail (every column, plus the three deliberate policy choices behind
this table) is documented in the `cli.py` module docstring.

### Logging

Work commands configure the root logger once (INFO, to stderr) so their own
progress is visible; register and EU/OAM fetch failures — a dead source, or a
partial multi-call traversal that still produced some periods — are logged as
WARNINGs, in addition to being folded into the run report and the trail below.

### The discovery-error trail

Every pillar that hits a dead source (SEC discovery, an EU/OAM backend, or a
national register) appends to `data/discovery_errors.jsonl`, one JSON line per
error, stamped by `Storage.record_errors` with `ts` (ISO-8601 UTC) and the
`run_id` of the run that hit it — so a trail entry ties back to its run report.
Written **only with `--write`** for the EU and register commands (a dry run
still reports the errors in `runs.jsonl`, but leaves no trail file — "DRY-RUN
(nothing written)" means exactly that). In a register's own coverage file,
`source-error` is a distinct status from `no-financials` / `unbalanced`: "the
register could not be read" is a fact about the source, never confused with
"the issuer filed nothing."

### `SOURCE_CODES`: one code per authority

The corpus pulls filings through three pillars — SEC EDGAR, the EU/OAM network
(13 national mechanisms + the filings.xbrl.org aggregator), and 8 national
company registers — and
[`company_corpus/source_codes.py`](company_corpus/source_codes.py) is the
canonical registry mapping every one of those **23 codes** to its real-world
regulatory authority, never to a module, class, or file-format variant (e.g.
`erst-fsa` and `erst-ifrs` are both the Danish `erst` authority; the finer
"how it reached us" detail lives in the row's `provenance` field, not in a
second near-duplicate code). `source_code_for()` resolves any producer/backend
tag to its canonical code and raises rather than guess.

## Documentation

| Doc | What's inside |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Layer map, data model, corpus lifecycle, issuer-resolution waterfall, design invariants |
| [`docs/SEC_PILLAR.md`](docs/SEC_PILLAR.md) | 🇺🇸 SEC guide: taxonomy, storage layout & naming, the full CLI, identity (rename/merger), ownership & XBRL financials |
| [`docs/EU_PILLAR.md`](docs/EU_PILLAR.md) | 🇪🇺 EU guide: the "European EDGAR" — `OamSource` architecture, identity resolution (LEI/ISIN/OpenFIGI/name), listing dispatch, cross-backend dedup, the `eu-acquire` command / `acquire()` |
| [`docs/EU_BACKENDS.md`](docs/EU_BACKENDS.md) | Per-country backend reference (source API, identity key, doc types, pagination caps) |
| [`docs/FINANCIALS.md`](docs/FINANCIALS.md) | The shared financials engine (reported + derived metrics, ~60 curated concepts) |
| [`docs/EU_FINANCIALS.md`](docs/EU_FINANCIALS.md) | Structured EU ESEF/IFRS financials — json_url stdlib (Tier A) + Arelle (Tier B) |
| [`docs/REGISTER_FINANCIALS.md`](docs/REGISTER_FINANCIALS.md) | Statutory financials from 8 national registers (🇳🇴 NO · 🇬🇧 UK · 🇧🇪 BE · 🇱🇺 LU · 🇫🇮 FI · 🇩🇰 DK · 🇪🇪 EE · 🇸🇰 SK); no-false-data gate; `leverage_basis` field |
| [`docs/INGESTION_RAG.md`](docs/INGESTION_RAG.md) | The RAG ingestion contract + a ready-to-paste orchestrator connector |

## Core principles

- **Official primary sources only** — every document comes from the issuer's
  regulator of record; provenance is recorded per filing.
- **Replicability** — stable, date-independent document ids; idempotent,
  convergent crawls; deterministic on-disk layout.
- **Exhaustivity** — discovery via each regulator's own indices/APIs; coverage is
  reconciled against what's expected; incompleteness is **recorded, never silently
  dropped** (a backend that caps a page records a `truncated` error).
- **No-guess identity** — an issuer is bound only on an exact/verified match
  (CIK, LEI, or ISIN); an ambiguous match is left unresolved, never guessed.
- **No-false-data** — in the register-financials pillar, any value that cannot be
  confirmed from structural anchors is suppressed and the reason recorded; a missing
  number beats a wrong one.

## Fair access

The HTTP client sends a declared, contact-carrying `User-Agent` and throttles per
host to stay at/under each regulator's published rate limit (e.g. the SEC's 10
requests/second). It retries only transient failures — connection errors,
timeouts, HTTP 429 and 5xx — with backoff (honouring `Retry-After`); any other
4xx is taken as the server's answer and costs exactly one request, so a missing
document never turns into four requests and seconds of backoff against the host.
Text bodies are decoded by their declared charset; when none is declared it
tries strict UTF-8 first and otherwise keeps the header default — `requests`
would otherwise default undeclared `text/*` to ISO-8859-1 and turn UTF-8 pages
into mojibake. There is no charset sniffing, which would mis-read genuine
latin-1 pages as something else.

**Belgium (FSMA STORI)** is the one exception, and it is opt-in
(`pip install '.[be]'`) and off by default. The FSMA JSON API sits behind an F5
BIG-IP ASM WAF that rejects plain HTTP clients, so `company_corpus/eu/sources/oam_be.py`
reaches it through `curl_cffi` impersonating Chrome (`impersonate="chrome124"`)
with browser-consistent `Origin`/`Referer` headers and a bootstrapped session
cookie, bypassing the shared `Fetcher` entirely — no project contact `User-Agent`,
no shared throttle. That is stated here factually as the reason this one backend
stays behind an explicit extra rather than being on by default.

## License

Code is licensed under the [MIT license](LICENSE). The metadata files
committed under `data/` and the manifests/tables this tool produces are
licensed under [CC BY 4.0](DATA_LICENSE); the underlying filings remain under
each issuing authority's own terms. The recorded response fixtures under
`tests/fixtures/` are reproductions of each authority's own output for
regression testing and are not covered by either license — see
[DATA_LICENSE](DATA_LICENSE) for the per-directory provenance.
