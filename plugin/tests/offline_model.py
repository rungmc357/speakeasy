"""Keep the developer's own Hermes model setup out of the plugin test run.

Without this, the run reads whatever Hermes home the developer's shell points at (``~/.hermes`` by
default): its configured provider and model decide where Hermes' side-model calls go. A test whose
code path happens to reach ``agent.auxiliary_client.call_llm`` then calls the developer's real model,
and on a machine whose configured provider can't be resolved (a custom local endpoint with no key,
a machine that is off) that call stalls until the test's own wait expires. A machine-local setting
then looks like a code defect.

Two layers:

* ``pin_run_home()`` — before anything imports Hermes, point ``HERMES_HOME`` at a run-owned temp
  home whose config names an invented model on a closed loopback port, and drop the shell's
  provider/model overrides. Nothing in the run reads the developer's config, sign-ins or ``.env``.
* ``block_real_model_calls()`` — replace Hermes' side-model entry points (``call_llm``,
  ``async_call_llm``) for the whole run with a stand-in that fails at once, the way a Hermes with no
  usable provider does, and records the call. Every caller in the plugin already treats a failed
  side-model call as "no side model", so tests that never faked the model behave as on a Hermes
  with no usable provider instead of calling (or waiting on) the developer's. A test that wants a
  model answer gives the plugin its own fake (``host.aux_client`` or the router helpers).

Test-only: production code is unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path

import process_state
import temp_homes

# An invented model on the discard port of this machine: a connection there is refused at once.
OFFLINE_PROVIDER = "custom"
OFFLINE_MODEL = "speakeasy-test-offline-model"
OFFLINE_BASE_URL = "http://127.0.0.1:9/v1"

# Shell settings that override the config's provider/model (Hermes reads them before config.yaml).
_MODEL_OVERRIDES = ("HERMES_INFERENCE_PROVIDER", "HERMES_MODEL")

REAL_MODEL_CALLS: list[str] = []
RUN_HOME: Path | None = None


class NoModelInTests(RuntimeError):
    """What the stand-in raises: the plugin's callers treat it like any failed side-model call."""


def pin_run_home() -> Path:
    """Point the run at its own offline Hermes home (once per process); returns it."""
    global RUN_HOME
    if RUN_HOME is not None:
        return RUN_HOME
    home = temp_homes.new_home()
    (home / "config.yaml").write_text(
        "model:\n"
        f"  provider: {OFFLINE_PROVIDER}\n"
        f"  default: {OFFLINE_MODEL}\n"
        f"  base_url: {OFFLINE_BASE_URL}\n"
        "  api_key: not-a-real-key\n"
    )
    os.environ["HERMES_HOME"] = str(home)
    for key in _MODEL_OVERRIDES:
        os.environ.pop(key, None)
    RUN_HOME = home
    return home


def _stand_in(name: str):
    def call(*args, **kwargs):
        task = kwargs.get("task") or kwargs.get("model") or "?"
        REAL_MODEL_CALLS.append(f"{name}({task})")
        raise NoModelInTests(f"tests may not call a real model ({name}, task {task!r}); "
                             "give the code under test a fake side model instead")

    async def acall(*args, **kwargs):
        return call(*args, **kwargs)

    return acall if name.startswith("async") else call


def block_real_model_calls() -> bool:
    """Swap Hermes' side-model entry points for the failing stand-in. False outside Hermes."""
    before = process_state.ProcessState()
    try:
        import agent.auxiliary_client as aux  # type: ignore[import-not-found]
    except Exception:
        return False
    finally:
        before.restore()  # importing Hermes modules can prepend their checkout to sys.path
    for name in ("call_llm", "async_call_llm"):
        if hasattr(aux, name):
            setattr(aux, name, _stand_in(name))
    return True


def configured_model() -> tuple[str, str]:
    """The provider/model Hermes' own reader sees for this run, ("", "") when it can't say."""
    try:
        from agent.auxiliary_client import _read_main_model, _read_main_provider  # type: ignore
        return str(_read_main_provider() or ""), str(_read_main_model() or "")
    except Exception:
        return "", ""


__all__ = ["NoModelInTests", "REAL_MODEL_CALLS", "RUN_HOME", "block_real_model_calls",
           "configured_model", "pin_run_home", "OFFLINE_MODEL", "OFFLINE_PROVIDER"]
