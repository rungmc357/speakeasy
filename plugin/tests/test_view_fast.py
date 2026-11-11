"""The view fast path: "show me build 75 tickets" is one fast-model turn, one tool call and a native
list widget, never a Hermes run (made-up issues throughout; Linear is faked)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fakes import http, wait_for
from test_routing import tasks
from speakeasy import view_fast as V


def _issue(n, title, state="Todo", stype="unstarted", labels=("build-75",), who=None):
    return {"identifier": f"SPK-{n}", "title": title, "url": f"https://linear.app/x/issue/SPK-{n}/t",
            "priority": 2, "updatedAt": "2026-11-11T20:00:00.000Z", "state": {"name": state, "type": stype},
            "assignee": {"displayName": who} if who else None, "delegate": None,
            "labels": {"nodes": [{"name": l} for l in labels]}}


class FakeLinear(V.LinearSource):
    """Answers the GraphQL queries LinearSource sends; records them. No network, no key."""

    def __init__(self, view_issues=None, label_issues=None):
        self.queries: list[tuple[str, dict]] = []
        self.view_issues, self.label_issues = view_issues, label_issues or []
        self._mod = SimpleNamespace(TEAM="SPK")

    def available(self):
        return True

    def _load(self):
        return self._mod

    def _gql(self, query, variables=None):
        self.queries.append((query, variables or {}))
        if "customViews" in query:
            nodes = [] if self.view_issues is None else [{"name": str((variables or {}).get("n", "")).title(),
                                                          "issues": {"nodes": self.view_issues}}]
            return {"customViews": {"nodes": nodes}}
        if "searchIssues" in query:
            return {"searchIssues": {"nodes": self.label_issues}}
        return {"issues": {"nodes": self.label_issues}}


BUILD_75 = [_issue(398, "Call voice promises a card only once it's on screen", "In Progress", "started", who="Dana"),
            _issue(387, "Every Claude call waited 30 s before starting", "Done", "completed"),
            _issue(390, "Home widget swipe reply keeps its page", "Ready to ship", "started", ("build-75", "iOS"))]


class FastModel:
    """The coordinator's fast model, faked: always one view tool call. Records every turn."""

    def __init__(self, tool=V.LINEAR_TOOL, args=None):
        self.turns: list[tuple[list, list]] = []
        self.tool, self.args = tool, args if args is not None else {"filter": "build 75", "kind": "build"}

    def __call__(self, messages, tools):
        self.turns.append((messages, tools))
        return {"tool": self.tool, "args": dict(self.args), "text": ""}, {"model": "fast-model"}


@pytest.mark.parametrize("words", ["show me build 75 tickets", "show linear widget for build 74.1 work",
                                   "what's in build 76", "list agent-ready issues", "pull up the speakeasy board"])
def test_list_requests_are_views(words):
    assert V.wanted(words)


@pytest.mark.parametrize("words", ["fix the build 75 tickets", "move SPK-12 to done", "show me how the router works",
                                   "why is build 75 late", "book a table", "show me"])
def test_work_is_never_a_view(words):
    assert not V.wanted(words)


def test_the_view_prompt_is_small():
    assert V.prompt_chars("show linear widget for build 74.1 work") < 4000


def test_build_rows_widget_from_one_linear_read():
    linear = FakeLinear(view_issues=BUILD_75)
    view, spoken = V.Sources(linear).run(V.LINEAR_TOOL, {"filter": "build 75", "kind": "build"})
    assert len(linear.queries) == 1 and "customViews" in linear.queries[0][0]
    assert view["kind"] == "rows" and view["entity"] == "issue" and view["title"] == "Build 75"
    assert [i["ref"] for i in view["items"]] == ["SPK-398", "SPK-390", "SPK-387"]   # open first, done last
    first = view["items"][0]
    assert first["state"] == "In Progress" and first["who"] == "Dana" and first["url"].startswith("https://linear.app/")
    assert view["items"][1]["note"] == "iOS"                                       # the build label isn't repeated
    assert spoken.startswith("Build 75: 3 issues")


def test_a_build_without_a_view_falls_back_to_its_label():
    linear = FakeLinear(view_issues=None, label_issues=BUILD_75[:1])
    view, _ = V.Sources(linear).run(V.LINEAR_TOOL, {"filter": "build 75"})
    assert view["title"] == "Build 75" and len(view["items"]) == 1
    assert linear.queries[1][1]["f"]["labels"] == {"name": {"eqIgnoreCase": "build-75"}}


def test_serve_makes_one_tool_call_and_renders_natively():
    model, marks = FastModel(), {}
    got = V.serve("show me build 75 tickets", model, V.Sources(FakeLinear(view_issues=BUILD_75)), marks.__setitem__)
    assert got is not None and got["tool"] == V.LINEAR_TOOL and len(model.turns) == 1
    assert [t["function"]["name"] for t in model.turns[0][1]] == [V.LINEAR_TOOL]   # only the view tools
    assert got["views"][0]["kind"] == "rows"
    assert set(marks) == {"view_first_model_ms", "view_tool_ms", "view_render_ms"}


def test_a_model_that_calls_no_tool_hands_over():
    assert V.serve("show me build 75 tickets", lambda m, t: "Here you go", V.Sources(FakeLinear(BUILD_75))) is None


def test_a_claude_subscription_tool_name_is_the_same_tool():
    assert V.tool_call({"tool": "mcp__speakeasy_linear_view", "args": {"filter": "build 75"}}) == (
        V.LINEAR_TOOL, {"filter": "build 75"})
    assert V.tool_call({"tool": "mcp__start_job", "args": {}}) is None


def test_kanban_board_rows(tmp_path):
    class Boards:
        def list(self):
            return {"boards": [{"slug": "speakeasy", "name": "Speakeasy"}], "suggested": "speakeasy"}

        def board(self, slug):
            return {"slug": slug, "name": "Speakeasy", "columns": [
                {"key": "blocked", "title": "Blocked", "count": 1, "cards": [{"title": "Ship 75", "reason": "Needs OK"}]},
                {"key": "running", "title": "Running", "count": 1, "cards": [{"title": "View fast path"}]}]}

    view, spoken = V.Sources(None, Boards()).run(V.KANBAN_TOOL, {"board": "speakeasy"})
    assert view["kind"] == "rows" and [i["name"] for i in view["items"]] == ["Ship 75", "View fast path"]
    assert view["items"][0]["tone"] == "warn" and "1 blocked" in spoken


def test_show_me_build_75_tickets_is_one_fast_turn_and_a_list_widget(server, service, hermes):
    """The whole path from Home's composer: no Hermes run, one fast-model turn with exactly one tool
    call, a rows widget on the finished card, and the steps timed in runs.timings."""
    model = FastModel()
    service.rt.view_call = model
    service.rt.view_sources = V.Sources(FakeLinear(view_issues=BUILD_75), service.boards)
    status, body = http(server.base_url, "POST", "/voice/work", {"text": "show me build 75 tickets"}, server.token)
    assert status == 202, body
    row = wait_for(lambda: [t for t in tasks(server) if t.get("run_id") == body["run_id"] and t["status"] == "completed"])[0]
    assert hermes.calls == []                                   # never the agent
    assert len(model.turns) == 1                                # one model turn …
    view = row["result"]["views"][0]
    assert view["kind"] == "rows" and view["title"] == "Build 75" and len(view["items"]) == 3
    assert "png" not in json.dumps(row["result"]).lower() and "<html" not in json.dumps(row["result"]).lower()
    idem = service.store.key_for_run(body["run_id"])
    timings = service.store.timings(idem) if idem else {}
    assert {"view_routing_ms", "view_first_model_ms", "view_tool_ms", "view_render_ms", "view_total_ms"} <= set(timings)


def test_a_coordinator_rewording_still_takes_the_users_words(service, hermes):
    """Home's coordinator may write the job in its own words; the user's words decide and are sent."""
    model = FastModel()
    service.rt.view_call = model
    service.rt.view_sources = V.Sources(FakeLinear(view_issues=BUILD_75), service.boards)
    out = service.start_job({"text": "Pull the Linear issues for build 75"}, None, asked="show me build 75 tickets")
    wait_for(lambda: service.store.run_status(service.store.key_for_run(out["run_id"]) or "") == "completed")
    assert hermes.calls == [] and model.turns[0][0][1]["content"] == "show me build 75 tickets"


def test_a_view_the_fast_path_cant_serve_goes_to_hermes(server, service, hermes):
    service.rt.view_call = lambda m, t: "no tool"
    service.rt.view_sources = V.Sources(FakeLinear(view_issues=BUILD_75), service.boards)
    status, body = http(server.base_url, "POST", "/voice/work", {"text": "show me build 75 tickets"}, server.token)
    assert status == 202
    wait_for(lambda: hermes.calls)


def test_view_turn_uses_the_coordinators_fast_model(service, monkeypatch):
    seen = {}

    def call_llm(**kw):
        seen.update(kw)
        fn = SimpleNamespace(name=V.LINEAR_TOOL, arguments=json.dumps({"filter": "build 75"}))
        msg = SimpleNamespace(content="", tool_calls=[SimpleNamespace(function=fn)])
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], model="fast-model")

    from speakeasy import host as host_mod
    monkeypatch.setattr(host_mod, "aux_client", lambda: (call_llm, lambda r: None))
    monkeypatch.setattr(service.home_chat, "route", lambda: {"provider": "fastco", "model": "fast-1", "source": "auto"})
    answer, _ = service.home_chat.view_turn(V.messages("show me build 75 tickets"), V.TOOLS)
    assert (seen["provider"], seen["model"]) == ("fastco", "fast-1")
    assert seen["tools"] == V.TOOLS and seen["max_tokens"] == V.MAX_TOKENS
    assert answer["tool"] == V.LINEAR_TOOL
