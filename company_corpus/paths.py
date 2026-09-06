"""Path-component safety for identifiers and filenames that come from outside.

Register identifiers and OAM-supplied filenames were used verbatim as
filesystem paths. A crafted file ``name`` escaped the document directory and
landed at the corpus root; an ABSOLUTE name wrote outside ``data_dir``
entirely and then made ``relative_to`` raise an uncaught ValueError that
aborted the whole acquire run; and ``write_register_financials_table("../../x")``
created a table two levels above ``data/`` (Rob-C7 / DI-M4). Everything that
becomes one path component goes through here.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

#: Deliberately permissive about content and strict about structure: register
#: identifiers legitimately carry letters, digits, dots and dashes in
#: country-specific shapes, but never a separator, a NUL, or a relative marker.
_UNSAFE = re.compile(r"[/\\\x00]")


class UnsafeIdentifier(ValueError):
    """A third-party identifier cannot be used as a path component."""


def safe_component(value, *, max_length: int) -> str:
    """Return ``value`` as a usable single path component, or raise.

    Raises :class:`UnsafeIdentifier` for ``None``, empty input, ``.`` / ``..``,
    anything containing a path separator or NUL, and anything longer than
    ``max_length`` (``Config.max_path_component_length``).

    ``None`` is refused explicitly: it is what a validating ``_norm_*`` helper
    returns for an identifier it could not read, and ``str(None)`` is the
    perfectly usable component ``"None"`` -- which is how a caller that forgot to
    check would have written every unreadable entity to one ``None.jsonl``.
    """
    if value is None:
        raise UnsafeIdentifier("not a usable path component: None")
    text = str(value).strip()
    if not text or text in (".", ".."):
        raise UnsafeIdentifier(f"not a usable path component: {value!r}")
    if _UNSAFE.search(text):
        raise UnsafeIdentifier(f"path separator or NUL in identifier: {value!r}")
    if len(text) > max_length:
        raise UnsafeIdentifier(
            f"identifier longer than {max_length} characters: {value!r}")
    return text


def safe_filename(name, *, url: str, max_length: int) -> str:
    """Return a safe leaf filename for a downloaded file. Never raises.

    A source that supplies a hostile filename must not cost us the document, so
    an unusable name falls back to ``sha1(url)`` plus the original suffix. The
    fallback is deterministic, so a re-run maps to the same file rather than
    downloading a second copy under a new name.
    """
    try:
        return safe_component(name, max_length=max_length)
    except UnsafeIdentifier:
        suffix = PurePosixPath(str(name or "")).suffix
        if _UNSAFE.search(suffix) or len(suffix) > 16:
            suffix = ""
        digest = hashlib.sha1((url or str(name)).encode("utf-8")).hexdigest()[:16]
        return f"{digest}{suffix}"
