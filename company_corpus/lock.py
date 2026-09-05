"""A single-writer lock over one corpus data directory.

Two overlapping runs (a cron firing while last night is still going, or an
operator running a command by hand) used to lose rows silently: the manifest
merge is read-modify-rewrite with no lock, and the atomic-write temp file had a
fixed name, so one writer replaced the other's temp file into place and the
other died with FileNotFoundError (DI-I1 / Rob-I9 / Contract I-7).

There is deliberately NO stale-lock timeout: ``flock`` is released by the kernel
the moment the holding process dies, so a lock left behind by a crash is not a
state that can occur on a local filesystem, and a timeout would only give us a
way to break a *live* lock. Caveat for a networked data directory: ``flock`` is
honoured over NFSv4, and over NFSv3 only with ``local_lock``; the corpus is
single-host today.
"""
from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

LOCK_FILENAME = ".corpus.lock"


class CorpusLocked(RuntimeError):
    """Another process holds the corpus lock."""


def lock_path(config: Config) -> Path:
    return config.data_dir / LOCK_FILENAME


def _holder(path: Path) -> str:
    """Human-readable description of whoever wrote the lock file last."""
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "holder unknown"
    if not isinstance(info, dict):
        return "holder unknown"
    return (f"pid {info.get('pid')} on {info.get('host')}, command "
            f"{info.get('purpose')!r}, started {info.get('started')}")


@contextmanager
def corpus_lock(config: Config, *, purpose: str):
    """Hold the exclusive writer lock on ``config.data_dir`` for the block.

    Waits at most ``config.lock_wait_seconds`` (default 0.0: fail immediately),
    then raises :class:`CorpusLocked` naming the holder. Read-only commands must
    not take this lock.
    """
    path = lock_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(0.0, config.lock_wait_seconds)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise CorpusLocked(
                        f"another company-corpus run holds {path} ({_holder(path)})"
                    ) from None
                time.sleep(0.05)
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(), "purpose": purpose,
            "started": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8"))
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
