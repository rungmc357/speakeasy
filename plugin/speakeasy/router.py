"""Task routing: follow-up vs new, splitting a compound request, and the topical channel.

``decide`` makes ONE call per handoff to the user's own Hermes auxiliary model (task
``speakeasy_router``; the user picks its model in Hermes: ``hermes model`` → auxiliary tasks, or
``auxiliary.speakeasy_router`` in config.yaml). It returns strict JSON
``{"follow_up_task_id": id|null, "parts": [1-4 self-contained requests], "channel": label|null}``.
On timeout (3 s), error or invalid JSON it falls back to ``route`` below, and never blocks a task.

Fallback (``route``): every spoken request is one new task unless it is marked as a follow-up to an open task:
- the voice model marks it (a handoff that names a ``task_id`` / ``follow_up_task_id`` of an open
  task), or
- it is unmistakably a follow-up: it carries a follow-up cue ("make it", "change that", "also add")
  and either only one task is open, or it shares clear words with exactly one open task.

Anything unclear stays a new task, which is the safe behavior (it never hijacks an open task).
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import functools
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable
from . import runstate as RS

from . import host as host_mod

from .transcript import ROUTER_CONTEXT_CHARS, tail_text

logger = logging.getLogger(__name__)

MAX_OPEN_TASKS = 8
MAX_PARTS = 4
AUX_TASK = "speakeasy_router"
AUX_DISPLAY_NAME = "Speakeasy task routing"
AUX_DESCRIPTION = "Decides, per spoken request, follow-up vs new task, splits compound asks, and picks a chat channel."
ROUTE_TIMEOUT_S = 3.0
CHAT_ROUTE_TIMEOUT_S = 5.0   # a longer prompt when existing conversations are in play
TITLE_TIMEOUT_S = 15.0
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="speakeasy-router")
NEW = "new"
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# "New thing", "something else", "separately": not about whatever the user is facing.
_WANTS_NEW = re.compile(r"(?i)\b(?:new (?:thing|task|one|idea)|something (?:else|new)|separate(?:ly)?|start (?:a )?(?:fresh|new)|another thing)\b")


def wants_new(request: str) -> bool:
    return bool(_WANTS_NEW.search(request or ""))

_FOLLOW_UP_CUE = re.compile(
    r"(?i)^(?:and |also |oh,? |actually,? |wait,? |no,? |okay,? |ok,? )*(?:"
    r"make (?:it|that|them)|change (?:it|that|them)|instead|also (?:add|include|check|send|make)|"
    r"add (?:to that|that)|cancel (?:it|that)|revise (?:it|that)|update (?:it|that)|"
    r"for (?:that|it)|about (?:that|it)|on (?:that|it)|the same|as well|too\b|"
    r"what about|and (?:then|also)|same (?:thing|one)|send (?:it|that)|reword|shorter|longer)")
_STOP = set("""a an the and or to of in on for with at by from is are was be this that it its my our your me we
you i he she they them do does did can could would should will just please tell ask go let lets get have has had
about into over up out so then than now new thing things make change also add check what how""".split())


@dataclasses.dataclass(frozen=True)
class OpenTask:
    task_id: str
    request: str
    status: str
    result: str = ""
    age_s: float | None = None   # seconds since it started, when known


@dataclasses.dataclass(frozen=True)
class Part:
    kind: str              # "new" | "follow_up"
    request: str
    task_id: str | None = None
    conversation: str | None = None  # a Chat.ref this part of a split request continues in


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


STATUS = "status"    # a "how's it going?" about an open task: answered from its steps, no new work
IGNORE = "ignore"    # a scrap with nothing to act on ("that", "it's"): no task

_STATUS_ASK = re.compile(
    r"(?i)^(?:(?:hey|so|okay|ok|and|um|uh|yo)[,.]? )*(?:"
    r"what(?:'s| is) the (?:status|update|latest|progress)|status(?: update)?|any (?:update|news|progress)|"
    r"(?:can|could) (?:you|i) (?:get|give me|have) (?:an? |the )?(?:update|status)|"
    r"how(?:'s| is| are) (?:it|that|this|things|everything|we|the \w+(?: \w+)?) (?:going|looking|coming along|doing)|"
    r"how (?:we|are we) looking|are you still (?:checking|working|on it|looking|going)|still (?:working|checking|going)|"
    r"where are we (?:at|on)|how far along|is it done|are you done|did (?:it|that) finish)")
_SCRAPS = set("""that it it's its um uh so okay ok hmm the and but like oh well yeah no huh what
hm er ah mm""".split())
_AGREE = re.compile(
    r"(?i)^(?:(?:uh|um|oh|okay|ok|yeah|yes)[,.]? )*(?:yes|yeah|yep|yup|sure|go|go ahead|do it|do that|"
    r"sounds good|let's do it|let's do that|please do|yes please|go for it|perfect|okay|ok|that works|"
    r"that's fine|fine|cool|great)(?:[,.]? (?:please|thanks|do it|go ahead|go for it|let's go))?[.!?]*$")
REPEAT_WINDOW_S = 30  # a slow answer gets repeated: never run it twice


def _norm(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def quick_intent(request: str, tasks: list["OpenTask"], replied_task_id: str | None = None) -> "Part | None":
    """Deterministic answers to the requests that must never become fresh work: a status question
    about something running, a bare yes to what the assistant just offered, a meaningless scrap,
    and the same request heard twice seconds apart. None: route normally."""
    words = _norm(request)
    if not words:
        return Part(IGNORE, request)
    live = [t for t in tasks if t.status in RS.IN_FLIGHT]
    if len(words) <= 14 and _STATUS_ASK.search(request.strip()) and tasks:
        pool = live or tasks
        asked = _words(request)
        scored = sorted(((len(asked & _words(t.request)), i, t) for i, t in enumerate(pool)),
                        key=lambda item: (item[0], item[1]), reverse=True)
        return Part(STATUS, request, scored[0][2].task_id)
    if replied_task_id and len(words) <= 6 and _AGREE.search(request.strip()):
        if any(t.task_id == replied_task_id for t in tasks):
            return Part("follow_up", request, replied_task_id)
    if len(words) <= 2 and all(w in _SCRAPS for w in words):
        return Part(IGNORE, request)
    if live:
        newest = live[-1]
        old = _norm(newest.request)
        if newest.age_s is not None and newest.age_s <= REPEAT_WINDOW_S and len(old) >= 3:
            # Heard again, or heard again with the rest of the sentence: most of the new request is
            # the old one. Two different asks back to back share little and stay separate tasks.
            shared = set(old) & set(words)
            if len(shared) / max(1, len(set(words))) >= 0.6 and len(shared) / max(1, len(set(old))) >= 0.7:
                return Part("follow_up", request, newest.task_id)
    return None


FRAGMENT_WINDOW_S = 45
# How a trailing half-thought starts: a joiner, or a bare question that leans on the previous one.
_FRAGMENT_START = re.compile(
    r"(?i)^(?:and|or|plus|also|like|oh|if|whether|gonna|going to|am i|are (?:we|they|you)|is (?:it|he|she|that)|"
    r"will (?:i|we|it|they|he|she)|would (?:i|we|it)|can (?:i|we|it)|do (?:i|we|they)|does (?:it|he|she)|did (?:i|we|it|they))\b")


_JOINER = re.compile(r"(?i)^(?:(?:and|or|plus|also|oh|so|like)[,\s]+)+")
_FILLER = set("""a an the i me my we you your it its is are am was be do does did will would can could
gonna going to of for on in at with about that this what how when where who why just still right now
yet so and or but also oh like um uh okay ok yeah hey there their they them he she his her get got
should shall might must someone something anyone anything one any some more maybe""".split())


def _content(words: list[str]) -> set[str]:
    return {w for w in (x.lower().strip("'") for x in words) if len(w) >= 3 and w not in _FILLER}


def continues_newest(request: str, tasks: list[OpenTask]) -> OpenTask | None:
    """The newest task, when this request is a short tail of it: said within seconds, while it
    runs, too thin to stand alone ("...gonna win?" after "How's my league team doing"), and about
    the same thing. Decided by topic, not timing: "and also, are we pulling the new model?" right
    after a deploy question starts with a joiner but brings its own subject, so it is a new task.
    """
    if not tasks:
        return None
    newest = tasks[-1]
    if newest.status not in RS.UNDER_WAY or newest.age_s is None:
        return None
    words = re.findall(r"[A-Za-z0-9.']+", request)
    if newest.age_s > FRAGMENT_WINDOW_S or not words or len(words) > 7:
        return None
    if not _FRAGMENT_START.search(request.strip()):
        return None
    rest = re.findall(r"[A-Za-z0-9.']+", _JOINER.sub("", request.strip()))
    new_subject = _content(rest) - _content(re.findall(r"[A-Za-z0-9.']+", newest.request))
    if len(new_subject) >= 3 or any(w[:1].isdigit() or (len(w) <= 4 and w.isupper()) for w in rest if w.lower() in new_subject):
        return None  # its own subject (or a named thing): a new task, whatever word it started with
    return newest


# Asks said seconds after a task started in the same call are, by default, more of that task: the
# user is still finishing the thought. Only an ask that is clearly about something else starts its own run.
JOIN_WINDOW_S = 20.0
_LIVE = RS.UNDER_WAY
# "set it to 75", "turn that off", "send them", "book that one": an action on the thing just asked about.
_LEANS = re.compile(r"(?i)\b(?:set|turn|make|change|move|send|book|cancel|add|put|do|try|keep|open|close|switch|"
                    r"bump|raise|lower|drop|use|fix|save|share|email|text|post|check|run|ship|undo|redo|"
                    r"start|stop|pause|skip|delete|remove|rename|update)\s+(?:it|that|them|those|this one|"
                    r"that one|the same)\b(?!\s+(?:the|a|an|my|your|our|his|her|their|there|it|this|that|i|we|you|they|he|she)\b)")
_ALTERNATIVE = re.compile(r"(?i)^(?:or|or else|or maybe|or just)\b")


def just_started(tasks: list[OpenTask]) -> OpenTask | None:
    """The newest task when it is still running and started within JOIN_WINDOW_S."""
    if not tasks:
        return None
    newest = tasks[-1]
    if newest.status not in _LIVE or newest.age_s is None or newest.age_s > JOIN_WINDOW_S:
        return None
    return newest


def _named(words: list[str], old: set[str]) -> bool:
    """A proper name or acronym the running task never mentioned (\"Rome\", \"PRs\" mid-sentence)."""
    return any((w[:1].isupper() and i > 0) or (len(w) <= 4 and w.isupper() and len(w) > 1)
               for i, w in enumerate(words) if w.lower().strip("'") not in old and w.lower() not in _FILLER)


def leans_on_newest(request: str, tasks: list[OpenTask]) -> OpenTask | None:
    """The task that started seconds ago, when this ask only makes sense as more of it: \"or using the
    menu bar\" (an alternative to what was just asked), or a short ask pointing at it (\"can you set it
    to 75\") that brings no subject of its own. Anything naming its own thing is left to routing."""
    newest = just_started(tasks)
    if newest is None:
        return None
    words = re.findall(r"[A-Za-z0-9.']+", request)
    if not words or len(words) > 10:
        return None
    old = _content(re.findall(r"[A-Za-z0-9.']+", newest.request))
    rest = re.findall(r"[A-Za-z0-9.']+", _JOINER.sub("", request.strip()))
    new_subject = _content(rest) - old
    if _named(rest, old):
        return None
    if _ALTERNATIVE.search(request.strip()) and len(words) <= 8:
        return newest  # "or using the menu bar": never an ask of its own
    others_live = any(t.status in _LIVE for t in tasks[:-1])
    if _LEANS.search(request) and len(new_subject) <= 2 and not others_live:
        return newest  # with several tasks running, which one "it" is goes to the routing model
    return None


def just_started_line(tasks: list[OpenTask]) -> str:
    """The routing model's hint that the newest task began seconds ago, or \"\"."""
    newest = just_started(tasks[-MAX_OPEN_TASKS:])
    if newest is None:
        return ""
    return (f"Task {newest.task_id} started {int(newest.age_s or 0)} seconds ago in this call and is still "
            f"running: the user is most likely still adding to it. Send the request there "
            f"(follow_up_task_id) unless it is clearly about something else (a different subject, thing, "
            f"place or person than that task).\n\n")


# "fix that helper the agent was telling me about", "do what it suggested", "the one you mentioned".
_BACK_REF = re.compile(
    r"(?i)\b(?:(?:you|it|he|she|they|hermes|the agent|the assistant)\s+(?:was|were|is)?\s*"
    r"(?:telling me about|told me about|mentioned|said|suggested|found|flagged|brought up|recommended|offered)"
    r"|(?:that|the)\s+(?:one|thing|fix|idea|issue|problem)\s+(?:you|it))\b")
_STOP = set("""the a an that this those these it its to of for and or but with on in at by from about me my you your
he she they them was were is are be been do did does fix make get go try can could would will please okay ok so
just now then also tell telling told mentioned said suggested found flagged brought up recommended offered agent
assistant hermes thing one""".split())
BACK_REF_WINDOW_S = 30 * 60


def refers_back(request: str, tasks: list[OpenTask]) -> OpenTask | None:
    """The one recent task whose own words the request points back to ("fix that helper the agent was
    telling me about" after a task said the helper was broken). None unless exactly one task matches."""
    if not tasks or not _BACK_REF.search(request):
        return None
    recent = [t for t in tasks if t.age_s is None or t.age_s <= BACK_REF_WINDOW_S]
    asked = {w for w in re.findall(r"[a-z0-9']+", request.lower()) if w not in _STOP and len(w) > 2}
    hits = [t for t in recent if asked & set(re.findall(r"[a-z0-9']+", (t.request + " " + t.result).lower()))]
    if len(hits) == 1:
        return hits[0]
    if not asked and len(recent) == 1:
        return recent[0]
    return None


def route(request: str, tasks: list[OpenTask], marked_task_id: Any = None) -> list[Part]:
    """One Part: the request as a new task, or a follow-up to one open task."""
    request = (request or "").strip()
    open_tasks = tasks[-MAX_OPEN_TASKS:]
    tail = continues_newest(request, open_tasks) or leans_on_newest(request, open_tasks)
    if tail is not None and not (isinstance(marked_task_id, str) and marked_task_id != tail.task_id):
        return [Part("follow_up", request, tail.task_id)]
    if isinstance(marked_task_id, str) and TASK_ID_RE.fullmatch(marked_task_id):
        if any(t.task_id == marked_task_id for t in open_tasks):
            return [Part("follow_up", request, marked_task_id)]
    if not request or not open_tasks or not _FOLLOW_UP_CUE.search(request):
        return [Part(NEW, request)]
    if len(open_tasks) == 1:
        return [Part("follow_up", request, open_tasks[0].task_id)]
    asked = _words(request)
    scored = sorted(((len(asked & _words(t.request + " " + t.result)), i, t) for i, t in enumerate(open_tasks)),
                    key=lambda item: (item[0], item[1]), reverse=True)
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
        return [Part("follow_up", request, scored[0][2].task_id)]
    # "make it for four" with several open tasks: the newest task is the natural referent only when
    # the request names nothing else.
    if not asked:
        return [Part("follow_up", request, open_tasks[-1].task_id)]
    return [Part(NEW, request)]


# -- "show me" --------------------------------------------------------------------------------

# A short request to SEE a task's work, not new work: "show me", "what are you looking at?",
# "let me see it", "can I see the design?", "pull it up". Anything naming a new thing to find
# ("show me flights to Paris") is ordinary work and does not match.
_SHOW_ME = re.compile(
    r"(?i)^(?:(?:hey|ok|okay|so|and|oh|yeah|um|uh|can you|could you|would you|please|just|go ahead and|"
    r"now)[,\s]+)*(?:"
    r"(?:show|let) me(?: (?:see|look at|have a look at))?(?: (?:it|that|this|them|what you(?:'re| are)? "
    r"(?:looking at|seeing|see|doing|working on|made|have|got|found)|what it looks like|the (?:design|screenshot|"
    r"image|picture|page|mockup|draft|result|screen|preview)s?|the (?:[a-z]+ ){1,2}(?:design|screenshot|image|picture|page|"
    r"mockup|preview)s?|your screen))?|"
    r"what (?:are|r) you (?:looking at|seeing|working on)|what(?:'s| is) on (?:your|the) screen|"
    r"what does it look like(?: so far| now)?|"
    r"(?:can|could|may) i (?:see|look at|have a look at)(?: (?:it|that|this|them|the (?:design|screenshot|image|"
    r"picture|page|mockup|result|preview)s?|the (?:[a-z]+ ){1,2}(?:design|screenshot|image|picture|page|mockup|preview)s?|"
    r"what you(?:'re| are)? (?:looking at|seeing|doing|made)))?|"
    r"(?:pull|bring) (?:it|that|them) up|put (?:it|that) on (?:my|the) screen"
    r")(?:[,\s]+(?:please|now|then|so far|for me))*[\s.!?]*$")


_ACTION = re.compile(r"(?i)\b(?:take|book|grab|pick|choose|go with|use|do|make|send|buy|order|cancel|stop|hold|"
                     r"wait|pause|skip|change|switch|move|add|remove|delete|confirm|reserve|schedule|set|turn|"
                     r"try|keep|continue|finish|yes|yeah|yep|sure|no|nope)\b")


def wants_to_see(request: str) -> bool:
    """The request asks for something visual (alone or alongside an instruction)."""
    return bool(_VISUAL.search(request or ""))


def only_looking(request: str) -> bool:
    """True when the request is only a look ("show me", "a picture of that page"), with no
    instruction riding along. "A picture of that page. OK, the 4:15 then" is False: the 4:15 part
    is an answer the task is waiting for and must reach it."""
    clauses = [c.strip() for c in re.split(r"[.!?;]+|,\s*(?=(?:yeah|yes|and|ok|okay|then|so)\b)", request or "") if c.strip()]
    if not clauses:
        return True
    for clause in clauses:
        if is_show_me(clause):
            continue
        if _VISUAL.search(clause) and not _ACTION.search(clause) and len(clause.split()) <= 6:
            continue
        return False
    return True


def is_show_me(request: str) -> bool:
    """True for a short 'show me what you're looking at' ask about existing work."""
    text = (request or "").strip()
    return bool(text) and len(text) <= 80 and bool(_SHOW_ME.match(text))


# -- "look at this": the user's own screen ------------------------------------------------------

# Things on a screen that "this ..." points at ("what's this error", "is this layout right?").
# Conservative: words that often mean something else ("this email" while dictating one, "this
# week", "this table" at a restaurant) are left out.
_SCREEN_THINGS = (r"(?:screen|window|tab|page|site|website|web ?page|app|dialog|pop-?up|error|warning|alert|"
                  r"notification|code|line|function|file|document|doc|pdf|spreadsheet|chart|graph|diagram|layout|"
                  r"design|mock-?up|photo|picture|image|screenshot|video|form|button|menu|setting|field|link|map|"
                  r"article|slide|invoice|receipt|log|stack ?trace|terminal|crash)(?:e?s)?")
_SCREEN_REF = re.compile(
    r"(?i)(?:"
    r"\b(?:my|this) (?:screen|display)\b(?!\s*(?:time|door|saver|protector|name)\b)"
    rf"|\b(?:this|these) {_SCREEN_THINGS}\b"
    r"|\b(?:look|looking|glance) at (?:this|these)\b(?!\s*(?:weekend|week|morning|afternoon|evening|night|month|"
    r"year|time|season|quarter|one)\b|'s)"
    r"|\bcheck (?:this|these) out\b|\b(?:you|u) see (?:this|these)\b|\b(?:reply|respond) to this\b"
    r"|\bwhat (?:i'?m|i am) (?:looking at|seeing|reading|watching)\b|\bwhat i (?:have|got) open\b"
    r"|\bin front of me\b"
    r")")
# "What's this?" on its own, said while looking at something.
_WHAT_IS_THIS = re.compile(
    r"(?i)^\W*(?:(?:hey|so|ok|okay|um|uh|and|but|wait|hmm|oh)[,\s]+)*(?:what(?:'s| is) this|what are these|"
    r"what does this (?:say|mean|do)|what am i looking at)\W*$")
# The user looking ("let me look at this"): they want to see the task's work, not show theirs.
_VIEWER = re.compile(r"(?i)\b(?:let me|lemme|let's|can i|could i|may i|i want to|i'd like to|i wanna)\s+"
                     r"(?:(?:take|have) a\s+)?(?:look|see|glance)\b")
# "Show me what you're looking at": the task's own view, even while the user is sharing theirs.
_TASK_VIEW = re.compile(
    r"(?i)\b(?:what (?:are|r) you (?:looking at|seeing|working on)|what you(?:'re| are)? (?:looking at|seeing|doing|"
    r"working on|made|have|got|found)|(?:on )?your screen"
    r"|(?:put|show|bring|pull) (?:it|that|them|those|the \w+) (?:up )?on my screen)\b")


def _plain(text: str | None) -> str:
    """Whitespace collapsed; a curly apostrophe (transcripts use both) reads as a straight one."""
    return " ".join((text or "").replace("\u2019", "'").split())


def refers_to_screen(request: str) -> bool:
    """The request points at what the user is looking at: "what's this error?", "look at this",
    "is this layout right?", "what's on my screen". Conservative on purpose: "look at this weekend's
    forecast", "show me what you're looking at" (the task's own view) and "let me look at this" never
    match. Dispatch skips the quick lanes for these and steers a running task with the screen; holds
    and spoken screen intents (U4) build on the same matcher. "What’s this?" with a curly apostrophe
    reads the same as with a straight one."""
    text = _plain(request)
    if not text:
        return False
    if _WHAT_IS_THIS.match(text):
        return True
    for match in _SCREEN_REF.finditer(text):
        if match.group(0).lower().startswith(("look", "glance")) and _VIEWER.search(
                text[max(0, match.start() - 24):match.end()]):
            continue  # "let me look at this": they want to see, not show
        return True
    return False


def about_task_view(request: str) -> bool:
    """A "show me" about what the task itself is looking at ("show me what you're looking at",
    "what's on your screen"): still the task's own view while the user shares theirs."""
    return bool(_TASK_VIEW.search(_plain(request)))


# -- spoken screen intents: "stop looking at my screen", "share my screen" ----------------------------

_SCREEN_NOUN = r"(?:screen|display)s?"
# Where else a screen gets shared: "stop sharing my screen in Zoom" is about that app, not Speakeasy.
_ELSEWHERE = (r"(?:zoom|teams|microsoft teams|google meet|meet|webex|slack|discord|facetime|skype|whereby|tv|"
              r"(?:the|a|my|this|our) (?:meeting|call|presentation|huddle|webinar|class|stream|tv|projector))")
_STOP_LOOKING = re.compile(
    rf"(?i)\b(?:stop|quit|cease) (?:looking at|watching|seeing|reading|sharing|capturing|recording|taking) "
    rf"(?:my|the) {_SCREEN_NOUN}\b"
    rf"|\b(?:don't|dont|do not|no more|never) (?:look(?:ing)? at|watch(?:ing)?|shar(?:e|ing)|captur(?:e|ing)|"
    rf"tak(?:e|ing)) (?:my|the) {_SCREEN_NOUN}\b"
    r"|\b(?:turn|switch|shut) off (?:the )?(?:screen ?shar(?:e|ing)|eye)\b"
    r"|\b(?:turn|switch|shut) (?:the )?(?:screen ?shar(?:e|ing)|eye) off\b"
    r"|\b(?:stop|end|disable|pause|quit|cancel|no more) (?:the )?screen ?shar(?:e|ing)\b"
    rf"|\bunshare (?:my|the) {_SCREEN_NOUN}\b")
# Earlier in the sentence: a question about how, or an errand for someone else ("how do I stop sharing
# my screen", "remind me to turn off screen sharing"). Right before it: a negation ("don't stop looking").
_STOP_ASKED_ABOUT = re.compile(
    r"(?i)\b(?:how|why|what happens|what if|remind|reminder|remember to|forget to|(?:tell|ask) (?:\w+ ){1,3}to|"
    r"text \w+|email \w+|message \w+)\b[^.!?]*$")
_STOP_NEGATED = re.compile(r"(?i)\b(?:don't|dont|do not|never|not|can't|cannot|couldn't|won't|wouldn't|doesn't)\s*$")
_STOP_ELSEWHERE = re.compile(rf"(?i)^\W*(?:in|on|during|for|from|with|to|over) {_ELSEWHERE}\b")
# "Share my screen" and the like, said on their own (fillers allowed): an ask to turn sharing on,
# which only the button can do. Anchored, so "share my screen with Sam in Zoom" is a request as usual.
_LEAD = r"(?:(?:hey|ok|okay|so|um+|uh+|and|alright|all right|please|now|yeah|yes|oh|well|actually|hmm+)[,.!\s]+)*"
_ASK = (r"(?:(?:can|could|would|will) you (?:please )?|please |go ahead and |let me |let's |lets |"
        r"i(?: want| need| would like|'d like| wanna)(?: you)? to |i(?:'m going to|'m gonna|'ll) )?")
_SHARE_REQUEST = re.compile(
    rf"(?i)^\W*{_LEAD}{_ASK}(?:"
    rf"(?:share|start sharing|begin sharing) (?:my|the) {_SCREEN_NOUN}(?: with you)?"
    r"|(?:turn|switch) on (?:the )?(?:screen ?shar(?:e|ing)|eye)|(?:turn|switch) (?:the )?(?:screen ?shar(?:e|ing)|eye) on"
    r"|(?:start|enable|begin|use) (?:the )?screen ?shar(?:e|ing)"
    rf"|(?:look at|watch|see|follow|keep an eye on) my {_SCREEN_NOUN} (?:while|as) (?:we|i)(?:'re| are|'m| am)? "
    r"(?:talk|chat|work|go)(?:ing)?"
    rf"|(?:look at|watch|keep an eye on) my {_SCREEN_NOUN} from now on"
    r")(?: (?:please|now|for me|with you|for a (?:bit|sec|second|minute|while)))*\W*$")
# Talk about a screen itself, which a look at it can't answer: the device ("my screen keeps flickering"),
# commands for the device ("lock my screen", "turn off my display", "set my screen brightness to 50
# percent"), sharing it somewhere else ("how do I share my screen in Zoom"), how-to questions ("how do I
# record my screen").
_DEVICE_TROUBLE = (r"flicker\w*|black|blank|dark|dim|frozen|froze|freez\w*|cracked|broken|broke|blurry|fuzzy|"
                   r"glitch\w*|dead|tearing|upside down|sideways|too (?:bright|dark|dim)|turned off|shaking|stuck|"
                   r"unresponsive|not working|isn't working|won't turn on|won't wake up|"
                   r"(?:turning|going|went) (?:off|black|dark|blank)")
_SCREEN_ITSELF = re.compile(
    rf"(?i)\b(?:my|the) {_SCREEN_NOUN}(?:'s| is| was| has been| keeps| kept| just| seems| looks| got| is all)*"
    rf" (?:{_DEVICE_TROUBLE})\b"
    r"|\b(?:lock|unlock|dim|brighten|darken|wake(?: up)?|rotate|flip|mirror|clean|wipe|record|calibrate|"
    rf"(?:turn|switch|shut) (?:on|off)) (?:my|the|this) {_SCREEN_NOUN}\b"
    rf"|\b(?:turn|switch|shut) (?:my|the|this) {_SCREEN_NOUN} (?:on|off)\b"
    rf"|\b(?:my|the|this) {_SCREEN_NOUN}(?:'s)? (?:brightness|resolution|timeout|time-?out|settings?|scaling|"
    r"refresh rate|orientation|sleep|lock)\b"
    r"|\b(?:share|sharing|record|recording|mirror|mirroring|cast|casting|present|presenting|project|projecting)"
    rf" (?:my|the) {_SCREEN_NOUN}(?= (?:in|on|during|for|from|with|to|onto|over) {_ELSEWHERE}\b)"
    r"|\bhow (?:do|can|could|should|would|to|does|did)(?: i| we| you| one)?(?: \w+)? (?:share|record|mirror|cast|"
    r"capture|screenshot|clean|calibrate|rotate|brighten|dim|lock|unlock|split|extend|fix|reset|wake(?: up)?|"
    rf"turn (?:on|off)|take a screenshot of|zoom in on) (?:my|the) {_SCREEN_NOUN}\b")


# Said while turning sharing on after being asked to ("ok, one sec", "there, it's on"): not a newer
# request, so a request waiting for the screen keeps waiting.
_ACKNOWLEDGE = re.compile(
    r"(?i)^\W*(?:(?:uh|um|oh|okay|ok|yeah|yes|yep|sure|alright|all right|right|got it|cool|great|thanks|thank you|"
    r"one (?:sec|second|moment|minute)|just a (?:sec|second|moment|minute)|hold on|hang on|wait|"
    r"give me a (?:sec|second|moment)|on it|doing it|done|there|there you go|it's on|it is on|now it's on|"
    r"(?:i'm |i am )?turning it on|i turned it on|let me (?:turn it on|do that|find it)|will do)\W*)+$")


# Talk about turning sharing on, said after the voice asked them to ("I'm turning on the eye", "how do I
# turn it on?", "where's the eye?", "ok, take a look now"). Matched per clause: each one must be about
# the button (or a plain acknowledgement), so "take a look at the weather" or "turn on the lights" never is.
_SHARING_THING = (r"(?:it|that|this|(?:the )?(?:screen ?shar(?:e|ing)|sharing)|the screen|my screen|"
                  r"the (?:eye|button|icon|toggle|switch)(?: button| icon| thing)?)")
_SHARING_TALK = re.compile(
    rf"(?i)^\W*{_LEAD}(?:"
    # doing it: "I'm turning on the eye", "turning it on now", "ok I pressed the button"
    r"(?:(?:i'm|i am|i'll|i will|i|we're|let me|gonna|going to|about to|trying to|just|still|now)\s+)*"
    rf"(?:(?:turn|switch|flip|put)(?:s|ed|ing)?\s+(?:on\s+{_SHARING_THING}|{_SHARING_THING}\s+on)"
    rf"|(?:press|click|hit|tap|push|enable|find|found)(?:es|s|ed|ing|ting|ping)?\s+(?:on\s+)?{_SHARING_THING})"
    # it's on: "it's on", "the eye is on now", "sharing is on"
    r"|(?:it|that|the eye|the button|sharing|screen sharing|now it|ok it)(?:'s| is| should be)\s+"
    r"(?:on|turned on|enabled|green|lit|active|working)"
    # how and where: "how do I turn it on?", "where's the eye?", "I can't find the button"
    r"|(?:how|where|which|what)\b[^.!?]*?\b(?:the eye|eye button|the button|turn (?:it|that|this|sharing|"
    r"screen ?sharing) on|turn on (?:the eye|it|sharing|screen ?sharing)|screen ?sharing|sharing on)\b[^.!?]*"
    r"|(?:i )?(?:can't|cannot|couldn't|don't|do not) (?:find|see) (?:the eye|the button|it|that)(?: button| icon)?"
    # the go-ahead: "take a look now", "ok go", "go ahead", "can you see it now?"
    r"|(?:go|go ahead|go for it|take a look|have a look|look now|try (?:it )?(?:now|again)|check (?:it )?(?:now|again)"
    r"|(?:can|do) you see (?:it|that|this|my screen)|you can (?:look|see)(?: it)?)"
    r")(?:\s+(?:now|please|then|for me|there|again|yet))*\W*$")


def acknowledges(request: str) -> bool:
    """Only an acknowledgement ("ok, one sec", "there, it's on") or talk about turning sharing on ("I'm
    turning on the eye", "where's the eye?", "ok, take a look now"): a request waiting for the screen
    keeps waiting through it."""
    text = _plain(request)
    return bool(_ACKNOWLEDGE.match(text)) or about_sharing_on(text)


def about_sharing_on(request: str) -> bool:
    """Talk about turning sharing on, after being asked to ("I'm turning on the eye", "how do I turn it
    on?", "it's on now, take a look"): every clause is about the button or a plain acknowledgement, and
    at least one is about the button. Never a request of its own."""
    clauses = [c for c in re.split(r"[,.!?;]+|\b(?:and then|and|then|so)\b", _plain(request)) if c.strip()]
    talk = [bool(_SHARING_TALK.match(c)) for c in clauses]
    return any(talk) and all(t or bool(_ACKNOWLEDGE.match(c)) for t, c in zip(talk, clauses))


def stops_looking(request: str) -> bool:
    """"Stop looking at my screen", "don't look at my screen", "turn off screen sharing": turn
    Speakeasy's screen sharing off. Matched on the user's transcript as it arrives as well as on
    handoffs, so it works even when the voice answers without handing it off. Questions and errands
    ("how do I stop sharing my screen", "remind me to turn off screen sharing"), "don't stop looking"
    and another app's sharing ("stop sharing my screen in Zoom") never match."""
    text = _plain(request)
    for match in _STOP_LOOKING.finditer(text):
        before, after = text[:match.start()], text[match.end():]
        if _STOP_ASKED_ABOUT.search(before) or _STOP_NEGATED.search(before) or _STOP_ELSEWHERE.match(after):
            continue
        return True
    return False


# Words that join a stop phrase to the rest of what was said, or carry nothing on their own.
_STOP_JOINER = re.compile(r"(?i)^(?:[\s,.;:!-]|\b(?:and then|and|then|also|but|so|please|ok(?:ay)?|now)\b)+"
                          r"|(?:[\s,.;:!-]|\b(?:and then|and|then|also|but|so|please|now|thanks|thank you)\b)+$")


# What makes the words around a stop phrase a request of their own: joined to it by and / then / also
# ("set a timer and stop looking at my screen", "stop looking at my screen, then check the weather"), or
# a whole clause on its own side of a break ("Stop looking at my screen. What's the weather in Boston?").
# Anything else ("you can stop looking ...", "I'm done, stop looking ...", "... for a bit") is part of
# the stop itself.
_STOP_JOINS = r"(?:and then|and also|and|then|also)"
_STOP_POLITE = r"(?:please|ok(?:ay)?|now|(?:can|could|would|will) you(?: please)?)"
_STOP_FOR_NOW = (r"(?:for (?:now|a (?:bit|sec|second|minute|moment|while)|today|the (?:moment|day|rest of the call))"
                 r"|right now|now|please|thanks|thank you)")
_JOINED_BEFORE = re.compile(rf"(?i)[\s,;:.!?-]*\b{_STOP_JOINS}\b(?:[\s,]+{_STOP_POLITE}\b)*[\s,;:.!?-]*$")
_JOINED_AFTER = re.compile(rf"(?i)^(?:[\s,;:.!?-]*\b{_STOP_FOR_NOW}\b)*[\s,;:.!?-]*\b{_STOP_JOINS}\b")
_CLAUSE_BEFORE = re.compile(r"[.!?;]\s*$")
_CLAUSE_AFTER = re.compile(r"^\s*[.!?;,]")
# Leftovers that are no request: closings and acknowledgements said with the stop.
_STOP_LEFTOVER = re.compile(
    r"(?i)^(?:\W*\b(?:you can(?: go)?|you may|i'm done|i am done|we're done|we are done|i'm good|i'm all set|"
    r"that's (?:it|all|enough|fine)|that is (?:it|all|enough)|enough|all done|done|"
    r"for (?:now|a (?:bit|sec|second|minute|moment|while)|today|the (?:moment|day|rest of the call))|"
    r"right now|now|ok|okay|yeah|yes|sure|alright|all right|thanks|thank you(?: so much)?|please|for me|"
    r"any ?more|again|too|as well|already|cool|great|got it|good|perfect|so)\b)+\W*$")


def after_stop(request: str) -> str:
    """What else was asked alongside "stop looking at my screen": words joined to the stop phrase by
    and / then / also ("set a timer for ten minutes and stop looking at my screen", "... , then check
    the weather in Boston"), or a whole clause of its own beside it ("Stop looking at my screen. What's
    the weather in Boston?"), without the joiners. Empty when the rest is part of the stop itself ("you
    can", "I'm done", "that's enough", "for a bit", "okay") or under three words: a stop never starts a
    task from its leftovers, and never swallows a real request."""
    text = _plain(request)
    for match in _STOP_LOOKING.finditer(text):
        before, after = text[:match.start()], text[match.end():]
        if _STOP_ASKED_ABOUT.search(before) or _STOP_NEGATED.search(before) or _STOP_ELSEWHERE.match(after):
            continue
        kept = []
        joined = _JOINED_BEFORE.search(before)
        if joined:
            kept.append(before[:joined.start()])
        elif _CLAUSE_BEFORE.search(before):
            kept.append(before)
        joined = _JOINED_AFTER.match(after)
        if joined:
            kept.append(after[joined.end():])
        elif _CLAUSE_AFTER.match(after):
            kept.append(after)
        asks = [words for said, words in ((k, _STOP_JOINER.sub("", k).strip()) for k in kept)
                if len(words.split()) >= 3 and not _STOP_LEFTOVER.match(said) and not acknowledges(words)]
        return " and ".join(asks)
    return ""


def screen_intent(request: str) -> str | None:
    """A spoken instruction about screen sharing itself, never work for Hermes: ``stop`` (see
    ``stops_looking``) or ``share`` ("share my screen", "turn on screen sharing", "look at my screen
    while we talk", on their own: only the panel's button turns sharing on). None otherwise."""
    if stops_looking(request):
        return "stop"
    if _SHARE_REQUEST.match(_plain(request)):
        return "share"
    return None


def needs_screen(request: str) -> bool:
    """A request that needs a look at the user's screen to be answered, so it waits for sharing to come
    on rather than going without it: ``refers_to_screen``, except the spoken intents (``screen_intent``),
    the task's own view (``about_task_view``) and talk about a screen itself ("my screen keeps
    flickering", "how do I share my screen in Zoom"), which go on as words."""
    if screen_intent(request) or about_task_view(request):
        return False
    return refers_to_screen(_SCREEN_ITSELF.sub(" ", _plain(request)))


# Words that only ask for a look ("now look at my screen", "ok, can you see this now?"): nothing a look
# already being taken doesn't answer. A thing named ("this error") or a question word is more than that.
_LOOK_WORDS = frozenset("""a again ahead alright and at can check could display do does glance go have hey here it
just look looking my now ok okay out please quick real screen see so take that the there these this well window
would yeah yes you""".split())


def only_looks(request: str) -> bool:
    """The request asks for a look at the screen and nothing else ("now look at my screen", "can you see
    my screen now"); "what's this error?" or "look at my screen and tell me what's wrong" ask more."""
    words = set(re.findall(r"[a-z']+", _plain(request).lower()))
    return bool(words) and not words - _LOOK_WORDS


def show_me_target(request: str, tasks: list[OpenTask], has_image: set[str]) -> OpenTask | None:
    """Which task 'show me' is about: one the words name, else the newest task with something to
    show, else the newest running task (it will be asked for a screenshot)."""
    tasks = tasks[-MAX_OPEN_TASKS:]
    if not tasks:
        return None
    asked = _words(request) - {"show", "see", "look", "looking", "screen", "image", "picture", "screenshot",
                               "design", "page", "preview", "result", "pull", "bring", "seeing", "doing",
                               "working", "made", "got", "found", "mockup", "draft"}
    if asked:
        scored = sorted(((len(asked & _words(t.request)), i, t) for i, t in enumerate(tasks)), key=lambda x: x[:2])
        if scored[-1][0] > 0:
            return scored[-1][2]
    running = [t for t in tasks if t.status in RS.IN_FLIGHT]
    for task in reversed(running):
        if task.task_id in has_image:
            return task
    for task in reversed(tasks):
        if task.task_id in has_image:
            return task
    return running[-1] if running else None


# -- the routing model ------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Topic:
    label: str
    topic: str


@dataclasses.dataclass(frozen=True)
class Chat:
    """An existing conversation the request might continue, as the routing model sees it."""
    ref: str                       # "c1", "c2", ... (the model answers with this)
    label: str                     # where it is: 'Discord "Voice build"'
    lines: tuple[str, ...] = ()    # the user's latest lines there, newest first
    voice_request: str = ""        # the last spoken request sent there
    age_s: float | None = None     # since its last message


@dataclasses.dataclass(frozen=True)
class Decision:
    parts: list[Part]
    channel: str | None = None     # an opted-in channel label picked by topic, or None
    source: str = "fallback"       # "model" | "marked" | "fallback"
    latency_ms: int = 0
    conversation: str | None = None  # a Chat.ref to continue in, or None
    show: bool = False             # the user wants to SEE something (what it looks like), not just hear it


@dataclasses.dataclass(frozen=True)
class Focus:
    """The task (and job) on the user's screen or in their hands: a strong hint, not an order."""
    task_id: str
    job: str = ""        # the job's short name, as the user sees it
    detail: str = ""     # what part of it they are looking at, when the app says


def focus_line(focus: Focus | None, tasks: list[OpenTask]) -> str:
    """The routing model's hint about what the user is looking at, or "" when there's nothing open."""
    if focus is None or not any(t.task_id == focus.task_id for t in tasks):
        return ""
    name = f" ({focus.job})" if focus.job else ""
    seen = f", looking at: {focus.detail[:200]}" if focus.detail else ""
    return (f"The user is looking at task {focus.task_id}{name}{seen}. Send the request there "
            f"(follow_up_task_id) unless it is clearly unrelated to that task.\n\n")


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min ago"
    return f"{minutes // 60} h ago" if minutes < 48 * 60 else f"{minutes // 1440} days ago"


def chat_lines(chats: list[Chat]) -> str:
    out = []
    for c in chats:
        head = f"- {c.ref}: {c.label}" + (f", last active {_ago(c.age_s)}" if c.age_s is not None else "")
        if c.voice_request:
            head += f'\n    you last sent work there by voice: "{c.voice_request}"'
        for line in c.lines:
            head += f'\n    user said: "{line}"'
        out.append(head)
    return "\n".join(out) or "none"


def route_messages(request: str, tasks: list[OpenTask], topics: list[Topic],
                   chats: list[Chat] | None = None, call_so_far: str = "",
                   replied_task_id: str | None = None, focus: Focus | None = None) -> list[dict[str, str]]:
    open_lines = "\n".join(f"- {t.task_id}: {t.request[:200]} ({t.status}"
                           + (f", started {int(t.age_s)}s ago" if t.age_s is not None else "") + ")"
                           + (f"\n    the assistant's last spoken answer came from this task: \"{t.result[:300]}\""
                              if t.task_id == replied_task_id and t.result else
                              f"\n    what this task has told the user: \"{t.result[-300:]}\"" if t.result else "")
                           for t in tasks[-MAX_OPEN_TASKS:]) or "none"
    channel_lines = "\n".join(f"- {c.label}: {c.topic or 'no description'}" for c in topics) or "none"
    return [
        {"role": "system", "content":
            "You route one spoken request for a voice assistant. Reply with strict JSON only, no prose: "
            '{"status_task_id": string or null, "follow_up_task_id": string or null, "conversation": string or null, '
            '"conversation_part": number or null, "parts": [strings], "channel": string or null, "show": true or false}. '
            "status_task_id: the id of an open task when the request only asks how that task is going, whether "
            "it's stuck, done or still working, or what it's doing now, with no new instruction (\"how's that coming "
            "along\", \"you seem stuck\", \"what's it doing?\"); else null. "
            "follow_up_task_id: the id of an open task ONLY when the request clearly adds to, changes, corrects "
            "or asks about that task (not just how it is going); else null. A reply to what the assistant just said (agreeing, disagreeing, "
            "correcting it, pushing back, answering its question: \"no, Hermes can\", \"you're missing a couple\", "
            "\"that's wrong\") is a follow-up to the task that answer came from, never a new task or conversation. "
            "A request that points back at something a task told the user (\"fix that script you found\", \"do the "
            "thing it suggested\", \"send the one you mentioned\") is a follow-up to that task, even when the task "
            "has finished: it continues in that task's thread. People pause mid-thought: a short fragment said seconds after "
            "a task started that only makes sense as the end of that request (\"...and am I gonna win?\") is a "
            "follow-up to it, never a new task. Seconds after a task started, anything still about that task's "
            "subject (another detail, an alternative, a change, pushback on it, more of the same ask: \"or using "
            "the menu bar\", \"it still doesn't have enough context\") is a follow-up to it, never a second task "
            "running beside it. Only the subject decides against it: a request that starts with "
            "\"and\", \"also\" or \"oh\" but brings a different subject (\"and also, are we pulling the new model?\" "
            "while a deploy runs) is NEW work, not a follow-up. An answer to a question a task asked (a time, a "
            "choice, yes/no) is a follow-up to that task. \"Hold off\", \"wait\", \"stop\" go to the task they are "
            "about, usually the one just discussed. parts: when not a follow-up, the request as 1 to 4 independent, "
            "self-contained asks. Split only clearly separate asks about different things; one question with "
            "several details about the same thing (storage used, what's taking space, what to delete) is ONE "
            "part; each part must make sense alone). channel: the label of the channel whose description clearly fits, else null. "
            "conversation: when the request is not a follow-up to an open task but continues work already going "
            "on in one of the existing conversations (same project, same bug, same thing being built, or it "
            "says 'that thread', 'where we were working on', 'keep going on'), that conversation's ref; judge by "
            "what was said there, not by its name, which is often stale. When several fit, prefer the one the "
            "user last sent work to by voice, then the most recently active. Null for anything new, for general "
            "questions, and when unsure: pick one only when you are confident it is the same piece of work, "
            "not just a shared topic or word. A new task is a fine outcome; the wrong conversation is not. "
            "When the request holds several separate asks, list every one in parts even if one of them continues a "
            "conversation; then set conversation to that conversation's ref and conversation_part to the index "
            "(0-based) of the one part that continues it. The other parts become new tasks. With a single part, "
            "conversation_part is null. "
            "show: true when the user wants to SEE something rather than just hear about it: how a thing looks "
            "(\"what does the new logo look like\", \"how do those office speakers it recommended look\", "
            "\"let me see the hotel\"), a design, page, product, place or a task's visual state. False for "
            "questions about facts, status or prices, and for asks to find new things to look at "
            "(\"show me flights to Paris\" is new work with show false)."},
        {"role": "user", "content": f"Open tasks:\n{open_lines}\n\nChannels:\n{channel_lines}\n\n"
                                    f"Existing conversations:\n{chat_lines(chats or [])}\n\n"
                                    + (f"The call so far (for what 'that', 'it', 'the X one' refer to):\n"
                                       f"{tail_text(call_so_far, ROUTER_CONTEXT_CHARS)}\n\n"
                                       if call_so_far and (chats or tasks) else "")
                                    + (focus_line(focus, tasks[-MAX_OPEN_TASKS:])
                                       or just_started_line(tasks))
                                    + f"Request: {request[:1500]}"},
    ]


def parse_decision(text: Any, request: str, tasks: list[OpenTask], topics: list[Topic],
                   chats: list[Chat] | None = None) -> Decision | None:
    """Validate the model's JSON strictly; None when anything is off (the caller falls back)."""
    if not isinstance(text, str):
        return None
    match = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(match.group(0) if match else text)
    except (ValueError, AttributeError):
        return None
    if not isinstance(data, dict):
        return None
    labels = {c.label.lower().lstrip("#"): c.label for c in topics}
    raw_channel = data.get("channel")
    if raw_channel is not None and not isinstance(raw_channel, str):
        return None
    channel = labels.get((raw_channel or "").lower().lstrip("#")) if raw_channel else None
    show = data.get("show") is True
    asked = data.get("status_task_id")
    if isinstance(asked, str) and any(t.task_id == asked for t in tasks[-MAX_OPEN_TASKS:]):
        return Decision([Part(STATUS, request, asked)], None, "model")
    follow = data.get("follow_up_task_id")
    if follow is not None:
        if not isinstance(follow, str) or not any(t.task_id == follow for t in tasks[-MAX_OPEN_TASKS:]):
            return None
        return Decision([Part("follow_up", request, follow)], None, "model", show=show)
    parts = data.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        return None
    clean = [" ".join(p.split())[:600] for p in parts if isinstance(p, str) and p.strip()]
    if len(clean) != len(parts):
        return None
    conversation = data.get("conversation")
    if conversation is not None:
        if not isinstance(conversation, str):
            return None
        ref = conversation.strip()
        if ref and not any(c.ref == ref for c in chats or []):
            return None
        if ref and len(clean) == 1:
            return Decision([Part(NEW, request)], None, "model", conversation=ref, show=show)
        if ref:
            # Several asks, one of which continues a conversation ("where'd we leave off with my PRs,
            # and how's my fantasy team"): that part goes to the conversation, the rest are new tasks.
            # Sending the whole request there buried the other asks inside that one thread.
            at = _conversation_part(data.get("conversation_part"), clean, next(c for c in chats or [] if c.ref == ref))
            return Decision([Part(NEW, p, conversation=ref if i == at else None) for i, p in enumerate(clean)],
                            channel, "model", show=show)
    if len(clean) == 1:
        clean = [request]  # one ask: keep the user's own words
    return Decision([Part(NEW, p) for p in clean], channel, "model", show=show)


def _conversation_part(index: Any, parts: list[str], chat: Chat) -> int | None:
    """Which part of a split request continues the chat: the model's index when valid, else the part
    sharing the most words with that chat (None when none does: every part is then a new task)."""
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(parts):
        return index
    seen = _words(" ".join((chat.label, chat.voice_request, *chat.lines)))
    best = max(range(len(parts)), key=lambda i: len(_words(parts[i]) & seen))
    return best if _words(parts[best]) & seen else None


_COMPOUND = re.compile(r"(?i)\b(?:and|also|plus|then|as well)\b|[,;]")
# Wanting to SEE something: the model decides whether a picture should come back and open on screen.
_VISUAL = re.compile(r"(?i)\b(?:look(?:s|ed|ing)?(?: like)?|see|show|picture|photo|image|screenshot|design|"
                     r"mock-?up|render|preview|what .{0,30} looks?)\b")


def needs_model(request: str, tasks: list[OpenTask], topics: list[Topic], chats: list[Chat] | None = None) -> bool:
    """Skip the model when there is nothing to decide: no open task to follow, no channel to pick,
    no conversation to continue, and nothing that could split. Keeps the plain case instant."""
    return bool(request) and bool(tasks or topics or chats or _COMPOUND.search(request) or _VISUAL.search(request))


def aux_call(messages: list[dict[str, str]], timeout: float = ROUTE_TIMEOUT_S, max_tokens: int = 300) -> str | None:
    """One request through Hermes' auxiliary client, in-process (the plugin runs in the gateway)."""
    try:
        call_llm, extract_content_or_reasoning = host_mod.aux_client()
        if extract_content_or_reasoning is None:
            return None
    except Exception:
        return None
    with _profile_scope():
        response = call_llm(task=AUX_TASK, messages=messages, temperature=0, max_tokens=max_tokens, timeout=timeout)
    return extract_content_or_reasoning(response)


def home_plan_call(messages: list[dict[str, str]]) -> str | None:
    """The home-control planner: the same fast routing model, with room for several device calls."""
    from .home_control import PLAN_TIMEOUT_S
    future = _EXECUTOR.submit(aux_call, messages, PLAN_TIMEOUT_S, 700)
    return future.result(timeout=PLAN_TIMEOUT_S + 0.5)


_TITLE_PROMPT = (
    "You name tasks a user handed to their AI assistant by voice, so the task list reads like a clear to-do "
    "list. You get the spoken request (a transcript: filler, false starts, misheard words) and sometimes the "
    "earlier task it continues or answers and the last turns of the call. Use that context to work out what "
    "a short request (\"Fix and ship\", \"Do both\", \"Yes, that one\") actually refers to.\n\n"
    "Rules:\n"
    "- 4 to 8 words, sentence case, no quotes, no trailing punctuation.\n"
    "- Name the specific subject and what is being done about it: the product, place, person, file or "
    "topic, plus the action or question. Someone reading only the title must know what the task is.\n"
    "- Never a meta title about the conversation: no \"Continue previous discussion\", \"Follow up on "
    "remaining items\", \"Confusion about ...\", \"Clarify request\", \"Voice task\", \"Quick question\".\n"
    "- Never just repeat a short request word for word; never a raw question cut short; never end on a "
    "dangling word (to, the, of, for, in, and).\n"
    "- Never put the user's or the assistant's name in a title (they are listed after \"Names to leave out\"): "
    "the list is the user's own, so \"Push my five open PRs\", not \"Push Sam's PRs\". Drop filler. Fix obvious "
    "speech-to-text slips from context: when a word is later restated or contradicted ('a good sleeper "
    "... a nice premium speaker'), use the intended word only, never both.\n"
    "- Only the earlier task named after \"It continues or answers\" is what a short request refers to; with "
    "none given, work from the request and the call's last turns alone and stay general rather than guess.\n"
    "- Spell product names exactly: Speakeasy, Hermes, TestFlight, ChatGPT, Codex, Claude Code, GitHub.\n"
    "- Never answer the request. Name it.\n\n"
    "Good: {\"title\": \"Ship the ChatGPT login onboarding fix\"} (for \"Fix and ship\" after a task about "
    "ChatGPT login during Speakeasy onboarding)\n"
    "Good: {\"title\": \"Check tomorrow's New York weather\"}\n"
    "Good: {\"title\": \"Find a premium speaker for the office\"} (for \"a good sleeper for my office, like a "
    "nice premium speaker\": sleeper was misheard)\n"
    "Good: {\"title\": \"Check fantasy football matchup this week\"}\n"
    "Bad: {\"title\": \"Fix and ship\"} (says nothing about what)\n"
    "Bad: {\"title\": \"Check work status\"}, {\"title\": \"Decide on holding\"} (generic verbs, no subject: "
    "name the project and what was asked, e.g. \"Speakeasy build 66: why task cards freeze\")\n"
    "Bad: {\"title\": \"Confirm the earlier task\"} (a reference: say what the earlier task was about)\n"
    "Bad: {\"title\": \"Continue previous discussion\"} (meta)\n"
    "Bad: {\"title\": \"Check the weather for tomorrow in\"} (cut off)\n\n"
    "Reply with JSON only: {\"title\": \"...\"}"
)
_TITLE_TRANSCRIPT_TAIL = {"to", "the", "a", "an", "of", "for", "in", "at", "with", "from", "and", "or", "but", "my",
                          "our", "your", "is", "are", "be", "going", "gonna", "about", "by", "into", "on", "that"}


def clean_task_title(raw: str | None, request: str = "") -> tuple[str | None, str]:
    """The titler's reply as a task title, or (None, why) when it isn't a usable one."""
    from .text import TITLE_META, fix_product_names, has_name, is_fragment, names_in, vague_title, _title_content
    text = str(raw or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        try:
            text = str(json.loads(match.group(0)).get("title") or "")
        except (ValueError, AttributeError):
            text = ""
    text = " ".join(text.split()).strip(" .\"'`“”")
    text = re.sub(r"^(?:title|task)\s*:\s*", "", text, flags=re.I)
    if not text:
        return None, "empty"
    if "…" in text or text.endswith("..."):
        return None, "cut off"
    words = text.split()
    if not 3 <= len(words) <= 9 or len(text) > 70:
        return None, f"{len(words)} words; it must be 4 to 8"
    if words[-1].lower().strip(",:;") in _TITLE_TRANSCRIPT_TAIL:
        return None, "it ends mid-phrase"
    if TITLE_META.search(text):
        return None, "it describes the conversation, not the task"
    if vague := vague_title(text):
        return None, vague
    named = has_name(text, names_in(request))
    if named:
        return None, f"it uses the name {named}; leave the user's and the assistant's names out"
    said = re.sub(r"\(.*$", "", request.split("\n", 1)[0].removeprefix("Request:")).strip()
    if said and is_fragment(said) and set(_title_content(text)) <= set(_title_content(said)):
        return None, "it only repeats the short request; name what it refers to"
    text = fix_product_names(text)
    return text[0].upper() + text[1:], ""


def smart_title(request: str, timeout: float = TITLE_TIMEOUT_S) -> str | None:
    """A specific 4-8 word to-do name for a voice task (task ``title_generation``, this plugin's
    profile scope). *request* is the titler input (``text.title_input``: the request plus the task
    it continues and recent turns). One retry with the reason when the first reply is unusable;
    None when unavailable, and callers keep the stand-in label."""
    text = str(request or "").strip()
    if not text:
        return None
    messages = [{"role": "system", "content": _TITLE_PROMPT}, {"role": "user", "content": text[:2500]}]
    for attempt in range(2):
        try:
            raw = _title_llm(messages, timeout)
        except Exception as exc:
            logger.info("speakeasy: task title unavailable (%s, attempt %d)", type(exc).__name__, attempt + 1)
            continue
        if raw is None:
            return _hermes_title(text, timeout) if attempt == 0 else None
        title, why = clean_task_title(raw, text)
        if title:
            return title
        logger.info("speakeasy: task title rejected (%s, attempt %d)", why, attempt + 1)
        messages = messages + [{"role": "assistant", "content": raw[:300]},
                               {"role": "user", "content": f"Not usable: {why}. Reply with a better title, JSON only."}]
    return None


def _title_llm(messages: list[dict[str, str]], timeout: float) -> str | None:
    """One titler request (task ``title_generation``); None when the aux client can't be imported."""
    try:
        call_llm, _ = host_mod.aux_client()
    except Exception:
        return None
    with _profile_scope():
        response = call_llm(task="title_generation", messages=messages, max_tokens=200, temperature=None,
                            timeout=timeout, reasoning_config={"enabled": False})
    return (response.choices[0].message.content or "").strip()


def _hermes_title(text: str, timeout: float) -> str | None:
    """Fallback: Hermes' own session titler, when the aux client can't be called directly."""
    try:
        from agent.title_generator import generate_title  # type: ignore
        with _profile_scope():
            title = generate_title(text, timeout=timeout)
    except Exception as exc:
        logger.info("speakeasy: task title unavailable (%s)", type(exc).__name__)
        return None
    cleaned, _ = clean_task_title(title, text)
    return cleaned


_POLISH_PROMPT = (
    "You tidy a spoken request so it reads cleanly as a written one. Rewrite the user's message as "
    "one or two clear sentences in their own voice (first person, addressed to their assistant).\n"
    "Rules:\n"
    "- Remove filler (um, like, you know, I don't know) and false starts.\n"
    "- Fix obvious speech-to-text slips when the intended word is clear from context.\n"
    "- Keep every name, number, place, date and specific detail; add nothing new.\n"
    "- Proper capitalization and punctuation. Never answer or comment on the request.\n"
    'Reply with JSON only: {"request": "..."}'
)


def polish_request(request: str, timeout: float = TITLE_TIMEOUT_S) -> str | None:
    """The spoken request as a clean written sentence, for the task detail's "Your request".
    Same model as Hermes' session titles (task ``title_generation``). None when unavailable."""
    text = " ".join(str(request or "").split())
    if not text:
        return None
    try:
        call_llm, _ = host_mod.aux_client()
    except Exception:
        return None
    try:
        with _profile_scope():
            response = call_llm(task="title_generation",
                                messages=[{"role": "system", "content": _POLISH_PROMPT},
                                          {"role": "user", "content": text[:1200]}],
                                max_tokens=400, temperature=None, timeout=timeout,
                                reasoning_config={"enabled": False})
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.info("speakeasy: request polish unavailable (%s)", type(exc).__name__)
        return None
    return clean_polished(raw, text)


_STATUS_PROMPT = (
    "You write the live status line for a task an assistant just started. Given the user's request, reply "
    "with JSON only: {\"status\": \"...\"}. The status is 2 to 6 words, starts with a present-participle verb "
    "(an -ing word), names the concrete thing being done, and uses plain letters, digits and spaces only. "
    "Good: {\"status\": \"Drafting your Portugal trip email\"}, {\"status\": \"Checking tomorrow's New York weather\"}. "
    "Fix obvious speech-to-text slips (a misheard word) from context. Never answer the request."
)


def working_status(request: str, timeout: float = TITLE_TIMEOUT_S) -> str | None:
    """A present-tense status for a task that has just been handed off ("Drafting your Portugal trip
    email"), so the panel names the work instead of a bare wait. None when unavailable or invalid."""
    text = " ".join(str(request or "").split())
    if not text:
        return None
    try:
        call_llm, _ = host_mod.aux_client()
    except Exception:
        return None
    try:
        with _profile_scope():
            response = call_llm(task="title_generation",
                                messages=[{"role": "system", "content": _STATUS_PROMPT},
                                          {"role": "user", "content": text[:1200]}],
                                max_tokens=60, temperature=None, timeout=timeout,
                                reasoning_config={"enabled": False})
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.info("speakeasy: working status unavailable (%s)", type(exc).__name__)
        return None
    return clean_status(raw)


_PLAN_PROMPT = (
    "You lay out the plan for a task a user just handed to their AI assistant, as the short steps a person "
    "would see on a progress line. Give 3 to 5 steps in order, each 2 to 5 words, starting with a verb. "
    "Every step names the actual thing from this request (the app, screen, feature, file, place, person or "
    "product it is about), so the plan could only belong to this task: for \"the checkout test keeps "
    "failing on the CI\" write \"Find why checkout fails\", \"Fix the checkout test\", \"Rerun CI on the "
    "branch\", not \"Find the cause\", \"Fix and test\". Never a stock step that fits any task "
    "(\"Find the cause\", \"Trace the issue\", \"Fix and test\", \"Gather info\", \"Do the work\"). The last step "
    "is the hand-back to the user, naming what they get (\"Try the new button\", \"Pick a hotel\", \"Read the "
    "summary\"). Plain words, no numbering, no punctuation at the end, nothing the request doesn't need. "
    "Reply with JSON only: {\"steps\": [\"...\"]}"
)


def clean_plan(raw: str | None) -> list[str] | None:
    """Model output as 3-5 short steps, or None when it isn't usable."""
    text = str(raw or "").strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        steps = json.loads(match.group(0)).get("steps")
    except (ValueError, AttributeError):
        return None
    if not isinstance(steps, list):
        return None
    out = []
    for step in steps:
        words = " ".join(str(step).split()).strip(" .;:-")
        if words and len(words) <= 40 and len(words.split()) <= 6:
            out.append(words[0].upper() + words[1:])
    return out[:5] if len(out) >= 3 else None


def plan_steps(request: str, timeout: float = TITLE_TIMEOUT_S) -> list[str] | None:
    """The steps a handed-off task will go through, for the progress line. Same model as Hermes'
    session titles (task ``title_generation``). None when unavailable or unusable."""
    text = " ".join(str(request or "").split())
    if not text:
        return None
    try:
        call_llm, _ = host_mod.aux_client()
    except Exception:
        return None
    try:
        with _profile_scope():
            response = call_llm(task="title_generation",
                                messages=[{"role": "system", "content": _PLAN_PROMPT},
                                          {"role": "user", "content": text[:1200]}],
                                max_tokens=120, temperature=None, timeout=timeout,
                                reasoning_config={"enabled": False})
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.info("speakeasy: task plan unavailable (%s)", type(exc).__name__)
        return None
    return clean_plan(raw)


SHAPE_TYPES = ("build", "research", "ideas", "setup", "answer")
_SHAPE_PROMPT = (
    "You read the final report an AI agent wrote for its user and pull out its structure, so an app can lay it "
    "out as panels instead of a wall of text. Never add facts that aren't in the report; copy numbers exactly. "
    "type: build (code/apps/features written, fixed, shipped or deployed), setup (machines, installs, accounts, "
    "configuration), research (findings, comparisons, evidence, reviews), ideas (options, designs, "
    "brainstorms, plans), answer (anything else). "
    "headline: the outcome in under 12 words. "
    "checks: only tests, verifications or measurements the agent actually ran (tests passed, live check, "
    "benchmark), never specs, prices or facts; each {\"label\": 2-4 words, \"value\": \"15/15\" or \"passed\" or a number, "
    "\"good\": true/false}; at most 4. "
    "shipped: what is now live, released, merged, installed or changed, each under 10 words; at most 5. "
    "open: what is still unfinished, blocked, or waiting on the user, each under 12 words; at most 4. "
    "sections: the report's main parts in order, each {\"title\": 1-4 words, \"points\": [up to 4 points, each "
    "under 16 words]}; at most 5. For research and ideas, each option or finding is a section. "
    "undo: true only if the report says the change can be rolled back. "
    "visual: the one picture that best shows the report's key data at a glance on a small screen, drawn only from "
    "numbers and names in the report, or null when nothing is worth drawing (plain prose, advice, a single fact). "
    "Pick the kind that fits what the report contains, never a format tied to a topic: "
    "two sides with scores or points (any matchup, game, race, projection) -> {\"kind\": \"game\", \"league\": short "
    "context, \"status\": \"live\"|\"final\"|\"projected\", \"detail\": one short line, \"teams\": [{\"name\", "
    "\"score\": as text, \"winner\": true/false, \"record\": optional}]}; "
    "two to four things compared on the same attributes -> {\"kind\": \"comparison\", \"title\", \"columns\": [names], "
    "\"rows\": [{\"label\", \"values\": [...]}], \"winner\": name}; "
    "a few headline numbers -> {\"kind\": \"stats\", \"title\", \"items\": [{\"label\", \"value\", \"delta\", "
    "\"good\": true/false}]} (at most 6); "
    "a trend over time or a ranking with numbers -> {\"kind\": \"chart\", \"title\", \"style\": \"bar\"|\"line\", "
    "\"unit\", \"labels\": [...], \"series\": [{\"name\", \"values\": [numbers]}]}; "
    "a short set of picks or to-dos -> {\"kind\": \"list\", \"title\", \"items\": [{\"text\", \"detail\"}]} (at most 8). "
    "Leave a field empty when the report has nothing for it. Reply with JSON only: {\"type\": ..., \"headline\": ..., "
    "\"checks\": [], \"shipped\": [], \"open\": [], \"sections\": [], \"undo\": false, \"visual\": null}"
)
VISUAL_KINDS = ("game", "comparison", "stats", "chart", "list")


def _short(value: Any, words: int, chars: int = 140) -> str | None:
    text = " ".join(str(value or "").split()).strip(" -•*")
    if not text or len(text.split()) > words + 4:
        return None
    return text[:chars]


def clean_shape(raw: str | None) -> dict[str, Any] | None:
    """Model output as a validated result shape, or None when it isn't usable."""
    match = re.search(r"\{.*\}", str(raw or ""), re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("type") not in SHAPE_TYPES:
        return None

    def strings(key: str, words: int, cap: int) -> list[str]:
        items = data.get(key) if isinstance(data.get(key), list) else []
        return [s for s in (_short(x, words) for x in items) if s][:cap]

    checks = []
    for c in data.get("checks") if isinstance(data.get("checks"), list) else []:
        if isinstance(c, dict) and (label := _short(c.get("label"), 5, 40)) and (value := _short(c.get("value"), 3, 24)):
            checks.append({"label": label, "value": value, "good": bool(c.get("good", True))})
    sections = []
    for s in data.get("sections") if isinstance(data.get("sections"), list) else []:
        if not isinstance(s, dict) or not (title := _short(s.get("title"), 5, 48)):
            continue
        points = [p for p in (_short(x, 18, 160) for x in (s.get("points") or [])[:6]) if p][:4]
        sections.append({"title": title, "points": points})
    shape = {"type": data["type"], "headline": _short(data.get("headline"), 14, 120) or "",
             "checks": checks[:4], "shipped": strings("shipped", 12, 5), "open": strings("open", 14, 4),
             "sections": sections[:5], "undo": bool(data.get("undo"))}
    visual = clean_visual(data.get("visual"))
    if visual:
        shape["_visual"] = visual  # moved into the result's views by the caller, never stored on the shape
    return shape if any(shape[k] for k in ("checks", "shipped", "open", "sections")) or visual else None


def clean_visual(raw: Any) -> dict[str, Any] | None:
    """The shaper's one picture of the report, validated like any view card; only data kinds."""
    from . import views
    if not isinstance(raw, dict) or raw.get("kind") not in VISUAL_KINDS:
        return None
    if raw.get("kind") == "game":
        status = str(raw.get("status") or "").lower()
        raw = {**raw, "status": status if status in ("live", "final") else "projected"}
        if len([t for t in raw.get("teams") or [] if isinstance(t, dict) and t.get("name")]) != 2:
            return None
    view = views.clean(raw)
    return view if view and len(view) > 1 else None


def result_shape(request: str, report: str, timeout: float = 12.0) -> dict[str, Any] | None:
    """The structure of a long finished report (type, checks, shipped, open, sections) for spatial and
    card layouts. Same model as Hermes' session titles. None when unavailable or unusable."""
    text = str(report or "").strip()
    if len(text) < 400:
        return None
    try:
        call_llm, _ = host_mod.aux_client()
    except Exception:
        return None
    try:
        with _profile_scope():
            response = call_llm(task="title_generation",
                                messages=[{"role": "system", "content": _SHAPE_PROMPT},
                                          {"role": "user", "content": f"Request: {str(request or '')[:400]}\n\nReport:\n{text[:9000]}"}],
                                max_tokens=2400, temperature=None, timeout=timeout,
                                reasoning_config={"enabled": False})
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.info("speakeasy: result shape unavailable (%s)", type(exc).__name__)
        return None
    return clean_shape(raw)


_HEADLINE_PROMPT = (
    "You write the one-line headline a task list shows for a finished task an AI agent did for its user: "
    "what came of it, as a short fact naming the subject, under 12 words, read cold a day later "
    "(\"Build 66 fix dropped; nothing shipped\", \"Kitchen lights set to 30% at sunset\", "
    "\"Oolong picks: three teas under $20\"). Use only what the reply says. Never an acknowledgment or opener "
    "(\"Okay\", \"Got it\", \"Sure thing\", \"I'm dropping it\"), never what the agent is about to do, never a "
    "question, no names of people. Reply with JSON only: {\"headline\": \"...\"}"
)


def result_headline(request: str, report: str, timeout: float = 10.0) -> str | None:
    """A finished task's outcome as a short fact (the board's and task card's headline) from the
    request and the reply. Same model as the titles; None when unavailable or unusable."""
    from .text import clean_headline
    text = str(report or "").strip()
    if not text:
        return None
    try:
        call_llm, _ = host_mod.aux_client()
    except Exception:
        return None
    try:
        with _profile_scope():
            response = call_llm(task="title_generation",
                                messages=[{"role": "system", "content": _HEADLINE_PROMPT},
                                          {"role": "user", "content": f"Request: {str(request or '')[:400]}\n\nReply:\n{text[:3000]}"}],
                                max_tokens=120, temperature=None, timeout=timeout,
                                reasoning_config={"enabled": False})
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.info("speakeasy: result headline unavailable (%s)", type(exc).__name__)
        return None
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        data = json.loads(match.group(0)) if match else {}
    except ValueError:
        return None
    return clean_headline(data.get("headline")) if isinstance(data, dict) else None


def clean_status(raw: str) -> str | None:
    from .text import valid_short_status
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    try:
        value = json.loads(raw).get("status") if raw.startswith("{") else raw
    except (ValueError, AttributeError):
        return None
    if not isinstance(value, str):
        return None
    value = re.sub(r"[^A-Za-z0-9 &'’/.-]", "", value).strip().rstrip(".")
    return valid_short_status(value)


def clean_polished(raw: str, original: str) -> str | None:
    """Accept only a plausible rewrite: parsed, non-empty, not much longer than what was said."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        value = json.loads(raw).get("request") if raw.startswith("{") else raw
    except (ValueError, AttributeError):
        return None
    value = " ".join(str(value or "").split()).strip('"\u201c\u201d ')
    if not value or len(value) > len(original) * 1.5 + 40:
        return None
    return value


_HOME: str | None = None


def bind_home(hermes_home: Any) -> None:
    """The Hermes home this plugin serves (set once by the adapter at start)."""
    global _HOME
    _HOME = str(hermes_home)
    host_mod.bind(host_mod.HermesHost(Path(hermes_home)))


def _profile_scope():
    """Credentials for the aux call. Under a multi-profile gateway, secrets resolve only inside a
    profile scope; the routing thread has none, so bind this plugin's home. Plain installs: no-op."""
    import contextlib
    if not _HOME:
        return contextlib.nullcontext()
    try:
        from agent.secret_scope import current_secret_scope, is_multiplex_active  # type: ignore
        if not is_multiplex_active() or current_secret_scope() is not None:
            return contextlib.nullcontext()
        from gateway.run import _profile_runtime_scope  # type: ignore
    except Exception:
        return contextlib.nullcontext()
    return _profile_runtime_scope(Path(_HOME))


def fallback_decision(request: str) -> Decision:
    """The request as new work, with nothing else decided: used when routing itself breaks."""
    return Decision(parts=[Part("new", (request or "").strip(), None)])


def decide(request: str, tasks: list[OpenTask], marked_task_id: Any = None, topics: list[Topic] | None = None,
           call: Callable[[list[dict[str, str]]], str | None] | None = None,
           timeout: float = ROUTE_TIMEOUT_S, chats: list[Chat] | None = None,
           call_so_far: str = "", replied_task_id: str | None = None,
           jev_place: Callable[..., dict[str, Any] | None] | None = None,
           focus: Focus | None = None) -> Decision:
    """Route one handoff. A task id the voice model marked wins outright (no model call). A focused
    task is a strong prior: the model is told the user is looking at it and sends the request there
    unless it is clearly unrelated; without a model answer the request goes to the focused task."""
    request = (request or "").strip()
    topics = topics or []
    open_tasks = tasks[-MAX_OPEN_TASKS:]
    quick = quick_intent(request, open_tasks, replied_task_id)
    if quick is not None and quick.kind in {STATUS, IGNORE}:
        return Decision([quick], None, "quick")
    if isinstance(marked_task_id, str) and any(t.task_id == marked_task_id for t in open_tasks):
        return Decision([Part("follow_up", request, marked_task_id)], None, "marked")
    if focus is not None and any(t.task_id == focus.task_id for t in open_tasks):
        budget = max(timeout, CHAT_ROUTE_TIMEOUT_S) if chats else timeout
        return _decide_with_focus(request, open_tasks, topics, call, budget, chats, call_so_far, replied_task_id,
                                  focus)
    if quick is not None:
        return Decision([quick], None, "quick")
    tail = continues_newest(request, open_tasks)
    if tail is not None and marked_task_id in (None, "", tail.task_id):
        return Decision([Part("follow_up", request, tail.task_id)], None, "fragment")
    joined = leans_on_newest(request, open_tasks)
    if joined is not None and marked_task_id in (None, "", joined.task_id):
        return Decision([Part("follow_up", request, joined.task_id)], None, "just_started")
    back = refers_back(request, open_tasks)
    if back is not None:
        return Decision([Part("follow_up", request, back.task_id)], None, "refers_back")
    started = time.monotonic()
    decision = None
    if jev_place is not None and not open_tasks:
        # Jev (optional) places a new task in well under a second; anything it isn't sure of goes to the
        # routing model below, as before. Open tasks always go to the model (follow-up judgement).
        placed = jev_place(request, [(t.label, t.topic) for t in topics],
                           [(c.ref, c.label + ": " + " / ".join(c.lines[:3])) for c in chats or []])
        if placed is not None:
            latency = int((time.monotonic() - started) * 1000)
            logger.info("speakeasy: jev placed the request in %d ms", latency)
            return Decision([Part(NEW, request)], placed["channel"], "jev", latency,
                            conversation=placed["conversation"], show=placed["show"])
    if chats:
        timeout = max(timeout, CHAT_ROUTE_TIMEOUT_S)
    if needs_model(request, open_tasks, topics, chats):
        # The model call gets the same budget routing waits for (it used to stop at the 3 s default
        # while routing waited 5 s, so every slow reply burned a retry and a fallback that could not land).
        model_call = call or functools.partial(aux_call, timeout=timeout)
        future = _EXECUTOR.submit(model_call, route_messages(request, open_tasks, topics, chats, call_so_far,
                                                             replied_task_id))
        try:
            decision = parse_decision(future.result(timeout=timeout), request, open_tasks, topics, chats)
        except Exception as exc:  # timeout or provider error: the rules below
            logger.info("speakeasy: routing model unavailable, using the fallback (%s)", type(exc).__name__)
    latency = int((time.monotonic() - started) * 1000)
    logger.debug("speakeasy: routing took %d ms (%s)", latency, "model" if decision else "fallback")
    if decision is None:
        return Decision(route(request, tasks, marked_task_id), None, "fallback", latency)
    return dataclasses.replace(decision, latency_ms=latency)


def _decide_with_focus(request: str, open_tasks: list[OpenTask], topics: list[Topic],
                       call: Callable[[list[dict[str, str]]], str | None] | None, timeout: float,
                       chats: list[Chat] | None, call_so_far: str, replied_task_id: str | None,
                       focus: Focus) -> Decision:
    """Ask the routing model with the focused task highlighted; it may still send an unrelated
    request elsewhere. No answer (no model, a timeout, bad JSON) keeps the old rule: the focus."""
    started = time.monotonic()
    decision = None
    model_call = call or functools.partial(aux_call, timeout=timeout)
    future = _EXECUTOR.submit(model_call, route_messages(request, open_tasks, topics, chats, call_so_far,
                                                         replied_task_id, focus))
    try:
        decision = parse_decision(future.result(timeout=timeout), request, open_tasks, topics, chats)
    except Exception as exc:
        logger.info("speakeasy: routing model unavailable, request goes to the focused task (%s)", type(exc).__name__)
    latency = int((time.monotonic() - started) * 1000)
    if decision is None:
        return Decision([Part("follow_up", request, focus.task_id)], None, "focus", latency)
    return dataclasses.replace(decision, latency_ms=latency)


def routing_model(config: dict[str, Any] | None = None) -> str:
    """Which model routing uses, for display: auxiliary.speakeasy_router in the user's config."""
    if config is None:
        try:
            from hermes_cli.config import load_config_readonly  # type: ignore
            config = load_config_readonly()
        except Exception:
            config = {}
    aux = (config or {}).get("auxiliary") if isinstance(config, dict) else None
    block = aux.get(AUX_TASK) if isinstance(aux, dict) else None
    block = block if isinstance(block, dict) else {}
    provider = str(block.get("provider") or "auto").strip() or "auto"
    model = str(block.get("model") or "").strip()
    if provider == "auto" and not model:
        return "Hermes default (your main model)"
    from .routing_choice import thinking_on
    text = f"{provider} · {model}" if model else provider
    return text if thinking_on(block) else f"{text} (thinking off)"


# "You working on that?", "where are you at", "how's it going", "any update", "is it done yet".
_STATUS_Q = re.compile(
    r"(?i)^\W*(?:(?:hey|yo|so|okay|ok|and|um+|uh+|hmm+|yeah|bro|dude|todd|hermes)\W+)*(?:"
    r"(?:are\s+)?you\s+(?:still\s+)?(?:working\s+on|on)\s+(?:that|it|this)"
    r"|(?:where|how\s+far)\s+(?:are\s+)?(?:you|we)\s+at"
    r"|where\s+(?:are\s+)?(?:you|we)\s+(?:with|on)\s+(?:that|it|this)"
    r"|how(?:'s|\s+is|\s+are)\s+(?:it|that|things|we|you)\s+(?:going|coming(?:\s+along)?|doing)"
    r"|(?:any|what's\s+the|whats\s+the)\s+(?:update|status|progress|news)"
    r"|(?:is\s+)?(?:it|that)\s+(?:done|ready|finished)(?:\s+yet)?"
    r"|(?:still|you)\s+(?:there|working|checking)"
    r"|what(?:'s|\s+is)\s+taking\s+so\s+long"
    r"|how\s+much\s+longer"
    r")\b(?:\W+(?:on|with|of)\s+(?:that|it|this|the\s+task))?"
    r"(?:\W+(?:yet|now|then|still|bro|man|dude|todd|hermes|buddy|there))*\W*$")


def is_status_question(request: str) -> bool:
    """A short "how's it going?" about running work. Answered from what the task has reported,
    never handed to the task itself (that queued it behind the very work it asks about)."""
    text = " ".join((request or "").split())
    return 0 < len(text.split()) <= 12 and bool(_STATUS_Q.search(text))


# "What are you testing?", "what's it checking?", "can't you look at the task and see": what the
# running work is doing, answered from the job itself (its request, tool log, findings), never sent
# to the task as more context.
_TASK_ASK = re.compile(
    r"(?i)\bwhat(?:'s|\s+is|\s+are|\s+was|\s+were)?\s+(?:it|you|he|she|they|that|this|todd|hermes|the\s+(?:task|job|agent|run))"
    r"\s+(?:(?:is|are|was|were)\s+)?(?:still\s+)?(?:testing|checking|doing|running|working\s+on|looking\s+(?:at|into|for)"
    r"|building|fixing|changing|waiting\s+(?:on|for)|stuck\s+on|on\s+(?:now|right\s+now))\b"
    r"(?!\s+(?:tonight|today|tomorrow|later|this\s+(?:weekend|week|evening|morning|afternoon)|for\s+(?:dinner|lunch|fun|work)))"
    r"|\bwhat\s+(?:tests?|checks?|commands?)\s+(?:is\s+it|are\s+you|are\s+they)\s+running\b"
    r"|\b(?:can'?t|could(?:n'?t)?|can)\s+you\s+(?:just\s+)?(?:look\s+at|check)\s+the\s+(?:task|job)\b")


def asks_about_task(request: str) -> bool:
    """A question about what the running work is doing (see _TASK_ASK)."""
    text = " ".join((request or "").split())
    return 0 < len(text.split()) <= 30 and bool(_TASK_ASK.search(text))


# Talk about the conversation itself, never work. HOLD: they're still mid-thought ("wait, I wasn't
# done", "let me finish"): stop and listen. UNDO: what just went out of their unfinished thought must
# not go ahead ("no, don't add it"). CONFUSED: they don't follow what the voice said ("for what?",
# "I don't know what test run you're talking about"): explain plainly, from the job if it's about one.
META_HOLD, META_UNDO, META_CONFUSED = "hold", "undo", "confused"
_META_HOLD = re.compile(
    r"(?i)\b(?:i\s+(?:was(?:n'?t|\s+not)|am\s+not|'?m\s+not)\s+(?:done|finished|through)(?:\s+(?:yet|speaking|talking|explaining))?"
    r"|(?:wasn'?t|not)\s+(?:done|finished)\s+(?:talking|speaking)"
    r"|let\s+me\s+(?:finish|talk|speak|explain|get\s+(?:this|it)\s+out)|(?:wanna|want\s+to|need\s+to|gonna)\s+finish\s+my\s+(?:thought|sentence|point)"
    r"|finish\s+my\s+(?:thought|sentence|point)|(?:you|ya)\s+(?:just\s+)?(?:cut|keep\s+cutting)\s+me\s+off"
    r"|stop\s+(?:interrupting(?:\s+me)?|cutting\s+me\s+off)|i'?m\s+(?:still\s+)?(?:talking|speaking|thinking)(?:\s+here)?"
    r"|(?:hold|hang)\s+on|wait(?:\s+a\s+(?:sec(?:ond)?|minute|moment))?|one\s+sec(?:ond)?|give\s+me\s+a\s+(?:sec(?:ond)?|minute))\b")
_META_UNDO = re.compile(
    r"(?i)\b(?:(?:don'?t|do\s+not)\s+(?:add|send|start|dispatch|kick\s+off|hand\s+off|run)\s+(?:it|that|this|anything)(?:\s+(?:yet|off|in))?"
    r"|(?:i\s+)?didn'?t\s+(?:mean\s+(?:to|for\s+you\s+to)\s+send|want\s+(?:you\s+)?to\s+(?:send|add|start))"
    r"|(?:before|until)\s+(?:i(?:'m|\s+am)?\s+(?:finish(?:ed)?|done)|you\s+send(?:\s+(?:it|that|incomplete\s+\w+))?)"
    r"|incomplete\s+(?:context|thought|request)|(?:un-?send|pull\s+back|take\s+back)\s+(?:it|that|this))\b")
_META_CONFUSED_ANY = re.compile(
    r"(?i)\b(?:what\s+do\s+you\s+mean|what\s+(?:\w+\s+){0,3}(?:are\s+you|you'?re)\s+(?:talking|referring)\s+(?:about|to)"
    r"|(?:don'?t|do\s+not)\s+(?:know|understand|get|follow)\s+what\s+(?:\w+\s+){0,3}(?:you'?re|you\s+are)\s+(?:talking|referring)\s+(?:about|to)"
    r"|what\s+are\s+you\s+on\s+about)\b")
_META_CONFUSED_SHORT = re.compile(
    r"(?i)^\W*(?:(?:but|so|wait|um+|uh+|and|okay|ok|no|sorry)\W+)*(?:(?:for|about|to)\s+what|which\s+(?:one|task|job|test|thing)"
    r"|what\s+(?:test|tests|task|job|run|thing)(?:\s+run)?|meaning\s+what|like\s+what)\W*$")
_META_WORDS = re.compile(r"[a-z0-9']+")


def meta_kind(request: str) -> str | None:
    """Talk about the conversation itself (see META_*), or None. Only when nothing else in it asks
    for work: "wait, also check the weather" is still a request."""
    text = " ".join((request or "").split())
    words = text.split()
    if not words or len(words) > 30:
        return None
    if _META_CONFUSED_SHORT.search(text) and len(words) <= 5:
        return META_CONFUSED
    undo, hold = _META_UNDO.search(text), _META_HOLD.search(text)
    confused = _META_CONFUSED_ANY.search(text)
    if not (undo or hold or confused):
        return None
    rest = _META_CONFUSED_ANY.sub(" ", _META_HOLD.sub(" ", _META_UNDO.sub(" ", text)))
    if _WORK.search(_HYPOTHETICAL.sub(" ", rest)) or _POLITE_ASK.search(rest) or asks_about_task(rest):
        return None
    if hold and not undo and not confused:
        # A bare "wait"/"hold on" leading into a real request ("wait, what time is the game") is a
        # request; "wait" alone or "I wasn't done" is about the conversation.
        tail = [w for w in _META_WORDS.findall(_META_HOLD.sub(" ", text).lower()) if w not in _STOP]
        strong = re.search(r"(?i)\b(?:done|finished|finish|through|cut|interrupt|talking|speaking|thinking)\b", hold.group(0))
        if not strong and len(tail) > 2:
            return None
    return META_UNDO if undo else META_CONFUSED if confused else META_HOLD


# A thought that trails off ("…because he", "and then", "so like"): not the end of the request.
_UNFINISHED_TAIL = re.compile(
    r"(?i)\b(?:and|or|but|so|because|cause|cuz|if|when|then|either|the|a|an|my|his|her|their|our|"
    r"he|she|they|we|i|um+|uh+|er+)\W*$")


def trails_off(request: str) -> bool:
    """The words stop mid-sentence: a dangling joiner, pronoun or article at the end, with enough
    before it to be a real thought (a short "and" on its own is a scrap, handled elsewhere)."""
    text = " ".join((request or "").split())
    # Only their own words: "(following up: ...)" / "(Geo is looking at: ...)" are added by Speakeasy.
    text = re.sub(r"\s*\((?:following up|\w+ is looking at):.*$", "", text, flags=re.I | re.S).strip()
    return len(text.split()) >= 6 and not re.search(r"[.!?]\W*$", text) and bool(_UNFINISHED_TAIL.search(text))


# Talking an idea through: answered by the voice itself, never a task.
_TALK = re.compile(
    r"(?i)\b(?:what\s+do\s+you\s+think|what\s+are\s+your\s+thoughts|(?:any\s+)?thoughts\s+on|your\s+take"
    r"|talk\s+(?:me|it|this|that)\s+through|think\s+(?:it|this|that)\s+through\s+with\s+me|let'?s\s+(?:just\s+)?(?:talk|think|brainstorm|ideate|riff|jam|kick\s+(?:it|this|the\s+ball))"
    r"|brainstorm|ideat(?:e|ing)|bounce\s+(?:an?\s+|some\s+)?ideas?|what\s+(?:about|if)\s+(?:the\s+idea|we|i|people|it)|how\s+about\s+(?:the\s+idea|we|i)"
    r"|(?:(?:could|should|can)\s+(?:i|we)|(?:i|we)\s+(?:could|should|might))\s+(?:make|build|do|call|start|try|charge|go|sell|launch)|would\s+(?:it|that)\s+(?:be|make|work)"
    r"|is\s+(?:it|that|this)\s+(?:a\s+)?(?:good|bad|dumb|smart|stupid)\s+idea|i'?m\s+(?:thinking|wondering)|i\s+was\s+thinking|i\s+feel\s+like)\b")
# Asking for the work itself: go ahead and hand it off.
_WORK = re.compile(
    r"(?i)\b(?:look\s+(?:it\s+|that\s+|this\s+)?(?:up|into)|research|search|google|find\s+(?:me|out|some|a|the)|check|dig\s+into|pull\s+up|show\s+me"
    r"|book|send|draft|email|text|message|schedule|remind|order|buy|play|turn\s+(?:on|off)|set\s+(?:up|a)|build|write|create|make\s+(?:me|a|the)|fix|deploy|ship"
    r"|go\s+ahead|kick\s+(?:it|that|this)\s+off|get\s+(?:started|going|on\s+it)|go\s+(?:do|start|work)|start\s+(?:on|working)|map\s+(?:out\s+)?(?:the\s+)?competitors"
    r"|what'?s\s+the\s+(?:latest|price|weather|score)|how\s+much\s+(?:is|does|are)|who\s+(?:owns|won|is\s+playing))\b")
# Saying you're just talking: hold work until asked.
_TALK_MODE_ON = re.compile(
    r"(?i)\b(?:don'?t\s+(?:build|start|do|make|research|look\s+up)\s+anything|just\s+(?:talk|chat|think\s+out\s+loud)(?:\s+(?:to|with)\s+me)?"
    r"|(?:we'?re|i'?m)\s+(?:still\s+|just\s+)?(?:in\s+the\s+)?(?:ideat|brainstorm|thinking|talking|exploring)|keep\s+(?:ideating|brainstorming|talking)"
    r"|no\s+(?:tasks?|work)\s+(?:yet|for\s+now)|stop\s+(?:sending|starting|delegating))")
# A reaction, not a request: "what, bro", "wait", "yeah okay", "hang on".
_REACTION = re.compile(
    r"(?i)^\W*(?:(?:um+|uh+|er+|oh|well)\W+)*(?:(?:what|huh|wait|hang\s+on|hold\s+on|yeah|yes|yep|no|nope|nah|okay|ok|right|sure|cool|nice|wow|damn|bro|dude|man|lol|haha|the\s+fuck|what\s+the\s+fuck|todd|hermes|really|seriously|for\s+real|got\s+it|i\s+see|interesting)\W*){1,4}$")


# "could I make…", "if you were to build…", "should we send…": a verb inside a what-if isn't an ask.
_HYPOTHETICAL = re.compile(r"(?i)\b(?:(?:could|should|would|might|if|what\s+if|can)\s+(?:i|we|they|people|someone|it)|if\s+you|(?:i|we|they)\s+(?:could|should|would|might))\s+(?:were\s+to\s+|ever\s+|just\s+)?\w+(?:\s+(?:a|an|the|it|this|that|me))?")


# A polite ask about something that already exists ("can we make the logo match", "could you fix my
# script"): a request, not a what-if. A what-if names something new ("could I make a sleep app").
_POLITE_ASK = re.compile(
    r"(?i)\b(?:can|could|would)\s+(?:we|you)\s+(?:please\s+|just\s+|maybe\s+)?"
    r"(?:make|build|do|change|update|fix|get|set|switch|turn|add|move|put|swap|match|start|try|redo|rename|"
    r"clean|finish|ship|send|run|open|close|merge|deploy|remove|delete)\s+"
    r"(?:the|my|our|this|that|these|those|it|them|his|her|their)\b")
# A question about where things stand ("where did our PRs leave off?", "any work we have to keep
# doing?"): it needs facts the voice doesn't have, so it is work.
_STATE_QUESTION = re.compile(
    r"(?i)\b(?:(?:where|what|which|how)\s+(?:did|do|does|is|are|was|were|has|have)\s+(?:our|my)\b"
    r"|where\s+(?:are|were|did)\s+we\s+(?:at|leave|land)"
    r"|(?:any|what)\s+(?:work|prs?|pull\s+requests?|tasks?|jobs?|tickets?|issues?|bugs?)\b.{0,40}?\b(?:we|i)\s+"
    r"(?:have|need|got|still))")


def is_conversation(request: str) -> bool:
    """Thinking out loud or asking for a take, with no ask to go do anything."""
    text = " ".join((request or "").split())
    if not text or _POLITE_ASK.search(text) or _STATE_QUESTION.search(text):
        return False
    return bool(_TALK.search(text)) and not _WORK.search(_HYPOTHETICAL.sub(" ", text))


# What the voice says when it takes on work ("On it", "let me pull that together", "I'll fix that").
# If it says this and nothing starts, the user is left waiting for work that never happens.
_WORK_ACK = re.compile(
    r"(?i)\b(?:on\s+it|doing\s+(?:that|it|so)\s+now|on\s+(?:that|it)\s+now"
    r"|(?:i'?m|i\s+am)\s+(?:getting|starting|jumping)\s+on\s+(?:it|that|this)"
    r"|let\s+me\s+(?:pull|look|check|dig|find|get|grab|take\s+a\s+look|go\s+(?:do|check|look|find)|fix|make|"
    r"set|start|kick|run|build|put|line|sort|track|round)"
    r"|i'?ll\s+(?:get|start|look|check|pull|dig|take\s+care|go\s+(?:do|check|look|find)|make|set|fix|handle|"
    r"kick|run|build|track|find|grab|sort|put|line)"
    r"|starting\s+(?:on\s+)?(?:that|it)(?:\s+now)?|kicking\s+(?:that|it|this)\s+off"
    r"|(?:switching|turning)\s+(?:those|them|it|that|the)\b"
    r"|let'?s\s+(?:make|get|fix|set|build)\s+(?:those|that|it|them|this|the)\b)")


def acknowledges_work(said: str) -> bool:
    """The voice said it is taking the work on (so a task must exist for it)."""
    return bool(_WORK_ACK.search(" ".join((said or "").split())))


# A scrap with nothing to act on: "that", "there it is", "uh, so". Only these words, at most 4.
_FRAGMENT_WORDS = set("""that this it it's its there here is are was the a an so and but or um umm uh er oh
well like what hmm mm mhm okay ok yeah yes yep sure right then now one those these them they thing
just i you we me hey""".split())


def is_fragment(request: str) -> bool:
    """Too little to start anything on its own: up to four words, none of them saying what to do."""
    words = re.findall(r"[a-z']+", (request or "").lower())
    return 0 < len(words) <= 4 and all(w in _FRAGMENT_WORDS for w in words)


# -- corrections: "living room... guest room, sorry", "no, turn them on" -------------------------
_CORRECTION_LEAD = re.compile(
    r"(?i)^\W*(?:(?:um+|uh+|er+|oh|wait)\W+)*(?:no|nope|nah|not\s+that|i\s+mean(?:t)?|or\s+rather|rather"
    r"|scratch\s+that|actually|correction)\b[\s,.;:!-]+(?P<rest>\S.*)$")
_CORRECTION_TAIL = re.compile(r"(?i)^(?P<rest>.*?\S)[\s,.;:!-]+(?:sorry|i\s+mean(?:t)?(?:\s+that)?)\W*$")
_CORRECTION_VERB = re.compile(
    r"(?i)^(?:please\s+)?(?:turn|switch|set|make|dim|brighten|open|close|play|pause|stop|start|send|book|"
    r"check|find|look|get|put|move|add|remove|cancel|use|go|do|keep|leave|change|show|tell|text|email|call)\b")
_LEADING_JUNK = re.compile(r"(?i)^(?:(?:or|our|and|but|so|um+|uh+|er+|oh|the)\s+)+")


def correction(request: str) -> tuple[str, str] | None:
    """("phrase", "guest room") for "Our guest room, sorry"; ("replace", "turn them on") for "no, turn
    them on"; None when it isn't a correction. A bare "no" is an answer, never a correction."""
    text = " ".join((request or "").split())
    if not text or len(text.split()) > 12:
        return None
    lead, tail = _CORRECTION_LEAD.match(text), _CORRECTION_TAIL.match(text)
    if lead:
        rest, by_lead = lead.group("rest"), True
    elif tail:
        rest, by_lead = tail.group("rest"), False
    else:
        return None
    trailing = _CORRECTION_TAIL.match(rest)
    rest = trailing.group("rest") if trailing else rest
    rest = rest.strip(" ,.;:!-")
    if re.match(r"(?i)^(?:thanks|thank\s+you|worries|problem|way|need|wait|rush|it'?s\s+fine|that'?s\s+fine)\b", rest):
        return None  # "no thanks", "no worries": an answer, not a correction
    if _CORRECTION_VERB.match(rest):
        # "No, turn them on": a whole new command (only after "no"/"I mean", never a polite "sorry").
        return ("replace", rest) if by_lead and re.match(r"(?i)^\W*(?:no|nope|nah|i\s+mean)", text) else None
    # "living room... guest room": what came before the break is the slip, the rest is the fix.
    rest = re.split(r"\s*(?:\.{2,}|…|—|--)\s*", rest)[-1]
    phrase = _LEADING_JUNK.sub("", rest).strip(" ,.;:!-")
    if not phrase or len(phrase.split()) > 5 or is_fragment(phrase) or not re.search(r"[a-z]{3,}", phrase.lower()):
        return None
    if all(re.sub(r"[^a-z]", "", w) in _FRAGMENT_WORDS | {"er", "erm", "be", ""} for w in phrase.lower().split()):
        return None
    return "phrase", phrase


def apply_correction(original: str, phrase: str) -> str:
    """Put the corrected words in place of the ones they correct: the same number of words ending in
    the phrase's last word ("in the living room" + "guest room" -> "in the guest room"). With no such
    word to anchor on, the correction is stated plainly instead of being run on as more words."""
    original = " ".join((original or "").split())
    words, fix = original.split(), phrase.split()
    if not words or not fix:
        return original
    anchor = re.sub(r"[^a-z0-9']", "", fix[-1].lower())
    norm = [re.sub(r"[^a-z0-9']", "", w.lower()) for w in words]
    for end in range(len(words) - 1, -1, -1):
        if norm[end] == anchor and anchor:
            start = max(0, end - len(fix) + 1)
            trail = re.sub(r"^[a-z0-9']*", "", words[end], flags=re.I)  # keep "room," punctuation
            return " ".join(words[:start] + fix[:-1] + [fix[-1] + trail] + words[end + 1:])
    return f"{original.rstrip(' .')} (correction: {phrase}, not what I said before)"


# Listening mode: a question about what was said in the room before the call ("what did Sam say the
# deadline was?", "who said we'd ship Monday?", "remind me what Priya promised"). It opens with a
# question and points back at something said, close by. A question word alone is a lookup ("what did
# Apple announce today"), and "remind me to…" is always a reminder: only "remind me what/who/…" asks.
_ROOM_ASK = re.compile(
    r"(?i)^\W*(?:(?:so|okay|ok|um+|uh+|hey|wait|sorry|and|but|quick\s+question)\W+)*(?:[a-z]+\s*,\s*)?"
    r"(?P<lead>(?:remind\s+me|tell\s+me|(?:can|could)\s+you\s+(?:remind|tell)\s+me|do\s+you\s+remember)\s+)?"
    # It opens with a question word (not a "what if" musing)…
    r"(?!what\s+if\b)(?=(?:what|who|whom|whose|when|where|which|how|why|did|didn'?t|was|wasn'?t|were|is|are"
    r"|has|have|had|do|does)(?:'s|'re|'d)?\b)"
    # …and soon points back at something said in the room.
    r".{0,80}?\b(?:said|mentioned|talked\s+about|agreed|decided|asked|promised|suggested|brought\s+up"
    r"|(?:did|didn'?t)\b.{0,60}?\b(?:say|mention|talk\s+about|agree|decide|ask|promise|suggest|tell)"
    r"|just\s+now|in\s+the\s+meeting|earlier)\b")
# Work asked for after the question, joined on ("…, and email it to Dana"): that's a task.
_THEN_WORK = re.compile(r"(?i)(?:\band\b|\bthen\b|[,;.!?])\s*(?:then\s+|also\s+|please\s+|(?:can|could|would)\s+you\s+)?"
                        r"(?=\S)")


def asks_about_room(request: str) -> bool:
    """A question about what was said in the room (or earlier in the call), which the voice answers
    from the room transcript instead of starting work. Built like ``is_conversation``: anything that
    asks for work is never a room question. What someone said ("who said we'd ship Monday") is not an
    ask, so work words count only in the question itself and in a request joined on after it."""
    text = " ".join((request or "").split())
    match = _ROOM_ASK.match(text)
    if not match:
        return False
    question = text[match.end("lead") if match.group("lead") else 0:match.end()]
    if _WORK.search(question):
        return False
    tail = text[match.end():]
    return not any(_WORK.match(tail, joined.end()) for joined in _THEN_WORK.finditer(tail))


def asks_for_work(request: str) -> bool:
    return bool(_WORK.search(" ".join((request or "").split())))


def starts_talk_mode(request: str) -> bool:
    return bool(_TALK_MODE_ON.search(" ".join((request or "").split())))


def is_reaction(request: str) -> bool:
    return bool(_REACTION.match(" ".join((request or "").split())))


# Setting a waiting question aside: "it's already handled", "never mind that one", "skip it". Strong
# phrases say it's handled or name the question; weak ones ("never mind", "forget it") also mean
# "stop the thing I just asked", so the call only takes them for a question that is the newest thing.
_ASIDE_STRONG = re.compile(
    r"(?i)\b(?:(?:it|that|this)(?:\s+one)?(?:'s|\s+is|\s+was|\s+has\s+been|\s+got)?\s+(?:already\s+)?"
    r"(?:handled|taken\s+care\s+of|sorted(?:\s+out)?|dealt\s+with|been\s+handled)"
    r"|(?:i|we)(?:'ve|\s+have)?\s+already\s+(?:handled|did|done|fixed|sorted|took\s+care\s+of|dealt\s+with)"
    r"|(?:handled|did|sorted|fixed)\s+(?:it|that|this)(?:\s+one)?\s+already"
    r"|already\s+(?:handled|taken\s+care\s+of|done|sorted)"
    r"|set\s+(?:it|that|this)(?:\s+one)?\s+aside|set\s+aside"
    r"|close\s+(?:it|that|this)(?:\s+one)?\s+out|close\s+out\s+(?:it|that|this)"
    r"|pass\s+on\s+(?:it|that|this)(?:\s+one)?|not\s+needed|(?:don't|do\s+not|no\s+longer)\s+need\s+(?:it|that|this)"
    r"|(?:skip|drop|ignore|forget(?:\s+about)?|dismiss|never\s*mind|nevermind)\s+(?:that|this|the)\s+(?:one|question|choice|decision))\b")
_ASIDE_WEAK = re.compile(
    r"(?i)^\W*(?:(?:oh|ok|okay|yeah|no|nah|nope|actually|um+|uh+|so|just|and|but)\W+)*"
    r"(?:never\s*mind|nevermind|forget\s+(?:about\s+)?(?:it|that)|skip\s+(?:it|that)|drop\s+(?:it|that)|"
    r"don't\s+worry\s+about\s+(?:it|that)|not\s+(?:right\s+)?now|leave\s+it)"
    r"(?:\W+(?:for\s+now|then|thanks|thank\s+you|please|man|dude))*\W*$")
# Words that make it an answer or new work, not setting it aside ("never mind, turn on the den").
_ASIDE_WORK = re.compile(r"(?i)\b(?:turn|switch|book|buy|order|send|go\s+with|pick|choose|use|instead|but\s+can|can\s+you|"
                         r"could\s+you|please\s+(?!.*\baside\b)\w+|look\s+up|find|check)\b")


def set_aside_kind(request: str) -> str | None:
    """"strong" or "weak" when the words set a waiting question aside, else None. Short utterances
    only, and nothing that asks for work or picks an option."""
    said = " ".join((request or "").split())
    if not said or len(said.split()) > 14 or _ASIDE_WORK.search(said):
        return None
    if _ASIDE_STRONG.search(said):
        return "strong"
    if _ASIDE_WEAK.search(said):
        return "weak"
    return None


# Answering a waiting approval by voice. Short replies only: anything longer, or anything that asks
# for other work, goes on as a normal request. "always"/"permanently" is recognized so it can be
# refused: a spoken answer never grants more than this one task.
_APPROVE_ALWAYS = re.compile(
    r"(?i)\b(?:always|permanent(?:ly)?|forever|from\s+now\s+on|every\s+time|for\s+good|all\s+the\s+time|"
    r"don't\s+ask\s+(?:me\s+)?again|never\s+ask)\b")
_APPROVE_SESSION = re.compile(
    r"(?i)\b(?:for\s+(?:the\s+rest\s+of\s+)?(?:this|the)\s+(?:session|task|job|run)|for\s+the\s+rest\s+of\s+(?:it|this|the\s+task)|"
    r"(?:this|the)\s+whole\s+(?:session|task)|until\s+(?:it's|it\s+is|this\s+is)\s+(?:done|finished))\b")
_APPROVE_ONCE = re.compile(
    r"(?i)\b(?:(?:just\s+)?(?:this|the)\s+once|just\s+(?:this|the)\s+(?:one\s+)?time|one\s+time|only\s+once|once)\b")
_APPROVE_WORD = re.compile(r"(?i)^\W*(?:(?:um+|uh+|oh|ok|okay|so|well|yes|yeah|yep)\W+)*(?:i\s+)?approve(?:d)?(?:\s+(?:it|that|this))?\W*$")
_APPROVE_DENY = re.compile(
    r"(?i)^\W*(?:(?:um+|uh+|actually|oh|well|so)\W+)*(?:no|nope|nah|deny(?:\s+it)?|don't(?:\s+do\s+it)?|do\s+not|"
    r"don't\s+allow\s+(?:it|that)|decline(?:\s+it)?|reject(?:\s+it)?|not\s+that)\b")
_APPROVE_YES = re.compile(
    r"(?i)^\W*(?:(?:um+|uh+|oh|ok|okay|so|well)\W+)*(?:yes|yeah|yep|yup|sure|ok|okay|go\s+ahead|do\s+it|"
    r"approve(?:d)?(?:\s+it)?|allow(?:\s+it)?|fine|go\s+for\s+it|that's\s+fine|please\s+do|sounds\s+good)\b")
_APPROVE_OTHER_WORK = re.compile(r"(?i)\b(?:can\s+you|could\s+you|look\s+up|find|search|what|how|why|when|where|who|"
                                 r"also|and\s+then|instead|book|buy|order|send|email|text|call)\b")


def approval_answer(request: str) -> str | None:
    """A short spoken answer to a waiting approval: once | session | deny | always (refused by the
    caller) | yes (a bare "yeah": once when only one approval waits and it was just asked) | None."""
    said = " ".join((request or "").split())
    if not said or len(said.split()) > 12 or _APPROVE_OTHER_WORK.search(said):
        return None
    if _APPROVE_ALWAYS.search(said) and not re.search(r"(?i)\bnot\s+(?:always|permanent|forever)", said):
        return "always"  # "yes, always", "don't ask me again": refused by the caller, never granted
    if _APPROVE_DENY.search(said) and not _APPROVE_YES.search(said):
        return "deny"
    if _APPROVE_SESSION.search(said):
        return "session"
    if _APPROVE_WORD.search(said):
        return "once"
    if _APPROVE_ONCE.search(said):
        return "once"
    if _APPROVE_YES.search(said):
        return "yes"
    return None


# A new task held behind another one editing the same project: what the user says about it.
_FENCE_ANYWAY = re.compile(
    r"(?i)\b(?:start\s+(?:it|this|that|mine|the\s+new\s+one)?\s*(?:anyway|now|right\s+away)|"
    r"(?:go\s+ahead|run\s+(?:it|both|them\s+both))\s+anyway|don't\s+wait|do\s+not\s+wait|no\s+need\s+to\s+wait|"
    r"run\s+(?:them\s+)?both|both\s+at\s+once|at\s+the\s+same\s+time|in\s+parallel|anyway)\b")
_FENCE_STOP = re.compile(
    r"(?i)\b(?:stop|cancel|kill|end|abort)\s+(?:the\s+)?(?:other|first|old|older|running|current|earlier)(?:\s+(?:one|task))?\b|"
    r"\b(?:stop|cancel|kill)\s+(?:that|it)\s+(?:one\s+)?(?:first|and\s+start)\b")
_FENCE_WAIT = re.compile(
    r"(?i)^\W*(?:(?:ok|okay|yeah|yes|sure|fine|no)\W+)*(?:(?:just\s+)?wait(?:\s+for\s+(?:it|that|that\s+one))?|"
    r"queue\s+it|let\s+it\s+wait|that's\s+fine|after\s+(?:it|that)(?:\s+one)?(?:\s+finishes)?|keep\s+it\s+queued)\b")


def fence_choice(request: str) -> str | None:
    """anyway | stop | wait for a task waiting on another, from a short reply; else None."""
    said = " ".join((request or "").split())
    if not said or len(said.split()) > 14:
        return None
    if _FENCE_STOP.search(said):
        return "stop"
    if _FENCE_ANYWAY.search(said):
        return "anyway"
    if _FENCE_WAIT.search(said):
        return "wait"
    return None


# -- spoken choices become cards ------------------------------------------------------------------
_CHOICE_LEAD = re.compile(
    r"(?i)^(?:(?:so|okay|ok|alright|great|sure|got it)[,.!]?\s+)*"
    r"(?:(?:do|would) you (?:want|like|prefer)(?: me)?(?: to)?|should i|shall i|want me to|"
    r"(?:which|what) (?:one |would you |do you )?(?:want|like|prefer)?(?: me to)?(?: do)?[:,]?|"
    r"would you rather|do you prefer|are you thinking|want to|is (?:it|this|that)|prefer)\s+")
_NOT_A_CHOICE = re.compile(r"(?i)\bor (?:not|no|something|anything|so|whatever|nothing|later)\b|\bwhether or\b|\bor else\b")
MAX_CHOICES = 4
MAX_CHOICE_WORDS = 10


def spoken_choice(said: str) -> tuple[str, list[str]] | None:
    """The voice offering the user a pick ("Should I book Nopa or Zuni?", "Den, bedroom, or both?"):
    (the question, its 2-4 options). None for a yes/no question or anything else."""
    text = " ".join(str(said or "").split())
    questions = re.findall(r"[^.!?]*\?", text)
    if not questions:
        return None
    question = questions[-1].strip()
    if not re.search(r"(?i)\bor\b", question) or _NOT_A_CHOICE.search(question):
        return None
    body = question.rstrip("?").strip()
    if ":" in body:  # "Which one do you want: Kayak, Expedia or Google Flights"
        body = body.split(":", 1)[1]
    # "Want the quick summary or the full report?" -> "the quick summary or the full report"
    body = _CHOICE_LEAD.sub("", body, count=1).strip(" ,:")
    parts = [p.strip(" ,.;:") for p in re.split(r"(?i),\s*(?:or\s+)?|\s+or\s+", body)]
    options = [p for p in parts if p]
    if not 2 <= len(options) <= MAX_CHOICES:
        return None
    if any(len(o.split()) > MAX_CHOICE_WORDS for o in options):
        return None
    if len(question.split()) > 40:
        return None
    options = [o[0].upper() + o[1:] for o in options]
    if len({o.lower() for o in options}) != len(options):
        return None
    return question, options
