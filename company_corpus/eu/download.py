"""Download every file of a Document and write a provenance manifest.

Raw layout mirrors the US pillar: data/raw/<LEI>/<DOC_FAMILY>/<year>/<doc_id>/<file>.
Idempotent: a file whose on-disk sha256 already matches is not re-downloaded.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from ..config import Config
from ..paths import safe_filename
from ..storage import data_file_mode
from .documents import DOC_FAMILY, Document


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def download_document(doc: Document, *, fetcher, config: Config) -> dict:
    # Every one of these four is third-party metadata (an OAM's LEI field, a
    # published_ts we slice for the year, a doc_id built from native ids), and
    # each one is a path component. ``safe_filename`` rather than
    # ``safe_component`` so a hostile value costs us a pretty directory name and
    # not the document itself; the fallback is a deterministic hash, so a re-run
    # lands on the same directory instead of downloading a second copy
    # (Rob-C7 / DI-M4).
    def _dir(value) -> str:
        return safe_filename(value, url=str(value),
                             max_length=config.max_path_component_length)

    lei = _dir(doc.lei or "UNRESOLVED")
    fam = _dir(DOC_FAMILY.get(doc.doc_type, "OTHER"))
    year = _dir(str(doc.period_end.year) if doc.period_end else (
        (doc.published_ts or "")[:4] or "unknown"))
    doc_dir = _dir(doc.doc_id)
    base = config.raw_dir / lei / fam / year / doc_dir
    base.mkdir(parents=True, exist_ok=True)

    files_out = []
    for f in doc.files:
        if f.get("content") is None and not f.get("url"):
            # Index-only file: nothing to fetch and no stable URL to retry (e.g. a DE
            # capture-at-discovery that failed — re-fetching its session-bound link
            # later would persist a stale page). Record it without downloading.
            files_out.append({k: v for k, v in f.items() if k != "content"})
            continue
        dest = base / safe_filename(
            f.get("name") or (f.get("url") or "file").rsplit("/", 1)[-1],
            url=f.get("url") or "",
            max_length=config.max_path_component_length)
        try:
            if not dest.exists():
                # A unique staging name per call: a fixed "<name>.part" sibling
                # meant two concurrent runs fetching the same document shared one
                # staging path, so one os.replace stole the other's bytes and the
                # loser died with FileNotFoundError (DI-I1 / Rob-I9).
                fd, tmp_name = tempfile.mkstemp(dir=dest.parent,
                                                prefix=f"{dest.name}.", suffix=".part")
                # mkstemp hardcodes 0o600. Both writers below reopen this same
                # inode in "wb" (truncate, not recreate), so the mode set here is
                # the mode the finished raw file keeps -- and a raw corpus the
                # ingester's account cannot read is useless.
                os.fchmod(fd, data_file_mode())
                os.close(fd)
                tmp = Path(tmp_name)
                try:
                    # Backends whose source has no stable, re-fetchable URL (e.g. the
                    # Bundesanzeiger's session-bound Wicket links) capture the bytes at
                    # discovery time and pass them inline via "content"; write those
                    # directly instead of re-fetching.
                    content = f.get("content")
                    if content is not None:
                        tmp.write_bytes(content.encode("utf-8")
                                        if isinstance(content, str) else content)
                    else:
                        fetcher.download(f["url"], tmp)
                    os.replace(tmp, dest)
                except BaseException:
                    # BaseException, as in storage._atomic_write_text: a Ctrl-C or
                    # a SystemExit mid-download must not leave the staging file
                    # behind either.
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
            sha = _sha256_file(dest)
            # Inside the try, with the sha: a dest that is somehow not under
            # data_dir made relative_to raise an uncaught ValueError that took
            # down the whole acquire run instead of costing one file (Rob-C7).
            rel = str(dest.relative_to(config.data_dir))
        except Exception as exc:  # noqa: BLE001
            files_out.append({k: v for k, v in f.items() if k != "content"}
                             | {"error": str(exc)})
            continue
        files_out.append({"name": dest.name, "url": f.get("url"), "kind": f.get("kind"),
                          "sha256": sha, "path": rel})

    # ``doc_id`` / ``lei`` are the SANITISED components, not the raw ones:
    # ``acquire._discard_download`` rebuilds ``manifest/<lei>/<doc_id>.json``
    # from these two fields, so a manifest whose body disagreed with its own
    # path would leave an orphan behind. For every legitimate value the two
    # spellings are identical; the raw spelling survives in ``native_meta`` and
    # in each file's ``url``.
    manifest = {
        "doc_id": doc_dir, "lei": lei, "country": doc.country, "doc_type": doc.doc_type,
        "period_end": doc.period_end.isoformat() if doc.period_end else None,
        "published_ts": doc.published_ts, "discovered_ts": doc.discovered_ts,
        "language": doc.language, "source": doc.source, "files": files_out,
        "native_meta": doc.native_meta,
    }
    mpath = config.data_dir / "manifest" / lei / f"{doc_dir}.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=2, default=str))
    return manifest
