"""Control-plane watchdog behind /livez.

Operations that need the control plane (an IS-05 activation, Program start and stop, a
source restart) register while they run. Each of them is bounded on its own, so one that is
still running after STUCK_AFTER_S seconds means the control plane cannot be used any more;
/livez then fails and Kubernetes restarts the pod instead of leaving it hung.
"""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

# Longest normal operation: a source restart waits up to 10 s, a Program stop up to 15 s,
# a Program start a few seconds. 60 s is well past all of them.
STUCK_AFTER_S = 60.0


class ControlPlaneWatchdog:
    def __init__(self, stuck_after_s: float = STUCK_AFTER_S) -> None:
        self.stuck_after_s = stuck_after_s
        self._lock = threading.Lock()
        self._ids = itertools.count()
        self._running: dict[int, tuple[str, float]] = {}

    def begin(self, operation: str) -> int:
        token = next(self._ids)
        with self._lock:
            self._running[token] = (operation, time.monotonic())
        return token

    def end(self, token: int) -> None:
        with self._lock:
            self._running.pop(token, None)

    @contextmanager
    def busy(self, operation: str) -> Iterator[None]:
        token = self.begin(operation)
        try:
            yield
        finally:
            self.end(token)

    def oldest(self) -> tuple[str, float] | None:
        """(operation, seconds running) of the longest-running operation, or None."""
        with self._lock:
            if not self._running:
                return None
            operation, started = min(self._running.values(), key=lambda item: item[1])
        return operation, time.monotonic() - started

    def stuck(self) -> tuple[str, float] | None:
        """The oldest operation when it has run for longer than stuck_after_s, else None."""
        oldest = self.oldest()
        if oldest is not None and oldest[1] > self.stuck_after_s:
            return oldest
        return None
