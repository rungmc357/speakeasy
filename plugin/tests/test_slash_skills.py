"""Skills as slash commands in the message box (speakeasy/skills.py, docs/API.md "Skills"): the list a
composer offers after "/", and a typed "/skill words" running as a real skill invocation built by
Hermes' own skill_commands. Hermes' module is faked here with invented skills."""
import sys
import types

import pytest

from fakes import http, wait_for
from speakeasy import skills as SK

SKILLS = {
    "/soup-scaler": {"name": "soup-scaler", "description": "Use when scaling a soup recipe for more people.",
                     "skill_dir": "/home/me/.hermes/skills/cooking/soup-scaler"},
    "/bike-fit": {"name": "bike-fit", "description": "Use when sizing a bike frame.   Asks for inseam.",
                  "skill_dir": "/home/me/.hermes/skills/bike-fit"},
    "/garden-plan": {"name": "garden-plan", "description": "x" * 400,
                     "skill_dir": "/home/me/.hermes/skills/outdoors/garden-plan"},
    "/hidden-one": {"name": "hidden-one", "description": "Disabled for the API server.",
                    "skill_dir": "/home/me/.hermes/skills/hidden-one"},
}


def _block(key, words):
    name = SKILLS[key]["name"]
    return (f'[IMPORTANT: The user has invoked the "{name}" skill, indicating they want you to follow its '
            f"instructions. The full skill content is loaded below.]\n\n# {name} steps\n\n"
            f"The user has provided the following instruction alongside the skill invocation: {words}")


@pytest.fixture
def fake_hermes_skills(monkeypatch):
    built = []
    sc = types.ModuleType("agent.skill_commands")
    sc.get_skill_commands = lambda: dict(SKILLS)

    def resolve(command, interactive=False):
        key = "/" + command.replace("_", "-").lower()
        return key if key in SKILLS else None

    def build(key, words="", task_id=None, runtime_note=""):
        built.append((key, words, task_id))
        return _block(key, words)

    def stacked(keys, words="", task_id=None):
        built.append((tuple(keys), words, task_id))
        return ("[IMPORTANT: The user has invoked the stacked skill bundle]\n" + "\n".join(keys)
                + f"\nUser instruction: {words}", [k.lstrip("/") for k in keys], [])

    sc.resolve_skill_command_key = resolve
    sc.build_skill_invocation_message = build
    sc.build_stacked_skill_invocation_message = stacked
    su = types.ModuleType("agent.skill_utils")
    su.get_disabled_skill_names = lambda platform=None: {"hidden-one"} if platform == "api_server" else set()
    agent = types.ModuleType("agent")
    agent.__path__ = []  # a package
    agent.skill_commands, agent.skill_utils = sc, su
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.skill_commands", sc)
    monkeypatch.setitem(sys.modules, "agent.skill_utils", su)
    SK.reset_cache()
    yield built
    SK.reset_cache()


def get(server, path):
    return http(server.base_url, "GET", path, token=server.token)


def post(server, path, body):
    return http(server.base_url, "POST", path, body, server.token)


# -- the list ------------------------------------------------------------------------------------

def test_skills_route_lists_enabled_skills_sorted_with_short_descriptions(server, fake_hermes_skills):
    status, body = get(server, "/voice/skills")
    assert status == 200, body
    commands = [s["command"] for s in body["skills"]]
    assert commands == ["bike-fit", "garden-plan", "soup-scaler"]  # sorted; the disabled one left out
    bike = body["skills"][0]
    assert bike == {"command": "bike-fit", "name": "bike-fit", "category": "",
                    "description": "Use when sizing a bike frame. Asks for inseam."}
    assert body["skills"][2]["category"] == "cooking"
    assert len(body["skills"][1]["description"]) <= SK.MAX_DESCRIPTION
    assert body["skills"][1]["description"].endswith("…")


def test_status_advertises_slash_skills(server):
    assert get(server, "/voice/status")[1]["capabilities"]["slash_skills"] is True


def test_no_skill_module_means_an_empty_list(server, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.skill_commands", None)  # an older Hermes: import fails
    SK.reset_cache()
    status, body = get(server, "/voice/skills")
    assert status == 200 and body == {"skills": []}
    SK.reset_cache()


# -- parsing ---------------------------------------------------------------------------------------

def test_leading_commands(fake_hermes_skills):
    assert SK.leading_commands("/soup-scaler for twelve") == (["/soup-scaler"], "for twelve")
    assert SK.leading_commands("/soup_scaler for twelve") == (["/soup-scaler"], "for twelve")
    assert SK.leading_commands("/bike-fit /soup-scaler then lunch") == (["/bike-fit", "/soup-scaler"], "then lunch")
    assert SK.leading_commands("/bike-fit") == (["/bike-fit"], "")
    assert SK.leading_commands("/not-a-skill hello") == ([], "/not-a-skill hello")
    assert SK.leading_commands("/hidden-one hello") == ([], "/hidden-one hello")   # disabled here
    assert SK.leading_commands("use /bike-fit please") == ([], "use /bike-fit please")  # not leading
    assert SK.leading_commands("/bike-fit/extra") == ([], "/bike-fit/extra")


def test_invocation_uses_hermes_builder(fake_hermes_skills):
    block = SK.invocation("/soup-scaler for twelve", "idem_1")
    assert block.startswith('[IMPORTANT: The user has invoked the "soup-scaler" skill')
    assert fake_hermes_skills == [("/soup-scaler", "for twelve", "idem_1")]
    stacked = SK.invocation("/bike-fit /soup-scaler both please")
    assert "stacked skill bundle" in stacked and "User instruction: both please" in stacked
    assert SK.invocation("plain words") == ""


# -- a typed /skill message runs the skill ------------------------------------------------------

def test_new_chat_with_a_skill_carries_the_skill_and_history_shows_the_typed_words(
        server, service, hermes, fake_hermes_skills):
    status, body = post(server, "/voice/work", {"text": "/soup-scaler for twelve people", "source": "typed"})
    assert status == 202, body
    call = wait_for(lambda: hermes.calls)[0]
    prompt = call["input"]
    assert '[IMPORTANT: The user has invoked the "soup-scaler" skill' in prompt
    assert "instruction alongside the skill invocation: for twelve people" in prompt
    # The skill sits before the transcript: the person's words stay its last "User:" line.
    assert prompt.index("soup-scaler steps") < prompt.index("Recent timestamped voice transcript")
    from speakeasy import history as H
    assert H.user_words(prompt) == ("user", "/soup-scaler for twelve people")


def test_plain_words_and_unknown_commands_go_as_typed(server, service, hermes, fake_hermes_skills):
    assert post(server, "/voice/work", {"text": "/not-a-skill scale the soup", "source": "typed"})[0] == 202
    prompt = wait_for(lambda: hermes.calls)[0]["input"]
    assert "has invoked" not in prompt and "/not-a-skill scale the soup" in prompt
    assert fake_hermes_skills == []


def test_a_skill_typed_to_a_running_task_steers_with_the_skill(server, service, hermes, fake_hermes_skills):
    from test_task_messages import one_task, send
    from test_routing import start_call
    hermes.hold = True
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Plan dinner for the block party")
    status, body = send(server, task["run_id"], "/soup-scaler make it serve forty")
    assert status == 200 and body["delivered_as"] == "steer", body
    steer = wait_for(lambda: hermes.steers)[-1][1]
    assert steer.index("/soup-scaler make it serve forty") < steer.index('invoked the "soup-scaler" skill')
    assert "instruction alongside the skill invocation: make it serve forty" in steer
