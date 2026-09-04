"""Oslo Børs NewsWeb backend — Norway.

The NewsWeb is a clean JSON API at ``https://api3.oslo.oslobors.no/v1/newsreader``
serving ~1 605 issuers.  No auth, no cookies, no WAF.

Identity is via the Oslo Børs **issuerSign** (ticker), NOT LEI/ISIN.
Resolution:

* ``POST /issuers`` → list of all issuers (fetched once, lazily cached).
* Resolve ``entity.name`` → ``issuerSign`` by **exact normalised name match**:
  strip diacritics, casefold, remove periods (``Ltd.`` → ``ltd``), collapse
  whitespace, then strip ONE trailing legal form from the set actually present in
  the live list (asa/as/ltd/limited/plc/inc/sa/nv/se/ab/bv/gmbh/ag/a-s).  Strict:
  0 or >1 active matches → record error and return ``[]`` (never a guessed bind).

Pagination:
* ``GET /list?issuer=…&fromDate=…&toDate=…`` returns ``data.messages[]`` and
  ``data.overflow`` (bool).  There is no offset parameter.
* When ``overflow`` is true, shift ``toDate`` to the oldest ``publishedTime``
  in the batch and repeat.  Stop when ``overflow`` is false or ``_MAX_WINDOWS``
  reached (record truncated).  Messages are deduped by ``messageId``.

Per-message detail:
* ``POST /message?messageId={id}`` → ``data.message.attachments:[{id, name}]``.
* One Document per message; skip if ``numbAttachments == 0`` in the list
  response.  If the detail hop fails, record the error and skip that message.

Download URL (stable, no auth):
``https://api3.oslo.oslobors.no/v1/newsreader/attachment?messageId={m}&attachmentId={a}``
"""
from __future__ import annotations

import unicodedata
from datetime import date, datetime, timezone

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

_BASE = "https://api3.oslo.oslobors.no/v1/newsreader"
_ISSUERS_URL = f"{_BASE}/issuers"
_LIST_URL = f"{_BASE}/list"
_MESSAGE_URL = f"{_BASE}/message"
_ATTACHMENT_URL = f"{_BASE}/attachment"

# Date-window start (far enough back to cover all historical filings).
_FROM_DATE = "2006-01-01"

# Maximum date-windows before recording truncation and stopping.
_MAX_WINDOWS = 40

# ---------------------------------------------------------------------------
# Category id → doc_type
# (pick the most specific from a message's category list)
# ---------------------------------------------------------------------------

# Ordered by specificity: earlier entries win.
_CAT_MAP: dict[int, str] = {
    1001: "annual_report",
    1002: "half_year_report",
    1003: "interim_statement",   # interim report
    1004: "interim_statement",   # quarterly report
    1005: "inside_information",
    1006: "holding_notification",
    1102: "holding_notification",  # managers' transactions
    1007: "other",               # own-shares transactions
    1010: "other",               # additional regulated
}

# Priority order for resolution when a message carries several categories.
_CAT_PRIORITY = [1001, 1002, 1003, 1004, 1005, 1006, 1102, 1007, 1010]


def _doc_type(category_ids: list[int]) -> str:
    """Map a list of category ids to a DOC_TYPES member (most specific wins)."""
    for cid in _CAT_PRIORITY:
        if cid in category_ids:
            return _CAT_MAP[cid]
    return "other"


# ---------------------------------------------------------------------------
# Name normalisation helpers
# ---------------------------------------------------------------------------

# Trailing legal forms, sorted longest-first so the most specific wins. Oslo Børs
# lists Norwegian issuers (ASA/AS) and a foreign tail under Limited/Ltd/plc/Inc/
# AB/SA/SE/NV/BV/GmbH/AG/A-S; GLEIF and Oslo often spell the form differently
# ("Golden Ocean Group Limited" vs "… Ltd"), so both must collapse to the same
# core for the exact match to hold. The set is exactly the forms that actually
# occur as a trailing token in the live /issuers list (after period removal):
# oyj/incorporated never appear and were dropped; gmbh/ag do appear and were
# added. A collision (e.g. an "AS" and an "ASA" issuer sharing a core) is safe:
# _resolve_issuer_sign requires exactly one active match, so it declines rather
# than mis-binds.
_LEGAL_SUFFIXES = sorted((
    " limited", " gmbh",
    " asa", " plc", " ltd", " inc", " sa", " nv", " se", " ab", " bv", " a/s", " as", " ag",
), key=len, reverse=True)


def _normalise(name: str) -> str:
    """Casefold + collapse whitespace + strip diacritics + strip legal suffixes.

    Periods are dropped first ("Ltd." -> "ltd", "S.A." -> "sa") so the suffix
    set needs no dotted variants.
    """
    # NFD decomposition → drop combining characters (diacritics)
    nfd = unicodedata.normalize("NFD", name or "")
    ascii_approx = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    folded = " ".join(ascii_approx.casefold().replace(".", "").split())
    # Strip ONE trailing legal form (longest first).
    for suffix in _LEGAL_SUFFIXES:
        if folded.endswith(suffix):
            folded = folded[: -len(suffix)].strip()
            break
    return folded


# ---------------------------------------------------------------------------
# Module-level helper patched in tests for deterministic "today"
# ---------------------------------------------------------------------------

def _today() -> str:
    """Return today's date as YYYY-MM-DD (monkeypatched in tests)."""
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class NewsWebNO(OamSource):
    """Norway OAM backend — Oslo Børs NewsWeb JSON API.

    Resolves ``entity.name`` to an Oslo Børs issuerSign (ticker) via the
    ``/issuers`` endpoint, then paginates ``/list`` by date-window.
    """

    name = "oam-no"
    country = "NO"

    # ------------------------------------------------------------------
    # Lazy issuers cache
    # ------------------------------------------------------------------

    _issuers_cache: list[dict] | None = None

    def _get_issuers(self) -> list[dict]:
        """Return the full issuers list, fetching it once and caching."""
        if self._issuers_cache is None:
            try:
                resp = self.fetcher.post_json(_ISSUERS_URL, {})
                self._issuers_cache = (resp.get("data") or {}).get("issuers") or []
            except Exception as exc:  # noqa: BLE001
                self._record_error("issuers", _ISSUERS_URL, exc)
                self._issuers_cache = []
        return self._issuers_cache

    def _resolve_issuer_sign(self, entity: Entity) -> str | None:
        """Resolve entity.name → issuerSign via exact normalised name match.

        Returns None (and records an error) if 0 or >1 active matches.
        """
        target = _normalise(entity.name)
        matches = [
            i for i in self._get_issuers()
            if i.get("isActive") and _normalise(i.get("name", "")) == target
        ]
        if len(matches) == 1:
            return matches[0]["issuerSign"]
        self._record_error(
            "resolve",
            _ISSUERS_URL,
            RuntimeError(
                f"expected exactly 1 active issuer for name {entity.name!r} "
                f"(normalised: {target!r}); found {len(matches)}"
            ),
        )
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def discover(self, entity: Entity) -> list[Document]:
        """Return all filings for *entity* from Oslo Børs NewsWeb.

        Resolves the entity name to an Oslo Børs issuerSign, paginates
        the /list endpoint by date-window, and fetches attachment ids via
        /message for each message that carries at least one attachment.
        """
        issuer_sign = self._resolve_issuer_sign(entity)
        if not issuer_sign:
            return []

        now = datetime.now(timezone.utc).isoformat()
        docs: list[Document] = []
        seen_ids: set[int] = set()
        to_date = _today()
        windows = 0

        while True:
            url = (
                f"{_LIST_URL}?issuer={issuer_sign}"
                f"&fromDate={_FROM_DATE}&toDate={to_date}"
            )
            try:
                resp = self.fetcher.get_json(url)
            except Exception as exc:  # noqa: BLE001
                self._record_error("list", url, exc)
                break

            data = resp.get("data") or {}
            messages = data.get("messages") or []
            overflow = bool(data.get("overflow"))
            windows += 1

            for msg in messages:
                mid = msg.get("messageId")
                if mid is None or mid in seen_ids:
                    continue
                seen_ids.add(mid)

                num_atts = msg.get("numbAttachments", 0)
                if not num_atts:
                    # Skip messages with no attachments entirely
                    continue

                # Fetch attachment ids
                detail_url = f"{_MESSAGE_URL}?messageId={mid}"
                try:
                    det = self.fetcher.post_json(detail_url, {})
                    det_msg = (det.get("data") or {}).get("message") or {}
                    attachments = det_msg.get("attachments") or []
                except Exception as exc:  # noqa: BLE001
                    self._record_error("message", detail_url, exc)
                    continue

                files = []
                for att in attachments:
                    att_id = att.get("id")
                    att_name = att.get("name") or f"attachment-{att_id}"
                    if att_id is None:
                        continue
                    ext = att_name.rsplit(".", 1)[-1].lower() if "." in att_name else ""
                    kind = "esef" if ext in ("zip", "xhtml") else "document"
                    dl_url = (
                        f"{_ATTACHMENT_URL}"
                        f"?messageId={mid}&attachmentId={att_id}"
                    )
                    files.append({"name": att_name, "kind": kind, "url": dl_url})

                # Extract category ids
                raw_cats = msg.get("category") or []
                cat_ids = [c["id"] for c in raw_cats if isinstance(c, dict) and "id" in c]

                doc = Document(
                    doc_id=f"no-{mid}",
                    lei=entity.lei,
                    country="NO",
                    doc_type=_doc_type(cat_ids),
                    period_end=None,
                    published_ts=msg.get("publishedTime"),
                    discovered_ts=now,
                    language=None,
                    source=self.name,
                    files=files,
                    native_meta={
                        "title": msg.get("title"),
                        "issuerName": msg.get("issuerName"),
                        "category": raw_cats,
                    },
                )
                docs.append(doc)

            if not overflow:
                break

            # Shift toDate to the oldest publishedTime in the current batch
            if messages:
                oldest = min(
                    (m.get("publishedTime") or "9999") for m in messages
                )
                # Use only the date part (YYYY-MM-DD) to avoid repeating the boundary
                to_date = (oldest[:10] if len(oldest) >= 10 else oldest)
            else:
                # No messages but overflow true — shouldn't happen; stop to avoid loop
                break

            if windows >= _MAX_WINDOWS:
                self._record_error(
                    "truncated",
                    url,
                    RuntimeError(
                        f"reached {_MAX_WINDOWS}-window cap for issuer {issuer_sign!r}; "
                        "remaining older filings not retrieved"
                    ),
                )
                break

        return docs
