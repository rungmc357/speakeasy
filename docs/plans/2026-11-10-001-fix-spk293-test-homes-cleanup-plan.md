# SPK-293: plugin tests must not leave their temp homes behind

## Cause (at e2d8cc1)

- `plugin/tests/fakes.py:365` `make_home()` is a bare `tempfile.mkdtemp(prefix="speakeasy-test-")`;
  nothing removes it unless the caller does.
  - `conftest.py:122-126` `home` fixture removes its own after the test (teardown runs on failure too).
  - `test_credentials_rule.py:33,47` call `make_home()` and never remove it: 2 per run.
  - `test_live_reload.py:48` removes it in fixture teardown, which never runs if setup fails after
    line 48; its own `speakeasy-reload-` mkdtemp (line 42) has the same gap.
  - `e2e_push.py:27` (manual script) never removes it.
- A killed run (SIGKILL, the parallel runner's `p.kill()` on a 25 min shard timeout) runs no
  teardown and no atexit: everything it made stays, and nothing ever sweeps it.
- `scripts/plugin-tests-parallel.py:128` makes `pt-*` (every shard's TMPDIR, holding the shards'
  homes and pytest tmp folders) and never removes it, on any exit.
- Measured (full suite at e2d8cc1, own TMPDIR, spawn probe): 33 `speakeasy-test-*` left. 27 are
  homes a `hermes send` child was launched into (44 launches from ~40 tests in 12 files, e.g.
  `test_follow_through`, `test_routing`, `test_screen_dispatch`); the child wrote them back with
  `installs/` after the fixture removed them. 3 held only `speakeasy/` (a service thread wrote it
  after teardown), 2 only `.env` (test_credentials_rule), 1 is a file:
  `test_visual_review.py:156` writes `<home>-outside.png` next to the home and never removes it.
- Full Hermes homes (`installs/`): e.g. `test_follow_through.py:91,105,117,128` set
  `delivery.target=telegram:555` on the conftest `service`, built with `notifier=None`, which
  `service.py:380` turns into a real `HermesSendNotifier`. `delivery.py:53` runs
  `python -m hermes_cli.main send` with `HERMES_HOME=<speakeasy-test-*>`. Hermes' launcher
  (`hermes_cli/venv_sync.py:425 prepare_launch` → `:536 _prepare_borrowed_launch`) gives a
  borrowing home its own dependency environment under `<home>/installs` unless
  `HERMES_DISABLE_LAZY_INSTALLS` is set, which the CONTRIBUTING.md `pytest tests` command doesn't set.
  The child can outlive its 60 s timeout and the fixture's rmtree.

## Data shape

- Owner: `plugin/tests/temp_homes.py`, the only place a `speakeasy-test-*` folder is made.
  `new_home()` records it in the creating process; `remove(path)` deletes it early (the `home`
  fixture) and keeps it recorded; an atexit hook deletes every recorded home again, pass or fail,
  so one written back after its test still goes.
- Leftover: a directory (not a symlink, not a file) directly in `tempfile.gettempdir()` whose name
  starts `speakeasy-test-` and whose mtime is more than 6 h old. Its owner is gone: no run lasts
  6 h (shards stop at 25 min). Fresh ones belong to a run that may still be going and are kept.
- `sweep()` removes leftovers. It runs at every pytest session start (conftest) and at the start
  of the parallel runner. It never follows a symlink (`is_dir(follow_symlinks=False)`, rmtree's
  fd-based walk).
- The parallel runner's shard folder is a `new_home()` too: deleted when the runner exits, swept
  by a later run when the runner was killed.

## Units

1. `temp_homes.py`; `make_home()` and the `home` fixture go through it; conftest sweeps at
   session start; `test_credentials_rule` uses the `home` fixture; `test_live_reload` uses `home`
   and `tmp_path` (its own mkdtemp/rmtree pair goes).
2. Parallel runner: `sweep()` first, shard folder from `new_home()`.
3. conftest, at import for the whole run: a `HermesSendNotifier` left with the real launcher starts
   no process and fails (what a real send from a test home returns); `HERMES_DISABLE_LAZY_INSTALLS`
   defaults to 1 as a backstop. No production change.
4. `remove()` keeps homes recorded; `test_visual_review` writes its outside image under `tmp_path`.

## Test scenarios (`plugin/tests/test_temp_homes.py`; most run pytest in a subprocess, own TMPDIR)

- A session with the `home` fixture, a home written back after its teardown, a bare `make_home()`
  and a failing test: exit 1, "1 failed, 3 passed", the 4 homes were made in that TMPDIR and none
  is left.
- A SIGKILLed session (exit -9) leaves one home; the next session keeps it (fresh); after it is
  backdated 7 h the next session removes it.
- Session start with: stale and fresh `speakeasy-test-*` dirs, a stale `other-stale` dir, a stale
  `speakeasy-test-file` file, a `speakeasy-test-link` symlink to a stale folder outside. Left:
  exactly `other-stale, speakeasy-test-file, speakeasy-test-fresh, speakeasy-test-link`; the
  outside folder still holds `keep.txt`.
- The parallel runner over two files: exit 0, "2 passed, 0 failed, 0 flaky", same listing as
  above (its own shard folder gone).
- A delivery from the conftest service starts no process and leaves no `installs/` in the home.
- In process: a thread making a home while the exit cleanup runs; both homes go.
- In process: a notifier built in a module fixture, before any test fixture, sends nothing.
- In process: the send guard wraps `HermesSendNotifier.__init__` as `(self, *args, **kwargs)`.
- In process: a notifier given its own runner (positional or keyword) keeps it and uses it.
- In process: a notifier left with the real launcher (left out, or `subprocess.run` passed) sends
  nothing.
- A session whose caller did not set `HERMES_DISABLE_LAZY_INSTALLS` gives its children `1`.
- The first five fail at e2d8cc1. The rest came in review (340e8a2) and were not run against e2d8cc1.
