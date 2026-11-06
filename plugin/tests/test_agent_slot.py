"""The agent slot and the host layer (agent_slot.py, host.py, backends/hermes.py): Hermes plugs into
both unchanged, and an agent without Hermes' optional features gets them switched off instead of
reading Hermes' files. Every agent here is invented; nothing reaches a real Hermes."""
import sys
import threading
import types

import pytest

from speakeasy import agent_slot, router
from speakeasy import host as host_mod
from speakeasy.backends import Capabilities, TaskBackend
from speakeasy.backends.base import SessionCatalog, SubagentSource
from speakeasy.backends.hermes import HermesSessionCatalog, HermesSubagentSource, hermes_agent
from speakeasy.calls import ServiceError
from speakeasy.hermes_api import HermesAPI
from speakeasy.host import Host, HermesHost
from speakeasy.service import CAPABILITIES, ROUTING_EXPLAINER

# The real side-model callers (conftest swaps them out so no test reaches a real model).
REAL_AUX_CALL, REAL_TITLE_LLM = router.aux_call, router._title_llm


class OtherAgent:
    """An invented agent with none of Hermes' optional features."""

    last_error = ""

    def capabilities(self):
        return Capabilities(kind="codex", display_name="Codex", file_changes=True, needs_workspace=True,
                            version="9.9.9")

    def health(self, timeout=2):
        return True

    def start_run(self, prompt, idem_key, session_id=None, session_key=None, images=None):
        return "run_1"

    def events(self, run_id, callback):
        callback({"event": "run.completed", "output": "done"})
        return True

    def get_run(self, run_id):
        return {"run_id": run_id, "status": "completed", "output": "done"}

    def stop(self, run_id):
        return {"run_id": run_id, "status": "stopped"}

    def steer(self, run_id, text):
        return False

    def approve(self, run_id, request_id, choice):
        return {}

    def session_messages(self, session_id, limit=60, timeout=1.5):
        return []

    def run_to_completion(self, prompt, idem_key, session_id=None):
        return "completed", "done"


# -- Hermes still satisfies the slot ---------------------------------------------------------------

def test_hermes_fills_the_agent_slot_with_every_optional_feature(tmp_path):
    api = HermesAPI("http://127.0.0.1:1", lambda: "", state_db=tmp_path / "state.db")
    assert isinstance(api, TaskBackend)
    caps = api.capabilities()
    assert (caps.kind, caps.display_name) == ("hermes", "Hermes")
    assert caps.history and caps.subagents and caps.questions
    assert caps.chat_delivery and caps.threads and caps.email_drafts and caps.daily_brief
    assert isinstance(api.session_catalog(), SessionCatalog)
    assert isinstance(api.subagent_source(), SubagentSource)
    assert isinstance(caps.version, str)


def test_the_service_runs_hermes_through_the_slot_and_host(service):
    assert isinstance(service.host, HermesHost) and isinstance(service.host, Host)
    assert isinstance(service.agent, HermesAPI) and service.hermes is service.agent
    assert service.rt.agent is service.agent and service.rt.host is service.host
    assert service.agent.state_db == service.rt.state_db
    assert isinstance(service.sessions, HermesSessionCatalog)
    assert service.agent_name == "Hermes"


def test_hermes_status_flags_are_exactly_the_servers_own(service):
    status = service.status()
    assert status["capabilities"] == CAPABILITIES  # nothing switched off for Hermes
    assert status["capabilities"]["questions"] is True
    assert status["agent"]["kind"] == "hermes" and status["agent"]["display_name"] == "Hermes"
    assert status["agent"]["host"] == "hermes" and isinstance(status["agent"]["version"], str)
    assert status["routing_explainer"] == ROUTING_EXPLAINER


def test_pairing_tells_the_app_which_agent_it_is(service):
    code = service.devices.new_pairing_code()
    out = service.pair({"code": code, "device_name": "Test Mac"})
    assert out["agent"]["kind"] == "hermes" and out["agent"]["display_name"] == "Hermes"


def test_an_agent_that_cant_describe_itself_is_treated_as_hermes(tmp_path):
    bare = types.SimpleNamespace()
    assert agent_slot.capabilities(CAPABILITIES, bare) == CAPABILITIES
    assert isinstance(agent_slot.session_catalog(bare, tmp_path / "state.db"), HermesSessionCatalog)
    assert isinstance(agent_slot.subagent_source(bare, tmp_path / "state.db"), HermesSubagentSource)
    assert agent_slot.agent_info(bare)["kind"] == "hermes"


# -- another agent: optional features off, its own name --------------------------------------------

def test_another_agent_gets_its_missing_features_switched_off(home):
    from speakeasy.service import VoiceService
    svc = VoiceService(home, agent=OtherAgent(), start_threads=False)
    assert isinstance(svc.agent, TaskBackend)
    status = svc.status()
    caps = status["capabilities"]
    assert caps["history"] is False and caps["job_subagents"] is False and caps["questions"] is False
    assert {k: v for k, v in caps.items() if k not in agent_slot.FLAGS_OF_FEATURES} == \
        {k: v for k, v in CAPABILITIES.items() if k not in agent_slot.FLAGS_OF_FEATURES}
    assert status["agent"] == {"kind": "codex", "display_name": "Codex", "version": "9.9.9", "host": "hermes"}
    assert "Codex agent does" in status["routing_explainer"]
    # No past conversations: the list says so in the agent's name, nothing reads Hermes' state.db.
    with pytest.raises(ServiceError) as err:
        svc.history({})
    assert err.value.status == 404 and "Codex" in err.value.message
    assert svc.sessions.session_meta("anything") is None
    assert agent_slot.subagent_source(svc.agent, svc.rt.state_db) is None


def test_wording_takes_the_agents_name():
    line = "Hermes didn't take that change. Try again."
    assert agent_slot.named(line, "Hermes") == line
    assert agent_slot.named(line, "Claude Code") == "Claude Code didn't take that change. Try again."
    assert agent_slot.named("run hermes voice setup", "Codex") == "run hermes voice setup"  # commands stay


# -- the host layer forwards to what Hermes did before --------------------------------------------

def test_hermes_host_forwards_to_the_hermes_readers(tmp_path, monkeypatch):
    from speakeasy import delivery, settings
    seen = []
    monkeypatch.setattr(settings, "hermes_secret", lambda home, name: seen.append((home, name)) or "v")
    monkeypatch.setattr(settings, "hermes_api_base", lambda home: "http://127.0.0.1:8642")
    monkeypatch.setattr(delivery, "threads_supported", lambda: True)
    host = HermesHost(tmp_path)
    assert host.agent_api_key() == "v" and seen == [(tmp_path, "API_SERVER_KEY")]
    assert host.agent_api_base() == "http://127.0.0.1:8642"
    assert host.threads_supported() is True
    assert isinstance(host.notifier("work"), delivery.HermesSendNotifier)
    agent = hermes_agent(host)
    assert isinstance(agent, HermesAPI) and agent.state_db == tmp_path / "state.db"


def test_side_model_calls_go_through_the_host(monkeypatch):
    class Reply:
        choices = [types.SimpleNamespace(message=types.SimpleNamespace(content="new"))]

    asked = []

    class FakeHost(HermesHost):
        kind = "standalone"

        def aux_client(self):
            return (lambda **kw: asked.append(kw["task"]) or Reply()), (lambda r: r.choices[0].message.content)

    before = host_mod._ACTIVE
    try:
        host_mod.bind(FakeHost(__import__("pathlib").Path("/nonexistent")))
        assert REAL_AUX_CALL([{"role": "user", "content": "hi"}]) == "new"
        assert REAL_TITLE_LLM([{"role": "user", "content": "hi"}], 1.0) == "new"
        assert asked == [router.AUX_TASK, "title_generation"]
    finally:
        host_mod._ACTIVE = before


def test_no_side_model_outside_hermes_still_means_none(monkeypatch):
    class NoAux(HermesHost):
        def aux_client(self):
            raise ImportError("no auxiliary client here")

    before = host_mod._ACTIVE
    try:
        host_mod.bind(NoAux(__import__("pathlib").Path("/nonexistent")))
        assert REAL_AUX_CALL([{"role": "user", "content": "hi"}]) is None
        assert REAL_TITLE_LLM([{"role": "user", "content": "hi"}], 1.0) is None
    finally:
        host_mod._ACTIVE = before


def test_hermes_host_reads_the_real_auxiliary_module(monkeypatch):
    fake = types.ModuleType("agent.auxiliary_client")
    fake.call_llm = lambda **kw: "called"
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake)
    call_llm, extract = HermesHost(__import__("pathlib").Path("/nonexistent")).aux_client()
    assert call_llm() == "called" and extract is None  # an older Hermes without the extractor


def test_liveness_asks_the_hosts_check():
    from speakeasy import liveness
    stop, alive = threading.Event(), []

    def activity(session_id):
        stop.set()
        return {"session_id": session_id, "running": True, "seconds_since_activity": 1}

    liveness.watch(lambda: "s1", alive.append, stop, every_s=0.01, activity=activity)
    assert alive and alive[0]["session_id"] == "s1"
