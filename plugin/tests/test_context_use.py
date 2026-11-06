"""Which model ran a task and how full its context got (context_use.py): recorded from Hermes'
``post_api_request`` notice as the work runs, kept per session across restarts, frozen on each task
when it finishes, and shown as one quiet, plain line labelled an estimate. Invented sessions only."""
import time

import pytest

from speakeasy import context_use as CU
from speakeasy.store import StateStore


@pytest.fixture(autouse=True)
def clean():
    CU._MEM.clear()
    CU.detach()
    yield
    CU._MEM.clear()
    CU.detach()


def call(sid, tokens, window=200_000, model="claude-opus-5-5", **extra):
    CU.after_api_request(session_id=sid, model=model, response_model=extra.pop("served", None), provider="anthropic",
                         usage={"prompt_tokens": tokens, "output_tokens": 300}, context_length=window, **extra)


def test_hook_keeps_the_newest_call_and_its_peak():
    call("s1", 30_000)
    call("s1", 62_000)
    call("s1", 44_000)                                    # compressed down: peak stays
    rec = CU.latest("s1")
    assert rec["tokens"] == 44_000 and rec["peak"] == 62_000 and rec["window"] == 200_000 and rec["calls"] == 3
    assert CU.latest("other") is None


def test_hook_never_raises_and_ignores_calls_without_tokens():
    CU.after_api_request(session_id="s2", usage=None)
    CU.after_api_request(session_id="", usage={"prompt_tokens": 10})
    CU.after_api_request(usage="not a dict")
    CU.attach(lambda sid, rec: (_ for _ in ()).throw(RuntimeError("disk full")), None)
    call("s3", 1000)                                      # a failing store must not touch the turn
    assert CU.latest("s2") is None and CU.latest("s3")["tokens"] == 1000


def test_the_answering_model_wins_over_the_asked_one():
    call("s4", 1000, model="claude-opus-5-5", served="claude-opus-5-5-20261001")
    assert CU.latest("s4")["model"] == "claude-opus-5-5-20261001"
    assert CU.latest("s4")["asked"] == "claude-opus-5-5"


def test_described_like_the_chat_footer_and_labelled_an_estimate():
    call("s5", 44_000)
    call("s5", 40_000)
    ctx = CU.describe(CU.latest("s5"), seconds=185, name_for=lambda m: "Claude Opus 5.5")
    assert ctx["line"] == "Claude Opus 5.5 · about 20% of context used · 3 min"
    assert ctx["estimate"] is True and "estimate" in ctx["detail"].lower()
    assert ctx["detail"].startswith("About 40K of 200K tokens in the latest model call (peak about 22%)")
    assert ctx["percent"] == 20 and ctx["peak_percent"] == 22 and ctx["seconds"] == 185
    assert ctx["use"] == "about 20% of context used" and ctx["took"] == "3 min"


def test_unknown_window_says_tokens_not_a_made_up_share():
    call("s6", 12_345, window=None)
    ctx = CU.describe(CU.latest("s6"))
    assert ctx["percent"] is None and ctx["window"] is None
    assert ctx["line"] == "claude-opus-5-5 · about 12K tokens of context"
    assert "isn't known" in ctx["detail"]


@pytest.mark.parametrize("seconds,text", [(None, None), (0.4, None), (23, "23 s"), (600, "10 min"),
                                          (3600, "1 h"), (5400, "1 h 30 min")])
def test_duration_text(seconds, text):
    assert CU.duration_text(seconds) == text


def test_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(CU, "MAX_SESSIONS", 3)
    for i in range(5):
        call(f"b{i}", 100)
    assert list(CU._MEM) == ["b2", "b3", "b4"]


def test_records_survive_a_restart_through_the_store(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3", tmp_path)
    CU.attach(store.record_context, store.context_record)
    call("s7", 50_000)
    store.close()
    CU._MEM.clear()
    again = StateStore(tmp_path / "state.sqlite3", tmp_path)
    CU.attach(again.record_context, again.context_record)
    assert CU.latest("s7")["tokens"] == 50_000
    CU.detach(again.record_context)
    assert CU._SINK["record"] is None


def test_a_task_card_shows_its_model_and_context_live_then_frozen(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3", tmp_path)
    CU.attach(store.record_context, store.context_record)
    store.reserve_run("k1", "vi_x", "call_k1", 1)
    store.update_run("k1", "run_0001", "running")
    assert "context" not in store.work(idem_key="k1")       # nothing recorded yet: no line at all
    store.set_session("k1", "chat_a")
    call("chat_a", 88_000)
    live = store.work(idem_key="k1")["context"]
    assert live["line"] == "Claude Opus 5.5 · about 44% of context used" and live["estimate"] is True
    store.update_run("k1", "run_0001", "completed")
    done = store.work(idem_key="k1")["context"]
    assert done["percent"] == 44 and done["line"].startswith("Claude Opus 5.5 · about 44% of context used")
    # A follow-up in the same chat keeps working: the finished task's figure doesn't move.
    store.reserve_run("k2", "vi_x", "call_k2", 1)
    store.update_run("k2", "run_0002", "running")
    store.set_session("k2", "chat_a")
    time.sleep(0.01)
    call("chat_a", 150_000)
    assert store.work(idem_key="k1")["context"]["percent"] == 44
    assert store.work(idem_key="k2")["context"]["percent"] == 75
    store.close()


def test_an_older_call_from_before_the_task_started_is_not_shown(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3", tmp_path)
    call("chat_b", 10_000)
    CU._MEM["chat_b"]["at"] = time.time() - 3600           # the chat's last call was an hour ago
    store.reserve_run("k3", "vi_x", "call_k3", 1)
    store.update_run("k3", "run_0003", "running")
    store.set_session("k3", "chat_b")
    assert "context" not in store.work(idem_key="k3")
    store.close()


def test_session_page_carries_the_chats_latest_context(service):
    from test_history import NOW, add_msg, add_session, make_db
    db = service.rt.state_db
    make_db(db)
    add_session(db, "c1", "discord", title="Garden plan", at=NOW - 900)
    add_msg(db, "c1", "user", "Plan the tomato beds", at=NOW - 890)
    add_session(db, "c2", "discord", parent="c1", at=NOW - 100)          # compression segment
    add_msg(db, "c2", "assistant", "Three beds, staggered.", at=NOW - 90)
    page = service.history_session("c1", {})["session"]
    assert "context" not in page                                         # nothing seen yet
    call("c1", 150_000)
    time.sleep(0.01)
    call("c2", 30_000, model="gpt-6.1")                                  # newest segment ran last
    ctx = service.history_session("c1", {})["session"]["context"]
    assert ctx["line"] == "GPT-6.1 · about 15% of context used" and ctx["estimate"] is True


def test_the_plugin_registers_the_hook_and_it_reaches_the_live_module():
    import speakeasy

    class Ctx:
        hooks: dict = {}

        def register_hook(self, name, fn):
            self.hooks[name] = fn

    ctx = Ctx()
    speakeasy._register_context_hook(ctx)
    hook = ctx.hooks["post_api_request"]
    hook(session_id="s9", model="gpt-6.1", usage={"prompt_tokens": 4000}, context_length=400_000)
    assert CU.latest("s9")["tokens"] == 4000
