"""A live reload leaves no database connection behind, and recovers from "disk I/O error" (SPK-443).

Seen live: the gateway held ten connections to state.sqlite3 from earlier plugin generations, with
-wal/-shm handles on files that no longer existed; every new open of the file in that process then
failed with "disk I/O error", so a reload and its fallback both failed and voice was down.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

from test_live_reload import _free_port, _health, _set_version, _settle, plugin  # noqa: F401 (fixture)

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="counts handles via /dev/fd")


def _handles(r, mod) -> int:
    return len(mod.open_db_handles(r.db_path))


def test_twenty_reloads_keep_the_database_handle_count_flat(plugin):
    r, pkg, port, name, mod = plugin
    baseline = _handles(r, mod)
    assert baseline >= 1                                  # the running server's own connection
    for i in range(20):
        _set_version(pkg, f"1.1.{i}")
        assert _settle(r)[-1] == "reloaded", i
        assert _health(port)["version"] == f"1.1.{i}"
        assert r.handles_left == 0, i                     # nothing left once the old server stopped
        assert _handles(r, mod) == baseline, i
    assert json.loads((r.state_dir / "reload-result.json").read_text())["db_handles_left"] == 0


def test_a_failed_reload_closes_the_store_the_half_built_server_opened(plugin):
    r, pkg, port, name, mod = plugin
    baseline = _handles(r, mod)
    # Breaks after the new StateStore is open: the half-built service is never returned or stopped.
    conf = pkg / "confirms.py"
    conf.write_text(conf.read_text() + "\n\ndef recover(store):\n    raise RuntimeError('broken update')\n")
    _set_version(pkg, "1.2.0")
    assert _settle(r)[-1] == "failed"
    assert _health(port)["version"] == "1.0.0"            # the old version came back
    assert r.handles_left == 0
    assert _handles(r, mod) == baseline


def test_a_store_closes_once_and_close_all_reaches_every_open_one(home):
    from speakeasy import store as S
    a = S.StateStore(home / "speakeasy" / "a.sqlite3")
    b = S.StateStore(home / "speakeasy" / "b.sqlite3")
    a.close()
    a.close()                                             # idempotent
    assert a.closed and not b.closed
    assert S.close_all() >= 1
    assert b.closed


def test_disk_io_error_from_stale_handles_is_recovered_with_a_new_file(plugin, caplog):
    """The live incident end to end: a connection left over from an older generation, its -wal/-shm
    deleted underneath it. Opening the file again in this process fails; the reload moves a checked
    backup into place as a new file and comes up, with the data kept."""
    r, pkg, port, name, mod = plugin
    db = r.db_path
    store = r.server.service.store
    store.set_meta("spk443-probe", "kept")
    store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # in the main file, as the live one was
    leaked = sqlite3.connect(db, check_same_thread=False)  # an old generation's connection
    leaked.execute("PRAGMA journal_mode=WAL")
    leaked.execute("SELECT count(*) FROM meta").fetchone()
    # Stop the running server so its own clean close doesn't checkpoint around the damage, then
    # remove the files the leftover still holds (what the gateway's lsof showed).
    r.server.stop()
    for suffix in ("-wal", "-shm"):
        (db.parent / (db.name + suffix)).unlink(missing_ok=True)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        probe = sqlite3.connect(db)
        probe.execute("PRAGMA journal_mode=WAL")
        probe.execute("SELECT count(*) FROM meta").fetchone()
    old_inode = os.stat(db).st_ino
    r.server = None
    r.retry_at = 0.0
    caplog.set_level("WARNING")
    assert r.tick() == "recovered"
    assert _health(port)["version"] == "1.0.0"
    assert os.stat(db).st_ino != old_inode                 # a new file, so no stale state applies
    assert r.server.service.store.get_meta("spk443-probe") == "kept"
    assert r.fresh_files == 1
    kept = list(r.state_dir.glob("recovered-*/state.sqlite3"))
    assert len(kept) == 1                                  # the old file is kept aside
    assert any("disk I/O error" in rec.getMessage() and "new file" in rec.getMessage() for rec in caplog.records)
    leaked.close()


def test_other_build_failures_never_touch_the_database(plugin):
    r, pkg, port, name, mod = plugin
    inode = os.stat(r.db_path).st_ino
    real = r._build
    calls = []

    def flaky():
        if not calls:
            calls.append(1)
            raise sqlite3.OperationalError("database is locked")
        return real()
    r._build = flaky
    _set_version(pkg, "1.3.0")
    assert _settle(r)[-1] == "failed"                     # not a disk I/O error: no recovery
    assert r.fresh_files == 0 and os.stat(r.db_path).st_ino == inode
    assert not list(r.state_dir.glob("recovered-*"))
