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


@pytest.mark.parametrize("words", ["show me build 75 tickets", "pull up the linear list for build 73.2",
                                   "what's in build 76", "list agent-ready issues", "pull up the speakeasy board"])
def test_list_requests_are_views(words):
    assert V.wanted(words)


@pytest.mark.parametrize("words", ["fix the build 75 tickets", "move SPK-12 to done", "show me how the router works",
                                   "why is build 75 late", "book a table", "show me"])
def test_work_is_never_a_view(words):
    assert not V.wanted(words)


def test_the_view_prompt_is_small():
    assert V.prompt_chars("pull up the linear list for build 73.2") < 4000


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


# -- status questions ("status of \"snappy\" work", "how's SPK-12 going") -----------------------------

SNAPPY = [_issue(391, "Snappy: Working card the moment a task is accepted", "Ready to ship", "started", ("plugin",)),
          _issue(380, "Snappier voice handoff", "Done", "completed", ("plugin",))]


class StatusLinear(FakeLinear):
    def _gql(self, query, variables=None):
        if "issue(id:" in query:
            self.queries.append((query, variables or {}))
            want = (variables or {}).get("id")
            return {"issue": next((i for i in SNAPPY if i["identifier"] == want), None)}
        return super()._gql(query, variables)


class CardBoards:
    """Kanban, faked: SPK-391 has a running card with a heartbeat note."""

    def __init__(self, cards=None):
        self.asked: list[list[str]] = []
        self.cards = cards if cards is not None else {"SPK-391": {
            "id": "t_1", "title": "SPK-391: Snappy", "status": "running", "column": "running", "board": "Speakeasy",
            "heartbeat": "Plugin suite 86%, no failures so far", "heartbeat_at": 1794430000, "summary": None}}

    def list(self):
        return {"boards": [], "suggested": None}

    def cards_for(self, refs):
        self.asked.append(list(refs))
        return {r: c for r, c in self.cards.items() if r in refs}


@pytest.mark.parametrize("words,looks_for", [
    ("status of \"snappy\" work for speakeasy", "snappy"),
    ("what's the status of the swipe reply fix", "swipe reply fix"),
    ("where is the login fix", "login fix"),
    ("how's SPK-391 going", "SPK-391"),
    ("hey todd, how's build 75 coming along?", "build 75"),
    ("any update on the duo two pane work", "duo two pane"),
])
def test_status_questions_take_the_fast_path(words, looks_for):
    assert V.status_wanted(words) and V.wanted(words)
    assert V.status_words(words) == looks_for


@pytest.mark.parametrize("words", ["how's it going", "how are you", "where are we", "how is your day",
                                   "how's the weather in Lisbon", "why is snappy late",
                                   "status of snappy and then move it to done", "how is my mom doing"])
def test_chit_chat_and_changes_are_not_status(words):
    assert not V.status_wanted(words)


def test_status_prompt_is_small_and_has_one_tool():
    words = "status of \"snappy\" work for speakeasy"
    assert V.prompt_chars(words) < 1500
    assert V.messages(words)[0]["content"] == V.STATUS_SYSTEM
    assert [t["function"]["name"] for t in V.Sources(StatusLinear(label_issues=SNAPPY)).tools(status=True)] == [V.STATUS_TOOL]


def test_status_tool_joins_linear_and_the_kanban_card():
    linear, boards = StatusLinear(label_issues=SNAPPY), CardBoards()
    view, spoken = V.Sources(linear, boards).run(V.STATUS_TOOL, {"words": "snappy"})
    assert len(linear.queries) == 1 and "searchIssues" in linear.queries[0][0]
    assert linear.queries[0][1]["t"] == "snappy"
    assert boards.asked == [["SPK-391", "SPK-380"]]
    assert view["kind"] == "rows" and [i["ref"] for i in view["items"]] == ["SPK-391", "SPK-380"]
    first = view["items"][0]
    assert first["state"] == "Ready to ship" and first["note"].startswith("Card running: Plugin suite 86%")
    assert view["items"][1]["note"] == ""                      # no card: Linear only
    lines = spoken.split("\n")
    assert len(lines) <= 3 and all(len(l) <= 140 for l in lines)
    assert lines[0].startswith("SPK-391 Snappy") and "Ready to ship in Linear, card running" in lines[0]
    assert lines[1] == "Plugin suite 86%, no failures so far" and lines[2] == "1 more match below."


def test_status_by_ref_reads_that_one_issue():
    linear = StatusLinear()
    view, spoken = V.Sources(linear, CardBoards({})).run(V.STATUS_TOOL, {"words": "spk-391"})
    assert "issue(id:" in linear.queries[0][0] and linear.queries[0][1] == {"id": "SPK-391"}
    assert len(view["items"]) == 1 and spoken.count("\n") == 0


def test_status_with_no_match_goes_to_the_agent():
    assert V.serve("status of the zebra project", lambda m, t: {"tool": V.STATUS_TOOL, "args": {"words": "zebra"}},
                   V.Sources(StatusLinear(label_issues=[]), CardBoards())) is None


def test_a_status_model_that_calls_no_tool_still_answers_from_the_words():
    linear = StatusLinear(label_issues=SNAPPY)
    got = V.serve("status of \"snappy\" work for speakeasy", lambda m, t: "It's going well", V.Sources(linear, CardBoards()))
    assert got is not None and got["tool"] == V.STATUS_TOOL and got["args"] == {"words": "snappy"}

    def broken(m, t):
        raise TimeoutError
    assert V.serve("status of snappy", broken, V.Sources(linear, CardBoards())) is not None


def test_cards_for_reads_status_and_latest_heartbeat(tmp_path):
    from test_kanban_board import make_board, task, event
    from speakeasy.kanban_board import KanbanBoards
    root = tmp_path / "hermes"
    conn = make_board(root, "speakeasy", "Speakeasy")
    task(conn, "t_old", "SPK-391: Snappy (first try)", "done", 100, completed=150, summary="Superseded")
    task(conn, "t_new", "SPK-391: Snappy", "running", 200, started=210, run_started=210)
    task(conn, "t_other", "SPK-3910: Not this one", "running", 300)
    event(conn, "t_new", "heartbeat", {"note": "Tests 40%"}, 220)
    event(conn, "t_new", "heartbeat", {"note": "Tests 86%, no failures"}, 260)
    got = KanbanBoards(root).cards_for(["SPK-391", "SPK-999", "not a ref"])
    assert set(got) == {"SPK-391"}
    card = got["SPK-391"]
    assert card["id"] == "t_new" and card["column"] == "running" and card["board"] == "Speakeasy"
    assert card["heartbeat"] == "Tests 86%, no failures" and card["heartbeat_at"] == 260


def test_status_of_snappy_from_home_is_one_fast_turn_and_a_widget(server, service, hermes):
    """Geo's real slow case (11-11 17:23): no Hermes run, no skills, the widget and at most 3 lines."""
    model = FastModel(tool=V.STATUS_TOOL, args={"words": "snappy"})
    service.rt.view_call = model
    service.rt.view_sources = V.Sources(StatusLinear(label_issues=SNAPPY), CardBoards())
    status, body = http(server.base_url, "POST", "/voice/work",
                        {"text": "status of \"snappy\" work for speakeasy"}, server.token)
    assert status == 202, body
    row = wait_for(lambda: [t for t in tasks(server) if t.get("run_id") == body["run_id"] and t["status"] == "completed"])[0]
    assert hermes.calls == [] and len(model.turns) == 1
    assert [t["function"]["name"] for t in model.turns[0][1]] == [V.STATUS_TOOL]
    view = row["result"]["views"][0]
    assert view["kind"] == "rows" and view["items"][0]["ref"] == "SPK-391"
    assert len(row["result"]["spoken"].split("\n")) <= 3


def test_task_agents_are_told_not_to_load_the_build_worker_skill_for_a_read():
    from speakeasy.prompt import builder as P
    brief = P.build_task_prompt(P.Names(), 1, "User: status of snappy")
    assert "speakeasy-linear-worker" in brief and "do not load build-worker skills" in brief


def test_first_fetch_of_a_result_is_logged_once_with_the_gap(service, caplog):
    import logging
    state = service.home_state
    t0 = state._fetch_born
    item = {"id": "r:1", "kind": "result", "run_id": "run_1", "added_at": t0 + 1, "at": t0 + 0.5}
    old = {"id": "r:0", "kind": "result", "run_id": "run_0", "added_at": t0 - 100, "at": t0 - 100}
    with caplog.at_level(logging.INFO, logger="speakeasy.home_state"):
        assert state.note_fetched([item, old, {"id": "d:1", "kind": "decision"}]) == ["r:1"]
        assert state.note_fetched([item]) == []                  # once per result
    lines = [r.getMessage() for r in caplog.records if "delivery.first_fetch" in r.getMessage()]
    assert len(lines) == 1 and "id=r:1 run=run_1 landed_to_fetch_ms=" in lines[0] and "finished_to_fetch_ms=" in lines[0]


def test_on_a_call_a_view_shows_the_working_card_at_once_then_the_widget_in_its_place(server, service, hermes, monkeypatch):
    """snappy + view fast path together: the accepted request is a Working card before routing, and
    the fast answer fills in that same card (never a second one, never a Hermes run)."""
    import threading
    import time
    from speakeasy import calls, router
    from test_routing import start_call
    from test_snappy_handoff_card import items, handoff
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)
    _, worker = start_call(server, service)
    gate = threading.Event()

    def slow_route(request, tasks_, marked, chats=None, call_so_far="", replied_task_id=None, focus=None):
        gate.wait(10)
        return router.fallback_decision(request)
    worker.rt.route = slow_route
    model = FastModel()
    worker.rt.view_call = model
    worker.rt.view_sources = V.Sources(FakeLinear(view_issues=BUILD_75), service.boards)
    t0 = time.monotonic()
    handoff(worker, "call_view", "show me build 75 tickets")
    held = wait_for(lambda: next((i for i in items(server) if i.get("handoff")), None), timeout=3)
    assert time.monotonic() - t0 < 1.0 and held["kind"] == "working"
    gate.set()
    wait_for(lambda: hermes.calls or [t for t in tasks(server) if t["status"] == "completed"])
    assert hermes.calls == [], [(t["status"], t.get("request")) for t in tasks(server)]
    row = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"
                            and (t.get("result") or {}).get("views")])[0]
    assert hermes.calls == [] and len(model.turns) == 1 and row["result"]["views"][0]["kind"] == "rows"
    wait_for(lambda: not any(i.get("handoff") for i in items(server)))
    assert len([i for i in items(server) if i["id"] == held["id"]]) <= 1
