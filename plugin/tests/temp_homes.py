"""The one owner of every ``speakeasy-test-*`` folder the plugin's tests make in the temp folder.

- ``new_home()`` makes one and records it for this process.
- ``remove(path)`` deletes it early (the ``home`` fixture does, after each test).
- Every recorded home is deleted (again) when the process exits, pass or fail.
- ``sweep()`` deletes what a killed run left: ``speakeasy-test-*`` directories directly in the temp
  folder, last modified more than ``MAX_AGE_S`` ago. Fresh ones belong to a run still going.

Stdlib only: the parallel runner imports it without the plugin or Hermes on its path.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import time
from pathlib import Path

PREFIX = "speakeasy-test-"
MAX_AGE_S = 6 * 3600  # far longer than any run (the parallel runner stops a shard at 25 min)

_made: set[Path] = set()


def new_home() -> Path:
    path = Path(tempfile.mkdtemp(prefix=PREFIX))
    _made.add(path)
    return path


def remove(path: Path) -> None:
    # Stays recorded: a service thread that outlives its test can write the home back.
    shutil.rmtree(path, ignore_errors=True)


@atexit.register
def remove_all() -> None:
    for path in _made:
        remove(path)


def sweep(root: str | os.PathLike | None = None, max_age_s: float = MAX_AGE_S) -> list[str]:
    """Delete stale ``speakeasy-test-*`` directories directly in *root* (default: the temp folder).
    Returns the names removed. Symlinks, files and other names are never touched."""
    root = Path(root if root is not None else tempfile.gettempdir())
    cutoff = time.time() - max_age_s
    removed: list[str] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return removed
    for entry in entries:
        try:
            if (not entry.name.startswith(PREFIX) or not entry.is_dir(follow_symlinks=False)
                    or entry.stat(follow_symlinks=False).st_mtime > cutoff):
                continue
        except OSError:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        removed.append(entry.name)
    return sorted(removed)
