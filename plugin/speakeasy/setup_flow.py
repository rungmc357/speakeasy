"""`hermes voice setup`: one command that leaves the Mac ready to pair.

Order matters. The pairing link opens the Mac app, and the app pairs immediately, so the link is
only opened once the voice server actually answers:

  1. Turn on what Speakeasy needs in this profile (Hermes API server on loopback, voice platform).
  2. Voice sign-in: ChatGPT through Codex (default), or an OpenAI API key.
  3. Check whether the voice server is already running. Setup never restarts or starts Hermes:
     that is the user's call, so it says a restart is needed and stops there.
  4. Publish the voice server on the user's tailnet when Tailscale is connected (auto-detected;
     ``--no-tailscale`` skips it) and remember that URL for `hermes voice pair`. Without Tailscale,
     show the Cloudflare address, or tell the agent to turn Cloudflare mode on (cli._cloudflare).
  5. Ask Hermes to write the voice brief, then hand over the pairing link: opened directly when
     the Mac is this machine, else sent to a chat (see handoff.py). When Hermes still needs a
     restart, the link is sent automatically once it's back.

Every external command goes through ``Env.run`` so tests can replace it. Nothing here prints a
secret; the API key prompt is hidden and the key is stored only in this profile, through Hermes'
own env writer (``hermes_cli.config.save_env_value``).
"""
from __future__ import annotations

import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .settings import OPENAI_KEY_NAME, Settings, codex_candidates, codex_too_old, find_codex, openai_api_key, save_hermes_secret

HEALTH_TIMEOUT_S = 60


@dataclass
class Env:
    """Side effects, injectable for tests."""
    run: Callable[..., Any] = subprocess.run
    ask: Callable[[str], str] = input
    secret: Callable[[str], str] = getpass.getpass
    out: Callable[[str], None] = print
    interactive: bool = field(default_factory=lambda: sys.stdin.isatty())
    health: Callable[[str], bool] | None = None
    sleep: Callable[[float], None] = time.sleep
    which: Callable[[str], str | None] = shutil.which
    clock: Callable[[], float] = time.monotonic

    def confirm(self, question: str, default: bool = True, assume: bool | None = None) -> bool:
        if assume is not None:
            return assume
        if not self.interactive:
            return default
        hint = "[Y/n]" if default else "[y/N]"
        answer = self.ask(f"{question} {hint} ").strip().lower()
        return default if not answer else answer in {"y", "yes"}


# -- 2. voice sign-in -------------------------------------------------------------------------------

def codex_signed_in(env: Env, binary: Path) -> bool:
    from .codex_transport import child_env
    try:
        done = env.run([str(binary), "login", "status"], capture_output=True, text=True, timeout=15,
                       env=child_env())
    except (OSError, subprocess.SubprocessError):
        return False
    text = f"{getattr(done, 'stdout', '')}\n{getattr(done, 'stderr', '')}".lower()
    return getattr(done, "returncode", 1) == 0 and "logged in" in text and "not logged in" not in text


def install_codex(env: Env) -> bool:
    """Install the Codex CLI with Homebrew or npm, whichever is present."""
    # npm first, into Speakeasy's own folder: always current, no sudo, doesn't touch a global
    # Codex. Homebrew's formula can lag behind the voice API.
    if env.which("npm"):
        target = Path.home() / ".hermes/speakeasy/codex"
        target.mkdir(parents=True, exist_ok=True)
        cmd = ["npm", "install", "--prefix", str(target), "@openai/codex@latest"]
    elif env.which("brew"):
        cmd = ["brew", "upgrade", "codex"] if env.which("codex") else ["brew", "install", "codex"]
    else:
        env.out("  Neither Homebrew nor npm is installed, so Codex can't be installed automatically.")
        env.out("  Install it from https://developers.openai.com/codex/cli, then run `hermes voice setup` again.")
        return False
    env.out(f"  Running: {' '.join(cmd)}")
    try:
        return getattr(env.run(cmd, timeout=900), "returncode", 1) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ask_api_key(env: Env, home: Path) -> bool:
    if not env.interactive:
        env.out(f"  Put your OpenAI API key in this Hermes profile as {OPENAI_KEY_NAME} (its .env, or "
                f"`hermes config set {OPENAI_KEY_NAME} <key>`), then run setup again.")
        return False
    key = env.secret("  Paste your OpenAI API key (hidden, stored only in this Hermes profile's .env): ").strip()
    if not key.startswith("sk-") or len(key) < 20:
        env.out("  That doesn't look like an OpenAI API key (they start with sk-). Skipped.")
        return False
    try:
        save_hermes_secret(home, OPENAI_KEY_NAME, key)
    except ValueError as exc:  # Hermes refused the write (e.g. the key is administrator-managed)
        env.out(f"  Hermes did not save the key: {exc}")
        return False
    env.out("  Saved. Calls will use your OpenAI API key and bill that account.")
    return True


def find_signed_in_codex(env: Env, settings: Settings) -> dict:
    """Every Codex copy on this machine, checked for a working sign-in before anyone is asked
    (codex_login.discover). The first copy signed in with ChatGPT is saved as voice.codex_path."""
    from .codex_login import discover
    from .codex_transport import login_status
    status = lambda b: login_status(b, runner=env.run, ttl_s=0)  # noqa: E731
    default = find_codex(settings.get()["voice"]["codex_path"])
    found = discover(default, status, codex_candidates(), codex_too_old)
    if found["kind"] == "chatgpt" and found["changed"]:
        settings.patch({"voice": {"codex_path": str(found["binary"])}})
    return found


REMOTE_RETRY = "Run `hermes voice setup --yes --sign-in remote` again for a new code."


def remote_sign_in(env: Env, home: Path, binary: Path, *, wait_s: float | None = None) -> bool:
    """Codex's device-code sign-in for a machine nobody is sitting at: print ONE machine-readable
    line (``SIGN_IN url=… code=… expires_in=…``), wait, and return True once Codex is signed in.
    Never opens a browser; the user approves on any device (their phone)."""
    from .codex_login import CodexLogin, CodexLoginError, SIGNED_IN, PENDING
    from .codex_transport import _LOGIN_CACHE, mark_signed_in
    flow = CodexLogin(home / "speakeasy" / "codex-login",
                      on_signed_in=lambda b: (_LOGIN_CACHE.pop(str(b), None), mark_signed_in(b)))
    try:
        started = flow.start(binary)
    except CodexLoginError as exc:
        env.out(f"✗ {exc}")
        env.out(f"  {REMOTE_RETRY}")
        return False
    env.out(f"SIGN_IN url={started['verification_url']} code={started['user_code']} "
            f"expires_in={started['expires_in']}")
    env.out(f"  Open {started['verification_url']} on any device, sign in to ChatGPT, and enter code "
            f"{started['user_code']}. Waiting…")
    deadline = None if wait_s is None else env.clock() + wait_s
    try:
        while True:
            state = flow.status()
            if state["state"] != PENDING:
                break
            if deadline is not None and env.clock() >= deadline:
                flow.cancel()
                state = flow.status()
                break
            env.sleep(1.0)
    except KeyboardInterrupt:
        flow.cancel()
        env.out("✗ Sign-in cancelled.")
        env.out(f"  {REMOTE_RETRY}")
        return False
    if state["state"] == SIGNED_IN:
        env.out("SIGNED_IN")
        return True
    env.out(f"SIGN_IN_FAILED state={state['state']}")
    env.out(f"✗ {state['message']}")
    env.out(f"  {REMOTE_RETRY}")
    return False


def voice_sign_in(env: Env, home: Path, settings: Settings, *, api_key: bool, assume: bool | None,
                  sign_in: str = "browser") -> bool:
    """Leave one working voice provider configured. Returns True when voice is ready.

    Order: (1) a working ChatGPT sign-in in any Codex copy on this machine is used silently;
    (2) with none, Codex is installed/updated if needed and the user signs in (``sign_in``:
    ``browser`` runs `codex login` here, ``remote`` prints a device code for another device);
    (3) an API-key-only sign-in is reported, and ``remote`` switches it to ChatGPT."""
    provider = settings.get()["voice"]["provider"]
    have_key = bool(openai_api_key(home))
    if api_key or (provider == "openai" and have_key and sign_in != "remote"):
        if not have_key and not ask_api_key(env, home):
            return False
        settings.patch({"voice": {"provider": "openai"}})
        env.out("✓ Voice: GPT-Live-1 through the OpenAI API (your API key, billed per minute)")
        return True

    found = find_signed_in_codex(env, settings)
    if found["kind"] == "chatgpt":
        settings.patch({"voice": {"provider": "codex"}})
        env.out(f"✓ Voice: GPT-Live-1 through Codex OAuth (your ChatGPT account, Codex at {found['binary']})")
        return True
    if found["kind"] == "api_key" and sign_in != "remote":
        settings.patch({"voice": {"provider": "codex"}})
        env.out("✓ Voice: GPT-Live-1 through Codex, which is signed in with an OpenAI API key, not a ChatGPT "
                "account (billed to that key). To use your ChatGPT plan instead: "
                "`hermes voice setup --yes --sign-in remote`, or Sign in to ChatGPT in the app.")
        return True

    binary = found["binary"]
    if binary is None and not found["too_old"]:
        env.out("• Voice is GPT-Live-1 through Codex OAuth (your ChatGPT account), and no copy of Codex was found \
(the ChatGPT desktop app includes one).")
        if env.confirm("  Install Codex now?", True, assume) and install_codex(env):
            binary = find_signed_in_codex(env, settings)["binary"]
        if binary is None:
            if env.interactive and assume is None and env.confirm("  Use an OpenAI API key instead?", False):
                return voice_sign_in(env, home, settings, api_key=True, assume=assume)
            env.out("✗ Voice isn't set up yet. Install the ChatGPT desktop app (it includes Codex) or the Codex CLI, "
                    "then run `hermes voice setup` again, or run "
                    "`hermes voice setup --api-key`.")
            return False
    elif binary is None:
        env.out("• Your Codex is too old for voice calls.")
        if env.confirm("  Update Codex now?", True, assume) and install_codex(env):
            binary = find_signed_in_codex(env, settings)["binary"]
        if binary is None or codex_too_old(binary):
            env.out("✗ Update Codex (npm install -g @openai/codex@latest), then run `hermes voice setup` again.")
            return False

    if codex_signed_in(env, binary) and sign_in != "remote":  # signed in during the install
        settings.patch({"voice": {"provider": "codex"}})
        env.out("✓ Voice: GPT-Live-1 through Codex OAuth (your ChatGPT account)")
        return True
    if sign_in == "remote":
        if remote_sign_in(env, home, binary):
            settings.patch({"voice": {"provider": "codex"}})
            env.out("✓ Voice: GPT-Live-1 through Codex OAuth (your ChatGPT account)")
            return True
        return False
    env.out("• Sign in to ChatGPT so Speakeasy can use GPT-Live-1 on your plan. A browser window will open.")
    if env.interactive and env.confirm("  Sign in now?", True, assume):
        from .codex_transport import child_env
        try:
            env.run([str(binary), "login"], timeout=600, env=child_env())
        except (OSError, subprocess.SubprocessError):
            pass
        if codex_signed_in(env, binary):
            settings.patch({"voice": {"provider": "codex"}})
            env.out("✓ Voice: GPT-Live-1 through Codex OAuth (your ChatGPT account)")
            return True
    env.out("✗ Not signed in yet. On a computer nobody is at, run `hermes voice setup --yes --sign-in remote` "
            "(prints a link and code to open on any device), or tap Sign in to ChatGPT in the app. "
            "Or run `codex login` here, then `hermes voice setup` again (or `hermes voice setup --api-key`).")
    return False


# -- 3. gateway -----------------------------------------------------------------------------------

def gateway_running(home: Path) -> bool:
    try:
        state = json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
        pid = int(state.get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return state.get("gateway_state") in {None, "running", "starting"}


def voice_health(url: str) -> bool:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=2) as r:  # noqa: S310 (loopback)
            return r.status == 200
    except Exception:
        return False


def wait_for_voice(env: Env, url: str, timeout_s: float = HEALTH_TIMEOUT_S) -> bool:
    check = env.health or voice_health
    deadline = env.clock() + timeout_s
    while True:
        if check(url):
            return True
        if env.clock() >= deadline:
            return False
        env.sleep(1.0)


def voice_server_running(env: Env, url: str) -> bool:
    """True when the voice server answers. Setup never restarts or starts Hermes itself."""
    return bool((env.health or voice_health)(url))


RESTART_NOTE = ("Hermes needs a restart, once, to turn Speakeasy on: `hermes gateway restart` if the gateway "
                "runs as a service, or stop `hermes gateway` and start it again if it runs in a terminal. "
                "Chats pause for a few seconds. Later updates load without a restart.")


# -- 4. tailscale ---------------------------------------------------------------------------------

TAILSCALE_HTTPS_FIX = ("Enable HTTPS in the Tailscale admin console: https://login.tailscale.com/admin/dns "
                       "(HTTPS Certificates)")


@dataclass(frozen=True)
class Tailnet:
    """What `tailscale status` says: state is "running", "stopped" or "missing"."""
    state: str
    dns: str = ""
    binary: str = ""


def banner(env: Env, lines: list[str]) -> None:
    """A boxed block that is hard to miss in setup output."""
    width = max(len(line) for line in lines) + 2
    env.out("┏" + "━" * width + "┓")
    for line in lines:
        env.out("┃ " + line.ljust(width - 1) + "┃")
    env.out("┗" + "━" * width + "┛")


def tailnet_status(env: Env) -> Tailnet:
    binary = env.which("tailscale")
    if not binary:
        return Tailnet("missing")
    try:
        done = env.run([binary, "status", "--json"], capture_output=True, text=True, timeout=15)
        data = json.loads(getattr(done, "stdout", "") or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return Tailnet("stopped", binary=binary)
    data = data if isinstance(data, dict) else {}
    dns = str((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
    if data.get("BackendState") == "Running" and dns:
        return Tailnet("running", dns, binary)
    return Tailnet("stopped", binary=binary)


def _serve_failure_is_https(done: Any) -> bool:
    text = f"{getattr(done, 'stdout', '')}\n{getattr(done, 'stderr', '')}".lower()
    return any(word in text for word in ("https", "certificate", "cert"))


def tailscale_url(env: Env, port: int, *, forced: bool = False) -> tuple[str | None, str]:
    """Publish the loopback voice server on the tailnet only (never Funnel).

    Returns (url or None, tailnet DNS name or ""). Auto-detects: running → imported with a boxed
    notice; installed but stopped → an equally prominent warning and local mode; not installed →
    one quiet line (or an error when ``forced``)."""
    net = tailnet_status(env)
    if net.state == "missing":
        env.out("✗ Tailscale isn't installed on this machine." if forced
                else "• Tailscale not found on this computer.")
        return None, ""
    if net.state == "stopped":
        banner(env, ["⚠ Tailscale is installed but not connected.",
                     "  Fix: run `tailscale up`, then `hermes voice setup` again,",
                     "  or use Cloudflare mode (below) if it needs a sign-in."])
        return None, ""
    try:
        done = env.run([net.binary, "serve", "--bg", f"--https={port}", f"http://127.0.0.1:{port}"],
                       capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        done = None
    if done is None or getattr(done, "returncode", 1) != 0:
        lines = ["⚠ Tailscale is connected, but `tailscale serve` failed.", "  Speakeasy stays local for now."]
        if done is None or _serve_failure_is_https(done):
            lines.append("  " + TAILSCALE_HTTPS_FIX)
        banner(env, lines)
        return None, ""
    url = f"https://{net.dns}:{port}"
    banner(env, [f"✓ Tailscale detected — Speakeasy is reachable from your other devices at {url}",
                 "  (your tailnet only, never the public internet)"])
    return url, net.dns


def api_key_value() -> str:
    return secrets.token_urlsafe(32)
