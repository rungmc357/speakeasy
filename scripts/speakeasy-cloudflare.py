#!/usr/bin/env python3
"""Cloudflare mode for Speakeasy: run a Cloudflare tunnel to the plugin as a background service.

Lives outside the plugin on purpose: Hermes' plugin install scan treats tunnel tooling inside a
plugin as high risk (it would block `hermes plugins update`), and opening a public address is the
user's decision. The plugin only follows the tunnel's address (plugin/speakeasy/tunnel.py).

    python3 speakeasy-cloudflare.py quick            # free quick tunnel, no account (address changes on restart)
    python3 speakeasy-cloudflare.py named speakeasy.example.com   # permanent address on your own domain
    python3 speakeasy-cloudflare.py restart | remove | status

Needs no Homebrew: without cloudflared on PATH it downloads Cloudflare's own release into
~/.hermes/bin and installs it only if it matches both GitHub's SHA-256 digest and the checksum in
Cloudflare's release notes (Homebrew is still used first when it's there).

Options: --hermes-home (default $HERMES_HOME or ~/.hermes), --port (default 8795), --metrics-port
(default 8797). Never Tailscale Funnel. Docs: docs/AGENT_SETUP.md › Cloudflare mode.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
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


class ServiceStartError(RuntimeError):
    """The computer wouldn't start the background service; the message says why in plain words."""


def _ran(run: Callable[..., Any], cmd: list[str]) -> bool:
    """Run a service-manager command; True when it worked (a fake ``run`` returning None counts)."""
    try:
        done = run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(done, "returncode", 0) == 0


def _launchd_domains() -> list[str]:
    """The logged-in screen session first, then the per-user one (which exists over SSH too)."""
    return [f"gui/{os.getuid()}", f"user/{os.getuid()}"]


def _forget(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def install_service(home: Path, binary: str, port: int, metrics_port: int,
                    run: Callable[..., Any] = subprocess.run, named: str = "") -> Path:
    """Write and (re)start the tunnel service. Returns the service file.

    When the service manager refuses (macOS over SSH has no logged-in screen session to start it
    in), removes the file it wrote and raises ServiceStartError."""
    label, log = service_label(home), log_path(home)
    log.parent.mkdir(parents=True, exist_ok=True)
    path = service_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    command = tunnel_command(binary, port, metrics_port, named)
    if sys.platform == "darwin":
        path.write_text(launchd_plist(label, command, log))
        for domain in _launchd_domains():
            _ran(run, ["launchctl", "bootout", f"{domain}/{label}"])
        for domain in _launchd_domains():
            if _ran(run, ["launchctl", "bootstrap", domain, str(path)]):
                return path
        _forget(path)
        raise ServiceStartError("macOS wouldn't start a background service from this session (common over SSH, "
                                "when nobody is logged in at the Mac's own screen).")
    path.write_text(systemd_unit(command, log))
    _ran(run, ["systemctl", "--user", "daemon-reload"])
    if not _ran(run, ["systemctl", "--user", "enable", "--now", path.name]):
        _forget(path)
        raise ServiceStartError("This computer wouldn't start a background service for your user from this "
                                "session (common over SSH; `loginctl enable-linger` allows it for good).")
    return path


def restart_service(home: Path, run: Callable[..., Any] = subprocess.run) -> None:
    label = service_label(home)
    if sys.platform == "darwin":
        for domain in _launchd_domains():
            if _ran(run, ["launchctl", "kickstart", "-k", f"{domain}/{label}"]):
                return
    else:
        _ran(run, ["systemctl", "--user", "restart", f"{label}.service"])


def remove_service(home: Path, run: Callable[..., Any] = subprocess.run) -> None:
    label, path = service_label(home), service_file(home)
    if sys.platform == "darwin":
        for domain in _launchd_domains():
            _ran(run, ["launchctl", "bootout", f"{domain}/{label}"])
    else:
        _ran(run, ["systemctl", "--user", "disable", "--now", path.name])
    _forget(path)


def rerun_command(action: str, hostname: str, home: Path) -> str:
    """The exact command to run again from the computer's own terminal."""
    parts = ["python3", shlex.quote(str(Path(__file__).resolve())), action] + ([hostname] if hostname else [])
    if Path(home).expanduser() != Path.home() / ".hermes":
        parts += ["--hermes-home", shlex.quote(str(home))]
    return " ".join(parts)


RELEASES_API = "https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
CHECKSUM_LINE_RE = re.compile(r"^\s*([A-Za-z0-9._-]+):\s*([0-9a-f]{64})\s*$", re.MULTILINE)


def release_asset_name(system: str, machine: str) -> str:
    """The release file for this computer, "" when Cloudflare publishes none for it."""
    system, machine = system.lower(), machine.lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, "")
    if not arch:
        return ""
    if system == "darwin":
        return f"cloudflared-darwin-{arch}.tgz"
    if system == "linux":
        return f"cloudflared-linux-{arch}"
    return ""


def bin_dir(home: Path) -> Path:
    """``~/.hermes/bin`` (the Hermes root, shared by every profile), where the download goes."""
    home = Path(home).expanduser()
    root = home.parent.parent if home.parent.name == "profiles" else home
    return root / "bin"


def _fetch(url: str, timeout: float = 120) -> bytes:
    """GET over HTTPS. A python.org Python on macOS often has no CA certificates installed
    (CERTIFICATE_VERIFY_FAILED), so on any failure this retries with the system's curl."""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "speakeasy-cloudflare", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (fixed https GitHub URLs)
            return r.read()
    except Exception:
        curl = shutil.which("curl")
        if not curl:
            raise
    done = subprocess.run([curl, "-fsSL", "--proto", "=https", "--max-time", str(int(timeout)),
                           "-A", "speakeasy-cloudflare", url], capture_output=True, timeout=timeout + 10)
    if done.returncode != 0:
        raise OSError(f"curl exited {done.returncode}")
    return done.stdout


def download_cloudflared(home: Path, out: Callable[[str], None], fetch: Callable[[str], bytes] = _fetch,
                         system: str = "", machine: str = "") -> str:
    """No package manager: download Cloudflare's own release into ``~/.hermes/bin`` and check it twice.

    The archive must match GitHub's SHA-256 digest for that asset, and the program inside must match
    the SHA-256 Cloudflare lists in the release notes. Anything else is refused and nothing is kept.
    Returns the program's path, or "" (with the reason printed)."""
    import io
    import platform
    import tarfile
    name = release_asset_name(system or platform.system(), machine or platform.machine())
    if not name:
        out(f"  Cloudflare publishes no download for this computer ({platform.system()} {platform.machine()}).")
        return ""
    try:
        release = json.loads(fetch(RELEASES_API))
        asset = next(a for a in release.get("assets") or [] if a.get("name") == name)
        digest = str(asset.get("digest") or "")
        listed = dict(CHECKSUM_LINE_RE.findall(str(release.get("body") or "")))
        program_sum = listed.get(name, "")
        if not digest.startswith("sha256:") or not program_sum:
            out("  The release has no checksum to verify the download against, so it wasn't installed.")
            return ""
        out(f"  Downloading {name} {release.get('tag_name', '')} from Cloudflare's GitHub releases…")
        data = fetch(str(asset["browser_download_url"]))
    except StopIteration:
        out(f"  The latest release has no {name}.")
        return ""
    except Exception as exc:
        out(f"  Couldn't download it ({type(exc).__name__}).")
        return ""
    if hashlib.sha256(data).hexdigest() != digest.split(":", 1)[1]:
        out("  The download didn't match GitHub's checksum, so it wasn't installed.")
        return ""
    if name.endswith(".tgz"):
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                member = next(m for m in tar.getmembers() if m.isfile() and Path(m.name).name == "cloudflared")
                data = tar.extractfile(member).read()  # type: ignore[union-attr]
        except (tarfile.TarError, StopIteration, AttributeError, OSError):
            out("  The download wasn't the archive it should be, so it wasn't installed.")
            return ""
    if hashlib.sha256(data).hexdigest() != program_sum:
        out("  The program didn't match the checksum in Cloudflare's release notes, so it wasn't installed.")
        return ""
    target = bin_dir(home) / "cloudflared"
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(".cloudflared.partial")
    partial.write_bytes(data)
    partial.chmod(0o755)
    os.replace(partial, target)
    out(f"  ✓ Installed and verified: {target}")
    return str(target)


def install_cloudflared(out: Callable[[str], None], which: Callable[[str], str | None] = shutil.which,
                        run: Callable[..., Any] = subprocess.run, home: Path | None = None,
                        download: Callable[..., str] = download_cloudflared) -> str:
    """cloudflared's path: already installed, or ``~/.hermes/bin``, or Homebrew when it's there, or a
    verified download of Cloudflare's release (no package manager needed). "" when none works."""
    found = which("cloudflared")
    if found:
        return found
    home = Path(home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    local = bin_dir(home) / "cloudflared"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    if which("brew"):
        out("  Running: brew install cloudflared")
        try:
            run(["brew", "install", "cloudflared"], timeout=900)
        except (OSError, subprocess.SubprocessError):
            pass
        found = which("cloudflared")
        if found:
            return found
    got = download(home, out)
    if got:
        return got
    out("  Install cloudflared from Cloudflare's packages (https://developers.cloudflare.com/cloudflare-one/"
        "connections/connect-networks/downloads/), then run this script again.")
    return ""



QUICK_HOST_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com$")
QUICK_URL_RE = re.compile(r"https://[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com")


def read_quick_url(home: Path, metrics_port: int) -> str:
    """The quick tunnel's address, like the plugin reads it: metrics ``/quicktunnel``, else the log."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{metrics_port}/quicktunnel", timeout=2) as r:  # noqa: S310
            host = str(json.loads(r.read() or b"{}").get("hostname") or "").lower()
        if QUICK_HOST_RE.fullmatch(host):
            return f"https://{host}"
    except Exception:
        pass
    try:
        found = QUICK_URL_RE.findall(log_path(home).read_text(errors="replace")[-256 * 1024:])
        return found[-1] if found else ""
    except OSError:
        return ""


def wait_for_quick_url(home: Path, metrics_port: int, timeout_s: float = 45.0,
                       read: Callable[[Path, int], str] = read_quick_url,
                       sleep: Callable[[float], None] = None, clock: Callable[[], float] = None) -> str:
    """Verify step: wait until the new tunnel reports its address ("" when it never does)."""
    import time
    sleep, clock = sleep or time.sleep, clock or time.monotonic
    deadline = clock() + timeout_s
    while True:
        url = read(home, metrics_port)
        if url or clock() >= deadline:
            return url
        sleep(1.0)


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
    binary = install_cloudflared(print, home=home)
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
    try:
        install_service(home, binary, args.port, args.metrics_port, named=NAMED_TUNNEL if hostname else "")
    except ServiceStartError as problem:
        where = "Terminal on the Mac itself" if sys.platform == "darwin" else "a terminal on this computer itself"
        print(f"{problem} Run this once from {where} (or after logging in to it): "
              f"{rerun_command(args.action, hostname, home)}")
        return 1
    hermes_config(home, ("server.tunnel", "cloudflare"), ("server.tunnel_metrics_port", str(args.metrics_port)),
                  ("server.tunnel_hostname", hostname or '""'))
    if not hostname:
        url = wait_for_quick_url(home, args.metrics_port)
        if not url:
            print(f"The tunnel service started, but it hasn't reported an address yet (log: {log_path(home)}). "
                  "Check the log, then run `python3 speakeasy-cloudflare.py restart`.")
            return 1
        print(f"✓ Cloudflare address is up: {url}")
    print("Cloudflare tunnel service is running. Now run `hermes voice setup` (it shows the address and the "
          "pairing link).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
