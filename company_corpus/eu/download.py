"""Download every file of a Document and write a provenance manifest.

Raw layout mirrors the US pillar: data/raw/<LEI>/<DOC_FAMILY>/<year>/<doc_id>/<file>.
Idempotent: a file whose on-disk sha256 already matches is not re-downloaded.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ..config import Config
from .documents import DOC_FAMILY, Document


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    h.update(p.read_bytes())
    return h.hexdigest()


def download_document(doc: Document, *, fetcher, config: Config) -> dict:
    lei = doc.lei or "UNRESOLVED"
    fam = DOC_FAMILY.get(doc.doc_type, "OTHER")
    year = str(doc.period_end.year) if doc.period_end else (
        (doc.published_ts or "")[:4] or "unknown")
    base = config.raw_dir / lei / fam / year / doc.doc_id
    base.mkdir(parents=True, exist_ok=True)

    files_out = []
    for f in doc.files:
        if f.get("content") is None and not f.get("url"):
            # Index-only file: nothing to fetch and no stable URL to retry (e.g. a DE
            # capture-at-discovery that failed — re-fetching its session-bound link
            # later would persist a stale page). Record it without downloading.
            files_out.append({k: v for k, v in f.items() if k != "content"})
            continue
        dest = base / (f.get("name") or (f.get("url") or "file").rsplit("/", 1)[-1])
        try:
            if not dest.exists():
                tmp = dest.with_name(dest.name + ".part")
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
                except Exception:
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:  # noqa: BLE001
                        pass
                    raise
            sha = _sha256_file(dest)
        except Exception as exc:  # noqa: BLE001
            files_out.append({k: v for k, v in f.items() if k != "content"}
                             | {"error": str(exc)})
            continue
        files_out.append({"name": dest.name, "url": f.get("url"), "kind": f.get("kind"),
                          "sha256": sha, "path": str(dest.relative_to(config.data_dir))})

    manifest = {
        "doc_id": doc.doc_id, "lei": lei, "country": doc.country, "doc_type": doc.doc_type,
        "period_end": doc.period_end.isoformat() if doc.period_end else None,
        "published_ts": doc.published_ts, "discovered_ts": doc.discovered_ts,
        "language": doc.language, "source": doc.source, "files": files_out,
        "native_meta": doc.native_meta,
    }
    mpath = config.data_dir / "manifest" / lei / f"{doc.doc_id}.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=2, default=str))
    return manifest
