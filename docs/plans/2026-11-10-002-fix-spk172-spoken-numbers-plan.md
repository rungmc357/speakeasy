# SPK-172 unit A: spoken numbers count, and a request never joins a task about another number

## Problem
The number checks see digits only, so "forty-one" and "forty-two" read as the same words and two asks merge.
Nothing compares numbers when an existing task is picked, so "push forty-one" joins the release 42 task.

## Data shape
`recap.ids(text) -> set[str]`: the identifier numbers a text names, as digit strings, after spelled numbers
are turned into digits (`recap.digits`: "forty-one", "forty one" -> 41, "twelve" -> 12; a lone "one" stays a
word). A number is an identifier when it is:
- next to an identifier word: after it ("release 42", "build sixty-eight", "#12", "v2") or before it ("the 41 build");
- or bare, with nothing after it or a joining word after it ("pushing 41 to public", "42 is fine", "push 67 now"),
  and not after a counting word ("top three", "in ten", "for 4", "make it six", "set it to 70").
Times ("4:15", "4 pm"), money and percentages are never identifiers.

## The rule
`recap.names_other_number(a, b)`: both `ids` sets are non-empty and share nothing. One guard in `router.decide`
applies it to every follow-up part, whichever path picked the task (router model `follow_up_task_id`, the
voice's marked id, focused-task fallback, and the deterministic picks). A follow-up whose task's request names
another number becomes a new task with the same words. `decide` becomes a thin wrapper over the old body.

Merging keeps its existing digit check (`recap.py` "different numbers are never one subject"); `subject()` now
reads `digits(text)`, so spoken numbers reach that check.

## Truth table (request vs task request)
| # | Case | ids(request) / ids(task) | Answer |
|---|---|---|---|
| 1 | "pushing forty-one to public now" / "release forty-two" | {41} / {42} | new task |
| 2 | "41" vs "release forty-two"; "forty-one" vs "release 42" | {41} / {42} | new task |
| 3 | "push sixty-seven to public now" / "build sixty-eight progress" | {67} / {68} | new task |
| 4 | "forty one" / "release forty two" | {41} / {42} | new task |
| 5 | 1-4 via model id, marked id, focus fallback | guard is on decide's output | new task |
| 6 | "push forty-two to public now", "42 is fine to push" | {42} / {42} | attach |
| 7 | "push it to public now" | {} / {42} | attach |
| 8 | "make it six" / "book a table for 4"; "set it to 70" / "...thermostat to 72" | {} / {} (after it, for, to) | attach |
| 9 | "top three blockers", "in ten minutes", "the 4:15 then" | {} (counting words, time) / {42} | attach |
| 10 | matching mark | not fired; source stays "marked" | attach |
| 11 | 41/42, 68/69, 12/13, "release 41"/"release forty-two" parts | subject numbers differ | two parts |
| 12 | "status of release forty-one?" + "what's still left on release 41?" | both {41} + "release" | one part |
| 13 | "the one you mentioned" | lone "one" not converted | no number |

## Files
- `plugin/speakeasy/recap.py`: `digits`, `ids`, `names_other_number`; `subject` reads `digits(text)`.
- `plugin/speakeasy/router.py`: `decide` wraps `_decide` and applies the guard once.
- `plugin/tests/test_call_replay_spk172.py`: the truth table as tests.

## Test scenarios
- Merge: row 11 pairs stay two parts; row 12 and a digits-only pair merge; row 13 has no "1" in its subject.
- `decide` truth table, parametrised over the three paths (model, marked, focus): rows 1-4 give exactly
  `[Part("new", words)]`; rows 6-9 and 13 give a follow-up to the task with source model/marked/focus.
- End to end through a call: release 42 finished, then running (Hermes held); the publish runs as its own task
  with its own words, nothing is steered into 42, nothing is announced as added to it. A publish naming no
  number still joins the release 42 task as "<words> (following up: ...)".
- Later units: "two different jobs" (setup now glues a numberless publish), progress line, lost question:
  `xfail(strict=True)`, each checked to fail at its own assertion.

## Not in scope
Status asks (`status_task_id`) about another number are not guarded (not joining work; a later unit can reuse
the rule). Hundreds ("one twenty-three") and a lone "one" are not read as numbers. Title numbers are SPK-243's.
