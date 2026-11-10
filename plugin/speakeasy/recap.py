"""Repeat status and recap asks: reuse the task that already knows.

"Where are we on build 12?" asked twice in ten minutes used to start two fresh investigations.
Here: recognise a status/recap ask that names a subject (``recap_ask``), find a recent task on the
same subject (``pick``) and decide what to do with it (``plan``): answer from its result when it
finished minutes ago (or came in after the user hung up), else continue in that task's own session.
``merge_parts`` is the routing model's guard: one subject never becomes parallel twin tasks.

Pure functions only; calls.py does the I/O.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Iterable

LOOKBACK_S = 4 * 3600        # a same-subject task this recent is the one that knows: its session continues
FRESH_S = 15 * 60            # finished less than this ago: the voice answers from its result right away
LATE_LOOKBACK_S = 12 * 3600  # an answer that came in after the user hung up is offered for this long
MAX_WORDS = 30

ANSWER = "answer"            # say the finished result now, offer a fresh check
CONTINUE = "continue"        # send the ask into that task's session as a follow-up
STATUS = "status"            # it is still running in this call: answer from its live steps

_LIVE = {"admitting", "working", "running", "waiting_for_approval", "waiting_on_task"}

# A status or recap ask about something: "status of build 12", "where'd we leave off on the PRs",
# "any update on the move", "how's the garden plan going", "did the export finish", "am I gonna win".
_RECAP = re.compile(
    r"(?i)\b(?:"
    r"(?:status|update|updates|latest|progress|news)\s+(?:on|of|with|for|about|from)\b"
    r"|where\s+(?:are|were|do|did|'re)?\s*(?:we|you|i|things|it)\s+(?:at|on|with|stand|standing)\b"
    r"|where(?:'d|\s+did|\s+had)?\s+(?:we|you|i)\s+(?:leave|left)\s+(?:it|off|things)\b"
    r"|where\s+(?:we|you|i)\s+(?:left|leave)\s+(?:it|off|things)\b"
    r"|where(?:'s|\s+is|\s+are)\s+(?:my|the|our|that|those|this)\b"
    r"|how(?:'s|\s+is|\s+are|\s+was|\s+were|\s+did)\s+(?:my|the|our|your|that|those|this)?\s*[\w' -]{1,40}?\s+"
    r"(?:going|looking|coming\s+along|doing|go|turn\s+out|end\s+up)\b"
    r"|(?:is|are|did|has|have|was)\s+(?:the|my|our|that|those|this)?\s*[\w' -]{1,40}?\s+"
    r"(?:done|finished|finish|shipped|ship|landed|land|ready|passed|pass|merged|out|live|fixed|working)\b"
    r"|what\s+happened\s+(?:with|to|on)\b"
    r"|what(?:'s|\s+is|\s+are)\s+still\s+(?:being\s+)?\w+"
    r"|(?:am\s+i|are\s+we|will\s+i|will\s+we|is\s+(?:my|our)\s+\w+)\s+(?:gonna|going\s+to)?\s*win\b"
    r"|(?:catch|fill)\s+me\s+(?:up|in)\b|recap\b|remind\s+me\s+where"
    r")")

# "Check again", "fresh check", "look again now", "re-check": the user wants a new look, not the old answer.
_FRESH = re.compile(
    r"(?i)\b(?:again|fresh|re-?check|re-?run|recheck|one\s+more\s+(?:time|look)|"
    r"latest\s+now|right\s+now|check\s+now|look\s+now|anything\s+new\s+since)\b")

# Words that make an ask a status ask, not its subject.
_RECAP_WORDS = set("""status update updates latest progress news where leave left off going looking coming along
doing done finished finish happened happen stand standing still being now check checking checked again fresh
recheck catch fill recap remind how what whats where'd how's what's where's did does is are was were has have
ready shipped ship landed land passed pass merged out live fixed working turn end
look looked any anything there that this those these things thing stuff today yet so far""".split())
_FILLER = set("""a an the i me my we you your it its is are am was be do does did will would can could gonna going
to of for on in at with about that this what how when where who why just still right now yet so and or but also
oh like um uh okay ok yeah hey there their they them he she his her get got should shall might must someone
something anyone anything one any some more maybe please tell let us our ours mine lot bit""".split())
_IRREGULAR = {"built": "build", "builds": "build", "building": "build", "ran": "run", "running": "run",
              "won": "win", "winning": "win", "wins": "win", "went": "go", "prs": "pr", "fixes": "fix",
              "tests": "test", "testing": "test", "games": "game", "teams": "team"}


def _stem(word: str) -> str:
    word = word.lower().strip("'").replace("'s", "")
    if word in _IRREGULAR:
        return _IRREGULAR[word]
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9'.-]*", text or "")


_UNITS = "one two three four five six seven eight nine".split()
_TEENS = "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_SPELLED = re.compile(r"(?i)\b(?:(" + "|".join(_TENS) + r")(?:[-\s]+(" + "|".join(_UNITS) + r"))?"
                      r"|(" + "|".join(_UNITS[1:] + _TEENS) + r"))\b")


def digits(text: str) -> str:
    """Spelled numbers as digits, the way the voice may write what it heard ("forty-one", "forty one",
    "twelve"). A lone "one" stays a word: "the one you mentioned" names no number."""
    def number(m: re.Match[str]) -> str:
        tens, unit, small = (g.lower() if g else g for g in m.groups())
        if small:
            return str((_UNITS + _TEENS).index(small) + 1)
        return str(20 + 10 * _TENS.index(tens) + (_UNITS.index(unit) + 1 if unit else 0))
    return _SPELLED.sub(number, text or "")


_ID_WORDS = {"build", "builds", "release", "releases", "version", "v", "issue", "ticket", "pr", "bug", "number",
             "#", "rc", "sprint", "milestone"}
# A bare number said before one of these is a thing's name ("push 41 to public", "42 is fine"); before
# anything else it most likely counts something ("three blockers", "ten minutes").
_ID_NEXT = {"to", "is", "isn't", "was", "wasn't", "and", "or", "now", "then", "too", "also", "out", "live",
            "public", "please", "today", "tonight", "yet", "go", "goes", "gets", "should", "will", "can", "has",
            "have", "needs", "as", "instead"}
_COUNTING = {"top", "first", "last", "next", "in", "for", "at", "by", "about", "around", "under", "over", "within",
             "every", "than", "it", "me", "of", "only", "just", "like", "to", "from", "until", "after", "before",
             "with", "per"}
_NOT_IDS = re.compile(r"(?i)\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm|o'clock)\b|\b\d{1,2}\s*[ap]\.m\.|[$€£]\s*\d[\d,.]*"
                      r"|\d[\d,.]*\s*%")


def ids(text: str) -> set[str]:
    """The identifier numbers a text names, as digit strings: next to a word like build or release
    ("release forty-two", "#12", "the 41 build"), or bare where a name goes ("pushing 41 to public").
    Counts, durations and times are not ("top three", "in ten minutes", "the 4:15")."""
    words = re.findall(r"#|\d+(?:\.\d+)*|[a-z]+(?:'[a-z]+)?", _NOT_IDS.sub(" ", digits(text).lower()))
    found = set()
    for i, word in enumerate(words):
        if not word[0].isdigit():
            continue
        before = words[i - 1] if i else ""
        after = words[i + 1] if i + 1 < len(words) else ""
        if (before in _ID_WORDS or after in _ID_WORDS
                or (before not in _COUNTING and (not after or after in _ID_NEXT))):
            found.add(word)
    return found


def names_other_number(a: str, b: str) -> bool:
    """Whether two texts are about differently numbered things (build 67 vs build 68): never one task."""
    x, y = ids(a), ids(b)
    return bool(x and y and not x & y)


def subject(text: str, recap_words: bool = True) -> set[str]:
    """What the ask is about: its content words (stemmed), without filler or status words."""
    out = set()
    for raw in _tokens(digits(text)):
        for piece in re.split(r"[.-]", raw):
            if not piece:
                continue
            word = _stem(piece)
            if word.isdigit():
                out.add(word)
                continue
            if len(word) < 2 or word in _FILLER or (recap_words and word in _RECAP_WORDS):
                continue
            if len(word) < 3 and not (len(piece) > 1 and piece[:2].isupper()):
                continue  # "PR" stays; "oh" doesn't
            out.add(word)
    return out


def _numbers(words: set[str]) -> set[str]:
    return {w for w in words if w.isdigit()}


def recap_ask(request: str) -> bool:
    """A status or recap ask that names what it's about ("where are we on build 12?"). Subject-less
    asks ("how's it going?") are the running-task status path's (router.is_status_question)."""
    text = " ".join((request or "").split())
    if not (0 < len(text.split()) <= MAX_WORDS) or not _RECAP.search(text):
        return False
    return bool(subject(text))


def wants_fresh(request: str) -> bool:
    return bool(_FRESH.search(request or ""))


_YES = re.compile(r"(?i)^\s*(?:yes|yeah|yep|yup|sure|please|ok(?:ay)?|go\s+ahead|do\s+it|check(?:\s+it)?)\b"
                  r"[\w ,.'!]{0,40}$")


def accepts_offer(request: str) -> bool:
    """A short yes to "want me to check again?" ("yeah, do it", "sure, go ahead")."""
    text = " ".join((request or "").split())
    return len(text.split()) <= 8 and bool(_YES.match(text)) and not re.search(r"(?i)\b(?:no|not|don't|nah)\b", text)


def same_subject(a: str, b: str, strict: bool = False) -> bool:
    """Whether two asks are about the same thing. Different numbers (build 12 vs build 13) never
    are. A shared number plus a shared word is; otherwise most of the shorter ask's subject must be
    in the other. ``strict`` (two parts of one sentence): the overlap must be heavy on both."""
    sa, sb = subject(a), subject(b)
    if not sa or not sb:
        return False
    na, nb = _numbers(sa), _numbers(sb)
    if na and nb and not (na & nb):
        return False
    shared = sa & sb
    if na & nb and shared - _numbers(shared):
        return True
    words = shared - _numbers(shared)
    small = min(len(sa - na), len(sb - nb)) or 1
    if strict:
        return len(words) >= 2 and len(words) / small >= 0.5
    return bool(words) and (len(words) >= 2 or len(words) / small >= 0.99) and len(words) / small >= 0.5


@dataclasses.dataclass(frozen=True)
class Earlier:
    """A recent task the repeat ask might be about (store.recent_subject_tasks rows)."""
    key: str
    task_id: str
    request: str
    status: str
    title: str = ""
    result: str = ""
    created: float = 0.0
    settled_at: float | None = None
    interaction_id: str = ""
    session_id: str | None = None
    seen: bool = False


def _about(e: Earlier) -> str:
    return f"{e.title} {e.request}"


def pick(request: str, earlier: Iterable[Earlier], now: float) -> Earlier | None:
    """The newest recent task on the same subject as ``request`` (most shared words first)."""
    asked = subject(request)
    best: tuple[int, float, Earlier] | None = None
    for e in earlier:
        when = e.settled_at or e.created
        if now - when > LATE_LOOKBACK_S or not same_subject(request, _about(e)):
            continue
        score = len(asked & subject(_about(e)))
        if best is None or (score, when) > (best[0], best[1]):
            best = (score, when, e)
    return best[2] if best else None


def late(e: Earlier, prior_call_end: float | None, call_opened: float) -> bool:
    """It finished after the call that asked for it ended, before this call: the user never heard it."""
    return (e.status == "completed" and e.settled_at is not None and prior_call_end is not None
            and e.created <= prior_call_end < e.settled_at <= call_opened)


def plan(request: str, e: Earlier, now: float, *, in_call: bool, prior_call_end: float | None,
         call_opened: float) -> str | None:
    """What to do with a repeat ask about ``e``: ANSWER, CONTINUE, STATUS, or None (start fresh)."""
    when = e.settled_at or e.created
    if e.status in _LIVE:
        return STATUS if in_call else None  # a running row no call holds (a restart): stale, start fresh
    if e.status != "completed":
        return None  # it failed or was stopped: it doesn't know; a fresh task does the work
    if wants_fresh(request):
        return CONTINUE if e.session_id or in_call else None
    if e.result and (now - when <= FRESH_S or late(e, prior_call_end, call_opened)):
        return ANSWER
    if now - when > LOOKBACK_S:
        return None
    return CONTINUE if e.session_id or in_call else None


# -- the routing model's parts -----------------------------------------------------------------

def merge_parts(parts: list[str]) -> list[list[int]]:
    """Groups of part indexes that are one subject (in order). Parts naming the same thing (same
    number, heavy word overlap) are one task, and two status checks of one thing are one task even
    when one of them leans on the other ("status of build 12? what's still being built?")."""
    parent = list(range(len(parts)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def status_like(text: str) -> bool:
        return bool(_RECAP.search(text)) or bool(re.search(r"(?i)\b(?:status|still|progress|update)\b", text))

    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            a, b = parts[i], parts[j]
            sa, sb = subject(a), subject(b)
            na, nb = _numbers(sa), _numbers(sb)
            if na and nb and not (na & nb):
                continue
            joined = same_subject(a, b, strict=True) or bool(na & nb and (sa & sb) - na)
            if not joined and status_like(a) and status_like(b):
                small, big = sorted((sa, sb), key=len)
                joined = not small or small <= big or bool(na & nb)
            if joined:
                parent[root(j)] = root(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(parts)):
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())


def merged(parts: list[str], groups: list[list[int]]) -> list[str]:
    return [" ".join(parts[i].rstrip() for i in g) for g in groups]
