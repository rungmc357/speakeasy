#!/usr/bin/env python3
"""Cloudflare mode for Speakeasy: run a Cloudflare tunnel to the plugin as a background service.

Lives outside the plugin on purpose: Hermes' plugin install scan treats tunnel tooling inside a
plugin as high risk (it would block `hermes plugins update`), and opening a public address is the
user's decision. The plugin only follows the tunnel's address (plugin/speakeasy/tunnel.py).

    python3 speakeasy-cloudflare.py quick            # free quick tunnel, no account (address changes on restart)
    python3 speakeasy-cloudflare.py named speakeasy.example.com   # permanent address on your own domain
    python3 speakeasy-cloudflare.py restart | remove | status

Options: --hermes-home (default $HERMES_HOME or ~/.hermes), --port (default 8795), --metrics-port
(default 8797). Never Tailscale Funnel. Docs: docs/AGENT_SETUP.md › Cloudflare mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

NAME_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def valid_hostname(name: str) -> bool:
    return bool(NAME_RE.fullmatch((name or "").lower())) and not name.lower().endswith(".trycloudflare.com")


def log_path(home: Path) -> Path:
    return Path(home) / "speakeasy" / "tunnel.log"   # the plugin reads the address from here as a fallback


def service_label(home: Path) -> str:
    """One service per Hermes profile (a hash of its folder), so a second profile never clashes."""
    digest = hashlib.sha256(str(Path(home).resolve()).encode()).hexdigest()[:8]
    return f"ai.speakeasyvoice.tunnel.{digest}"


NAMED_TUNNEL = "speakeasy"

def tunnel_command(binary: str, port: int, metrics_port: int, named: str = "") -> list[str]:
    """Quick tunnel by default; ``named`` runs that named tunnel (a permanent hostname) instead."""
    base = [binary, "tunnel", "--no-autoupdate", "--metrics", f"127.0.0.1:{metrics_port}"]
    if named:
        return base + ["run", "--url", f"http://127.0.0.1:{port}", named]
    return base + ["--url", f"http://127.0.0.1:{port}"]


def cloudflare_logged_in() -> bool:
    """`cloudflared tunnel login` leaves an origin certificate here."""
    return (Path.home() / ".cloudflared" / "cert.pem").exists()


def create_named_tunnel(binary: str, hostname: str, run: Callable[..., Any] = subprocess.run,
                        name: str = NAMED_TUNNEL) -> str:
    """Create the named tunnel (once) and route ``hostname`` to it. "" on success, else the problem."""
    try:
        listed = run([binary, "tunnel", "list", "--output", "json"], capture_output=True, text=True, timeout=60)
        existing = {t.get("name") for t in json.loads(getattr(listed, "stdout", "") or "[]") or []}
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        existing = set()
    try:
        if name not in existing:
            done = run([binary, "tunnel", "create", name], capture_output=True, text=True, timeout=60)
            if getattr(done, "returncode", 1) != 0:
                return f"`cloudflared tunnel create {name}` failed: {(getattr(done, 'stderr', '') or '').strip()[:200]}"
        done = run([binary, "tunnel", "route", "dns", "--overwrite-dns", name, hostname],
                   capture_output=True, text=True, timeout=60)
        if getattr(done, "returncode", 1) != 0:
            return (f"`cloudflared tunnel route dns {name} {hostname}` failed: "
                    f"{(getattr(done, 'stderr', '') or '').strip()[:200]}")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"cloudflared failed ({type(exc).__name__})"
    return ""


def launchd_plist(label: str, command: list[str], log: Path) -> str:
    args = "".join(f"\n        <string>{a}</string>" for a in command)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>{args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
"""


def systemd_unit(command: list[str], log: Path) -> str:
    return ("[Unit]\nDescription=Speakeasy Cloudflare quick tunnel\nAfter=network-online.target\n\n"
            f"[Service]\nExecStart={' '.join(command)}\nRestart=always\nRestartSec=5\n"
            f"StandardOutput=append:{log}\nStandardError=append:{log}\n\n"
            "[Install]\nWantedBy=default.target\n")


def service_file(home: Path) -> Path:
    label = service_label(home)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    return Path.home() / ".config" / "systemd" / "user" / f"{label}.service"


def install_service(home: Path, binary: str, port: int, metrics_port: int,
                    run: Callable[..., Any] = subprocess.run, named: str = "") -> Path:
    """Write and (re)start the tunnel service. Returns the service file."""
    label, log = service_label(home), log_path(home)
    log.parent.mkdir(parents=True, exist_ok=True)
    path = service_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    command = tunnel_command(binary, port, metrics_port, named)
    if sys.platform == "darwin":
        path.write_text(launchd_plist(label, command, log))
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, timeout=30)
        run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, timeout=30, check=True)
    else:
        path.write_text(systemd_unit(command, log))
        run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=30)
        run(["systemctl", "--user", "enable", "--now", path.name], capture_output=True, timeout=30, check=True)
    return path


def restart_service(home: Path, run: Callable[..., Any] = subprocess.run) -> None:
    label = service_label(home)
    if sys.platform == "darwin":
        run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"], capture_output=True, timeout=30)
    else:
        run(["systemctl", "--user", "restart", f"{label}.service"], capture_output=True, timeout=30)


def remove_service(home: Path, run: Callable[..., Any] = subprocess.run) -> None:
    label, path = service_label(home), service_file(home)
    if sys.platform == "darwin":
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True, timeout=30)
    else:
        run(["systemctl", "--user", "disable", "--now", path.name], capture_output=True, timeout=30)
    try:
        path.unlink()
    except OSError:
        pass


def install_cloudflared(out: Callable[[str], None], which: Callable[[str], str | None] = shutil.which,
                        run: Callable[..., Any] = subprocess.run) -> str:
    """cloudflared's path, installing it with Homebrew when that's available. "" when it isn't."""
    found = which("cloudflared")
    if found:
        return found
    if which("brew"):
        out("  Running: brew install cloudflared")
        try:
            run(["brew", "install", "cloudflared"], timeout=900)
        except (OSError, subprocess.SubprocessError):
            pass
        return which("cloudflared") or ""
    out("  Install cloudflared from Cloudflare's packages (https://developers.cloudflare.com/cloudflare-one/"
        "connections/connect-networks/downloads/), then run this script again.")
    return ""



def hermes_config(home: Path, *pairs: tuple[str, str], run: Callable[..., Any] = subprocess.run) -> None:
    """`hermes voice config set` for this profile (the plugin validates the values)."""
    env = dict(os.environ, HERMES_HOME=str(home))
    for key, value in pairs:
        run(["hermes", "voice", "config", "set", key, value], env=env, check=True, timeout=60)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=["quick", "named", "restart", "remove", "status"])
    ap.add_argument("hostname", nargs="?", default="")
    ap.add_argument("--hermes-home", default=os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes"))
    ap.add_argument("--port", type=int, default=8795)
    ap.add_argument("--metrics-port", type=int, default=8797)
    args = ap.parse_args(argv)
    home = Path(args.hermes_home).expanduser()
    if args.action == "remove":
        remove_service(home)
        hermes_config(home, ("server.tunnel", '""'), ("server.tunnel_url", '""'), ("server.tunnel_hostname", '""'))
        print("Removed Speakeasy's Cloudflare tunnel. Run `hermes voice setup` to publish the remaining address.")
        return 0
    if args.action == "restart":
        restart_service(home)
        print("Restarted. A quick tunnel gets a new address; Speakeasy picks it up and tells the app.")
        return 0
    if args.action == "status":
        print(json.dumps({"service_file": str(service_file(home)), "installed": service_file(home).exists(),
                          "log": str(log_path(home))}, indent=2))
        return 0
    binary = install_cloudflared(print)
    if not binary:
        return 1
    hostname = args.hostname.strip().lower().rstrip(".")
    if args.action == "named":
        if not valid_hostname(hostname):
            print("Give the permanent address, like: named speakeasy.example.com")
            return 2
        if not cloudflare_logged_in():
            print("First, once: run `cloudflared tunnel login` (a browser opens; sign in to Cloudflare and pick "
                  "your domain). Then run this again.")
            return 1
        problem = create_named_tunnel(binary, hostname)
        if problem:
            print(problem)
            return 1
    install_service(home, binary, args.port, args.metrics_port, named=NAMED_TUNNEL if hostname else "")
    hermes_config(home, ("server.tunnel", "cloudflare"), ("server.tunnel_metrics_port", str(args.metrics_port)),
                  ("server.tunnel_hostname", hostname or '""'))
    print("Cloudflare tunnel service is running. Now run `hermes voice setup` (it shows the address and the "
          "pairing link).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
