#!/usr/bin/env python3
"""Run the plugin test suite split across several pytest processes, then rerun failures alone.

The suite is wall-clock bound, not CPU bound: most tests wait on fake servers and timers (a serial
run spends ~18 min on ~7 s of CPU). Splitting the files over N processes cuts the run to the longest
shard. No pytest-xdist needed (the Hermes venv doesn't ship it).

    cd plugin && env -i HOME=$HOME PATH=/opt/homebrew/bin:/usr/bin:/bin TMPDIR=$TMPDIR \\
        HERMES_DISABLE_LAZY_INSTALLS=1 ~/.hermes/hermes-agent/venv/bin/python \\
        ../scripts/plugin-tests-parallel.py [-j 8] [--log-dir DIR] [--no-rerun] [files...]

- Files are balanced over shards by their last measured time (kept in
  ~/.cache/speakeasy-plugin-test-times.json, refreshed from every run's JUnit report); unknown
  files are weighed by size.
- Each shard gets its own TMPDIR, so temp homes, sockets and stores never collide. They all sit in
  one speakeasy-test-* folder, deleted when the runner exits; a killed run's is swept by a later
  run (plugin/tests/temp_homes.py).
- A test that fails in its shard is rerun on its own (up to --retries times, default 2). Every
  first-pass failure stays in the ledger (ledger.json in the log dir + one LEDGER line each: test,
  shard, shard log, solo-retry results, revision, known issue), whatever its retries do.
- Exit 0: no persistent failure, and every retry-pass is attributed to an issue listed in
  scripts/known-test-issues.tsv. Exit 1: a test failed every attempt, a shard crashed or timed
  out, or a test tripped the real-Hermes-port guard (never waived by a passing retry). Exit 3
  (UNRESOLVED): something failed first and only passed alone, with no known issue — file it.
- Prints "plugin ledger: …" and "plugin suite: N passed, F failed, K flaky in M:SS (J shards)".
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "tests"))
import temp_homes  # noqa: E402

TIMES = Path.home() / ".cache" / "speakeasy-plugin-test-times.json"
SHARD_TIMEOUT_S = 1500  # one shard should take a few minutes; a hang must not hold a release


def load_times() -> dict[str, float]:
    try:
        return {k: float(v) for k, v in json.loads(TIMES.read_text()).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_times(times: dict[str, float]) -> None:
    try:
        TIMES.parent.mkdir(parents=True, exist_ok=True)
        tmp = TIMES.with_suffix(".tmp")
        tmp.write_text(json.dumps(times, indent=1, sort_keys=True))
        tmp.replace(TIMES)
    except OSError:
        pass


def balance(files: list[str], jobs: int, times: dict[str, float]) -> list[list[str]]:
    """Longest-first greedy split: each file goes to the currently lightest shard."""
    def weight(f: str) -> float:
        if f in times:
            return times[f]
        try:
            return Path(f).stat().st_size / 2000.0  # ~1 s per 2 KB of test code, a rough prior
        except OSError:
            return 1.0
    shards: list[tuple[float, list[str]]] = [(0.0, []) for _ in range(max(1, jobs))]
    for f in sorted(files, key=weight, reverse=True):
        i = min(range(len(shards)), key=lambda k: shards[k][0])
        load, members = shards[i]
        shards[i] = (load + weight(f), members + [f])
    return [m for _, m in shards if m]


def junit_results(path: Path) -> tuple[dict[str, float], list[str], int]:
    """(seconds per test file, failed test ids, passed count) from one shard's JUnit report."""
    per_file: dict[str, float] = {}
    failed: list[str] = []
    passed = 0
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return per_file, failed, passed
    for case in root.iter("testcase"):
        cls = case.get("classname", "")           # e.g. "tests.test_routing" or "tests.test_x.TestY"
        parts = cls.split(".")
        mod = next((i for i, p in enumerate(parts) if p.startswith("test_")), None)
        if mod is None:
            continue
        file = "/".join(parts[:mod + 1]) + ".py"
        rest = parts[mod + 1:]
        node = "::".join([file, *rest, case.get("name", "")])
        per_file[file] = per_file.get(file, 0.0) + float(case.get("time") or 0)
        if case.find("failure") is not None or case.find("error") is not None:
            failed.append(node)
        elif case.find("skipped") is None:
            passed += 1
    return per_file, failed, passed


def run_shard(idx: int, files: list[str], log_dir: Path, base_tmp: Path) -> subprocess.Popen:
    tmp = base_tmp / f"shard{idx}"
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TMPDIR=str(tmp))
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
           f"--junitxml={log_dir / f'shard{idx}.xml'}", *files]
    log = open(log_dir / f"shard{idx}.log", "w")
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-j", "--jobs", type=int, default=int(os.environ.get("SPEAKEASY_TEST_JOBS", "8")))
    ap.add_argument("--log-dir", default=None)
    ap.add_argument("--no-rerun", action="store_true")
    ap.add_argument("--retries", type=int, default=2, help="solo retries per first-pass failure")
    ap.add_argument("--known", default=str(Path(__file__).resolve().parent / "known-test-issues.tsv"))
    ap.add_argument("files", nargs="*")
    args = ap.parse_args()

    files = args.files or sorted(str(p) for p in Path("tests").glob("test_*.py"))
    if not files:
        print("plugin suite: no test files (run from plugin/)", file=sys.stderr)
        return 2
    log_dir = Path(args.log_dir or tempfile.mkdtemp(prefix="plugin-tests-"))
    log_dir.mkdir(parents=True, exist_ok=True)
    temp_homes.sweep()
    base_tmp = temp_homes.new_home()
    times = load_times()
    shards = balance(files, args.jobs, times)
    started = time.monotonic()
    print(f"plugin suite: {len(files)} files over {len(shards)} shards, logs in {log_dir}", flush=True)

    procs = [(i, run_shard(i, s, log_dir, base_tmp)) for i, s in enumerate(shards)]
    broken: list[str] = []
    for i, p in procs:
        left = SHARD_TIMEOUT_S - (time.monotonic() - started)
        try:
            p.wait(timeout=max(1, left))
        except subprocess.TimeoutExpired:
            p.kill()
            broken.append(f"shard {i} timed out after {SHARD_TIMEOUT_S}s (see shard{i}.log)")
        print(f"  shard {i}: exit {p.returncode} after {time.monotonic() - started:.0f}s", flush=True)

    passed = 0
    failed: list[str] = []
    shard_of: dict[str, int] = {}
    for i, _ in procs:
        per_file, f, n = junit_results(log_dir / f"shard{i}.xml")
        times.update(per_file)
        failed += f
        shard_of.update({node: i for node in f})
        passed += n
        if not per_file and not any(b.startswith(f"shard {i} ") for b in broken):
            broken.append(f"shard {i} wrote no report (crash at start? see shard{i}.log)")
    save_times(times)
    # pytest exit 1 = tests failed (counted above); anything else (2 interrupted, 3 internal, 4 usage,
    # negative = killed) means the shard didn't finish its files.
    for i, p in procs:
        if p.returncode not in (0, 1) and not any(b.startswith(f"shard {i} ") for b in broken):
            broken.append(f"shard {i} exited {p.returncode} (see shard{i}.log)")

    # The ledger: every first-pass failure stays on the record, whatever its solo retries do.
    known = load_known(Path(args.known))
    revision = os.environ.get("SPEAKEASY_REVISION") or git_revision()
    ledger: list[dict] = []
    for node in failed:
        i = shard_of.get(node, -1)
        msg = failure_text(log_dir / f"shard{i}.xml", node)
        entry = {"test": node, "first": "failed", "shard": i, "log": str(log_dir / f"shard{i}.log"),
                 "first_message": msg[:300], "retries": [], "revision": revision,
                 "issue": known_issue(known, node), "guard": GUARD_MARK in msg}
        for attempt in range(0 if args.no_rerun else max(0, args.retries)):
            r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               timeout=600, env=dict(os.environ, TMPDIR=str(base_tmp)))
            entry["retries"].append("passed" if r.returncode == 0 else "failed")
            entry["guard"] = entry["guard"] or GUARD_MARK in r.stdout
            if r.returncode == 0:
                break
            (log_dir / "rerun.log").open("a").write(f"==== {node} (solo retry {attempt + 1})\n{r.stdout}\n")
        ledger.append(entry)
    (log_dir / "ledger.json").write_text(json.dumps(ledger, indent=1))

    retry_pass = [e for e in ledger if e["retries"][-1:] == ["passed"] and not e["guard"]]
    persistent = [e for e in ledger if e not in retry_pass]
    unexplained = [e for e in retry_pass if not e["issue"]]
    took = time.monotonic() - started
    for e in ledger:
        tries = "/".join(e["retries"]) or "not retried"
        why = (f"known: {e['issue']}" if e["issue"] else "UNEXPLAINED") if e in retry_pass else (
            "GUARD: reached a real Hermes API (never waived)" if e["guard"] else "persistent")
        print(f"LEDGER {e['test']} | first: failed (shard {e['shard']}) | solo retries: {tries} | {why}")
    for e in retry_pass:
        print(f"FLAKY (failed in the full run, passed alone after {len(e['retries'])} solo "
              f"{'retry' if len(e['retries']) == 1 else 'retries'}): {e['test']}"
              f"{' — ' + e['issue'] if e['issue'] else ' — no known issue'}")
    for e in persistent:
        print(f"FAILED: {e['test']}")
    for b in broken:
        print(f"BROKEN: {b}")
    print(f"plugin ledger: {len(ledger)} first-pass failures, {len(retry_pass)} passed on solo retry"
          f" ({len(unexplained)} unexplained), {len(persistent)} persistent · revision {revision}"
          f" · {log_dir / 'ledger.json'}")
    print(f"plugin suite: {passed + len(retry_pass)} passed, {len(persistent)} failed, {len(retry_pass)} flaky"
          f" in {int(took // 60)}:{int(took % 60):02d} ({len(shards)} shards)")
    if persistent or broken:
        return 1
    if unexplained:
        print("plugin suite: UNRESOLVED — a test failed first and only passed alone, with no known issue "
              f"listed in {args.known}; file it (or attribute it) before calling the run green")
        return 3
    return 0


GUARD_MARK = "tried to reach a real Hermes API"   # SPK-141's live-port guard: a retry never waives it


def load_known(path: Path) -> list[tuple[str, str]]:
    """Known flaky tests: '<test id or test name>\\t<SPK-N>\\t<why>' per line, '#' comments."""
    out: list[tuple[str, str]] = []
    try:
        for line in path.read_text().splitlines():
            parts = line.split("\t")
            if line.strip() and not line.startswith("#") and len(parts) >= 2:
                out.append((parts[0].strip(), " ".join(p.strip() for p in parts[1:3])))
    except OSError:
        pass
    return out


def known_issue(known: list[tuple[str, str]], node: str) -> str | None:
    name = node.rsplit("::", 1)[-1]
    return next((why for test, why in known if test == node or test == name), None)


def failure_text(xml: Path, node: str) -> str:
    try:
        root = ET.parse(xml).getroot()
    except (OSError, ET.ParseError):
        return ""
    name = node.rsplit("::", 1)[-1]
    for case in root.iter("testcase"):
        if case.get("name") == name:
            bad = case.find("failure") if case.find("failure") is not None else case.find("error")
            if bad is not None:
                return f"{bad.get('message') or ''}\n{bad.text or ''}"
    return ""


def git_revision() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                             timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                               text=True, timeout=10).stdout.strip()
        return (rev + ("+uncommitted" if dirty else "")) or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


if __name__ == "__main__":
    sys.exit(main())
