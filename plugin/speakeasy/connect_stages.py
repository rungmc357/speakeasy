"""Where a call's connect time goes: one ``call.connect stages ... total=`` log line per session.

``create_session`` marks each stage as it finishes; the voice transport adds its own (spawn,
initialize, thread, realtime) and whether a pre-warmed spare was used. Read it in agent.log next to
the phone's mic-check "connect timeline" to tell server time from network/phone time.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class ConnectStages:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.began = self.last = clock()
        self.parts: list[tuple[str, int]] = []
        self.notes: list[tuple[str, str]] = []

    def mark(self, name: str) -> None:
        """The stage *name* just finished: its time is everything since the previous mark."""
        now = self.clock()
        self.parts.append((name, int((now - self.last) * 1000)))
        self.last = now

    def add(self, timings: dict[str, Any] | None, prefix: str = "") -> None:
        """Stages timed elsewhere (the transport's own); they are part of the last mark's span."""
        for name, ms in (timings or {}).items():
            if isinstance(ms, (int, float)):
                self.parts.append((prefix + name, int(ms)))

    def note(self, name: str, value: Any) -> None:
        self.notes.append((name, str(value)))

    def total_ms(self) -> int:
        return int((self.clock() - self.began) * 1000)

    def line(self) -> str:
        parts = " ".join(f"{name}={ms}ms" for name, ms in self.parts)
        notes = " ".join(f"{name}={value}" for name, value in self.notes)
        return " ".join(x for x in ("call.connect stages", parts, notes, f"total={self.total_ms()}ms") if x)

    def log(self) -> str:
        line = self.line()
        logger.info("speakeasy: %s", line)
        return line
