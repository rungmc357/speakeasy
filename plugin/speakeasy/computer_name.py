"""The Hermes computer's own name, the way its owner sees it ("Studio Mac"), for the apps' copy.

The apps say "paired with Hermes on Studio Mac" instead of an address: a raw IP or host name
(``127.0.0.1``, ``hermes.tail1234.ts.net``, ``calm-river.trycloudflare.com``) means nothing to most
people. macOS keeps the name from System Settings › General › Sharing (``scutil --get ComputerName``);
Linux keeps an optional pretty name (``/etc/machine-info`` PRETTY_HOSTNAME). When there's no such
name, or it only looks like a host name or an address, this answers "" and the apps fall back to
"your Hermes computer". The answer is cached: the name rarely changes and /voice/status is polled.
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

_TTL = 600.0
_lock = threading.Lock()
_cache: tuple[float, str] | None = None


def plain(name: str) -> str:
    """``name`` if it reads like a person-facing computer name, else "" (addresses, host names)."""
    name = " ".join((name or "").split())[:64]
    if not name or name.lower() in {"localhost", "localhost.localdomain"}:
        return ""
    try:
        ipaddress.ip_address(name.strip("[]"))
        return ""
    except ValueError:
        pass
    # One word with dots (a DNS name) or a machine-made host label: not what the owner calls it.
    if " " not in name and ("." in name or ":" in name or re.fullmatch(r"(ip|host|vm|node)?[-_0-9a-f]{6,}", name.lower())):
        return ""
    return name


def _mac() -> str:
    out = subprocess.run(["/usr/sbin/scutil", "--get", "ComputerName"], capture_output=True, text=True,
                         timeout=2, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def _linux(machine_info: Path = Path("/etc/machine-info")) -> str:
    try:
        for line in machine_info.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "PRETTY_HOSTNAME":
                return value.strip().strip("\"'")
    except OSError:
        pass
    return ""


def read() -> str:
    """The raw name from the OS ("" when there is none or reading it fails)."""
    try:
        if sys.platform == "darwin":
            return _mac()
        if sys.platform.startswith("linux"):
            return _linux()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def computer_name(now: float | None = None) -> str:
    """The person-facing name of this computer, or "" (cached for ten minutes)."""
    global _cache
    now = time.monotonic() if now is None else now
    with _lock:
        if _cache is not None and now - _cache[0] < _TTL:
            return _cache[1]
    name = plain(read())
    with _lock:
        _cache = (now, name)
    return name
