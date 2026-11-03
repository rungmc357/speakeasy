"""Behaviour seen on real phone calls: status questions, pushback, scraps, repeats, late results."""
from speakeasy import router, text
from speakeasy.prompt import builder as P

SETUP = router.OpenTask("t_setup", "Right. Keep going with provisioning the new computer", "running", age_s=300)
DOCK = router.OpenTask("t_dock", "Copy the laptop's dock layout over", "completed",
                       "Your dock matches the laptop now.", age_s=600)
FOOD = router.OpenTask("t_food", "Find a taco spot open near me", "running", age_s=90)


def never_called(_messages):
    raise AssertionError("the routing model must not be asked")


def test_status_questions_are_answered_not_queued():
    for said in ["What's the status", "Any progress on provisioning",
                 "Are you still checking?", "How we looking", "how's the taco search going"]:
        d = router.decide(said, [DOCK, SETUP, FOOD], None, [], never_called)
        assert d.parts[0].kind == router.STATUS, said
    assert router.decide("how's the taco search going", [SETUP, FOOD], None, [], never_called
                         ).parts[0].task_id == "t_food"
    assert router.decide("What's the status", [SETUP], None, [], never_called).parts[0].task_id == "t_setup"


def test_status_question_with_no_tasks_is_ordinary_work():
    d = router.decide("what's the status of my package", [], None, [], lambda m: '{"parts": ["x"]}')
    assert d.parts[0].kind == router.NEW


def test_scraps_start_nothing():
    for said in ["that", "It's", "uh", "Okay."]:
        assert router.decide(said, [SETUP], None, [], never_called).parts[0].kind == router.IGNORE, said


def test_bare_yes_answers_what_was_just_offered():
    d = router.decide("Uh, sure", [SETUP, DOCK], None, [], never_called, replied_task_id="t_dock")
    assert d.parts[0].kind == "follow_up" and d.parts[0].task_id == "t_dock"
    # Without a known last answer, a yes is not guessed onto some task by the quick rules.
    assert router.quick_intent("sure", [SETUP, DOCK], None) is None


def test_pushback_is_routed_with_the_answer_it_disputes():
    seen = {}

    def capture(messages):
        seen["text"] = messages[0]["content"] + messages[1]["content"]
        return '{"follow_up_task_id": "t_dock"}'

    d = router.decide("You don't, but Hermes does", [SETUP, DOCK], None, [], capture, replied_task_id="t_dock",
                      chats=[router.Chat("c1", "Discord | old chat", ("hi",))])
    assert d.parts[0].task_id == "t_dock"
    assert "last spoken answer came from this task" in seen["text"] and "pushing back" in seen["text"]


def test_same_request_twice_seconds_apart_is_one_task():
    first = router.OpenTask("t_on", "Walk me through the onboarding screen for Speakeasy", "running", age_s=3)
    again = "Walk me through the onboarding screen for Speakeasy, it's asking about the microphone"
    d = router.decide(again, [first], None, [], never_called)
    assert d.parts[0].kind == "follow_up" and d.parts[0].task_id == "t_on"


def test_two_different_asks_back_to_back_stay_separate():
    first = router.OpenTask("t_a", "Book a table for two tonight", "running", age_s=1)
    assert router.quick_intent("Find the cheapest flight to Rome next week", [first]) is None


def test_titles_are_never_made_of_filler():
    assert text.short_title("Uh, sure") is None
    assert text.short_title("that") is None
    assert text.short_title("Find a taco spot open near me") == "Find a taco spot open near"


def test_status_answer_uses_reported_steps_not_plumbing():
    line = P.status_answer("Laptop setup", "running",
                           ["Continuing in Discord \"old chat\"", "Installing apps with Homebrew",
                            "Copying settings from the laptop"], 125, None)
    assert "Installing apps with Homebrew" in line and "Copying settings" in line
    assert "Continuing in" not in line and "2 min" in line and "do not start new work" in line
    done = P.status_answer("Dock", "completed", [], 30, "Your dock matches the laptop now.")
    assert "matches the laptop" in done


def test_late_result_says_what_it_answers_first():
    line = P.late_result("organize my dock", "Buzz and Screens were taken off.")
    assert "organize my dock" in line and "answer that first" in line


def test_rules_forbid_refusing_on_the_backends_behalf():
    rules = P.rules_text(P.Names.from_settings({"assistant_name": "Todd", "user_name": "Sam"}))
    assert "never tell Sam you can't do something" in rules
    assert "never delegate a status question" in rules


def test_voice_introduces_itself_by_its_name_never_as_chatgpt():
    from speakeasy.prompt import builder as PB
    names = PB.Names("Todd", "Geo")
    text = PB.build_live_instructions(names, brief="B" * 50)
    assert "# Who you are\nYour name is Todd. Greet Geo as Todd; never introduce yourself as ChatGPT" in text
