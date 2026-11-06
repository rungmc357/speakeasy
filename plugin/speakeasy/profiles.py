"""Which Hermes profile a voice server belongs to, and a port no other profile uses.

Every profile used to default to port 8795. With two profiles, setup in the second one found the
first one's server on that port, called it "running", and minted a pairing code the other server
had never heard of, so the phone was told its link had expired. Now the local ``/health`` answer
names its profile (a hash of its folder plus the profile name), setup checks it is talking to its
own server, and a profile whose port is taken by another profile moves to a free one.

Other profiles' config.yaml files are only read, never written.
"""
from __future__ import annotations

import hashlib
import json
import socket
import urllib.request
from pathlib import Path
from typing import Callable

from .settings import DEFAULT_PORT, hermes_config

PORT_STEP = 10      # 8795, 8805, 8815 …: room for the Cloudflare metrics port (+2) beside each one
PORT_TRIES = 40


def home_id(home: Path) -> str:
    """Stable id for a Hermes home: never the path itself (``/health`` is local-only, but still)."""
    return hashlib.sha256(str(Path(home).expanduser().resolve()).encode()).hexdigest()[:12]


def profile_name(home: Path) -> str:
    p = Path(home).expanduser().resolve()
    return p.name if p.parent.name == "profiles" else "default"


def hermes_root(home: Path) -> Path:
    p = Path(home).expanduser().resolve()
    return p.parent.parent if p.parent.name == "profiles" else p


def other_homes(home: Path) -> list[Path]:
    """The other profiles of the same Hermes install (the root home and ``profiles/*``)."""
    me = Path(home).expanduser().resolve()
    root = hermes_root(me)
    found = [root] + sorted(d for d in (root / "profiles").glob("*") if d.is_dir())
    return [d.resolve() for d in found if d.resolve() != me and (d / "config.yaml").exists()]


def configured_port(home: Path) -> int | None:
    """The voice port this profile's config.yaml sets, None when it sets none."""
    try:
        voice = hermes_config(home)["gateway"]["platforms"]["voice"]
    except (KeyError, TypeError):
        return None
    if not isinstance(voice, dict):
        return None
    try:
        return int((voice.get("extra") or {})["port"])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_PORT if voice.get("enabled") else None


def claimed_ports(home: Path) -> dict[int, str]:
    """Port → profile name, for every other profile with Speakeasy's voice platform configured."""
    claimed: dict[int, str] = {}
    for other in other_homes(home):
        port = configured_port(other)
        if port:
            claimed.setdefault(port, profile_name(other))
    return claimed


def health_identity(url: str) -> dict | None:
    """The local ``/health`` answer as a dict; None when nothing (or not Speakeasy) answers."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=2) as r:  # noqa: S310 (loopback)
            if r.status != 200:
                return None
            data = json.loads(r.read() or b"{}")
    except Exception:
        return None
    return data if isinstance(data, dict) and data.get("platform") == "voice" else None


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def whose(home: Path, ident: dict | None, port: int, claimed: dict[int, str]) -> str:
    """"ours", "none" (nothing answers) or the other profile's name for what answers on ``port``.
    An older Speakeasy that doesn't name its profile counts as ours unless another profile's
    config claims that port."""
    if ident is None:
        return "none"
    if ident.get("home_id"):
        return "ours" if ident["home_id"] == home_id(home) else str(ident.get("profile") or "another profile")
    return claimed.get(port, "ours")


def choose_port(home: Path, *, identity: Callable[[str], dict | None] = health_identity,
                free: Callable[[int], bool] = port_free) -> tuple[int, str]:
    """(port this profile should use, the profile that held its old port or "").

    Keeps the configured (or default) port unless another profile claims it or another profile's
    server answers there; then picks the next port no profile claims and nothing listens on."""
    claimed = claimed_ports(home)
    start = configured_port(home) or DEFAULT_PORT
    owner = whose(home, identity(f"http://127.0.0.1:{start}"), start, claimed)
    if owner == "none" and start not in claimed and not free(start):
        owner = "another program"  # something that isn't Speakeasy holds the port
    if owner == "ours" or (owner == "none" and start not in claimed):
        return start, ""
    holder = owner if owner != "none" else claimed[start]
    for i in range(1, PORT_TRIES + 1):
        port = start + i * PORT_STEP
        if port in claimed or port + 2 in claimed:
            continue
        ident = identity(f"http://127.0.0.1:{port}")
        if whose(home, ident, port, claimed) == "ours" or (ident is None and free(port) and free(port + 2)):
            return port, holder
    return start, holder
