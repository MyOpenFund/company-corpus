"""One primitive for the register API helpers: report a swallowed failure.

The keyless register clients (``no_brreg``, ``bnb_cbso``, ``virk_api``,
``prh_api``, ``sk_registeruz``) are batch-safe by contract — they turn any
exception into ``[]`` / ``None`` so one dead entity never aborts a run. That
contract also hides a dead register behind "the issuer filed nothing" when the
*first* call for an entity is the one that dies. Each helper therefore takes an
optional ``errors`` out-parameter and calls :func:`note_error` on the caught
exception; a producer that passes a list can tell "dead" from "empty", a caller
that passes nothing keeps the historical behaviour.

This module deliberately imports nothing from the package so the helpers stay
leaf modules.
"""
from __future__ import annotations


def note_error(
    errors: "list[dict] | None", *, entity_id, source: str, stage: str, exc: BaseException,
) -> None:
    """Append ``{entity_id, source, stage, error}`` to ``errors`` (no-op when None).

    ``stage`` is the helper's own name so a coverage row or trail entry says
    *which* call died; ``error`` is ``"<ExceptionType>: <message>"``.
    """
    if errors is None:
        return
    errors.append({
        "entity_id": entity_id,
        "source": source,
        "stage": stage,
        "error": f"{type(exc).__name__}: {exc}",
    })
