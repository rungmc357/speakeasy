"""Process-wide state a test can leave behind for every test after it: ``sys.path`` (plus the import
machinery that reads it) and ``os.environ``.

Two jobs:

* ``import_hermes_bootstrap_inert()`` — Hermes' entry-point bootstrap (``hermes_bootstrap``, imported
  first thing by ``gateway.run``, ``run_agent``, ``hermes_cli.main`` …) *activates the install's
  dependency environment* when it is imported: it drops this interpreter's ``site-packages`` from
  ``sys.path``, adds the committed PM environment's (which can be built for another Python, e.g.
  ``lib/python3.14/site-packages`` under a 3.11 test run), and rewrites ``PYTHONPATH``, ``PATH`` and
  ``TMPDIR``. In a server process that is the point; in a test process it means the first test that
  happens to import ``gateway.run`` swaps the import path for the rest of the run: later imports of
  compiled packages (``PIL._imaging``) resolve to the foreign environment and fail, and every child a
  later test spawns inherits the foreign ``PYTHONPATH``. Importing the bootstrap once, up front, with
  activation and launch relaunching switched off and the path/environment restored afterwards, makes
  every later ``import hermes_bootstrap`` a cached no-op.

* ``ProcessState`` — a snapshot of ``sys.path``/import hooks/``os.environ``/cwd that a per-test
  fixture takes before each test and puts back after it.
"""
from __future__ import annotations

import contextlib
import importlib
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field


@dataclass
class ProcessState:
    path: list[str] = field(default_factory=lambda: list(sys.path))
    path_hooks: list = field(default_factory=lambda: list(sys.path_hooks))
    meta_path: list = field(default_factory=lambda: list(sys.meta_path))
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    cwd: str | None = field(default_factory=lambda: _cwd())

    def path_changes(self) -> str | None:
        """A readable description of how ``sys.path`` differs from the snapshot, or None."""
        if sys.path == self.path:
            return None
        added = [p for p in sys.path if p not in self.path]
        removed = [p for p in self.path if p not in sys.path]
        if added or removed:
            return f"added {added}, removed {removed}"
        return f"reordered to {sys.path[:6]} …"

    def foreign_path_changes(self) -> list[str]:
        """The harmful kind of leftover change: ``site-packages`` directories built for another
        Python added to ``sys.path``, or this interpreter's own ``site-packages`` dropped from it.
        (Hermes modules prepending their own checkout at import time are a one-off import side
        effect; the per-test restore undoes those without failing whichever test imported first.)"""
        problems = [f"added {p}" for p in sys.path if p not in self.path and _foreign_site(p)]
        problems += [f"removed {p}" for p in self.path if p not in sys.path and _own_site(p)]
        return problems

    def environ_changes(self) -> list[str]:
        now = os.environ
        return sorted({k for k in now.keys() ^ self.environ.keys()}
                      | {k for k in now.keys() & self.environ.keys() if now[k] != self.environ[k]})

    def restore(self) -> None:
        foreign = [p for p in sys.path if p not in self.path and _foreign_site(p)]
        if sys.path != self.path:
            sys.path[:] = self.path
            sys.path_importer_cache.clear()
        if sys.path_hooks != self.path_hooks:
            sys.path_hooks[:] = self.path_hooks
            sys.path_importer_cache.clear()
        if sys.meta_path != self.meta_path:
            sys.meta_path[:] = self.meta_path
        changed = self.environ_changes()
        for key in changed:
            if key in self.environ:
                os.environ[key] = self.environ[key]
            else:
                os.environ.pop(key, None)
        if {"TMPDIR", "TEMP", "TMP"} & set(changed):
            tempfile.tempdir = None  # tempfile caches the directory it first resolved
        if self.cwd is not None and _cwd() != self.cwd:
            with contextlib.suppress(OSError):
                os.chdir(self.cwd)
        if foreign:  # a module loaded from the other environment would outlive the path entry
            for name, module in list(sys.modules.items()):
                origin = getattr(module, "__file__", None) or ""
                if any(origin.startswith(p.rstrip("/\\") + os.sep) for p in foreign):
                    del sys.modules[name]
        importlib.invalidate_caches()


_SITE = re.compile(r"[\\/]lib[\\/]python(\d+)\.(\d+)[\\/](?:site|dist)-packages\b")


def _site_version(entry: str) -> tuple[int, int] | None:
    match = _SITE.search(str(entry))
    return (int(match.group(1)), int(match.group(2))) if match else None


def _foreign_site(entry: str) -> bool:
    """A ``…/lib/pythonX.Y/site-packages`` directory for a Python other than the running one."""
    version = _site_version(entry)
    return version is not None and version != sys.version_info[:2]


def _own_site(entry: str) -> bool:
    return _site_version(entry) == sys.version_info[:2]


def foreign_site_entries(path: list[str] | None = None) -> list[str]:
    return [p for p in (sys.path if path is None else path) if _foreign_site(p)]


def _cwd() -> str | None:
    try:
        return os.getcwd()
    except OSError:  # a test removed the directory the run sits in
        return None


def _patched(module_name: str, attr: str, replacement):
    """Swap ``module.attr`` for ``replacement`` if the module and attribute exist (Hermes versions
    differ); returns an undo callable."""
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return lambda: None
    if not hasattr(module, attr):
        return lambda: None
    original = getattr(module, attr)
    setattr(module, attr, replacement)
    return lambda: setattr(module, attr, original)


def import_hermes_bootstrap_inert(module_name: str = "hermes_bootstrap") -> bool:
    """Import Hermes' entry-point bootstrap without letting it switch this process's interpreter
    environment, then put ``sys.path``/import hooks/``os.environ`` back exactly as they were.

    True when the bootstrap is now imported (or was already), False when this Hermes has none."""
    if module_name in sys.modules:
        return True
    before = ProcessState()
    undo = []
    try:
        # Never select/activate the install's PM environment, and never re-exec this process into
        # another interpreter: the test run already runs on the interpreter it was given.
        undo.append(_patched("pm.environments", "activate_dependencies", lambda project_root: None))
        undo.append(_patched("hermes_cli.venv_sync", "prepare_launch", lambda project_root, argv: None))
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            if exc.name != module_name:
                raise
            return False
        return True
    finally:
        for fn in reversed(undo):
            fn()
        before.restore()
