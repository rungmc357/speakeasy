"""Getting the pairing link to the Mac the user will talk from.

Hermes may run on the Mac the user talks from (setup opens the link there) or on another
computer. For another computer, setup sends one link to a chat the user already has with their
agent. That link goes to a page on speakeasyvoice.ai which opens Speakeasy if it's installed, or
offers the download first. The server address and code sit in the URL fragment (after ``#``), which browsers
never send to the web server.

Setup also never restarts Hermes. When the voice server isn't running yet, setup records who
asked (``pending_link.json``) and the user restarts Hermes themselves. On the next start the voice
platform finds that record and sends the link, so nobody has to come back and ask for it.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

logger = logging.getLogger(__name__)

PAIR_PAGE = "https://speakeasyvoice.ai/pair"
DOWNLOAD_PAGE = "https://speakeasyvoice.ai/#download"
LINK_TTL_S = 30 * 60          # long enough to download and install the app first
PUBLIC_LINK_TTL_S = 10 * 60   # with a Cloudflare tunnel the server is on the public internet: shorter


def link_ttl(home: Path) -> int:
    """How long a new pairing code lasts: 10 minutes once a Cloudflare tunnel is on, else 30."""
    from .settings import Settings
    return PUBLIC_LINK_TTL_S if Settings(home).get()["server"]["tunnel"] else LINK_TTL_S
PENDING_MAX_AGE_S = 24 * 3600  # a record older than a day is stale; drop it
DELIVERY_DELAY_S = 15.0        # let the chat platforms reconnect after a restart
DELIVERY_ATTEMPTS = 3


def app_link(server: str, code: str) -> str:
    """Opens Speakeasy directly (only useful on the Mac that has it installed)."""
    return f"speakeasy://pair?server={quote(server, safe='')}&code={code}"


def web_link(server: str, code: str) -> str:
    """Works from any chat on any Mac: opens the app, or offers the download first."""
    return f"{PAIR_PAGE}#server={quote(server, safe='')}&code={code}"


def chat_message(link: str, minutes: int = LINK_TTL_S // 60) -> str:
    return ("Speakeasy is ready. On the Mac or iPhone you'll talk from, open this link. It connects the app, "
            "and offers the download first if you don't have it yet.\n"
            f"{link}\n"
            f"(Works once, for {minutes} minutes. Ask me for a new one any time.)")


def _pending_path(home: Path) -> Path:
    return Path(home) / "speakeasy" / "pending_link.json"


def save_pending(home: Path, target: str, now: float | None = None) -> None:
    path = _pending_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"target": target, "created": time.time() if now is None else now}))


def pending_target(home: Path, now: float | None = None) -> str:
    try:
        data = json.loads(_pending_path(home).read_text())
    except (OSError, ValueError):
        return ""
    now = time.time() if now is None else now
    if not isinstance(data, dict) or now - float(data.get("created") or 0) > PENDING_MAX_AGE_S:
        clear_pending(home)
        return ""
    return str(data.get("target") or "")


def clear_pending(home: Path) -> None:
    try:
        _pending_path(home).unlink()
    except OSError:
        pass


def deliver_pending(home: Path, server: str, *, send: Callable[[str, str], bool],
                    new_code: Callable[[], str], sleep: Callable[[float], None] = time.sleep,
                    delay: float = DELIVERY_DELAY_S, attempts: int = DELIVERY_ATTEMPTS) -> bool:
    """Send the pairing link recorded by setup. True once sent (the record is then removed)."""
    target = pending_target(home)
    if not target:
        return False
    for attempt in range(attempts):
        sleep(delay * (attempt + 1))
        if send(target, chat_message(web_link(server, new_code()), link_ttl(home) // 60)):
            clear_pending(home)
            logger.info("speakeasy: sent the pairing link to %s", target.split(":", 1)[0])
            return True
    logger.warning("speakeasy: couldn't send the pairing link; `hermes voice pair --send` makes a new one")
    return False


def start_pending_delivery(home: Path, server: str) -> None:
    """Called when the voice platform starts. Returns at once; the send happens in the background."""
    if not pending_target(home):
        return
    from .delivery import HermesSendNotifier
    from .devices import DeviceStore
    notifier = HermesSendNotifier(home)
    store = DeviceStore(home)
    thread = threading.Thread(
        target=deliver_pending, name="speakeasy-pairing-link", daemon=True,
        kwargs=dict(home=home, server=server, send=notifier.send,
                    new_code=lambda: store.new_pairing_code(ttl=link_ttl(home))))
    thread.start()
