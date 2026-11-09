"""Live reload: pick up a reinstalled plugin without restarting the Hermes gateway.

``hermes plugins install`` copies new files into place while the gateway keeps running. Hermes may
re-import some of them, and then new and old Speakeasy code run side by side (seen live: new call
code calling the old ``Runtime.route`` broke every handoff until a restart).

This module and ``adapter.py`` are the stable shell that stays loaded. Everything else is swapped
as a whole:

* keep running code consistent: if someone re-imports Speakeasy modules underneath us, put the
  running ones back (they only get replaced by a reload below);
* notice new files on disk (a stable fingerprint over two checks, so a half-copied install is
  never loaded);
* when no call is live (running and held jobs carry on through it), or when ``hermes voice reload``
  asks, stop the server, drop every non-shell module, import the new code fresh and start again on
  the same port;
* if the new code fails to import or start, put the old modules back and restart the old server.

The contract with the swapped code is small and must stay stable: ``router.bind_home(home)``,
``service.VoiceService(home)`` with ``.busy()``, ``server.SpeakeasyServer(service, host, port)``
with ``.start()``, ``.stop()``, ``.service`` and ``.base_url``.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any

logger = logging.getLogger(__name__)

SHELL = {"adapter", "reloader"}   # never swapped: the gateway holds the adapter instance
CHECK_S = 2.0
RETRY_MIN_S = 5.0     # with no server running, try to start one again this soon...
RETRY_MAX_S = 60.0    # ...backing off to this
REQUEST_FILE = "reload-request"
RESULT_FILE = "reload-result.json"
RESTORE_LOG_EVERY_S = 3600.0  # a repeating re-import is logged at most this often (with a count)


def fingerprint(plugin_dir: Path) -> str:
    """A hash over every source file of the plugin (names, sizes and contents)."""
    digest = hashlib.sha256()
    for path in sorted(plugin_dir.rglob("*")):
        if "__pycache__" in path.parts or not path.is_file() or path.suffix not in {".py", ".yaml", ".md", ".json"}:
            continue
        digest.update(str(path.relative_to(plugin_dir)).encode())
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
    return digest.hexdigest()


def manifest_version(plugin_dir: Path) -> str:
    try:
        text = (plugin_dir / "plugin.yaml").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r"^version:\s*['\"]?([0-9][0-9A-Za-z.+-]*)", text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


class LiveReloader:
    def __init__(self, package: str, plugin_dir: Path, home: Path, host: str, port: int,
                 check_s: float = CHECK_S):
        self.package, self.plugin_dir, self.home = package, Path(plugin_dir), Path(home)
        self.host, self.port, self.check_s = host, port, check_s
        self.server: Any = None
        self.modules: dict[str, ModuleType] = {}
        self.loaded_fp = ""
        self.seen_fp = ""          # last fingerprint seen on disk (loads only once it holds still)
        self.failed_fp = ""        # a fingerprint that failed to load: don't retry until files change
        self.pending = False
        self.version = ""
        self.retry_s = RETRY_MIN_S  # with no server: wait before the next attempt (doubles to RETRY_MAX_S)
        self.retry_at = 0.0         # monotonic time of the next attempt to start a server again
        self.attempts = 0           # failed attempts since the server went down
        self.restores = 0           # times the running code was put back (see _note_restore)
        self._restore_cause: tuple[tuple[str, ...], tuple[str, ...]] | None = None
        self._restore_logged = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- files the CLI and the reloader share --------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return self.home / "speakeasy"

    # -- modules -------------------------------------------------------------------------------
    def _owned(self) -> dict[str, ModuleType]:
        prefix = self.package + "."
        return {name: mod for name, mod in list(sys.modules.items())
                if name.startswith(prefix) and mod is not None
                and name[len(prefix):].split(".")[0] not in SHELL}

    def _evict(self) -> None:
        for name in self._owned():
            sys.modules.pop(name, None)
            parent, _, child = name.rpartition(".")
            if parent in sys.modules and getattr(sys.modules[parent], child, None) is not None:
                try:
                    delattr(sys.modules[parent], child)
                except AttributeError:
                    pass

    def _restore(self, modules: dict[str, ModuleType]) -> None:
        self._evict()
        for name, mod in modules.items():
            sys.modules[name] = mod
            parent, _, child = name.rpartition(".")
            if parent in sys.modules:
                setattr(sys.modules[parent], child, mod)

    def _build(self) -> Any:
        package = importlib.import_module(self.package)
        version = manifest_version(self.plugin_dir)
        setattr(package, "__version__", version)  # server/service read it at import
        importlib.import_module(f"{self.package}.router").bind_home(self.home)
        service_mod = importlib.import_module(f"{self.package}.service")
        server_mod = importlib.import_module(f"{self.package}.server")
        server = server_mod.SpeakeasyServer(service_mod.VoiceService(self.home), self.host, self.port)
        server.start()
        self.version = version
        return server

    # -- lifecycle -----------------------------------------------------------------------------
    def start(self) -> Any:
        """Start the server with the code already loaded, then watch for updates."""
        with self._lock:
            self.loaded_fp = self.seen_fp = fingerprint(self.plugin_dir)
            self.server = self._build()
            self.modules = self._owned()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="speakeasy-reloader")
        self._thread.start()
        return self.server

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            if self.server is not None:
                try:
                    self.server.stop()
                finally:
                    self.server = None

    def _loop(self) -> None:
        while not self._stop.wait(self.check_s):
            try:
                self.tick()
            except Exception as exc:  # never let the watcher die
                logger.warning("speakeasy: live reload check failed: %s", exc)

    def _note_restore(self, differs: list[str], owned: dict[str, ModuleType]) -> None:
        """Log a re-import underneath the running server once per cause, then at most every
        RESTORE_LOG_EVERY_S with a count (SPK-270: a repeating cause logged every 2 s, about 1,800 lines
        an hour, and agent.log rotated away the evidence for bug reports). Names which modules
        differed and whether they were missing or replaced, so the cause can be found."""
        missing = sorted(n for n in differs if n not in owned)
        replaced = sorted(n for n in differs if n in owned)
        cause = (tuple(missing), tuple(replaced))
        self.restores += 1
        now = time.monotonic()
        if cause == self._restore_cause and now - self._restore_logged < RESTORE_LOG_EVERY_S:
            return
        short = lambda names: ", ".join(n[len(self.package) + 1:] for n in names[:6]) + (" …" if len(names) > 6 else "")
        logger.info("speakeasy: plugin files were re-imported underneath the running server; kept the running "
                    "code until the update loads (%d time(s) so far; missing: %s; replaced: %s)",
                    self.restores, short(missing) or "none", short(replaced) or "none")
        self._restore_cause, self._restore_logged = cause, now

    # -- one check -----------------------------------------------------------------------------
    def tick(self) -> str | None:
        """Keep running code consistent, notice new files, reload when idle or asked.
        Returns what happened ("restored", "reloaded", "failed") or None."""
        with self._lock:
            if self.server is None:
                return self._recover()
            outcome = None
            owned = self._owned()
            differs = [name for name, mod in self.modules.items() if owned.get(name) is not mod]
            if differs:
                self._restore(self.modules)
                outcome = "restored"
                self._note_restore(differs, owned)
            else:
                # Only new names: the running code lazily imported a module it hadn't used yet (the
                # first pairing imports address_drop). Adopt it. Restoring here evicted the module
                # while its import was still running, so that request failed with a KeyError.
                self.modules.update({name: mod for name, mod in owned.items() if name not in self.modules})
            forced = (self.state_dir / REQUEST_FILE).exists()
            current = fingerprint(self.plugin_dir)
            steady = current == self.seen_fp
            self.seen_fp = current
            changed = current != self.loaded_fp and current != self.failed_fp
            self.pending = changed
            if forced or (changed and steady and not self._busy()):
                outcome = self.reload(forced=forced)
            return outcome

    def _busy(self) -> bool:
        try:
            return bool(self.server.service.busy())
        except Exception:
            return True  # can't tell: don't cut anything off; `hermes voice reload` still works

    def reload(self, forced: bool = False) -> str:
        with self._lock:
            old_server, old_modules, old_version = self.server, dict(self.modules), self.version
            target_fp = fingerprint(self.plugin_dir)
            started = time.monotonic()
            try:
                old_server.stop()
            except Exception as exc:
                logger.warning("speakeasy: stopping the old server failed: %s", exc)
            self._evict()
            importlib.invalidate_caches()
            try:
                self.server = self._build()
            except Exception as exc:
                logger.error("speakeasy: new plugin code failed to load, keeping %s: %s: %s",
                             old_version, type(exc).__name__, exc)
                self._restore(old_modules)
                self.failed_fp = target_fp
                self.server = None
                try:
                    self.server = self._build()
                except Exception as again:
                    # Seen live: a transient "disk I/O error" failed both. Never stay down: tick() keeps
                    # trying (at once on `hermes voice reload`, otherwise with backoff).
                    logger.error("speakeasy: restarting the previous version failed too, voice server is down; "
                                 "retrying in %.0fs: %s: %s", RETRY_MIN_S, type(again).__name__, again)
                    self._schedule_retry(reset=True)
                self.modules = self._owned()
                if self.server is None:
                    self.modules = old_modules  # what to fall back to while recovering
                self._write_result(False, old_version, f"{type(exc).__name__}: {exc}")
                self.pending = False
                return "failed"
            self.modules = self._owned()
            self.loaded_fp = self.seen_fp = target_fp
            self.failed_fp = ""
            self.pending = False
            logger.info("speakeasy: reloaded plugin %s -> %s in %.1fs%s", old_version, self.version,
                        time.monotonic() - started, " (asked)" if forced else "")
            self._write_result(True, self.version, "")
            return "reloaded"

    # -- no server running: keep trying --------------------------------------------------------
    def _schedule_retry(self, reset: bool = False) -> None:
        if reset:
            self.retry_s, self.attempts = RETRY_MIN_S, 0
        self.retry_at = time.monotonic() + self.retry_s
        self.retry_s = min(self.retry_s * 2, RETRY_MAX_S)

    def _recover(self) -> str | None:
        """The server is down (a reload and its fallback both failed). Try again: right away when
        `hermes voice reload` asks, otherwise once the backoff (5s doubling to 60s) is up. The code
        on disk goes first, then the last code that ran."""
        forced = (self.state_dir / REQUEST_FILE).exists()
        if not forced and time.monotonic() < self.retry_at:
            return None
        self.attempts += 1
        current = fingerprint(self.plugin_dir)
        fallback = dict(self.modules)
        # The disk code goes first even if it failed before: the failure may have been transient.
        tries: list[tuple[str, dict[str, ModuleType] | None]] = [("disk", None)]
        if fallback:
            tries.append(("previous", fallback))
        errors: list[str] = []
        for label, modules in tries:
            if modules is None:
                self._evict()
            else:
                self._restore(modules)
            importlib.invalidate_caches()
            try:
                self.server = self._build()
            except Exception as exc:
                self.server = None
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
                continue
            self.modules = self._owned()
            if modules is None:
                self.loaded_fp = self.seen_fp = current
                self.failed_fp = ""
            self.pending = self.loaded_fp != current
            logger.info("speakeasy: voice server recovered (%s code, version %s) after %d attempt%s%s",
                        label, self.version, self.attempts, "" if self.attempts == 1 else "s",
                        " (asked)" if forced else "")
            self._schedule_retry(reset=True)
            self.retry_at = 0.0
            self._write_result(True, self.version, "")
            return "recovered"
        if fallback:
            self._restore(fallback)  # keep the last good set for the next try
        wait = self.retry_s
        self._schedule_retry()
        error = "; ".join(errors) or "nothing to load"
        logger.warning("speakeasy: voice server still down after attempt %d, next try in %.0fs: %s",
                       self.attempts, wait, error)
        self._write_result(False, self.version, error)
        return "failed"

    def _write_result(self, ok: bool, version: str, error: str) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / REQUEST_FILE).unlink(missing_ok=True)
            (self.state_dir / RESULT_FILE).write_text(json.dumps(
                {"ok": ok, "version": version, "error": error[:500], "at": time.time(),
                 "running": self.server is not None}))
        except OSError as exc:
            logger.warning("speakeasy: could not record the reload result: %s", exc)
