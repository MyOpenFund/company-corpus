"""Estonia Äriregister (RIK) bulk CSV-join register — keyless.

Downloads annual bulk CSV files from avaandmed.ariregister.rik.ee (CC-BY,
no API key required) and joins the elements dump with the metadata dump on
``report_id`` to produce one structured dict per annual report.

Source CSVs
-----------
The RIK publishes two open-data exports at::

    https://avaandmed.ariregister.rik.ee/et/avaandmete-allalaadimine

* **Elements** (``4.<year>_aruannete_elemendid_<snapshot>.zip``): one row per
  financial element in each filed report.  Semicolon-delimited, UTF-8.
  Columns: ``report_id;tabel;elemendi_label;elemendi_nimetus;vaartus``.

* **Metadata** (``1.aruannete_yldandmed_<snapshot>.zip``): one row per report
  with company identity and filing metadata.  Semicolon-delimited, UTF-8.
  Columns include: ``report_id``, ``registrikood``, ``aruandeaasta``,
  ``kas konsolideeritud?``, ``period_end`` (``DD.MM.YYYY``).

Data policy: CC-BY.  No registration or API key required.

Public API
----------
iter_ee_reports(elem_source, meta_source) -> Iterable[dict]
    Join the two CSVs on ``report_id`` and yield one structured dict per
    report.  Each source may be a file path or raw bytes (zip or plain CSV).

download_ee_bulk(year, *, fetcher, elem_url=None, meta_url=None)
    Keyless GET of the two bulk zips.  Returns ``(elem_bytes, meta_bytes)``.
    File names rotate with each RIK snapshot; pass explicit URLs when known,
    or build them from the listing at the download page.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import zipfile
from typing import Iterable, Union

log = logging.getLogger(__name__)

# ── DD.MM.YYYY → YYYY-MM-DD ──────────────────────────────────────────────────
_DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")


def _parse_period_end(raw: str) -> str | None:
    """Convert DD.MM.YYYY to ISO YYYY-MM-DD; return None if the format differs."""
    m = _DATE_RE.match(raw.strip())
    if m:
        dd, mm, yyyy = m.groups()
        return f"{yyyy}-{mm}-{dd}"
    return None


# ── zip-or-CSV reader ─────────────────────────────────────────────────────────

def _load_csv_text(source: Union[str, bytes, os.PathLike]) -> str:
    """Return the text content of *source*, transparently decompressing a zip.

    *source* may be:
    * a file path (``str`` / ``os.PathLike``) to a ``.csv`` **or** a ``.zip``
      that contains a single ``.csv`` member;
    * raw ``bytes`` of a CSV; or
    * raw ``bytes`` of a zip (detected by the ``PK\\x03\\x04`` magic bytes).
    """
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as fh:
            data: bytes = fh.read()
    else:
        data = source

    # Decompress zip if magic bytes match
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise ValueError("Zip contains no .csv member")
            with zf.open(csv_names[0]) as member:
                data = member.read()

    # Decode; strip UTF-8 BOM if present
    return data.decode("utf-8-sig")


# ── metadata index ────────────────────────────────────────────────────────────

def _build_meta_index(meta_source: Union[str, bytes, os.PathLike]) -> dict:
    """Parse the metadata CSV and return ``{report_id: meta_dict}``."""
    text = _load_csv_text(meta_source)
    reader = csv.reader(io.StringIO(text), delimiter=";")
    header = next(reader, None)
    if header is None:
        return {}

    # Locate columns by name so we are robust to extra/reordered columns
    h = [col.strip() for col in header]

    def _col(name: str) -> int:
        try:
            return h.index(name)
        except ValueError:
            return -1

    ci_id = _col("report_id")
    ci_rk = _col("registrikood")
    ci_yr = _col("aruandeaasta")
    ci_ko = _col("kas konsolideeritud?")
    ci_pe = _col("period_end")

    index: dict = {}
    for row in reader:
        if not row or ci_id < 0 or ci_id >= len(row):
            continue
        report_id = row[ci_id].strip()
        if not report_id:
            continue

        registrikood = row[ci_rk].strip() if ci_rk >= 0 and ci_rk < len(row) else None
        aruandeaasta_raw = row[ci_yr].strip() if ci_yr >= 0 and ci_yr < len(row) else ""
        kas_konsolideeritud = row[ci_ko].strip() if ci_ko >= 0 and ci_ko < len(row) else ""
        period_end_raw = row[ci_pe].strip() if ci_pe >= 0 and ci_pe < len(row) else ""

        try:
            aruandeaasta: int | None = int(aruandeaasta_raw)
        except (ValueError, TypeError):
            aruandeaasta = None

        index[report_id] = {
            "registrikood": registrikood or None,
            "period_end": _parse_period_end(period_end_raw),
            "kas_konsolideeritud": kas_konsolideeritud,
            "aruandeaasta": aruandeaasta,
        }
    return index


# ── main iterator ─────────────────────────────────────────────────────────────

def iter_ee_reports(
    elem_source: Union[str, bytes, os.PathLike],
    meta_source: Union[str, bytes, os.PathLike],
) -> Iterable[dict]:
    """Iterate annual reports from the EE Äriregister bulk CSV exports.

    Joins the elements dump with the metadata dump on ``report_id`` and yields
    one structured dict per report.

    Parameters
    ----------
    elem_source:
        File path or bytes of the elements CSV (or its zip).  Semicolon-
        delimited; columns ``report_id;tabel;elemendi_label;elemendi_nimetus;
        vaartus``.
    meta_source:
        File path or bytes of the metadata CSV (or its zip).

    Yields
    ------
    dict::

        {
            "report_id":         str,
            "registrikood":      str | None,
            "period_end":        str | None,   # ISO YYYY-MM-DD
            "aruandeaasta":      int | None,
            "kas_konsolideeritud": str,
            "elements":          {nimetus: float},
        }

    Notes
    -----
    * Elements whose ``elemendi_nimetus`` ends with ``"Consolidated"`` are
      dropped (standalone-only reporting).
    * First occurrence wins on duplicate ``elemendi_nimetus`` values (a name
      may appear in more than one table with the same value).
    * Rows whose ``vaartus`` is not a valid number are skipped silently.
    * A report present in the elements CSV but absent from metadata is still
      emitted with ``registrikood=None`` and ``period_end=None`` (batch-safe).

    Data source: avaandmed.ariregister.rik.ee, CC-BY, keyless.
    """
    # Build metadata index first (it is much smaller than the elements file)
    meta_index = _build_meta_index(meta_source)

    # Stream elements CSV, accumulating by report_id
    text = _load_csv_text(elem_source)
    reader = csv.reader(io.StringIO(text), delimiter=";")
    header = next(reader, None)
    if header is None:
        return

    h = [col.strip() for col in header]
    try:
        ci_id = h.index("report_id")
        ci_nm = h.index("elemendi_nimetus")
        ci_vl = h.index("vaartus")
    except ValueError as exc:
        raise ValueError(f"Elements CSV missing expected column: {exc}") from exc

    # Accumulate elements grouped by report_id, preserving insertion order
    groups: dict[str, dict[str, float]] = {}
    order: list[str] = []  # track first-seen order for deterministic output

    for row in reader:
        if len(row) <= max(ci_id, ci_nm, ci_vl):
            continue
        report_id = row[ci_id].strip()
        if not report_id:
            continue
        nimetus = row[ci_nm].strip()
        # Skip empty nimetus or names ending in "Consolidated"
        if not nimetus or nimetus.endswith("Consolidated"):
            continue
        # Skip non-numeric values
        vaartus_raw = row[ci_vl].strip()
        try:
            value = float(vaartus_raw)
        except (ValueError, TypeError):
            continue

        if report_id not in groups:
            groups[report_id] = {}
            order.append(report_id)
        # First-wins on duplicate nimetus
        if nimetus not in groups[report_id]:
            groups[report_id][nimetus] = value

    # Yield one dict per report_id (in first-seen order)
    for report_id in order:
        meta = meta_index.get(report_id, {})
        yield {
            "report_id": report_id,
            "registrikood": meta.get("registrikood"),
            "period_end": meta.get("period_end"),
            "aruandeaasta": meta.get("aruandeaasta"),
            "kas_konsolideeritud": meta.get("kas_konsolideeritud", ""),
            "elements": groups[report_id],
        }


# ── keyless downloader ────────────────────────────────────────────────────────

# Base URL for the RIK open data download page
_RIK_BASE = "https://avaandmed.ariregister.rik.ee"


def download_ee_bulk(
    year: int,
    *,
    fetcher,
    elem_url: str | None = None,
    meta_url: str | None = None,
) -> tuple[bytes, bytes]:
    """Download the EE Äriregister bulk CSV zips for *year*.

    Parameters
    ----------
    year:
        The fiscal year of interest (e.g. ``2025``).  Used only when
        *elem_url* / *meta_url* are not supplied explicitly, as a hint for
        documentation purposes.  The actual file names rotate with each RIK
        snapshot date, so callers should pass explicit URLs whenever possible.
    fetcher:
        A :class:`company_corpus.http.Fetcher` instance (or any object that
        exposes ``get(url) -> response`` with a ``.content`` attribute).
    elem_url:
        Full URL of the elements zip
        (``4.<year>_aruannete_elemendid_<snapshot>.zip``).
        Obtain from the RIK download listing at
        ``https://avaandmed.ariregister.rik.ee/et/avaandmete-allalaadimine``.
    meta_url:
        Full URL of the metadata zip
        (``1.aruannete_yldandmed_<snapshot>.zip``).

    Returns
    -------
    tuple[bytes, bytes]
        ``(elem_bytes, meta_bytes)`` — raw zip bytes suitable for passing
        directly to :func:`iter_ee_reports`.

    Raises
    ------
    ValueError
        If neither *elem_url* nor a resolvable default URL can be constructed.
    RuntimeError
        On HTTP/network failure.  Callers needing batch-safe behaviour should
        catch ``Exception``.

    Notes
    -----
    Access is completely open (CC-BY); no registration or API key is required.
    """
    if elem_url is None or meta_url is None:
        raise ValueError(
            f"RIK bulk file names include a snapshot date that rotates with each "
            f"release.  Please pass explicit elem_url and meta_url for year {year}.  "
            f"Obtain URLs from: {_RIK_BASE}/et/avaandmete-allalaadimine"
        )

    log.info("Downloading EE elements zip: %s", elem_url)
    try:
        elem_resp = fetcher.get(elem_url)
        elem_bytes: bytes = elem_resp.content
    except Exception as exc:
        raise RuntimeError(f"Failed to download EE elements from {elem_url}: {exc}") from exc

    log.info("Downloading EE metadata zip: %s", meta_url)
    try:
        meta_resp = fetcher.get(meta_url)
        meta_bytes: bytes = meta_resp.content
    except Exception as exc:
        raise RuntimeError(f"Failed to download EE metadata from {meta_url}: {exc}") from exc

    return elem_bytes, meta_bytes
