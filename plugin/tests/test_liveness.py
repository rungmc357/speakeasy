"""A thread task's card stays live while the gateway is really running its turn, and only then."""
import threading

from speakeasy import liveness


class Agent:
    def __init__(self, session_id, quiet=5.0, tool=None):
        self.session_id, self.quiet, self.tool = session_id, quiet, tool

    def get_activity_summary(self):
        return {"seconds_since_activity": self.quiet, "current_tool": self.tool}


class Runner:
    def __init__(self, *agents):
        self.agents = {f"k{i}": a for i, a in enumerate(agents)}

    def _snapshot_running_agents(self):
        return dict(self.agents)


def test_running_session_is_found_and_alive():
    runner = Runner(Agent("other"), Agent("s1", quiet=200, tool="terminal"))
    summary = liveness.session_activity("s1", runner)
    assert summary["current_tool"] == "terminal"
    assert liveness.alive(summary)
    assert liveness.doing(summary) == "Still working: running a command"


def test_no_running_turn_is_not_vouched_for():
    runner = Runner(Agent("other"))
    assert liveness.session_activity("s1", runner) is None
    assert not liveness.alive(None)
    assert liveness.session_activity(None, runner) is None


def test_wedged_agent_is_not_vouched_for():
    summary = liveness.session_activity("s1", Runner(Agent("s1", quiet=liveness.QUIET_LIMIT_S + 1)))
    assert summary is not None and not liveness.alive(summary)


def test_doing_words():
    assert liveness.doing({"current_tool": None}) == "Still working: thinking"
    assert liveness.doing({"current_tool": "mcp__browser_exec"}) == "Still working: working in the browser"
    assert liveness.doing({"current_tool": "some_new_tool"}) == "Still working"


def test_watch_calls_back_only_while_alive_and_stops():
    agent = Agent("s1", tool="delegate_task")
    runner = Runner(agent)
    stop, seen = threading.Event(), []

    def on_alive(summary):
        seen.append(summary["current_tool"])
        if len(seen) == 2:
            runner.agents.clear()  # the turn ended: no more vouching
        if len(seen) > 2:
            raise AssertionError("vouched for a turn that is gone")

    t = threading.Thread(target=liveness.watch, args=(lambda: "s1", on_alive, stop),
                         kwargs={"every_s": 0.01, "runner": runner})
    t.start()
    threading.Event().wait(0.2)
    stop.set()
    t.join(1)
    assert not t.is_alive()
    assert seen == ["delegate_task", "delegate_task"]


def test_broken_runner_is_harmless():
    class Broken:
        def _snapshot_running_agents(self):
            raise RuntimeError("boom")
    assert liveness.session_activity("s1", Broken()) is None
