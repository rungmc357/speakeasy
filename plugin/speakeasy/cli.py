"""``hermes voice`` — setup, pairing, devices, config and status for Speakeasy.

``setup`` touches only this profile: it enables the Hermes API server on loopback (generating
``API_SERVER_KEY`` through Hermes' env writer only if the profile lacks one, never overwriting values),
enables the ``voice`` platform in ``config.yaml``, sets up voice sign-in, restarts Hermes (asked
first) and waits for the voice server, starts the first voice-brief write and only then opens the
pairing link. See setup_flow.py.
"""
from __future__ import annotations

import argparse

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .devices import PAIR_TTL_S, DeviceStore, client_label
from .settings import valid_delivery_target, DEFAULT_PORT, Settings, SettingsError, get_path, hermes_config as read_hermes_config, hermes_secret, patch_for, profile_env_defines, save_hermes_secret


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _store(home: Path | None = None) -> DeviceStore:
    return DeviceStore(home or _home())


def setup_parser(parser) -> None:
    sub = parser.add_subparsers(dest="voice_command")
    setup = sub.add_parser("setup", help="Set up Speakeasy for this Hermes profile and pair a Mac")
    setup.add_argument("--server", default="", help="URL the Mac will use (default http://127.0.0.1:<port>)")
    setup.add_argument("--tailscale", action="store_true",
                       help="Require Tailscale (tailnet only); it is auto-detected without this flag")
    setup.add_argument("--no-tailscale", action="store_true", help="Stay local; don't publish on Tailscale")
    setup.add_argument("--api-key", action="store_true",
                       help="Use GPT-Live-1 through the OpenAI API (an API key) instead of Codex OAuth (your ChatGPT sign-in)")
    setup.add_argument("--send", default="", help="Send the pairing link to this Hermes chat (e.g. telegram or discord:<chat_id>); "
                            "if Hermes needs a restart first, it is sent once Hermes is back")
    setup.add_argument("--yes", "-y", action="store_true", help="Answer yes to every question (installing Codex)")
    setup.add_argument("--sign-in", choices=("browser", "remote"), default="browser",
                       help="How to sign Codex in to ChatGPT when no copy on this computer is signed in: "
                            "browser (codex login here) or remote (print a link + one-time code to open on "
                            "any device, wait, exit 0 once signed in)")
    setup.add_argument("--no-restart", action="store_true", help=argparse.SUPPRESS)  # setup never restarts Hermes now
    setup.add_argument("--no-open", action="store_true", help="Print the pairing link instead of opening it")
    setup.add_argument("--here", action="store_true",
                       help="The Mac you'll talk from is this machine: open Speakeasy (or its download page) here")
    pair = sub.add_parser("pair", help="Show a one-time code to pair a Mac")
    pair.add_argument("--server", default="",
                      help="URL the Mac will use (default: the URL setup advertised, else http://127.0.0.1:<port>)")
    pair.add_argument("--send", default="", help="Also send the pairing link via `hermes send --to <target>`")
    sub.add_parser("devices", help="List paired devices")
    revoke = sub.add_parser("revoke", help="Revoke a paired device")
    revoke.add_argument("device_id")
    config = sub.add_parser("config", help="Get or set Speakeasy settings")
    config_sub = config.add_subparsers(dest="config_command")
    get = config_sub.add_parser("get", help="Print settings (or one dotted key)")
    get.add_argument("key", nargs="?", default="")
    set_ = config_sub.add_parser("set", help="Set one dotted key, e.g. delivery.target telegram")
    set_.add_argument("key")
    set_.add_argument("value")
    sub.add_parser("status", help="Show voice provider, Hermes API and brief status")
    routing = sub.add_parser("routing", help="Show or switch the model that routes voice requests")
    routing.add_argument("provider", nargs="?", default="",
                         help="a provider Hermes is signed in to, or 'default' (omit to list)")
    routing.add_argument("model", nargs="?", default="", help="a model of that provider")
    thinking = routing.add_mutually_exclusive_group()
    thinking.add_argument("--no-thinking", dest="thinking", action="store_false", default=None,
                          help="turn the model's reasoning off (much faster for routing)")
    thinking.add_argument("--thinking", dest="thinking", action="store_true",
                          help="leave the model's reasoning on")
    routing.add_argument("--models", action="store_true", help="list every model of PROVIDER")
    home_ = sub.add_parser("home", help="Instant home control through Home Assistant: show, or turn on/off")
    home_.add_argument("state", nargs="?", choices=("on", "off", "status"), default="status")
    fast_ = sub.add_parser("fast", help="Quick answers and optional Jev routing: show or change")
    fast_.add_argument("what", nargs="?", default="status",
                       help="status | quick on|off | jev venice|openrouter|typesafe|off | home CITY")
    fast_.add_argument("value", nargs="*", default=[])
    where_ = sub.add_parser("where", help="Where voice work is posted: one place, home + approved threads, or by topic")
    where_.add_argument("mode", nargs="?", choices=("status", "single", "home", "topic"), default="status")
    tune_ = sub.add_parser("tune", help="Improve the voice brief from your recent calls (you review every edit)")
    tune_.add_argument("action", nargs="?", choices=("start", "show", "apply", "dismiss"), default="show")
    tune_.add_argument("ids", nargs="*", help="edit ids to apply (default: all)")
    reload_ = sub.add_parser("reload", help="Load a newly installed Speakeasy now, without restarting Hermes "
                                            "(ends a live call; running tasks keep going in Hermes)")
    reload_.add_argument("--timeout", type=float, default=30, help=argparse.SUPPRESS)


# -- helpers --------------------------------------------------------------------------------------

def add_env_if_missing(home: Path, values: dict[str, str]) -> list[str]:
    """Store each NAME=value this profile does not set yet, through Hermes' env writer. Never
    rewrites a value the user already has."""
    added = [k for k in values if not profile_env_defines(home, k)]
    for name in added:
        save_hermes_secret(home, name, values[name])
    return added


def enable_voice_platform(home: Path, port: int) -> bool:
    """Turn on gateway.platforms.voice in this profile's config.yaml. Returns True if changed.
    Only adds settings; refuses (config untouched) if the file can't be read."""
    from . import hermes_config

    def turn_on(data: dict) -> bool:
        gateway = data.setdefault("gateway", {})
        if not isinstance(gateway, dict):
            return False
        platforms = gateway.setdefault("platforms", {})
        if not isinstance(platforms, dict):
            return False
        voice = platforms.setdefault("voice", {})
        if not isinstance(voice, dict):
            return False
        changed = not voice.get("enabled")
        voice["enabled"] = True
        extra = voice.setdefault("extra", {})
        if isinstance(extra, dict) and "port" not in extra:
            extra["port"] = port
            changed = True
        plugins = data.setdefault("plugins", {})
        if isinstance(plugins, dict):
            enabled = plugins.setdefault("enabled", [])
            if isinstance(enabled, list) and "speakeasy" not in enabled:
                enabled.append("speakeasy")
                changed = True
        return changed

    return hermes_config.update(home / "config.yaml", turn_on)


def voice_port(home: Path) -> int:
    data = read_hermes_config(home)
    try:
        return int(data["gateway"]["platforms"]["voice"]["extra"]["port"])
    except (KeyError, TypeError, ValueError):
        return DEFAULT_PORT


def pairing_link(server: str, code: str) -> str:
    from .handoff import app_link
    return app_link(server, code)


def _hermes_cmd(home: Path) -> list[str]:
    from .delivery import hermes_command
    return hermes_command()


def _open(url: str) -> bool:
    """Open the link; False when nothing handles speakeasy:// (the app isn't installed)."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        return subprocess.run([opener, url], check=False, timeout=10, capture_output=True).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# -- commands -------------------------------------------------------------------------------------

def _prepare_profile(home: Path, port: int, out) -> None:
    """Step 1: what Speakeasy needs in this profile. Never overwrites an existing value."""
    added = add_env_if_missing(home, {
        "API_SERVER_KEY": secrets.token_urlsafe(32),
        "API_SERVER_HOST": "127.0.0.1",
    })
    host = hermes_secret(home, "API_SERVER_HOST") or "127.0.0.1"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        out(f"  Note: API_SERVER_HOST is {host}; Speakeasy only talks to it over loopback.")
    if added:
        out("✓ Turned on Hermes' local API (Speakeasy uses it to hand work to your agent)")
    enable_voice_platform(home, port)
    Settings(home).ensure()  # settings.json (0600) with defaults
    out("✓ Speakeasy is enabled in this Hermes profile")


def cmd_setup(args, home: Path, env=None) -> int:
    from . import handoff as H
    from . import setup_flow as F
    env = env or F.Env()
    out = env.out
    assume = True if getattr(args, "yes", False) else None
    port = voice_port(home)
    local = f"http://127.0.0.1:{port}"
    out("Setting up Speakeasy…")
    from .hermes_config import ConfigWriteRefused
    try:
        _prepare_profile(home, port, out)
    except ConfigWriteRefused as exc:
        out(f"✗ Left your Hermes config.yaml untouched: {exc}")
        out("  Fix or restore config.yaml (hermes config check), then run setup again.")
        return 1

    settings = Settings(home)
    sign_in = getattr(args, "sign_in", "browser") or "browser"
    voice_ok = F.voice_sign_in(env, home, settings, api_key=getattr(args, "api_key", False), assume=assume,
                               sign_in=sign_in)
    if sign_in == "remote" and not voice_ok:
        return 3  # the code expired, was cancelled or failed; the retry hint is printed
    up = F.voice_server_running(env, local)
    server = _advertise(args, env, settings, port) or local
    server = _cloudflare(env, home, settings, tailnet=not is_local_url(server),
                         here=getattr(args, "here", False)) or server

    if up:
        out("✓ Voice server is running")
        out("✓ Your agent will write a short voice brief in the background: what the voice should know about "
            "you and what it can hand off. You can read and edit it in the app.")
    _print_routing_model(out)
    _print_home_offer(home, settings, out)
    _sync_thread_routes(home, settings, out)
    out("")

    target = getattr(args, "send", "") or ""
    if not up:
        out(f"• {F.RESTART_NOTE}")
        if target and valid_delivery_target(target) and target != "none":
            H.save_pending(home, target)
            out(f"  Once Hermes is back, the pairing link arrives in {_chat_name(target)} by itself.")
        else:
            out("  Then run `hermes voice pair` for the pairing link.")
        out(f"  No Speakeasy on your Mac yet? Download it: {H.DOWNLOAD_PAGE}")
        return 2 if voice_ok else 1

    H.clear_pending(home)
    delivered = _deliver_link(args, env, home, settings, server)
    if not delivered:
        code = _store(home).new_pairing_code(ttl=H.link_ttl(home))
        out("Open this link on the Mac you'll talk from. It connects Speakeasy, or offers the download first:")
        out(f"  {H.web_link(server, code)}")
        out(f"  Or type code {code} in Speakeasy › Connect (works once, for {H.link_ttl(home) // 60} minutes).")
    if not voice_ok:
        out("Voice isn't signed in yet; the app will show how to finish that.")
        return 1
    return 0


def _chat_name(target: str) -> str:
    return target.split(":", 1)[0].title()


def is_local_url(url: str) -> bool:
    """True when the URL only works on this machine (a Mac elsewhere can't use it)."""
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


def _advertise(args, env, settings: Settings, port: int) -> str:
    """The URL other devices use: --server, else a detected tailnet (stored for `hermes voice pair`)."""
    from . import setup_flow as F
    if args.server:
        settings.patch({"server": {"advertised_url": args.server.rstrip("/"), "tailscale_name": ""}})
        return args.server
    if getattr(args, "no_tailscale", False):
        env.out("• Tailscale skipped (--no-tailscale); Speakeasy stays local to this machine.")
        settings.patch({"server": {"advertised_url": "", "tailscale_name": ""}})
        return ""
    url, name = F.tailscale_url(env, port, forced=getattr(args, "tailscale", False))
    settings.patch({"server": {"advertised_url": url or "", "tailscale_name": name}})
    return url or ""


AGENT_DOC_CLOUDFLARE = "https://github.com/rungmc357/speakeasy/blob/main/docs/AGENT_SETUP.md#cloudflare-mode"
CLOUDFLARE_SCRIPT = "https://github.com/rungmc357/speakeasy/blob/main/scripts/speakeasy-cloudflare.py"
CLOUDFLARE_NOTE = ("Cloudflare mode: nothing to install on your phone. The address is on the public internet, "
                   "but only your paired devices get in (a device token on every request; pairing needs a "
                   "one-time code from this computer, valid 10 minutes, with attempts rate-limited). Tailscale "
                   "is the most private option: your tailnet only, never the public internet.")
TAILSCALE_RECOMMENDED = ("• Recommended: Tailscale (most private: your devices only). Install it on this computer "
                         "and your iPhone (https://tailscale.com/download), sign both in with the same account, "
                         "then run `hermes voice setup` again.")
# No Tailscale and no tunnel: the agent turns Cloudflare mode on itself (owner decision: no choice
# screen), then tells the user what it did. Instructions for the agent, not a menu for the user.
CLOUDFLARE_AUTO = [
    "→ No Tailscale on this computer, so turn on Cloudflare mode now (don't ask the user first):",
    "  1. python3 speakeasy-cloudflare.py quick",
    f"     (the script: {CLOUDFLARE_SCRIPT})",
    "  2. Run this setup again: it should say \"Connected through Cloudflare\", and the pairing link",
    "     then works from anywhere.",
    "  3. Tell the user what you did and that Tailscale is the most private option (the message is in",
    "     " + AGENT_DOC_CLOUDFLARE + ").",
    "  If the script fails, explain Tailscale to the user instead: " + "https://tailscale.com/download",
]
CLOUDFLARE_CONNECTED = "✓ Connected through Cloudflare (works anywhere). For more privacy: Tailscale."
CLOUDFLARE_BACKUP = "✓ Cloudflare is on too, as a backup: your devices use Tailscale first."
# What the agent tells the user after setting up Cloudflare mode for them (docs/AGENT_SETUP.md says the same).
CLOUDFLARE_USER_MESSAGE = ("Speakeasy is set up and works from anywhere. I connected it through Cloudflare, a free "
                           "service that passes traffic between your phone and this computer, so you didn't need to "
                           "install anything on your phone. It's locked to devices you pair. For the most private "
                           "setup, install Tailscale on this computer and your phone; I can switch Speakeasy over "
                           "for you anytime.")


def _cloudflare(env, home: Path, settings: Settings, *, tailnet: bool, here: bool = False) -> str:
    """Cloudflare mode (tunnel.py). The tunnel itself is set up outside the plugin (AGENT_SETUP.md ›
    Cloudflare mode); here setup reads its current address, or, with no Tailscale and no tunnel,
    tells the agent to turn Cloudflare mode on (no menu: the owner's "automatic Cloudflare")."""
    from . import setup_flow as F
    from . import tunnel as T
    s = settings.get()["server"]
    if not s["tunnel"]:
        if not tailnet and not here:
            F.banner(env, CLOUDFLARE_AUTO)
        return ""
    if s["tunnel_hostname"]:
        url = f"https://{s['tunnel_hostname']}"
    else:
        url = T.wait_for_url(home, s["tunnel_metrics_port"], timeout_s=10, sleep=env.sleep, clock=env.clock)
        if not url:
            env.out("✗ Cloudflare mode is on, but its tunnel isn't reporting an address (is the tunnel service "
                    "running?). Run `python3 speakeasy-cloudflare.py restart`, then setup again. "
                    f"See {AGENT_DOC_CLOUDFLARE}")
            return ""
        T.apply_new_url(settings, url)
    env.out(CLOUDFLARE_BACKUP if tailnet else CLOUDFLARE_CONNECTED)
    env.out(f"  Cloudflare {'permanent ' if s['tunnel_hostname'] else ''}address: {url}")
    if not s["tunnel_hostname"]:
        env.out("  This free address changes when the tunnel restarts; the app finds the new one by itself "
                "(an encrypted note via ntfy.sh) and Hermes messages you a link too. For one that never changes, "
                "see the permanent address in " + AGENT_DOC_CLOUDFLARE)
    env.out(f"  {CLOUDFLARE_NOTE}")
    if not tailnet:
        env.out(f"  Agent: once the user has paired, tell them: \"{CLOUDFLARE_USER_MESSAGE}\"")
    return "" if tailnet else url


def _sync_thread_routes(home: Path, settings: Settings, out) -> None:
    """Channels that open a thread per task need a webhook route each (Hermes hot-reloads them)."""
    from .service import VoiceService
    service = VoiceService(home, start_threads=False)
    try:
        wanted = service.thread_channels(settings.get())
        if wanted:
            service.sync_thread_routes(settings.get())
            status = service.thread_status()
            out("• New-thread channels: " + ("ready." if status["threads_supported"] else status["threads_reason"]))
    finally:
        service.close()


def _print_home_offer(home: Path, settings: Settings, out) -> None:
    """Hermes has Home Assistant: say that instant home control can be turned on (in the app, or
    `hermes voice home on`). Nothing is turned on here, and nothing is said when it isn't set up."""
    from .home_control import credentials
    if settings.get()["home_control"]["enabled"]:
        out("• Home control is on: `hermes voice home` shows which devices it can use.")
    elif credentials(home):
        out("• Your Hermes has Home Assistant: turn on instant home control in the app's setup, "
            "or with `hermes voice home on`.")


def cmd_home(home: Path, args) -> int:
    from .service import HOME_EXPLAINER, VoiceService, ServiceError
    svc = VoiceService(home, start_threads=False)
    try:
        state = getattr(args, "state", "status")
        if state in {"on", "off"}:
            try:
                svc.put_home({"enabled": state == "on"})
            except ServiceError as exc:
                print(f"Error: {exc.message}")
                return 2
        info = svc.get_home()
        if state == "status":
            print("Home control: " + HOME_EXPLAINER + "\n")
        print(f"Home control is {'on' if info['enabled'] else 'off'}.")
        if not info["available"]:
            print(info["reason"])
            return 0 if state != "on" else 2
        chosen = [d for d in info["devices"] if d["included"]]
        print(f"It can use {len(chosen)} of the {len(info['devices'])} devices Home Assistant offers:")
        for d in chosen:
            print(f"  {d['name']:<34} {d['kind']}")
        print("\nChange which devices it can use in the app: Settings › Home.")
        return 0
    finally:
        svc.close()


WHERE_MODES = {
    "single": ("One place", "Everything goes to your home channel. Speakeasy never posts anywhere else, "
                            "and never continues a chat outside it."),
    "home": ("Home, plus approved threads", "New work goes to your home channel. A follow-up to something "
             "already running in one of your approved channels continues in that thread."),
    "topic": ("Sort by topic", "Like the one above, plus new work goes to the approved channel whose "
              "description fits it."),
}


def cmd_where(home: Path, args) -> int:
    """`hermes voice where`: show or pick where voice work is posted."""
    from .delivery import target_label
    from .settings import Settings, SettingsError
    settings = Settings(home)
    choice = getattr(args, "mode", "status") or "status"
    if choice != "status":
        try:
            settings.patch({"delivery": {"mode": choice}})
        except SettingsError as exc:
            print(f"Error: {exc}")
            return 2
    d = settings.get()["delivery"]
    print(f"Home channel: {target_label(d['target']) if d['target'] != 'none' else 'none set'}")
    print(f"Approved channels: {', '.join(c['label'] for c in d['channels']) or 'none'}\n")
    for key, (name, what) in WHERE_MODES.items():
        mark = "●" if d["mode"] == key else "○"
        print(f"{mark} {name}  (hermes voice where {key})\n    {what}")
    print("\nNaming a channel out loud (\"put this in #work\") still sends it there, except in One place.")
    return 0


def cmd_fast(home: Path, args) -> int:
    """`hermes voice fast`: quick answers (one web search, no full agent) and optional Jev routing."""
    from . import jev
    from .settings import Settings, SettingsError
    settings = Settings(home)
    raw = getattr(args, "value", []) or []
    raw = " ".join(raw) if isinstance(raw, list) else str(raw)
    what, value = getattr(args, "what", "status"), raw.strip().lower()
    try:
        if what == "quick" and value in {"on", "off"}:
            settings.patch({"fast_routing": {"quick_answers": value == "on"}})
        elif what == "jev" and value in {"off", "", *jev.PROVIDERS}:
            if value not in {"off", ""} and not jev.available(home, value):
                key = jev.PROVIDERS[value]["key"]
                print(f"No {key} in this Hermes profile. Add it with `hermes config set {key} <value>` "
                      "(or to the profile's .env), then run this again.")
                return 2
            settings.patch({"fast_routing": {"jev": "" if value in {"off", ""} else value}})
        elif what == "home":
            settings.patch({"fast_routing": {"home_place": raw.strip()[:80]}})
        elif what != "status":
            print("Usage: hermes voice fast [status | quick on|off | jev venice|openrouter|typesafe|off | home CITY]")
            return 2
    except SettingsError as exc:
        print(f"Error: {exc}")
        return 2
    fast = settings.get()["fast_routing"]
    print("Quick answers: simple public-fact questions (who owns, how tall, what time) are answered from one web "
          "search in a few seconds instead of a full Hermes task; anything unclear still goes to Hermes.")
    print(f"  Quick answers are {'on' if fast['quick_answers'] else 'off'}.")
    print(f"  Weather without a place uses: {fast.get('home_place') or 'nothing set (hermes voice fast home CITY)'}.")
    print("\nJev (optional): TypeSafe's decision model sorts each request (home, quick answer, Hermes task) in "
          "about half a second. Without it, only plainly-worded questions with no task running get a quick try.")
    print(f"  Jev is {'on, via ' + jev.PROVIDERS[fast['jev']]['label'] if fast['jev'] else 'off'}.")
    for p in jev.providers_status(home):
        print(f"  {p['label']:<12} {'key found' if p['has_key'] else 'no ' + p['key_env']}")
    return 0


def cmd_tune(home: Path, args) -> int:
    """Runs in this process (like `hermes voice home`); the proposal is a file the app reads too."""
    from .service import VoiceService, ServiceError
    action = getattr(args, "action", "show")
    svc = VoiceService(home, start_threads=False)
    try:
        if action == "start":
            print("Reading your recent calls (this takes a minute or two)…")
            try:
                state = svc.tune.start(background=False)
            except Exception as exc:
                print(f"Can't tune yet: {exc}")
                return 2
            print(_format_tune(state))
            return 0 if state.get("state") == "ready" else 1
        state = svc.get_tune()
        if action == "dismiss":
            svc.dismiss_tune()
            print("Dismissed the proposed edits.")
            return 0
        if action == "apply":
            ids = getattr(args, "ids", None) or [e["id"] for e in state.get("edits", [])]
            try:
                done = svc.apply_tune({"accept": ids})
            except ServiceError as exc:
                print(f"Error: {exc.message}")
                return 2
            print(f"Applied {done.get('applied', 0)} edit(s) to your voice brief. Your next call uses it.")
            return 0
        print(_format_tune(state))
        return 0
    finally:
        svc.close()


def _format_tune(state: dict) -> str:
    lines = []
    if state.get("state") == "working":
        return "Still reading your calls…"
    if state.get("state") == "failed":
        return f"The last tune failed: {state.get('error')}"
    if state.get("state") != "ready":
        return (f"No tune yet. {state.get('calls', 0)} recent call(s) to learn from. "
                "Run `hermes voice tune start`.")
    if state.get("summary"):
        lines += [state["summary"], ""]
    for e in state.get("edits", []):
        verb = {"add": f"Add to {e.get('section')}", "change": "Change", "remove": "Remove"}[e["kind"]]
        lines.append(f"[{e['id']}] {verb}")
        if e.get("old"):
            lines.append(f"    was: {e['old']}")
        if e.get("new"):
            lines.append(f"    now: {e['new']}")
        lines.append(f"    why: {e['why']}" + (f" ({e['evidence']})" if e.get("evidence") else ""))
    if state.get("product_issues"):
        lines += ["", "Not something the brief can fix:"]
        lines += [f"  - {i['what']}" + (f" ({i['evidence']})" if i.get("evidence") else "")
                  for i in state["product_issues"]]
    lines += ["", "Apply all with `hermes voice tune apply`, some with `hermes voice tune apply e1 e3`, "
                  "or `hermes voice tune dismiss`."]
    return "\n".join(lines)


def _print_routing_model(out) -> None:
    from .router import AUX_TASK, routing_model
    out(f"• Task routing uses: {routing_model()}. Switch it with `hermes voice routing`, or set "
        f"auxiliary.{AUX_TASK} in config.yaml.")


def _deliver_link(args, env, home: Path, settings: Settings, server: str) -> str:
    """"opened" | "sent" | "": ``--send`` sends a link to that chat; ``--here`` opens it on this
    machine (the app, or the download page when it isn't installed). Without either, open here
    when the app is installed on this machine and the server is local; else offer a chat."""
    from .handoff import app_link, chat_message, link_ttl, web_link
    if getattr(args, "send", ""):
        code = _store(home).new_pairing_code(ttl=link_ttl(home))
        return "sent" if _send_link(home, args.send, web_link(server, code), env.out) else ""
    here = getattr(args, "here", False)
    if here or (is_local_url(server) and not args.no_open and _app_installed()):
        local = f"http://127.0.0.1:{voice_port(home)}"
        code = _store(home).new_pairing_code(ttl=link_ttl(home))
        installed = _app_installed()
        if _open(app_link(local, code) if installed else web_link(local, code)):
            env.out("✓ Opened Speakeasy to pair. Finish setup there (about a minute)." if installed else
                    "✓ Opened the Speakeasy download page on this Mac. Install the app, then click Open Speakeasy there.")
            return "opened"
    target = _pick_chat(env, home, settings)
    if target:
        code = _store(home).new_pairing_code(ttl=link_ttl(home))
        if _send_link(home, target, web_link(server, code), env.out):
            return "sent"
    return ""


def _app_installed() -> bool:
    return any(Path(p).exists() for p in ("/Applications/Speakeasy.app",
                                          str(Path.home() / "Applications/Speakeasy.app")))


def _pick_chat(env, home: Path, settings: Settings) -> str:
    """Ask which connected chat gets the pairing link (Enter = the default chat, n = none)."""
    from .delivery import destinations, flat_chats
    if not env.interactive:
        return ""
    dest = destinations(home)
    entries = flat_chats(dest)
    if not entries:
        return ""
    current = settings.get()["delivery"]["target"]
    default = next((t for t in (current, dest.get("suggested")) if any(e["target"] == t for e in entries)),
                   entries[0]["target"])
    env.out("• Your Mac looks like another computer. Send the pairing link to one of your chats?")
    for i, entry in enumerate(entries[:9], 1):
        mark = "  (default)" if entry["target"] == default else ""
        env.out(f"  {i}. {entry.get('label') or entry['target']}{mark}")
    answer = env.ask("  Number, Enter for the default, or n to skip: ").strip().lower()
    if answer in {"n", "no"}:
        return ""
    if answer.isdigit() and 1 <= int(answer) <= min(len(entries), 9):
        return entries[int(answer) - 1]["target"]
    return default if default and default != "none" else ""


def _send_link(home: Path, target: str, link: str, out) -> bool:
    from .delivery import HermesSendNotifier
    from .settings import valid_delivery_target
    if not valid_delivery_target(target) or target == "none":
        out("✗ --send needs a Hermes chat like telegram or discord:<chat_id>")
        return False
    from .handoff import chat_message, link_ttl
    ok = HermesSendNotifier(home).send(target, chat_message(link, link_ttl(home) // 60))
    out(f"✓ Sent the pairing link to {_chat_name(target)}. Open it on your Mac." if ok
        else "✗ Couldn't send the pairing link.")
    return ok


def cmd_pair(args, home: Path) -> int:
    from .handoff import link_ttl, web_link
    code = _store(home).new_pairing_code(ttl=link_ttl(home))
    server = (args.server or Settings(home).get()["server"]["advertised_url"]
              or f"http://127.0.0.1:{voice_port(home)}")
    link = web_link(server, code)
    print(f"Pairing code: {code}  (single use, expires in {link_ttl(home) // 60} minutes)")
    print(f"Pairing link: {link}")
    if args.send:
        return 0 if _send_link(home, args.send, link, print) else 1
    return 0


def cmd_config(args, home: Path) -> int:
    settings = Settings(home)
    command = getattr(args, "config_command", None)
    if command == "set":
        try:
            updated = settings.patch(patch_for(args.key, args.value))
        except SettingsError as exc:
            print(f"Error: {exc}")
            return 2
        print(json.dumps(get_path(updated, args.key)))
        return 0
    current = settings.get()
    if command == "get" and args.key:
        try:
            print(json.dumps(get_path(current, args.key), indent=2))
        except KeyError:
            print(f"No setting {args.key}")
            return 1
        return 0
    print(json.dumps(current, indent=2))
    return 0


def cmd_routing(home: Path, args) -> int:
    from . import hermes_config, routing_choice
    provider, model = getattr(args, "provider", ""), getattr(args, "model", "")
    if provider and getattr(args, "models", False):
        models = routing_choice.provider_models(provider)
        print("\n".join(models) if models else f"Hermes lists no models for {provider!r}.")
        return 0 if models else 1
    if provider:
        try:
            state = routing_choice.choose(home, provider, model, getattr(args, "thinking", None))
        except (routing_choice.RoutingChoiceError, hermes_config.ConfigWriteRefused) as exc:
            print(f"Error: {exc}")
            return 2
        print(f"Routing now uses {state['label']}. Takes effect on the next request; no restart needed.")
        return 0
    state = routing_choice.choices(home)
    from .service import ROUTING_EXPLAINER
    print("Task routing: " + ROUTING_EXPLAINER + "\n")
    print(f"Routing uses: {state['label']}\n")
    print("Providers Hermes is signed in to (the same list `hermes model` shows):")
    for p in state["providers"]:
        sample = ", ".join(p["models"][:4]) + (" …" if len(p["models"]) > 4 else "")
        print(f"  {p['id']:<28} {sample}")
    print("\nSwitch:   hermes voice routing <provider> <model> [--no-thinking]")
    print("Models:   hermes voice routing <provider> --models")
    print("Default:  hermes voice routing default")
    return 0


def cmd_status(home: Path) -> int:
    from .service import VoiceService
    service = VoiceService(home, start_threads=False)
    try:
        status = service.status()
    finally:
        service.close()
    for key, value in status.items():
        print(f"{key}: {value}")
    return 0


def cmd_reload(home: Path, timeout: float = 30) -> int:
    """Ask the running gateway's Speakeasy to load the installed files now."""
    import json as _json
    from .reloader import REQUEST_FILE, RESULT_FILE
    state = home / "speakeasy"
    state.mkdir(parents=True, exist_ok=True)
    result = state / RESULT_FILE
    before = result.stat().st_mtime if result.exists() else 0
    (state / REQUEST_FILE).write_text(str(time.time()))
    deadline = time.time() + timeout
    while time.time() < deadline:
        if result.exists() and result.stat().st_mtime > before:
            try:
                answer = _json.loads(result.read_text())
            except ValueError:
                time.sleep(.2)
                continue
            if answer.get("ok"):
                print(f"Speakeasy {answer.get('version')} loaded. No gateway restart needed.")
                return 0
            if answer.get("running") is False:
                print(f"Speakeasy couldn't start, so the voice server is down for now. It keeps retrying on its own, "
                      f"and `hermes voice reload` tries again right away. Error: {answer.get('error')}")
                return 1
            print(f"The new Speakeasy didn't load, so {answer.get('version')} is still running: {answer.get('error')}")
            return 1
        time.sleep(.3)
    (state / REQUEST_FILE).unlink(missing_ok=True)
    print("No answer from Speakeasy. Is the Hermes gateway running with Speakeasy 0.2.17 or newer? "
          "If it's older, restart the gateway once: `hermes gateway restart`.")
    return 1


def handle(args) -> int:
    command = getattr(args, "voice_command", None)
    home = _home()
    if command == "setup":
        return cmd_setup(args, home)
    if command == "pair":
        return cmd_pair(args, home)
    if command == "config":
        return cmd_config(args, home)
    if command == "status":
        return cmd_status(home)
    if command == "reload":
        return cmd_reload(home, getattr(args, "timeout", 30))
    if command == "routing":
        return cmd_routing(home, args)
    if command == "home":
        return cmd_home(home, args)
    if command == "tune":
        return cmd_tune(home, args)
    if command == "fast":
        return cmd_fast(home, args)
    if command == "where":
        return cmd_where(home, args)
    store = _store(home)
    if command == "devices":
        devices = store.devices()
        if not devices:
            print("No paired devices.")
        for d in devices:
            seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.last_seen_at)) if d.last_seen_at else "never"
            # The app build it last reported (iOS 27.0 · app 1.4 (60) · iPhone); older apps don't say.
            client = client_label(d.client) or "app version unknown"
            print(f"{d.id}  {d.name}  {client}  last used {seen}")
        return 0
    if command == "revoke":
        if store.revoke(args.device_id):
            print(f"Revoked {args.device_id}.")
            return 0
        print(f"No device {args.device_id}.")
        return 1
    print("Usage: hermes voice {setup|pair|devices|revoke <id>|config get|set|status|routing|home|reload}")
    return 2
