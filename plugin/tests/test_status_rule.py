"""The live status rule: narrate real steps (including background work), never filler or raw reasoning."""
from speakeasy.prompt import builder as P


def test_status_rule_asks_for_goal_terms_once_per_part_of_the_job():
    rule = P.status_rule(P.Names("Ava", "Sam"))
    # The two-line format the app parses is unchanged.
    assert "STATUS: <" in rule and "DETAIL: <" in rule
    assert "DONE: <" in rule and "SPOKEN: <" in rule
    # Goal terms, a fresh pair only when the job moves to a new part (SPK-200's validated wording),
    # and still as interim commentary before the tool call (the wording Qwen needs).
    assert "in Sam's terms" in rule and "starting with an -ing verb" in rule
    assert "only when the job moves to a new part (not every step)" in rule
    assert "write interim commentary" in rule and "before the tool call" in rule
    assert "Name the goal, not the tool or file operation" in rule
    # DETAIL says what is done or found, not a restatement of STATUS (the doubled live line).
    assert "what is done or found so far" in rule and "at this moment" not in rule
    # Pairs never land in the final answer (split_result strips only DONE/SPOKEN).
    assert "Never put STATUS or DETAIL lines in the final answer" in rule
    # Nothing nudges toward other sessions or background workers, which the task prompt forbids.
    assert "parallel sessions" not in rule and "delegated workers" not in rule
    # Placeholders were rendered, never left raw.
    assert "{user_name" not in rule and "Sam" in rule


def test_status_rule_is_about_half_the_old_rules_size():
    """The old step-by-step rule's live-status part was ~350 prompt tokens; the goal-terms one ~165."""
    rule = P.status_rule(P.Names("Ava", "Sam"))
    status_part = rule[:rule.index("End your final answer")]
    assert len(status_part) < 800, len(status_part)


def test_status_rule_never_asks_for_the_models_thinking():
    """Anthropic's filter refuses prompts that look like they pull out a model's reasoning
    (stop_reason=refusal, category reasoning_extraction). The task brief's status rule asks for the
    work and what was found, in words that never point at the model's own thinking."""
    rule = P.status_rule(P.Names("Ava", "Sam")).lower()
    for phrase in ("thinking", "reasoning", "deliberat", "thought"):
        assert phrase not in rule, phrase


# Wording that reads as a request for the model's own reasoning (Anthropic refuses such prompts as
# reasoning_extraction). None of it may appear in anything Speakeasy sends a task.
REASONING_ASKS = ("thinking", "reasoning", "deliberat", "thought", "chain of", "step-by-step", "step by step",
                  "how you weigh", "how you decide", "your thinking", "inner monologue", "scratchpad",
                  "explain your", "narrate the")


def _no_reasoning_ask(text):
    low = text.lower()
    return [p for p in REASONING_ASKS if p in low]


def test_task_brief_never_asks_for_or_replays_reasoning():
    """The whole task brief (status rule, card rules, task line) carries no reasoning-request phrasing,
    for a call, a typed task and a follow-up of an earlier task."""
    names = P.Names("Ava", "Sam")
    briefs = [P.build_task_prompt(names, 1, "User: check the weather", "check the weather"),
              P.build_task_prompt(names, 3, "User: what's the status of build 67\nAssistant: Build 67 has 5 open tickets.",
                                  "which tickets are still open for that")]
    for brief in briefs:
        assert _no_reasoning_ask(brief) == [], _no_reasoning_ask(brief)


def test_every_task_prompt_carries_the_status_rule():
    names = P.Names("Ava", "Sam")
    rule = P.status_rule(names)
    assert rule in P.build_task_prompt(names, 1, "User: check the weather", "check the weather")


def test_the_status_examples_pass_the_status_validator():
    from speakeasy.text import valid_short_status
    for example in ("Checking the weather forecast", "Reviewing the build results", "Adding the composer to Home"):
        assert valid_short_status(example) == example
    assert valid_short_status("Build finished") is None  # why the rule insists on an -ing verb
