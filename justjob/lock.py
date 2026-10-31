"""One pipeline run at a time: the hourly refresh and the full run write the same database."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt

    def _lock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(f) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def run_lock(path: Path, wait: float = 0):
    """Yields True while holding the lock, or False if another run still held it after `wait`
    seconds. It's an OS file lock, so a crashed or killed run never leaves a stale one behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                _lock(f)
                break
            except OSError:
                left = deadline - time.monotonic()
                if left <= 0:
                    yield False
                    return
                time.sleep(min(10.0, left))
        try:
            yield True
        finally:
            _unlock(f)
    finally:
        f.close()
