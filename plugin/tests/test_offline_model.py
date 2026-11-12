"""The run's own offline Hermes home and the real-model block (``tests/offline_model.py``).

A developer whose configured Hermes provider can't be resolved (a custom local endpoint with no key,
a machine that's off) must get the same results as everyone else: the suite never reads their
config and never waits on their model.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import offline_model

PLUGIN = Path(__file__).resolve().parents[1]


def test_the_run_uses_its_own_offline_hermes_home():
    assert offline_model.RUN_HOME is not None
    assert os.environ["HERMES_HOME"] == str(offline_model.RUN_HOME)
    assert Path(os.environ["HERMES_HOME"]).resolve() != (Path.home() / ".hermes").resolve()
    for key in ("HERMES_INFERENCE_PROVIDER", "HERMES_MODEL"):
        assert key not in os.environ
    provider, model = offline_model.configured_model()
    if (provider, model) != ("", ""):  # a Hermes with the reader
        assert model == offline_model.OFFLINE_MODEL


def _take_recorded(start: int) -> list[str]:
    calls = offline_model.REAL_MODEL_CALLS[start:]
    del offline_model.REAL_MODEL_CALLS[start:]  # made on purpose here: keep them out of the run's count
    return calls


def test_a_real_side_model_call_fails_at_once_and_is_recorded():
    aux = pytest.importorskip("agent.auxiliary_client")
    start = len(offline_model.REAL_MODEL_CALLS)
    began = time.monotonic()
    with pytest.raises(offline_model.NoModelInTests):
        aux.call_llm(task="title_generation", messages=[{"role": "user", "content": "hi"}])
    with pytest.raises(offline_model.NoModelInTests):
        asyncio.run(aux.async_call_llm(task="compression", messages=[]))
    assert time.monotonic() - began < 1.0
    assert _take_recorded(start) == ["call_llm(title_generation)", "async_call_llm(compression)"]


def test_the_plugin_side_model_helpers_fall_back_quietly():
    pytest.importorskip("agent.auxiliary_client")
    from speakeasy import host as host_mod
    from speakeasy import router
    start = len(offline_model.REAL_MODEL_CALLS)
    before = host_mod._ACTIVE
    try:
        host_mod.bind(host_mod.HermesHost(offline_model.RUN_HOME))
        call_llm, _ = host_mod.aux_client()
        with pytest.raises(offline_model.NoModelInTests):
            call_llm(task=router.AUX_TASK, messages=[])
    finally:
        host_mod._ACTIVE = before
    assert _take_recorded(start) == [f"call_llm({router.AUX_TASK})"]


def test_suite_passes_with_an_unresolvable_configured_provider(tmp_path):
    """The SPK-447 handoff test, run the way a developer with a broken custom provider runs it: no
    pinning on their side, and it finishes well inside its own waits."""
    home = tmp_path / "broken-hermes"
    home.mkdir()
    # A custom endpoint with no key on an address that never answers: the configuration that made
    # the handoff test stall until its 10 s wait expired.
    (home / "config.yaml").write_text("model:\n  provider: custom\n  default: local-model\n"
                                      "  base_url: http://10.255.255.1:9/v1\n")
    env = {"HOME": os.environ.get("HOME", str(tmp_path)), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "TMPDIR": str(tmp_path), "HERMES_DISABLE_LAZY_INSTALLS": "1", "HERMES_HOME": str(home),
           "HERMES_INFERENCE_PROVIDER": "custom", "HERMES_MODEL": "local-model"}
    began = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_codex_transport.py::test_handoff_starts_hermes_task_and_result_is_appended"],
        cwd=PLUGIN, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "1 passed" in proc.stdout
    assert time.monotonic() - began < 60
