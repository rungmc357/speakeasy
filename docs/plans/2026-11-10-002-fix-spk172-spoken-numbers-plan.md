# SPK-172: a request never joins a task about another build, and no everyday follow-up is cut off

## Problem
The number checks saw digits only, so "forty-one" and "forty-two" merged as one ask, and nothing compared
numbers when an existing task was picked: "push forty-one" joined release 42's task. Unit A (462548f) fixed
that with a bare-number classifier (`ids`: the words next to a number decide whether it names a build). Seven
reviewers found it refused dates, rooms, budgets, options, counts and spoken times ("change it to May 14" on a
May 12 booking became a new task with no context) and still attached "go ahead with forty-one". Round 1
removes the classifier: the words around a number do not say whether it names a build.

## Data shape
`recap.keyed(text) -> {family: {numbers}}`: numbers said right after a singular naming word, by family:
builds (`build release version v rc`) and tickets (`issue ticket pr bug #`). "release forty-two", "#12",
"v2", a list after the word ("PR 12 and 13"), and two or more digits before the word ("the 41 build", task
side only). A plural after a number is a count ("the last three builds"); `number`, `sprint`, `milestone`
are not naming words. Spelled numbers become digits first (`recap.digits`), but only a number said on its own:
one touching another number stays words ("twenty-first", "two point three", "four thirty", "one twenty-three").

## The rule (`recap.names_other_number(task_request, follow_up)`, applied in `router.decide`)
A follow-up leaves its task only when the task names its build or ticket with a naming word and the follow-up,
without saying that number, names another one: with a naming word of the same family ("release 41"), or as a
bare whole number of the same digit count within 3 of it ("push 41 now"). A correction ("no, I meant 43") stays.
1. The task names no keyed number: never refuse (dates, rooms, budgets, thermostats, options).
2. The follow-up says one of the task's keyed numbers anywhere, keyed or bare: never refuse.
3. A correction (`^no|nope|sorry|I mean(t)|make that`, or `sorry` at the end): never refuse.
4. The follow-up has a keyed number of the task's family (said after its word): refuse. Another family is not
   compared ("issue 41" on release 42's task).
5. A bare number M refuses when: whole, two digits or more; some task number N has the same digit count and
   0 < |M - N| <= 3; no number or number word touches it, and no `$ € £ #` before it (times, decimals,
   hundreds, prices); not followed by a unit of time or amount (`minutes`, `pm`, `percent`, `dollars`...).
The task's whole request is read, "(following up: ...)" included: after one follow-up the build is often only
named in that note. A refused follow-up becomes `Part("new", words)` and the decision's source is
`"kept_apart"` (calls.py only logs it; what the voice is told does not change).

`subject()` digitises a spelled number only after a naming word, so two builds said as words in one breath stay
two tasks, and spelled counts ("three blockers" vs "five blockers") are words again, as before unit A.

## Truth table (task / follow-up -> answer; every row runs through model, marked and focus picks)
| # | Case | Answer |
|---|---|---|
| 1-4 | "pushing forty-one to public now" / release forty-two; 41 vs release 42; 67 vs build 68 | own task |
| 5 | rows 1-4 via model id, marked id, focus fallback | own task, source `kept_apart` |
| 6 | "push forty-two to public now", "42 is fine", "push forty-two at nine thirty" | stays |
| 7 | "push it to public now", "push the one you mentioned" | stays |
| 8 | "make it six" / table for 4; "set it to 70" / thermostat 72; "make it June 5" / June 3 | stays (rule 1) |
| 9 | "top three blockers", "in ten minutes", "the 4:15 then", "the four fifteen then", "in 40 minutes" | stays |
| 10 | "go ahead with / what about / do the same for / roll back to / show me forty-one" on release 42 | own task |
| 11 | 41/42, 68/69, 12/13, "release 41"/"release forty-two" parts said together | two parts |
| 12 | "status of release forty-one?" + "what's still left on release 41?" | one part |
| 13 | "the one you mentioned" | no number |
| 14 | "no, I meant forty-three", "sorry, forty-three" on build forty-two | stays (correction) |
| 15 | "fifty is fine" on release 41 (9 away), "go with option two", "step 2 is failing" | stays |
| 16 | "and thirteen?" on "PR 12 and 13" | stays (listed after PR) |

## Files
- `plugin/speakeasy/recap.py`: `digits` (standalone numbers only, `named_only`), `keyed`, `names_other_number`;
  `subject` reads `digits(text, named_only=True)`. `ids`, `_ID_NEXT`, `_COUNTING`, `_NOT_IDS` are gone.
- `plugin/speakeasy/router.py`: `decide` passes `_decide` its arguments by keyword; `_keep_apart` calls
  `names_other_number(task, words)` and sets source `kept_apart`.
- `plugin/tests/test_call_replay_spk172.py`: every reviewer sentence as a row, in three lists (own task, stays,
  corrections), plus `digits` and repeat-status rows; xfails take `raises=AssertionError`.

## Known wrong outcomes, kept
- Refused: a count within 3 of the build number with the same digit count ("add forty testers" on release 42);
  "iOS version 18" ("version" is a build word); "fix the 12 release blockers" is not refused (before-position
  keys count on the task side only).
- Attached: "Push forty-one, not forty-two" (rule 2); a build more than 3 away ("push fifty"); a task that never
  names its build with a word ("Is forty-two going out today?"); "#41" on a release task (ticket family).

## Not in scope
A refused follow-up joining another open task about its number; carrying the old task's context into the new
one ("backport that fix to release forty-one"); status asks (`status_task_id`, `quick_intent`); the three
later-unit symptoms (xfail). Title numbers are SPK-243's.
