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
    live = [t for t in tasks if t.status in {"admitting", "working", "running", "waiting_for_approval"}]
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
    if newest.status not in {"admitting", "working", "running"} or newest.age_s is None:
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
    tail = continues_newest(request, open_tasks)
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
    running = [t for t in tasks if t.status in {"admitting", "working", "running", "waiting_for_approval"}]
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
                   replied_task_id: str | None = None) -> list[dict[str, str]]:
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
            '{"follow_up_task_id": string or null, "conversation": string or null, "parts": [strings], '
            '"channel": string or null, "show": true or false}. '
            "follow_up_task_id: the id of an open task ONLY when the request clearly adds to, changes, corrects "
            "or asks about that task; else null. A reply to what the assistant just said (agreeing, disagreeing, "
            "correcting it, pushing back, answering its question: \"no, Hermes can\", \"you're missing a couple\", "
            "\"that's wrong\") is a follow-up to the task that answer came from, never a new task or conversation. "
            "A request that points back at something a task told the user (\"fix that script you found\", \"do the "
            "thing it suggested\", \"send the one you mentioned\") is a follow-up to that task, even when the task "
            "has finished: it continues in that task's thread. People pause mid-thought: a short fragment said seconds after "
            "a task started that only makes sense as the end of that request (\"...and am I gonna win?\") is a "
            "follow-up to it, never a new task. Decide by topic, never by timing: a request that starts with "
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
            "A conversation is never split into parts. "
            "show: true when the user wants to SEE something rather than just hear about it: how a thing looks "
            "(\"what does the new logo look like\", \"how do those office speakers it recommended look\", "
            "\"let me see the hotel\"), a design, page, product, place or a task's visual state. False for "
            "questions about facts, status or prices, and for asks to find new things to look at "
            "(\"show me flights to Paris\" is new work with show false)."},
        {"role": "user", "content": f"Open tasks:\n{open_lines}\n\nChannels:\n{channel_lines}\n\n"
                                    f"Existing conversations:\n{chat_lines(chats or [])}\n\n"
                                    + (f"The call so far (for what 'that', 'it', 'the X one' refer to):\n"
                                       f"{call_so_far[-1200:]}\n\n" if call_so_far and (chats or tasks) else "")
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
    follow = data.get("follow_up_task_id")
    if follow is not None:
        if not isinstance(follow, str) or not any(t.task_id == follow for t in tasks[-MAX_OPEN_TASKS:]):
            return None
        return Decision([Part("follow_up", request, follow)], None, "model", show=show)
    conversation = data.get("conversation")
    if conversation is not None:
        if not isinstance(conversation, str):
            return None
        if conversation.strip():
            if not any(c.ref == conversation.strip() for c in chats or []):
                return None
            return Decision([Part(NEW, request)], None, "model", conversation=conversation.strip(), show=show)
    parts = data.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= MAX_PARTS:
        return None
    clean = [" ".join(p.split())[:600] for p in parts if isinstance(p, str) and p.strip()]
    if len(clean) != len(parts):
        return None
    if len(clean) == 1:
        clean = [request]  # one ask: keep the user's own words
    return Decision([Part(NEW, p) for p in clean], channel, "model", show=show)


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
        from agent.auxiliary_client import call_llm, extract_content_or_reasoning  # type: ignore
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


def smart_title(request: str, timeout: float = TITLE_TIMEOUT_S) -> str | None:
    """A to-do style name for a voice task from Hermes' own session titler (task
    ``title_generation``), in this plugin's profile scope. None when unavailable; callers keep the
    instant word-based label."""
    try:
        from agent.title_generator import generate_title  # type: ignore
    except Exception:
        return None
    try:
        with _profile_scope():
            title = generate_title(request, timeout=timeout)
    except Exception as exc:
        logger.info("speakeasy: task title unavailable (%s)", type(exc).__name__)
        return None
    title = " ".join(str(title or "").split()).strip(" .")
    return title[:60] or None


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
        from agent.auxiliary_client import call_llm  # type: ignore
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
        from agent.auxiliary_client import call_llm  # type: ignore
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
    "would see on a progress line. Give 3 to 5 steps in order, each 2 to 4 words, starting with a verb "
    "(\"Compare flights\", \"Draft itinerary\", \"Check availability\"). The last step is the hand-back "
    "to the user (\"Your review\", \"Confirm booking\"). Plain words, no numbering, no punctuation at the "
    "end, nothing the request doesn't need. Reply with JSON only: {\"steps\": [\"...\"]}"
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
        from agent.auxiliary_client import call_llm  # type: ignore
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


_PROGRESS_PROMPT = (
    "You write one short spoken progress update for a voice assistant whose background agent is working on "
    "a task for the user. You get the user's request, the agent's recent steps (newest last) and updates "
    "already spoken. Say what the agent is doing and what it has found so far, concretely: names, numbers, "
    "sources, what is next. One or two short sentences, under 30 words, natural speech, first person "
    "(\"I found...\", \"I'm now...\"). Never say \"still on it\", \"still working\" or \"still checking\" "
    "on their own, never repeat an earlier update, never invent results the steps don't show, and never "
    "give the final answer. Reply with JSON only: {\"say\": \"...\"}, or {\"say\": \"\"} when the steps "
    "add nothing worth saying."
)


def progress_update(request: str, steps: list[str], told: list[str], timeout: float = 6.0) -> str | None:
    """A spoken update that says what the task is actually doing and seeing. None when unavailable."""
    if not steps:
        return None
    try:
        from agent.auxiliary_client import call_llm  # type: ignore
    except Exception:
        return None
    body = json.dumps({"request": " ".join(str(request).split())[:600], "recent_steps": steps[-8:],
                       "already_said": told[-3:]})
    try:
        with _profile_scope():
            response = call_llm(task="title_generation",
                                messages=[{"role": "system", "content": _PROGRESS_PROMPT},
                                          {"role": "user", "content": body}],
                                max_tokens=120, temperature=None, timeout=timeout,
                                reasoning_config={"enabled": False})
        raw = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        logger.info("speakeasy: progress update unavailable (%s)", type(exc).__name__)
        return None
    return clean_progress(raw)


def clean_progress(raw: str) -> str | None:
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (raw or "").strip())
    try:
        said = json.loads(raw).get("say") if raw.startswith("{") else raw
    except (ValueError, AttributeError):
        return None
    said = " ".join(str(said or "").split())
    if not said or len(said) > 240 or re.fullmatch(r"(?i)(still (on it|working|checking)[^.]*\.?)", said):
        return None
    return said


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
           jev_place: Callable[..., dict[str, Any] | None] | None = None) -> Decision:
    """Route one handoff. A task id the voice model marked wins outright (no model call)."""
    request = (request or "").strip()
    topics = topics or []
    open_tasks = tasks[-MAX_OPEN_TASKS:]
    quick = quick_intent(request, open_tasks, replied_task_id)
    if quick is not None and quick.kind in {STATUS, IGNORE}:
        return Decision([quick], None, "quick")
    if isinstance(marked_task_id, str) and any(t.task_id == marked_task_id for t in open_tasks):
        return Decision([Part("follow_up", request, marked_task_id)], None, "marked")
    if quick is not None:
        return Decision([quick], None, "quick")
    tail = continues_newest(request, open_tasks)
    if tail is not None and marked_task_id in (None, "", tail.task_id):
        return Decision([Part("follow_up", request, tail.task_id)], None, "fragment")
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
    r"(?i)^\W*(?:(?:what|huh|wait|hang\s+on|hold\s+on|yeah|yes|yep|no|nope|nah|okay|ok|right|sure|cool|nice|wow|damn|bro|dude|man|lol|haha|the\s+fuck|what\s+the\s+fuck|todd|hermes|really|seriously|for\s+real|got\s+it|i\s+see|interesting)\W*){1,4}$")


# "could I make…", "if you were to build…", "should we send…": a verb inside a what-if isn't an ask.
_HYPOTHETICAL = re.compile(r"(?i)\b(?:(?:could|should|would|might|if|what\s+if|can)\s+(?:i|we|they|people|someone|it)|if\s+you|(?:i|we|they)\s+(?:could|should|would|might))\s+(?:were\s+to\s+|ever\s+|just\s+)?\w+(?:\s+(?:a|an|the|it|this|that|me))?")


def is_conversation(request: str) -> bool:
    """Thinking out loud or asking for a take, with no ask to go do anything."""
    text = " ".join((request or "").split())
    return bool(text) and bool(_TALK.search(text)) and not _WORK.search(_HYPOTHETICAL.sub(" ", text))


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
