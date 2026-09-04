# 🇪🇺 EU backends — reference

Per-country technical detail for the EU pillar. The architecture, identity
resolution, dispatch and dedup are in [`EU_PILLAR.md`](EU_PILLAR.md); this is the
quick reference for each backend's source, identity key, access technique and
pagination. Each backend is one file under
[`company_corpus/eu/sources/`](../company_corpus/eu/sources/).

## At a glance

| Backend | Country | Source | Identity key | Access | Pagination (cap) |
|---|---|---|---|---|---|
| `InfoFinanciereFR` | 🇫🇷 FR | AMF info-financiere.gouv.fr | LEI + ISIN | Opendatasoft v2.1 JSON | `offset` → `total_count` (10 000) |
| `BundesanzeigerDE` | 🇩🇪 DE | Bundesanzeiger | name | stateful Wicket scrape | pager links (25 pages) |
| `OneInfoIT` | 🇮🇹 IT | CONSOB 1Info | name → `ndg` | unauth JSON REST | `start` → total (50 000) |
| `CnmvES` | 🇪🇸 ES | CNMV | name → NIF | ASP.NET WebForms scrape | `&page=N` (cap) |
| `AfmNL` | 🇳🇱 NL | AFM | issuer name | bulk XML export | none (full export) |
| `StoriBE` | 🇧🇪 BE | FSMA STORI | ISIN | JSON API + `curl_cffi` (WAF) | `startRowIndex` (5 000) |
| `NsmGB` | 🇬🇧 GB / 🇮🇪 IE | FCA NSM | **exact LEI** | Elasticsearch JSON | `from`/`size` (10 000) |
| `OamSE` | 🇸🇪 SE | Finanscentralen | name (verified) | ASP.NET WebForms scrape | `Page$Next`/grid (30) |
| `OamDK` | 🇩🇰 DK | Finanstilsynet OAM | name → CVR | JSON (`Accept-Language: en`) | `page` → `totalPages` (100) |
| `OamFI` | 🇫🇮 FI | Nasdaq Helsinki oam.fi | name → id | HTML scrape | `page` (60) |
| `NewsWebNO` | 🇳🇴 NO | Oslo Børs NewsWeb | name → issuerSign | JSON | date-window (40) |
| `DisclosureCH` | 🇨🇭 CH | SIX + EQS | ISIN / name (ISIN-verified) | JSON + HTML aggregator | SIX 200 / EQS 80 pages |
| `EuronextSource` | 🇵🇹 PT + NL/BE/FR/NO | Euronext | ISIN | JSON-in-HTML notices | `pageNum` (200) |
| `FilingsXbrlOrg` | 🇪🇺 (ESEF) | filings.xbrl.org | LEI | JSON:API | single page (100) |

Every backend records a `truncated` error if it ever hits its cap, so a capped
crawl is never silently partial.

## Notes per backend

### 🇫🇷 `InfoFinanciereFR` — AMF
Opendatasoft Explore v2.1 `records` API over `flux-amf-new-prod` (~531k records).
Query is an OR-clause over LEI **and** each ISIN (so pre-LEI-era records aren't
dropped). ODS caps `limit` at 100/request, so it pages by `offset` (most-recent
first, `order_by`) to `total_count`. Downloads via the record's
`url_de_recuperation` (an ftp.opendatasoft.com PDF/ESEF). doc_type from
`subtype_of_information`.

### 🇩🇪 `BundesanzeigerDE` — Bundesanzeiger
No captcha but a **stateful Wicket** session. The right registers are
`/pub/de/suche-kapitalmarkt` and `/pub/de/suche-rechnungslegung` (the `/pub/de/nlp`
one is net-short-positions — wrong). Bootstrap a session → fulltext POST → parse
`<div class="row">` → **filter rows to the issuer** (fulltext is noisy). Publication
links are session-bound with no stable URL, so it **captures the bytes at discovery**
(stored inline via the file `content` key); a capture failure degrades to index-only.

### 🇮🇹 `OneInfoIT` — CONSOB 1Info
Unauthenticated JSON REST at `consob.1info.it/PORTALE1INFO`. name → `ndg` via
`/API/companies/documenti`, then POST `/API/Documenti` + `/API/Comunicati`
**paginated to exhaustivity** (`length=200` is silently truncated by the server, so
walk `start` to `recordsFiltered`). Downloads at the **site ROOT**
`consob.1info.it/PdfViewer/PdfShow.aspx` (under `/PORTALE1INFO` it 404s); ESEF zip
via `protocolCodeXbrl`.

### 🇪🇸 `CnmvES` — CNMV
ASP.NET WebForms, fully HTTP-scrapable (no headless). name → **NIF**: GET
`BusquedaPorEntidad.aspx` → scrape `__VIEWSTATE`/`__EVENTVALIDATION` → POST → parse
the `<select>` options (exact name, diacritic-folded). Registers `resultado-ip`
(inside info), `resultado-oir` (other); annual reports = the `IFA/ListadoIFA`
`<table gridInformes>` (individual + consolidated + **ESEF**). Downloads via the
stable `webservices/verdocumento/ver?t=`.

### 🇳🇱 `AfmNL` — AFM
The cleanest feed: a **bulk XML export** GET
`afm.nl/export.aspx?type=<guid>&format=xml` → ~19k `<vermelding>`. Filter by issuer
(exact, ` N.V.`/` B.V.` folded). Per-doc details hop → a **stateless**
`downloadregisterfile.aspx?…&enc=<token>` (index-only on hop failure). v1 covers the
`financiele-verslaggeving` register; other AFM registers have their own export GUID.

### 🇧🇪 `StoriBE` — FSMA STORI
FSMA is behind an **F5 BIG-IP ASM WAF** (the "support ID" error page) that resets
non-browser clients. The modern source is the JSON API `webapi.fsma.be/api/v1/{lang}/stori`:
search `POST /result {isinCode|companyId, startRowIndex, pageSize}` → download `GET
/download?fileDataId=`. **Search by ISIN = clean identity.** The HTTP layer uses
`curl_cffi` (Chrome impersonation, optional dep `pip install ".[be]"`) + FSMA
Origin/Referer; must run from a non-datacenter IP.

### 🇬🇧 `NsmGB` — FCA NSM (also 🇮🇪 Ireland)
The richest source: an **Elasticsearch JSON API** over ~5.3M disclosures. POST
`api.data.fca.org.uk/search?index=fca-nsm-searchdata` with a custom envelope
(`criteriaObj.criteria:[{name:"lei", value:<LEI>}]`) → standard ES hits.
**Exact-LEI identity** (no LEI → no docs). Stateless GET
`data.fca.org.uk/artefacts/<download_link>`. The lei filter returns all disclosures
*concerning* the issuer (incl. third-party 8.3/8.5 dealing disclosures) — desirable
for exhaustivity. **Also wired for Ireland**: Euronext Dublin's per-issuer feed is
empty, but the NSM holds Irish issuers' regulated info by LEI (verified across the
full Dublin equity list, incl. small Growth-market names).

### 🇸🇪 `OamSE` — Finanscentralen
ASP.NET WebForms scrape (`finanscentralen.fi.se`). Scrape `__VIEWSTATE` etc. → POST
the name search (**body must be a dict** so requests form-encodes it — a pre-encoded
string silently drops the fields → 0 docs) → company profile → parse 6 GridViews
(year/half/quarter/bookend reports, flaggings, stockaffect), paging each via
`Page$Next`. **No-guess**: the search does a substring match and jumps to a profile
even for the wrong subsidiary ("Nordea" → "Nordea Hypotek"), so it requires the
profile's `lblCompanyName` to be **equal** (suffix-stripped) to the entity name.

### 🇩🇰 `OamDK` — Finanstilsynet OAM
JSON OAM at `weappegressprod.azurewebsites.net`. Identity via **CVR** from `GET
/config` (exact normalised name). `POST /search {filters:[{key:IssuerFilter,
options:[cvr]}]}`; `GET /details/{id}` → Azure-blob URLs + the **English** category
label — which only appears with `Accept-Language: en` (the default returns Danish →
everything mislabelled). The header is sent **per-request** (a session-header
mutation would contaminate the German/Dutch backends).

### 🇫🇮 `OamFI` — Nasdaq Helsinki
Nasdaq `oam.fi` scrape. Bootstrap `GET /` → `_csrf` + the company list (name → OAM id,
parsed from HTML-attribute-escaped JSON in `<nef-form-select>`) + categories. POST `/`
(urlencoded, 1-indexed pages) → `GET /view/{id}` → `viewAttachment.action`.

### 🇳🇴 `NewsWebNO` — Oslo Børs NewsWeb
JSON API `api3.oslo.oslobors.no/v1/newsreader`. name → **issuerSign** via `POST
/issuers` (exact normalised name; the suffix set is grounded in the live list —
asa/as/ltd/limited/plc/inc/sa/nv/se/ab/bv/gmbh/ag/a-s). `GET /list?issuer=…` is
date-window paginated (shift `toDate` to the oldest message on `overflow`).
Identity is **name/ticker** (the list carries no ISIN) — for foreign-domiciled
Oslo-listed issuers it is reached only via the *corroborated* path (see
[`EU_PILLAR.md`](EU_PILLAR.md)).

### 🇨🇭 `DisclosureCH` — SIX + EQS aggregator
Switzerland has no statutory OAM, so this unions two clean public sources, both
keyed on GLEIF ISINs (no-guess), deduped by `(title, day)`:
- **SIX** Share Explorer per-ISIN feed (`share_details.equityissuer.json`) —
  announcement HTML inline + attachment PDFs.
- **EQS News** — searched by name then **verified by ISIN** (`data-news-isin` ∈ the
  entity's ISINs) before binding, then paged through the per-company feed.

Issuers that self-distribute (Novartis, Roche, UBS…) are in neither archive and
show as `no-documents` — a structural ceiling, not a silent gap.

### 🇵🇹 / Euronext `EuronextSource`
`live.euronext.com` is one platform for every Euronext market, so one backend covers
them all via the per-issuer notices feed `GET /en/ajax/getNoticePublicData/<ISIN>-<MIC>`
— exchange corporate-event notices (dividends, admissions, name changes, GM…), with
`notice-download` PDFs. **The feed is ISIN-keyed and ignores the MIC**, so a *listing*
mode (`force_mic`) can probe by ISIN for any country, verifying each notice's issuer
cell (rejecting market-wide "Multiple" noise). **Primary** for Portugal (no national
backend); **complement** for NL/BE/FR/NO (listed *after* the national backend so the
national document wins dedup).

### 🇪🇺 `FilingsXbrlOrg` — filings.xbrl.org
ESEF aggregator (JSON:API), by LEI → one `annual_report` per filing (package zip +
report). A complement, **never a census** (DE/IE missing, IT partial); many entities
404. Single page (an issuer's ESEF reports are far under the 100 cap).
