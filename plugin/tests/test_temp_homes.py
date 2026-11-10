"""SPK-293: test runs leave no speakeasy-test-* folders behind, and clear what a killed run left.

Each test runs a real pytest session (or the parallel runner) in a subprocess whose temp folder is
this test's own, then looks at what is left in it.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from fakes import HERMES_SRC, PLUGIN_ROOT

TESTS = Path(__file__).resolve().parent
RUNNER = PLUGIN_ROOT.parent / "scripts" / "plugin-tests-parallel.py"
SEVEN_HOURS_AGO = time.time() - 7 * 3600

MAKES_HOMES = '''
import os
from fakes import make_home

def _note(path):
    with open(os.environ["MADE_LOG"], "a") as f:
        f.write(f"{path}\\n")

def test_with_the_home_fixture(home):
    _note(home)

def test_with_make_home_directly():
    _note(make_home())

def test_that_fails_after_making_a_home():
    _note(make_home())
    assert False
'''

KILLED_MID_RUN = '''
import os, signal
from fakes import make_home

def test_killed():
    make_home()
    os.kill(os.getpid(), signal.SIGKILL)
'''

NOTHING = '''
def test_nothing():
    pass
'''


def _env(tmp_path: Path, temp: Path) -> dict[str, str]:
    (tmp_path / "user").mkdir(exist_ok=True)
    (tmp_path / "hermes-home").mkdir(exist_ok=True)
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path / "user"),
            "TMPDIR": str(temp), "HERMES_HOME": str(tmp_path / "hermes-home"),
            "HERMES_AGENT_SRC": str(HERMES_SRC), "HERMES_DISABLE_LAZY_INSTALLS": "1",
            "PYTHONPATH": os.pathsep.join([str(TESTS), str(PLUGIN_ROOT)]),
            "MADE_LOG": str(tmp_path / "made.txt")}


def _session(tmp_path: Path, temp: Path, source: str) -> subprocess.CompletedProcess:
    """One pytest session over *source*, with the suite's conftest, in temp folder *temp*."""
    suite = tmp_path / "suite"
    suite.mkdir(exist_ok=True)
    (suite / "test_inner.py").write_text(source)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "conftest",
                           str(suite / "test_inner.py")], cwd=suite, env=_env(tmp_path, temp),
                          capture_output=True, text=True, timeout=120)


def _left(temp: Path) -> list[str]:
    return sorted(p.name for p in temp.iterdir() if p.name.startswith("speakeasy-test-"))


def _temp(tmp_path: Path) -> Path:
    temp = tmp_path / "temp"
    temp.mkdir()
    return temp


def test_a_session_leaves_none_of_its_homes_behind_even_when_a_test_fails(tmp_path):
    temp = _temp(tmp_path)
    done = _session(tmp_path, temp, MAKES_HOMES)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "1 failed, 2 passed" in done.stdout
    made = (tmp_path / "made.txt").read_text().split()
    assert len(made) == 3
    assert [Path(p).parent for p in made] == [temp, temp, temp]
    assert [Path(p).exists() for p in made] == [False, False, False]
    assert _left(temp) == []


def test_a_killed_runs_home_is_cleared_by_a_later_start_once_it_is_stale(tmp_path):
    temp = _temp(tmp_path)
    killed = _session(tmp_path, temp, KILLED_MID_RUN)
    assert killed.returncode == -9
    [left] = _left(temp)

    assert _session(tmp_path, temp, NOTHING).returncode == 0
    assert _left(temp) == [left]  # fresh: it may be a run still going

    os.utime(temp / left, (SEVEN_HOURS_AGO, SEVEN_HOURS_AGO))
    assert _session(tmp_path, temp, NOTHING).returncode == 0
    assert _left(temp) == []


def _neighbours(tmp_path: Path, temp: Path) -> Path:
    """Stale and fresh speakeasy-test-* folders plus things the sweep must leave alone."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the sweep's")
    for name in ("speakeasy-test-stale", "speakeasy-test-fresh", "other-stale"):
        (temp / name).mkdir()
        (temp / name / "file.txt").write_text("x")
    (temp / "speakeasy-test-file").write_text("a file, not a folder")
    (temp / "speakeasy-test-link").symlink_to(outside, target_is_directory=True)
    for name in ("speakeasy-test-stale", "other-stale", "speakeasy-test-file"):
        os.utime(temp / name, (SEVEN_HOURS_AGO, SEVEN_HOURS_AGO))
    os.utime(outside, (SEVEN_HOURS_AGO, SEVEN_HOURS_AGO))
    os.utime(temp / "speakeasy-test-link", (SEVEN_HOURS_AGO, SEVEN_HOURS_AGO), follow_symlinks=False)
    return outside


def test_a_session_start_removes_only_stale_speakeasy_test_folders(tmp_path):
    temp = _temp(tmp_path)
    outside = _neighbours(tmp_path, temp)
    assert _session(tmp_path, temp, NOTHING).returncode == 0
    assert sorted(os.listdir(temp)) == ["other-stale", "speakeasy-test-file", "speakeasy-test-fresh",
                                        "speakeasy-test-link"]
    assert (temp / "speakeasy-test-link").is_symlink()
    assert sorted(os.listdir(outside)) == ["keep.txt"]


def test_the_parallel_runner_sweeps_first_and_leaves_no_folder_of_its_own(tmp_path):
    temp = _temp(tmp_path)
    outside = _neighbours(tmp_path, temp)
    suite = tmp_path / "suite"
    suite.mkdir()
    for name in ("test_one.py", "test_two.py"):
        (suite / name).write_text("from fakes import make_home\n\ndef test_home():\n    assert make_home().is_dir()\n")
    done = subprocess.run([sys.executable, str(RUNNER), "-j", "2", "--no-rerun", "--log-dir", str(tmp_path / "logs"),
                           "test_one.py", "test_two.py"], cwd=suite, env=_env(tmp_path, temp),
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "plugin suite: 2 passed, 0 failed, 0 flaky" in done.stdout
    assert sorted(os.listdir(temp)) == ["other-stale", "speakeasy-test-file", "speakeasy-test-fresh",
                                        "speakeasy-test-link"]
    assert sorted(os.listdir(outside)) == ["keep.txt"]
