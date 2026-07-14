from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


class WriterLockTimeout(TimeoutError):
    pass


@contextmanager
def writer_lock(lock_path: Path, *, timeout_seconds: float = 10.0) -> Iterator[IO[str]]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WriterLockTimeout(f"等待 Hub 写锁超时：{lock_path}")
                time.sleep(0.05)
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={__import__('os').getpid()}\n")
        handle.flush()
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
