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

On the task side a naming word must name a numbered thing, not count things (round 2). "Build", "release" and
"issue" are also things to do: as a sentence's first word (the request's, or the one after "(following up:"),
with a plural right after the number or after one word describing it ("Issue 30 refunds", "Build 12 more
slides", "Build 20 landing pages"), the number is a count and nothing is keyed. A filler or a word ending in
"s" in between keeps the key ("Push release 42 to testers", "Release 42 has issues"). A number before the
word is not keyed when a plural follows the word ("the 12 release blockers"). Unsure means not keyed: the task
then takes its follow-ups, as on main.

## The rule (`recap.names_other_number(task_request, follow_up)`, applied in `router.decide`)
A follow-up leaves its task only when the task names its build or ticket with a naming word and the follow-up,
without saying that number, names another one: with a naming word of the same family ("release 41"), or as a
bare whole number of the same digit count within 3 of it ("push 41 now"). A correction ("no, I meant 43") stays.
1. The task names no keyed number: never refuse (dates, rooms, budgets, thermostats, options).
2. The follow-up says one of the task's keyed numbers anywhere, keyed or bare: never refuse.
3. A correction: never refuse. `router.corrects` decides, on the same reading of the correction words as
   `router.correction` (`router._correction`: no / nope / nah / not that / I mean(t) / or rather / rather /
   scratch that / actually / correction first, or sorry / I mean(t) last; at most 12 words; "no thanks / worries
   / problem / rush / need / way" is an answer; a command after anything but no / nope / nah / I mean is a new
   request). It also takes a fix too small to rewrite a request with ("no, 43"), and "sorry" or "make that"
   said first when a correction follows or only the fix does: one phrase of at most five words, no comma or
   colon in it, no command verb first, not "sorry to ...". "No problem, go ahead with forty-one" and "Sorry
   to bother you, push forty-one" are not corrections. `recap` no longer has a correction pattern of its own.
4. The follow-up has a keyed number of the task's family (said after its word): refuse. Another family is not
   compared ("issue 41" on release 42's task).
5. A bare number M refuses when: whole, two digits or more; some task number N has the same digit count and
   0 < |M - N| <= 3; no number or number word touches it, and no `$ € £ #` before it (times, decimals,
   hundreds, prices); not followed by a unit of time or amount (`minutes`, `pm`, `percent`, `dollars`...);
   not said with a month: right after one ("May 14", "Nov 13") or right before one other than "May" ("14 June").
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
| 17 | "No problem / No worries / No rush, ... forty-one", "Sorry to bother you, push forty-one", "Sorry, one more thing: push forty-one to public" on release 42 | own task |
| 18 | "actually, I meant / nah, / scratch that, / or rather / rather, forty-three", "no, 43", "sorry, 43", "make that forty-three", "Sorry, no, forty-three" on build forty-two | stays (correction) |
| 19 | "push it on May 14", "May fourteen", "Nov 13", "14 June" on release 12; "push 14 now" | stays; own task |
| 20 | "make it 22" on "Build 20 landing pages", "make it 32" on "Issue 30 refunds", "just do 10 of them" on "fix the 12 release blockers" | stays (rule 1) |
| 21 | "push 22 now" on "What's happening with build 20?", "push 67 now" on "check the build 68 logs", "what about 41" on "Push release 42 to testers" | own task |

## Files
- `plugin/speakeasy/recap.py`: `digits` (standalone numbers only, `named_only`), `keyed`, `names_other_number`;
  `subject` reads `digits(text, named_only=True)`. `ids`, `_ID_NEXT`, `_COUNTING`, `_NOT_IDS` are gone.
- `plugin/speakeasy/router.py`: `decide` passes `_decide` its arguments by keyword; `_keep_apart` calls
  `names_other_number(task, words)` unless `corrects(words)`, and sets source `kept_apart`. `correction` is
  `_correction` plus its own check that the phrase says enough (unchanged results); `corrects` is new.
- `plugin/tests/test_call_replay_spk172.py`: every reviewer sentence as a row, in three lists (own task, stays,
  corrections), plus `digits` and repeat-status rows; xfails take `raises=AssertionError`, and their setup
  steps run inside `setting_up()`, which turns a timeout or a wrong starting point into `BrokenSetup`.

## Known wrong outcomes, kept
- Refused: a count within 3 of the build number with the same digit count ("add forty testers" on release 42);
  "iOS version 18" ("version" is a build word); "fix the 12 release blockers" is not refused (before-position
  keys count on the task side only). Round 2, each with the sentence a fix would wrongly attach: "push it on
  14 May" on release 12 ("forty-one may be ready to go"); "is it in 2025" on build 2024 ("is it fixed in
  2025"); "ship it on the twenty first" on release 22 ("push the forty first"); "actually make it
  forty-three" on build forty-two ("actually, go ahead with forty-one"); a count after a verb that is not a
  sentence's first word ("Please build 20 landing pages").
- Attached: "Push forty-one, not forty-two" (rule 2); a build more than 3 away ("push fifty"); a task that never
  names its build with a word ("Is forty-two going out today?"); "#41" on a release task (ticket family).
  Round 2: a task that opens with its build and a plural-looking word after the number is read as a count and
  is not guarded ("Release 42 looks stuck, check it", "Build 68 logs", and "the 41 build logs"); a request
  after a correction word that is no command `router.correction` knows ("Actually, push forty-one to
  public", "Sorry, push forty-one"; "no, push forty-one" already did).

## Not in scope
A refused follow-up joining another open task about its number; carrying the old task's context into the new
one ("backport that fix to release forty-one"); status asks (`status_task_id`, `quick_intent`); the three
later-unit symptoms (xfail). Title numbers are SPK-243's.
