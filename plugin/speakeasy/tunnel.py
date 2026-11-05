"""Cloudflare mode: reach Speakeasy without Tailscale, through a Cloudflare tunnel.

Tailscale stays the recommended (most private) way. Without it, the user's agent can give
Speakeasy a Cloudflare address: ``scripts/speakeasy-cloudflare.py`` in the Speakeasy repo (or the
manual steps in docs/AGENT_SETUP.md › Cloudflare mode) installs Cloudflare's tunnel daemon, runs a
quick tunnel to ``http://127.0.0.1:<port>`` (no Cloudflare account) as a background service of its
own (launchd on macOS, a systemd user unit on Linux) with its metrics server on loopback, and sets
``server.tunnel`` to "cloudflare". Never Tailscale Funnel.

The plugin itself never installs or starts a tunnel (Hermes' plugin install scan treats tunnel
tooling inside a plugin as high risk, and it's the user's call): it only follows the address.

A quick tunnel's address (``https://<words>.trycloudflare.com``) is public and changes whenever the
daemon restarts. ``TunnelWatcher`` (run by the server) reads it from the daemon's local metrics
server (``/quicktunnel``), falling back to its log, and when it changes:

* updates ``server.tunnel_url`` (and ``server.advertised_url`` when that was the tunnel), so
  new pairing links and ``GET /voice/endpoints`` carry it;
* publishes the encrypted address note (address_drop.py), so the app finds the new address itself;
* has Hermes message the user on their usual chat with an "Update Speakeasy's address" link that
  carries only the new URL. The app switches only after the address proves it's the same Hermes.

Permanent address (optional upgrade): a *named* tunnel on the user's own domain
(``server.tunnel_hostname``, e.g. ``speakeasy.example.com``; a free Cloudflare account and a domain,
about $10/year). Its address never changes, so there is nothing to watch.

Public-internet hardening lives in server.py (Host allow-list, minimal /health, pairing limits).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

logger = logging.getLogger(__name__)

DEFAULT_METRICS_PORT = 8797
CHECK_S = 15.0
URL_RE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com")
HOST_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com$")
NOTIFY_ATTEMPTS = 5
UPDATE_PAGE = "https://speakeasyvoice.ai/pair"


def valid_tunnel_url(url: str) -> bool:
    return bool(URL_RE.fullmatch(url or ""))


def log_path(home: Path) -> Path:
    """Where the tunnel service writes its log (scripts/speakeasy-cloudflare.py sets this up)."""
    return Path(home) / "speakeasy" / "tunnel.log"


NAME_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def valid_hostname(name: str) -> bool:
    return bool(NAME_RE.fullmatch((name or "").lower())) and not name.lower().endswith(".trycloudflare.com")


# -- reading the address ------------------------------------------------------------------------

def read_url(home: Path, metrics_port: int, timeout: float = 2.0) -> str:
    """The tunnel's current https URL: the tunnel daemon's metrics ``/quicktunnel``, else its log."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{metrics_port}/quicktunnel", timeout=timeout) as r:  # noqa: S310
            host = str(json.loads(r.read() or b"{}").get("hostname") or "").lower()
        if HOST_RE.fullmatch(host):
            return f"https://{host}"
    except Exception:
        pass
    try:
        with log_path(home).open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 256 * 1024))
            found = URL_RE.findall(f.read().decode("utf-8", "replace"))
        return found[-1] if found else ""
    except OSError:
        return ""


def wait_for_url(home: Path, metrics_port: int, timeout_s: float = 45.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> str:
    deadline = clock() + timeout_s
    while True:
        url = read_url(home, metrics_port)
        if url or clock() >= deadline:
            return url
        sleep(1.0)


# -- address changes ----------------------------------------------------------------------------

def update_link(url: str) -> str:
    """An "Update Speakeasy's address" link: only the new URL, no code (it pairs nothing)."""
    return f"{UPDATE_PAGE}#server={quote(url, safe='')}&update=1"


def update_message(url: str) -> str:
    return ("Speakeasy's address changed (the Cloudflare tunnel restarted). Open this link on your iPhone "
            "to update it; the app checks it's really your Hermes before switching:\n"
            f"{update_link(url)}")


def apply_new_url(settings: Any, url: str) -> bool:
    """Store a newly seen tunnel URL. True when it changed (and the old one was set)."""
    s = settings.get()["server"]
    old = s.get("tunnel_url", "")
    if url == old:
        return False
    patch: dict[str, Any] = {"tunnel_url": url}
    if not s["advertised_url"] or s["advertised_url"] == old:
        patch["advertised_url"] = url
    settings.patch({"server": patch})
    logger.info("speakeasy: Cloudflare tunnel address is now %s", url)
    return bool(old)


class TunnelWatcher:
    """Follows the quick tunnel's address while ``server.tunnel`` is "cloudflare"."""

    def __init__(self, service: Any, *, check_s: float = CHECK_S,
                 reader: Callable[[Path, int], str] = read_url,
                 send: Callable[[str, str], bool] | None = None,
                 drop: Callable[[str, str], bool] | None = None):
        self.service, self.check_s, self.reader = service, check_s, reader
        self._send = send
        self._drop = drop   # the address-drop publisher (tests replace it)
        self.pending = ""        # a new URL whose message hasn't gone out yet
        self.attempts = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="speakeasy-tunnel-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.check_s):
            try:
                self.tick()
            except Exception as exc:
                logger.warning("speakeasy: tunnel check failed (%s)", type(exc).__name__)

    def send(self, target: str, text: str) -> bool:
        if self._send is not None:
            return self._send(target, text)
        from .delivery import HermesSendNotifier
        return HermesSendNotifier(self.service.home).send(target, text)

    def tick(self) -> None:
        settings = self.service.settings
        s = settings.get()
        if s["server"].get("tunnel") != "cloudflare" or s["server"].get("tunnel_hostname"):
            return  # off, or a named tunnel: its address never changes
        url = self.reader(self.service.home, int(s["server"].get("tunnel_metrics_port") or DEFAULT_METRICS_PORT))
        if valid_tunnel_url(url) and apply_new_url(settings, url):
            self.pending, self.attempts = url, 0
        from . import address_drop
        address_drop.publish_safely(self.service.home, settings.get(), **({"send": self._drop} if self._drop else {}))
        if self.pending and self.pending != settings.get()["server"].get("tunnel_url"):
            self.pending = ""   # superseded by an even newer address: that one gets its own message
        if not self.pending or self.attempts >= NOTIFY_ATTEMPTS:
            return
        target = s["delivery"]["target"]
        if not target or target == "none":
            logger.warning("speakeasy: tunnel address changed but no chat is set to tell the user; "
                           "`hermes voice pair` shows the new link")
            self.pending = ""
            return
        self.attempts += 1
        if self.send(target, update_message(self.pending)):
            logger.info("speakeasy: sent the new address to %s", target.split(":", 1)[0])
            self.pending = ""


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()
