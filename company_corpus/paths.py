"""Path-component safety for identifiers and filenames that come from outside.

Register identifiers and OAM-supplied filenames were used verbatim as
filesystem paths. A crafted file ``name`` escaped the document directory and
landed at the corpus root; an ABSOLUTE name wrote outside ``data_dir``
entirely and then made ``relative_to`` raise an uncaught ValueError that
aborted the whole acquire run; and ``write_register_financials_table("../../x")``
created a table two levels above ``data/`` (Rob-C7 / DI-M4). Everything that
becomes one path component goes through here.

Scope is POSIX: the corpus is written on Linux/macOS (the run lock is
``fcntl.flock``), so the Windows-only filename hazards -- reserved DEVICE names
(``CON``, ``NUL``, ``COM1``…) and trailing dot/space, which Win32 silently
strips -- are deliberately NOT handled. Porting the writers to Windows means
extending :func:`safe_component`, not just swapping the lock.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath

#: Deliberately permissive about content and strict about structure: register
#: identifiers legitimately carry letters, digits, dots and dashes in
#: country-specific shapes, but never a separator, a NUL, or a relative marker.
_UNSAFE = re.compile(r"[/\\\x00]")

#: Longest suffix :func:`safe_filename` will carry over from a rejected name onto
#: its hash fallback. Real document extensions are short (``.html``, ``.xhtml``,
#: ``.json``); anything longer is not an extension but the tail of a hostile name
#: riding along, so it is dropped rather than trusted.
_FALLBACK_SUFFIX_MAX = 16

#: Hex characters of the sha1 kept as the fallback stem: 16 hex = 64 bits, which
#: makes a collision between two hostile names in one corpus negligible while
#: keeping the directory listing readable. Both bounds stay well inside
#: ``Config.max_path_component_length`` (128).
_FALLBACK_DIGEST_HEX = 16


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
        if _UNSAFE.search(suffix) or len(suffix) > _FALLBACK_SUFFIX_MAX:
            suffix = ""
        digest = hashlib.sha1(
            (url or str(name)).encode("utf-8")).hexdigest()[:_FALLBACK_DIGEST_HEX]
        return f"{digest}{suffix}"
