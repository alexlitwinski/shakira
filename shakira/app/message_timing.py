"""Metricas de latencia por mensagem (rolling window em memoria)."""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

_MAX_SAMPLES = 50


@dataclass
class MessageTimings:
    phone: str = ""
    ha_states_ms: float = 0.0
    gemini_ms: float = 0.0
    gemini_calls: int = 0
    wa_steps: int = 0
    total_ms: float = 0.0
    _started: float = field(default_factory=time.monotonic, repr=False)

    def mark_ha_done(self, ha_ms: float) -> None:
        self.ha_states_ms = ha_ms

    def add_gemini(self, ms: float) -> None:
        self.gemini_ms += ms
        self.gemini_calls += 1

    def finish(self, *, wa_steps: int = 0) -> None:
        self.wa_steps = wa_steps
        self.total_ms = (time.monotonic() - self._started) * 1000.0
        _record_sample(self)


_lock = threading.Lock()
_recent: deque[MessageTimings] = deque(maxlen=_MAX_SAMPLES)


def _record_sample(t: MessageTimings) -> None:
    with _lock:
        _recent.append(t)


def recent_averages() -> dict[str, float]:
    with _lock:
        samples = list(_recent)
    if not samples:
        return {}
    n = len(samples)
    return {
        "samples": float(n),
        "avg_total_ms": sum(s.total_ms for s in samples) / n,
        "avg_ha_ms": sum(s.ha_states_ms for s in samples) / n,
        "avg_gemini_ms": sum(s.gemini_ms for s in samples) / n,
        "avg_gemini_calls": sum(s.gemini_calls for s in samples) / n,
    }
