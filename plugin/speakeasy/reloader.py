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
* if the new code fails to import or start, put the old modules back and restart the old server;
* close every state-store connection the old code held before the next code opens its own, and
  check that none are left (SPK-443: leftovers kept stale -wal/-shm handles in the gateway process,
  and every later open of the same file failed with "disk I/O error");
* if a build still fails with "disk I/O error", back the database up from a separate process, move
  the copy over the original (a new file, so none of this process's stale SQLite state applies to
  it), drop the old -wal/-shm, keep the old files aside, and try once more.

The contract with the swapped code is small and must stay stable: ``router.bind_home(home)``,
``service.VoiceService(home)`` with ``.busy()``, ``server.SpeakeasyServer(service, host, port)``
with ``.start()``, ``.stop()``, ``.service`` and ``.base_url``.
"""
from __future__ import annotations

import gc
import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
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
DB_NAME = "state.sqlite3"
FRESH_EVERY_S = 60.0   # the new-file recovery runs at most this often
KEEP_RECOVERED = 3     # old database copies kept aside after a recovery
BACKUP_TIMEOUT_S = 120.0
# Runs in a separate process: this one's stale SQLite state for the file is what broke it.
_BACKUP = (
    "import sqlite3, sys\n"
    "src = sqlite3.connect(sys.argv[1], timeout=10)\n"
    "dst = sqlite3.connect(sys.argv[2])\n"
    "src.backup(dst)\n"
    "ok = dst.execute('PRAGMA integrity_check').fetchone()[0]\n"
    "dst.execute('PRAGMA journal_mode=DELETE')\n"
    "dst.close(); src.close()\n"
    "print(ok)\n"
)


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


def _fd_path(fd: int) -> str | None:
    if sys.platform == "darwin":
        import fcntl
        try:
            raw = fcntl.fcntl(fd, getattr(fcntl, "F_GETPATH", 50), bytes(1024))
        except OSError:
            return None
        return raw.split(b"\0", 1)[0].decode(errors="replace") or None
    try:
        return os.readlink(f"/dev/fd/{fd}")  # Linux: the file's path, " (deleted)" when it's gone
    except OSError:
        return None


def open_db_handles(db: Path) -> list[str]:
    """This process's open file handles on the database and its -wal/-shm, including ones whose file
    was deleted or replaced underneath (they still count: they are what broke later opens)."""
    names: set[str] = set()
    for base in {str(db), os.path.realpath(db)}:
        names |= {base, base + "-wal", base + "-shm", base + "-journal"}
    try:
        fds = [int(n) for n in os.listdir("/dev/fd")]
    except (OSError, ValueError):
        return []
    out = []
    for fd in fds:
        path = _fd_path(fd)
        if path is None:
            continue
        if path.removesuffix(" (deleted)") in names:
            out.append(path)
    return out


def is_disk_io_error(exc: BaseException) -> bool:
    return type(exc).__name__ == "OperationalError" and "disk i/o error" in str(exc).lower()


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
        self.handles_left = 0       # database handles still open after the last release (should be 0)
        self.fresh_files = 0        # times the database was moved to a new file (disk I/O recovery)
        self._fresh_at = -FRESH_EVERY_S
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- files the CLI and the reloader share --------------------------------------------------
    @property
    def state_dir(self) -> Path:
        return self.home / "speakeasy"

    @property
    def db_path(self) -> Path:
        return self.state_dir / DB_NAME

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    # -- database handles (SPK-443) ------------------------------------------------------------
    def _close_stores(self, modules: dict[str, ModuleType] | None = None) -> int:
        """Close every state store opened by one set of modules (default: what's loaded now), even
        one whose service never finished building."""
        mods = modules if modules is not None else self._owned()
        store_mod = mods.get(f"{self.package}.store")
        close_all = getattr(store_mod, "close_all", None)
        closed = 0
        if callable(close_all):
            try:
                closed = int(close_all() or 0)
            except Exception as exc:
                logger.warning("speakeasy: closing the old state store failed: %s", exc)
        return closed

    def _release(self, modules: dict[str, ModuleType] | None, why: str) -> int:
        """After a server stopped (or a build failed): close its stores, drop unreachable ones, and
        check no handle on the database is left. Returns how many are left (0 = clean)."""
        closed = self._close_stores(modules)
        gc.collect()  # a half-built service's store is only reachable from a dead cycle
        left = open_db_handles(self.db_path)
        self.handles_left = len(left)
        logger.info("speakeasy: reload.db closed=%d left=%d (%s)", closed, len(left), why)
        if left:
            logger.error("speakeasy: %d database handle(s) still open after %s (should be none): %s",
                         len(left), why, ", ".join(sorted({Path(p).name for p in left})))
        return len(left)

    def _fresh_db(self) -> bool:
        """The "disk I/O error" recovery: a consistent backup made by a separate process, moved over
        the original so it is a new file, the old -wal/-shm removed, the old files kept aside.
        Returns True when the database was replaced."""
        db = self.db_path
        now = time.monotonic()
        if not db.exists() or now - self._fresh_at < FRESH_EVERY_S or not sys.executable:
            return False
        self._fresh_at = now
        tmp = db.with_name(DB_NAME + ".fresh")
        tmp.unlink(missing_ok=True)
        try:
            done = subprocess.run([sys.executable, "-c", _BACKUP, str(db), str(tmp)], capture_output=True,
                                  text=True, timeout=BACKUP_TIMEOUT_S, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("speakeasy: database recovery could not run the backup: %s", exc)
            tmp.unlink(missing_ok=True)
            return False
        if done.returncode != 0 or done.stdout.strip() != "ok" or not tmp.exists():
            logger.error("speakeasy: database recovery stopped, the backup was not clean (%s): %s",
                         done.returncode, (done.stdout.strip() or done.stderr.strip())[-300:])
            tmp.unlink(missing_ok=True)
            return False
        keep = self.backups_dir / f"recovered-{time.strftime('%Y%m%d-%H%M%S')}"
        try:
            keep.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                old = db.with_name(DB_NAME + suffix)
                if old.exists():
                    shutil.copy2(old, keep / old.name)
            os.replace(tmp, db)
            for suffix in ("-wal", "-shm"):
                db.with_name(DB_NAME + suffix).unlink(missing_ok=True)
        except OSError as exc:
            logger.error("speakeasy: database recovery failed while swapping files: %s", exc)
            tmp.unlink(missing_ok=True)
            return False
        for old in sorted(self.backups_dir.glob("recovered-*"))[:-KEEP_RECOVERED]:
            shutil.rmtree(old, ignore_errors=True)
        self.fresh_files += 1
        logger.warning("speakeasy: reload.db recovered: the state database failed with \"disk I/O error\"; moved "
                       "a checked backup into place as a new file (old files kept in backups/%s)", keep.name)
        return True

    def _build_safe(self) -> Any:
        """Build a server; on "disk I/O error" close what the attempt opened, do the new-file recovery
        once, and try again."""
        try:
            return self._build()
        except Exception as exc:
            if not is_disk_io_error(exc):
                raise
            self._release(None, "a build failed with disk I/O error")
            if not self._fresh_db():
                raise
            importlib.invalidate_caches()
            logger.warning("speakeasy: reload.db retrying the build once on the recovered database")
            return self._build()

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
            self.server = self._build_safe()
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
                    self._release(self.modules, "stopping the server")

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
            old_server = None
            self._release(old_modules, "stopping " + (old_version or "the old server"))
            self._evict()
            importlib.invalidate_caches()
            try:
                self.server = self._build_safe()
            except Exception as exc:
                logger.error("speakeasy: new plugin code failed to load, keeping %s: %s: %s",
                             old_version, type(exc).__name__, exc)
                self._release(None, "the new code failed to load")
                self._restore(old_modules)
                self.failed_fp = target_fp
                self.server = None
                try:
                    self.server = self._build_safe()
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
                self.server = self._build_safe()
            except Exception as exc:
                self.server = None
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
                self._release(None, f"a {label} build failed")
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
                 "running": self.server is not None, "db_handles_left": self.handles_left,
                 "fresh_files": self.fresh_files}))
        except OSError as exc:
            logger.warning("speakeasy: could not record the reload result: %s", exc)
