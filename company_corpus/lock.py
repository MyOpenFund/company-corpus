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

SMB/CIFS (the usual NAS export) is NOT supported: ``flock`` there is either
refused outright or emulated per-client, so it cannot serialise two hosts. The
lock is in any case *advisory* -- it stops two company-corpus runs, never an
unrelated process editing the same files. The corpus must therefore be written
from ONE host; other hosts may read it.
"""
from __future__ import annotations

import errno
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

#: How long to sleep between attempts while waiting for a held lock. Short
#: enough that a freed lock is picked up without a human noticing the delay,
#: long enough that a long wait costs a few thousand syscalls rather than a core.
LOCK_POLL_SECONDS = 0.05

#: The only errnos that mean "someone else holds it". Everything else out of
#: ``flock`` is a filesystem that cannot lock at all (ENOTSUP/EOPNOTSUPP on
#: SMB, ENOLCK when the kernel's lock table is full, EINVAL on some NFS
#: mounts) -- a condition no amount of waiting can resolve.
#:
#: ``EACCES`` is deliberately NOT here. ``flock`` never returns it (``lockf``
#: does), so it bought nothing -- and a superset is not harmless on this set:
#: anything that did reach us as EACCES would be waited out for
#: ``lock_wait_seconds`` and then blamed on a holder that does not exist,
#: instead of being reported as a corpus that cannot be locked at all.
_CONTENDED_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK})


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
            except OSError as exc:
                if exc.errno not in _CONTENDED_ERRNOS:
                    # Not contention: the filesystem cannot lock. Waiting would
                    # never succeed, and reporting it as a busy corpus would hide
                    # a data directory on which single-writer does not hold.
                    raise OSError(
                        exc.errno,
                        f"cannot take the corpus lock on {path}: {exc.strerror} — "
                        f"is {path.parent} on a filesystem that supports flock?",
                    ) from exc
                if time.monotonic() >= deadline:
                    raise CorpusLocked(
                        f"another company-corpus run holds {path} ({_holder(path)})"
                    ) from None
                time.sleep(LOCK_POLL_SECONDS)
        # Serialise first, then truncate + write in one syscall: the window in
        # which the file is empty (and _holder() tells a contender "holder
        # unknown") is one write wide, not a gethostname + json.dumps wide.
        record = json.dumps({
            "pid": os.getpid(), "host": socket.gethostname(), "purpose": purpose,
            "started": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, record)
        yield path
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            # Closing the fd releases the lock anyway, and a filesystem that
            # refused the lock refuses the unlock too -- that must not mask
            # whatever we are already unwinding with.
            pass
        finally:
            os.close(fd)
