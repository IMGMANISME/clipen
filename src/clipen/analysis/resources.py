"""Keep local FFmpeg and ASR subprocesses from competing for memory/compute."""
from contextlib import contextmanager
import threading
from typing import Callable, Iterator

_SLOT = threading.Lock()


@contextmanager
def local_analysis_slot(cancel: threading.Event | None, error: Callable[[], Exception]) -> Iterator[None]:
    while True:
        if cancel is not None and cancel.is_set():
            raise error()
        if _SLOT.acquire(timeout=0.05):
            break
    try:
        if cancel is not None and cancel.is_set():
            raise error()
        yield
    finally:
        _SLOT.release()
