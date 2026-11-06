"""The live status rule: narrate real steps (including background work), never filler or raw reasoning."""
from speakeasy.prompt import builder as P


def test_status_rule_narrates_steps_and_background_work_without_filler():
    rule = P.status_rule(P.Names("Ava", "Sam"))
    # The two-line format the app parses is unchanged.
    assert "STATUS: <" in rule and "DETAIL: <" in rule
    assert "DONE: <" in rule and "SPOKEN: <" in rule
    # A fresh pair at each meaningful step, including subagent calls and long operations started in this session.
    assert "each meaningful step" in rule
    assert "subagent" in rule and "long operation" in rule and "meanwhile" in rule
    # Pairs never land in the final answer (split_result strips only DONE/SPOKEN), and STATUS stays an
    # -ing action: valid_short_status drops anything else.
    assert "Never put STATUS or DETAIL lines in the final answer" in rule
    assert "STATUS always starts with an -ing verb" in rule
    # Nothing nudges toward other sessions or background workers, which the task prompt forbids.
    assert "parallel sessions" not in rule and "delegated workers" not in rule
    # No filler, no repeats, and still no raw reasoning, credentials or tool arguments.
    assert "Skip a pair that would only repeat the last one" in rule and "still working" in rule
    assert "never how you weigh it: no credentials or raw tool arguments" in rule
    # Placeholders were rendered, never left raw.
    assert "{user_name" not in rule and "Sam" in rule


def test_status_rule_never_asks_for_the_models_thinking():
    """Anthropic's filter refuses prompts that look like they pull out a model's reasoning
    (stop_reason=refusal, category reasoning_extraction). The task brief's status rule asks for the
    work and what was found, in words that never point at the model's own thinking."""
    rule = P.status_rule(P.Names("Ava", "Sam")).lower()
    for phrase in ("thinking", "reasoning", "deliberat", "thought"):
        assert phrase not in rule, phrase


def test_every_task_prompt_carries_the_status_rule():
    names = P.Names("Ava", "Sam")
    rule = P.status_rule(names)
    assert rule in P.build_task_prompt(names, 1, "User: check the weather", "check the weather")


def test_the_status_examples_pass_the_status_validator():
    from speakeasy.text import valid_short_status
    for example in ("Checking the weather forecast", "Reviewing the build results"):
        assert valid_short_status(example) == example
    assert valid_short_status("Build finished") is None  # why the rule insists on an -ing verb
