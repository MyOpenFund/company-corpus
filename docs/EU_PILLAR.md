# 🇪🇺 EU pillar — the "European EDGAR"

There is no single European EDGAR: under the EU Transparency Directive, regulated
information is stored **per country** in an *Officially Appointed Mechanism* (OAM)
— AMF in France, FCA NSM in the UK, CONSOB in Italy, AFM in the Netherlands, and
so on. (A pan-EU access point, **ESAP**, is only expected around mid-2027.)

This pillar federates those national OAMs — plus the cross-market **Euronext**
feed and the ESEF aggregator **filings.xbrl.org** — behind one pluggable backend
interface, so a single `acquire()` call pulls an issuer's regulated filings
wherever they live. Code lives in [`company_corpus/eu/`](../company_corpus/eu/).

Per-country specifics (source API, identity key, doc types, pagination caps) are
in the companion reference: [`EU_BACKENDS.md`](EU_BACKENDS.md).

## Coverage

13 national backends + Portugal via Euronext = **14 jurisdictions**, plus the
Euronext cross-market complement and the ESEF complement.

| | Country | Backend | Source of record |
|---|---|---|---|
| 🇫🇷 | France | `InfoFinanciereFR` | AMF — info-financiere.gouv.fr |
| 🇩🇪 | Germany | `BundesanzeigerDE` | Bundesanzeiger |
| 🇮🇹 | Italy | `OneInfoIT` | CONSOB — 1Info |
| 🇪🇸 | Spain | `CnmvES` | CNMV |
| 🇳🇱 | Netherlands | `AfmNL` | AFM |
| 🇧🇪 | Belgium | `StoriBE` | FSMA — STORI |
| 🇬🇧 | United Kingdom | `NsmGB` | FCA — National Storage Mechanism |
| 🇮🇪 | Ireland | `NsmGB` (by LEI) | FCA NSM (the de-facto OAM for Irish issuers) |
| 🇸🇪 | Sweden | `OamSE` | Finansinspektionen — Finanscentralen |
| 🇩🇰 | Denmark | `OamDK` | Finanstilsynet OAM |
| 🇫🇮 | Finland | `OamFI` | Nasdaq Helsinki — oam.fi |
| 🇳🇴 | Norway | `NewsWebNO` | Oslo Børs — NewsWeb |
| 🇨🇭 | Switzerland | `DisclosureCH` | SIX Swiss Exchange + EQS (aggregator) |
| 🇵🇹 | Portugal | `EuronextSource` | Euronext Lisbon (no national backend) |
| 🇪🇺 | — | `EuronextSource` | Euronext cross-market notices (NL/BE/FR/PT/NO) — *complement* |
| 🇪🇺 | — | `FilingsXbrlOrg` | filings.xbrl.org — ESEF reports — *complement* |

Every backend was built **recon-first** (capture the real responses before writing
a parser) and **validated live** against real issuers — and that discipline caught
a real bug in essentially every one (wrong download host, a WAF, the wrong
register, a doc-type mislabel, a dict-vs-string body, a fixed param order…).

## Architecture

```
specs (lei | isin | name)
        │
        ▼  resolve_entities()  ── GLEIF (LEI/ISIN/name) → OpenFIGI bridge
   Entity(lei, name, country, isins, resolution)
        │
        ▼  acquire()  ── dispatch by country + listing
   national backend  +  filings.xbrl.org  +  Euronext complement  +  (listing fallback)
        │
        ▼  merge_documents()  ── cross-backend dedup
        ▼  download_document() ── atomic write → data/raw/<LEI>/<FAMILY>/<year>/<doc_id>/
        ▼  byte-confirmed dedup ── drop duplicates that share an identical file
        ▼  reconcile()         ── coverage report (per entity, by LEI)
```

### `OamSource` — the backend contract

Every backend subclasses [`OamSource`](../company_corpus/eu/oam_base.py):

```python
class OamSource(ABC):
    country: str
    name: str
    def discover(self, entity: Entity) -> list[Document]: ...   # never raises out
    def list_issuers(self) -> list[IssuerRef]: ...              # scale-up; usually []
    def _record_error(self, context, url, error): ...           # never silently partial
```

`discover` is the only method that matters: given a resolved `Entity`, return its
`Document`s — and **never raise** (the dispatcher wraps it, but errors are recorded
via `_record_error`, never swallowed). Adding a country = one new `OamSource`
subclass + one entry in `COUNTRY_BACKENDS` (in
[`acquire.py`](../company_corpus/eu/acquire.py)).

### The `Document` model

A [`Document`](../company_corpus/eu/documents.py) carries `native_id`, `lei`,
`country`, `doc_type`, `period_end`, `published_ts`, `source`, a list of `files`
(each `{name, kind, url|content, sha256, …}`), and `native_meta`. `doc_type` is one
of:

```
annual_report · half_year_report · interim_statement · inside_information
holding_notification · prospectus · governance · other
```

A file can be a downloadable `url`, an inline `content` blob (capture-at-discovery,
for sources whose links are session-bound), or index-only (metadata, no file) —
which is recorded, never a silent drop.

#### Document identity — `doc_id` is computed, never minted

`Document.doc_id` is a read-only property:

```
doc_id = sha1("<source>|<country>|<native_id>")[:16]
```

`native_id` is the **source's own stable handle** on the document (an OAM row id,
a disclosure id, a register number) — the only field a backend supplies. It must
be stable across runs; a backend that has no id of its own builds one with
`stable_native_id(*parts)` from the publication's own facts (its artefact URL, its
title, its publication date), and `source_key(*parts)` guards a source key that is
present but falsy (`0` is a key; `None` is not). Neither helper will invent an id:
when every part is empty they raise, the backend records the gap through its
`errors` list and skips the document. An id minted from the wall clock, from a
page offset or from `len(files)` — which three backends used to do — renames the
same document on every run, and a renamed document is a re-download into a fresh
directory that never converges.

Three consequences worth knowing:

- Because the source and the country are inside the hash basis, they are **not**
  in the id string: there are no more `se-…` / `fi-…` prefixes.
- `doc_id` is 16 hex characters, so it can never be a hostile path component. The
  `safe_filename` call in `download.py` is now belt-and-braces for it (it still
  earns its keep for the LEI and for file names).
- **No compatibility shim.** Documents acquired before this rule keep their old
  directory under `data/raw/…` and their old `data/manifest/<LEI>/<doc_id>.json`,
  and nothing will read them again. Re-run `eu-acquire` and delete the orphans;
  the two spellings will never converge on their own.

The basis is normalised so a cosmetic difference cannot fork one document in two:
the native id is stripped, the country is upper-cased. Each manifest carries the
`native_id` beside the `doc_id`, so the one-way hash stays traceable back to the
row of the register it came from (and recomputable to check).

**A collision is an error, never a dedup.** Two *different* documents that compute
one `doc_id` would share one raw directory and one manifest path, so one of them
cannot be acquired at all. [`merge_documents`](../company_corpus/eu/dispatcher.py)
compares the two (files, `published_ts`, title) and reports the clash to
`acquire`, which records it against the losing backend and degrades that entity's
coverage row to `source-error` — the run continues for every other issuer. Only
byte-identical copies (equal `sha256` sets) merge, and that merge is near-nil at
discovery time by design: the authoritative cross-backend dedup runs *after*
download, on `(lei, published-day, sha256)`, where the bytes actually exist.

**One bad row costs one row.** A listing row whose source gives no key at all is
skipped through `OamSource._emit`, which records a `native-id` error for that
document and keeps the rest of the listing. Previously the `ValueError` escaped
`discover()` and cost the entity its whole listing on that backend.

## Identity resolution (no-guess)

US identity is the CIK; EU identity is the **GLEIF Legal Entity Identifier (LEI)**
and the issuer's **ISINs**. [`resolve_entities`](../company_corpus/eu/entities.py)
turns a spec into an `Entity`, recording *how* it resolved (the `resolution` tier):

1. **`lei`** — direct GLEIF record lookup.
2. **`isin`** — GLEIF `filter[isin]` → LEI. On a miss, the **OpenFIGI bridge**
   (`isin-figi`): OpenFIGI maps the ISIN → issuer name (broader ISIN coverage than
   GLEIF), then GLEIF resolves the LEI from the *core* name — binding only on a
   single normalised match. (GLEIF's ISIN→LEI mapping is incomplete — it can hold
   an issuer's LEI yet not its equity ISIN; this bridge recovers those.)
3. **`name`** — GLEIF exact legal-name match, country-filtered, **only if exactly
   one** candidate remains. Two candidates → `unresolved` (never a guessed bind).

Resolved entities also carry the issuer's ISINs (from GLEIF) — the search key for
the ISIN-keyed backends (BE, CH, Euronext).

## Dispatch — by home country *and* by listing

For each entity, `acquire()` runs:

- the **national backend** for `entity.country` (if any),
- **filings.xbrl.org** (ESEF complement, always),
- the **Euronext complement** if the country is a Euronext market (NL/BE/FR/PT/NO),
- a **listing fallback** if the home country has *no* backend (e.g. a Bermuda- or
  Luxembourg-domiciled issuer): the Euronext notices feed is ISIN-keyed, so it's
  queried by the entity's ISINs and each notice's issuer name is verified (rejecting
  market-wide noise) — covering issuers the home-country dispatch would miss,
- **corroborated Oslo coverage**: when the Euronext probe returned an Oslo (`OSL_`)
  notice for a non-Norwegian issuer, the issuer is confirmed listed on Oslo Børs, so
  Oslo NewsWeb is queried too (a name match backed by a second, independent Oslo
  signal — Oslo's list has no ISIN, so name alone would be a guess).

## Cross-backend dedup

The same disclosure can surface from two backends (e.g. an ESEF report from both
the national OAM and filings.xbrl.org). Two layers collapse them:

1. **Pre-download** ([`merge_documents`](../company_corpus/eu/dispatcher.py)) —
   by `(lei, doc_type, period_end, file names/hashes)`, first-occurrence wins
   (national backends are listed before complements, so the more-complete one wins).
2. **Post-download, byte-confirmed** (in `acquire`) — after files are downloaded,
   two documents sharing the same `(lei, publication-day)` **and a byte-identical
   file (sha256)** are the same disclosure; the lower-priority copy is dropped.
   `doc_type` is deliberately *not* in the key — backends often classify the same
   file differently, and identical bytes already prove identity.

## Running it

### CLI — `eu-acquire`

```bash
python -m company_corpus eu-acquire --isins FR0010193052            # dry-run: discovery only
python -m company_corpus eu-acquire --leis 969500EWVT9M8RJTKM50 --write
python -m company_corpus eu-acquire --leis ... --write --no-download # discovery-only, with reports
```

`--leis` / `--isins` (one required, comma-separated; ISINs resolve to LEIs via
GLEIF). The command is dry-run by default, like every other work command:

| flags | what `acquire` gets | on disk |
|---|---|---|
| (none) / `--no-download` | `download=False, write=False` | nothing — prints what *would* be acquired |
| `--write` | `download=True, write=True` | files, manifests, entity index, coverage file, error trail |
| `--write --no-download` | `download=False, write=True` | entity index, coverage file, error trail (no files) |

`--no-download` alone is accepted and equals the default (downloading is already
gated on `--write`, since downloaded files are writes). The run report
(`data/runs.jsonl`) gets **one row per backend** the command dispatched to
(`docs_seen` = entities asked, `docs_new` = documents kept from that backend —
counted in a dry run too, `docs_failed` = that backend's errors), resolved to the
authority's code (`amf`, `banz`, `xbrlorg`, `euronext`, …) — so a dead national
OAM is counted as that backend's own failure on its own `sources` row, never
hidden behind the aggregator's. The run itself degrades only when no document
was acquired at all (the zero-useful-work rule): a dead OAM next to a productive
aggregator is an `ok` run whose report names the dead authority. Every error
item is appended to `discovery_errors.jsonl` with `--write`. A spec that resolves
to no LEI reaches no backend and no row, so the command prints it
(`unresolved: ISIN …, LEI …`, in input order) — on a dry run that line is its
only trace. A run whose specs *all* failed to resolve (bad identifiers, or GLEIF
unreachable) reached no backend at all and exits as `failed` rather than as a
green nothing-to-do. Without `--write` the command also prints
`note: --no-download is implied without --write`. There is no country filter:
the country comes from the resolved entity, not from the caller.

### Library — `acquire()`

`acquire(specs, *, fetcher, config, download=True, write=True)` is the entry point:

```python
from company_corpus.http import Fetcher
from company_corpus.config import Config
from company_corpus.eu.acquire import acquire

cfg = Config(data_dir="data", contact="you@example.com")
summary = acquire([{"isin": "FR0010193052"}],          # Catana Group SA
                  fetcher=Fetcher(cfg), config=cfg, download=True)
# {'entities': 1, 'unresolved': 0, 'unresolved_specs': [], 'documents': 257, 'manifests': 257,
#  'deduped_by_bytes': 3, 'download_errors': 0, 'documents_failed': 0,
#  'coverage_path': 'data/reports/eu_coverage.jsonl', 'errors': [...], 'error_items': [...],
#  'sources': {'oam-fr': {'entities': 1, 'documents': 240, 'errors': 0, 'not_indexed': 0,
#                         'truncated': False},
#              'xbrlorg': {...}, 'euronext': {...}}}
```

Specs accept `{"lei": …}`, `{"isin": …}`, or `{"name": …, "country": …}`. With
`download=False` you get discovery-only (no files written) — useful to size a run
first; with `write=False` nothing at all is written (no entity index, no coverage
file; `coverage_path` is `None`), and `download=True` with `write=False` raises.
The coverage report (`data/reports/eu_coverage.jsonl`) lists every entity with its
doc count, doc types, and any gap. The summary also carries `unresolved` (specs
that resolved to no LEI) and `sources` — per backend `name`, the entities it was
asked about, the kept documents it contributed and its errors — and every entry
of `errors` is tagged with the backend's `source` (a raised discovery under the
backend that died, a download failure under the document's source).

### Storage layout

```
data/raw/<LEI>/<FAMILY>/<year>/<doc_id>/<file>      # the documents (git-ignored)
data/manifest/<LEI>/<doc_id>.json                   # per-document provenance manifest
data/universe/eu_entities.jsonl                     # the resolved entities
data/reports/eu_coverage.jsonl                      # per-entity coverage / gaps
```

`<FAMILY>` is the doc-type family (`ESEF-AR`, `HY`, `MAR`, `GOV`, `OTHER`, …),
mirroring the SEC pillar's code/year layout but keyed on the **LEI** instead of the
CIK.

## Honest limitations

- **Coverage ≠ exhaustive everywhere.** Some sources are partial by nature:
  Switzerland has no statutory OAM (the SIX+EQS aggregator covers ~the issuers that
  use those disseminators); Euronext notices are *exchange* corporate-event notices
  (a complement, not the full financial-report set). These are documented per
  backend in [`EU_BACKENDS.md`](EU_BACKENDS.md).
- **Never silently partial.** Where a source caps a page, the backend paginates to
  its backstop and records a `truncated` error; an uncovered issuer shows up as
  `no-documents` in the coverage report, and an issuer whose backend actually FAILED
  shows up as `source-error` (with the message) instead — a dead OAM is never
  reported as an issuer that published nothing. Incompleteness is always *visible*.
- **Out of reach (recorded, not hidden):** CMVM Portugal-direct is an opaque,
  auth-gated OutSystems portal (PT is covered via Euronext instead).
- **Structured ESEF/IFRS extraction (Pillar B) is done** — see
  [`EU_FINANCIALS.md`](EU_FINANCIALS.md) for the json_url stdlib path (Tier A)
  and the Arelle iXBRL path (Tier B). Remaining gap: acquisition-side fix for
  DE/SE to enable Tier B for those backends.
