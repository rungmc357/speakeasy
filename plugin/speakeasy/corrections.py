"""Corrections to an open task's words: "no, not Harbor Lights, Harbour Lite", "I meant the pine
desk", a typed "pine desk" after the voice heard "spine desk", "there is no Pinewood Hall".

Speech-to-text mishears names, and a request that hinges on one (a machine, a model, a project)
sends the task after something that doesn't exist. A correction is never more words lined up for the
task: it rewrites what the task was asked (its request and title), reaches the running task at once
as a correction (redo anything based on the misheard words), and every status line still written
around the misheard words stops being shown or spoken (store.work, calls.card_update).

``detect`` only calls something a correction when the words it corrects are really in the task's
request (or, for "I meant Y", something close enough to Y is): "not that one, the blue one" about a
different subject is a follow-up as before.
"""
from __future__ import annotations

import dataclasses
import difflib
import re
from typing import Iterable

CORRECTING = "Applying your correction"  # a corrected task's line until its next real step
MAX_PHRASE_WORDS = 8
MAX_BARE_WORDS = 4        # a typed fix with nothing around it ("pine desk")
WRONG_MATCH = 0.75        # "not X": X must really be (nearly) in the request
RIGHT_MATCH = 0.6         # "I meant Y": the misheard span must sound like Y
NO_MATCH = 0.7            # "no, Y": closer
BARE_MATCH = 0.8          # a typed "Y" alone: closer still (a short follow-up is usually more work)
SNAP_MATCH = 0.8          # a corrected name close to one of the user's own names takes its spelling

_ADDRESS_TAIL = re.compile(r"(?i)(?:\s*,\s*(?:bro|dude|mate|buddy|man|lol)|[\s,.;:!-]+(?:bro|dude|lol|please|thanks|"
                           r"thank\s+you|sorry))\W*$")
_FILLER_LEAD = re.compile(r"(?i)^\W*(?:(?:um+|uh+|er+|erm|oh|ah|ok|okay|wait|so|hey|no|nope|nah|sorry|actually|but)\b\W*)+")
# How a voice hands a correction over in its own words ("The user corrected: it's Y, not X").
_RELAYED = re.compile(r"(?i)^(?:(?:the\s+)?(?:user|caller)|they|he|she|[A-Z][a-z]+)?\s*(?:corrected|correction|"
                      r"clarified|clarifies|corrects|means|meant|says|said)\s*(?:that\s+)?(?:it\s+)?[:,-]?\s*(?=\S)")
_NOT_X_Y = re.compile(
    r"(?i)^(?:it'?s\s+|i\s+said\s+|i\s+meant?\s+)?not\s+(?P<wrong>[^,;.\u2014]+?)\s*"
    r"(?:[,;.\u2014]+\s*|\s+-+\s+|\s+but\s+)(?:but\s+|rather\s+|it'?s\s+|i\s+(?:said|meant?)\s+)?(?P<right>.+)$")
_Y_NOT_X = re.compile(r"(?i)^(?:i\s+(?:said|meant?)\s+|it'?s\s+)?(?P<right>[^,;]+?)\s*,?\s+not\s+(?P<wrong>[^,;]+)$")
_NO_SUCH = re.compile(r"(?i)^there(?:'s|\s+is|\s+are|\s+isn'?t\s+any)\s+no\s+(?:such\s+(?:thing\s+as\s+)?)?(?P<wrong>.+)$")
_MISHEARD = re.compile(r"(?i)^(?P<wrong>.+?)\s+(?:was|is)\s+(?:a\s+|just\s+a\s+)?(?:mishearing|misheard|typo|"
                       r"transcription\s+(?:slip|error|mistake)|wrong)\b")
_I_MEANT = re.compile(r"(?i)^(?:i\s+(?:meant?|said)|like|it'?s|as\s+in)\s+(?P<right>.+)$")
_LEAD_NO = re.compile(r"(?i)^\W*(?:no|nope|nah)\b")
# A whole new instruction ("no, turn them on", "check the logs instead"), not a corrected name.
_COMMAND = re.compile(
    r"(?i)^(?:please\s+)?(?:turn|switch|set|make|dim|open|close|play|pause|stop|start|send|book|check|find|look|"
    r"get|put|move|add|remove|cancel|use|go|do|keep|leave|change|show|tell|text|email|call|write|run|fix|"
    r"make|try|search|read|ask|buy|order|let|can|could|would|will|should|don'?t|just)\b")
_ANSWER = re.compile(r"(?i)^(?:thanks|thank\s+you|worries|problem|way|need|rush|it'?s\s+fine|that'?s\s+fine|"
                     r"yes|yeah|yep|ok|okay|sure|good|great|perfect|cool)\b")
_STOP_WORDS = {"the", "a", "an", "my", "our", "your", "of", "to", "for", "in", "on", "at", "and", "or", "it", "that",
               "this", "is", "was", "be", "with", "which", "we", "i", "you", "today", "now"}


@dataclasses.dataclass(frozen=True)
class Fix:
    wrong: str | None     # the words as the task had them (None: the task already has `right`)
    right: str | None     # what was meant (None: only "X was a mishearing")
    request: str          # the task's request with the fix applied


def norm(text: str | None) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def _clean(words: str) -> str:
    text = " ".join(str(words or "").split())
    for _ in range(3):
        text = _ADDRESS_TAIL.sub("", text).strip()
    return text.strip(" ,.;:!?-\u2014\"'")


def _content(phrase: str) -> list[str]:
    return [w for w in norm(phrase).split() if w not in _STOP_WORDS]


def find_span(text: str, target: str, min_ratio: float) -> tuple[int, int, str, float] | None:
    """The run of words in `text` that best matches `target` (by letters, so "Man Pros" matches
    "mac pros"): (first word, last word + 1, the words as written, ratio), or None under `min_ratio`."""
    words = str(text or "").split()
    goal = norm(target)
    if not words or not goal:
        return None
    k = len(goal.split())
    best: tuple[int, int, str, float] | None = None
    for n in range(max(1, k - 1), k + 2):
        for start in range(0, len(words) - n + 1):
            window = " ".join(words[start:start + n])
            got = norm(window)
            if not got or not _content(window):
                continue
            ratio = difflib.SequenceMatcher(None, got, goal).ratio()
            if best is None or ratio > best[3] + 1e-9:
                best = (start, start + n, window.strip(" ,.;:!?\"'()"), ratio)
    return best if best and best[3] >= min_ratio else None


def rewrite(text: str, wrong: str | None, right: str | None) -> str:
    """`text` with every `wrong` (as written, any case) put as `right`; unchanged without both."""
    if not text or not wrong or not right:
        return text
    pattern = re.compile(r"(?i)(?<![A-Za-z0-9])" + re.escape(wrong) + r"(?![A-Za-z0-9])")
    if pattern.search(text):
        return pattern.sub(lambda _m: right, text)
    span = find_span(text, wrong, 0.9)
    if span is None:
        return text
    words = text.split()
    tail = re.sub(r"^.*?([,.;:!?)\"']*)$", r"\1", words[span[1] - 1])
    lead = re.sub(r"^([(\"']*).*$", r"\1", words[span[0]])
    return " ".join(words[:span[0]] + [lead + right + tail] + words[span[1]:])


def mentions(text: str | None, wrongs: Iterable[str]) -> bool:
    """`text` still says one of the misheard phrases (any case, a plural "s" either way)."""
    said = f" {norm(text)} "
    for wrong in wrongs:
        w = norm(wrong)
        if w and (f" {w} " in said or f" {w.rstrip('s')} " in said or f" {w}s " in said):
            return True
    return False


def _singular(text: str) -> str:
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in norm(text).split())


def _has(text: str, phrase: str) -> bool:
    """`text` already says `phrase` (any case, plurals either way)."""
    want = _singular(phrase)
    return bool(want) and f" {want} " in f" {_singular(text)} "


def snap(phrase: str, vocabulary: Iterable[str]) -> str:
    """The corrected words with any near-miss of the user's own names spelled their way ("Max Pro"
    -> "Mac Pro" when Mac Pro is one of their machines)."""
    out = phrase
    for term in sorted({t for t in vocabulary if t}, key=len, reverse=True):
        if f" {norm(term)} " in f" {norm(out)} ":
            # Already the name: only spelled their way ("mac pro" -> "Mac Pro").
            out = re.sub(r"(?i)(?<![A-Za-z0-9])" + r"\W+".join(map(re.escape, norm(term).split())) + r"(?![A-Za-z0-9])",
                         lambda _m: term, out)
            continue
        span = find_span(out, term, SNAP_MATCH)
        if span is None or len(norm(span[2]).split()) != len(norm(term).split()):
            continue
        got, want = norm(span[2]).replace(" ", ""), norm(term).replace(" ", "")
        if got in want or want in got:
            continue  # "Mac" is not "iMac"
        words = out.split()
        out = " ".join(words[:span[0]] + [term] + words[span[1]:])
    return out


def _said(request: str) -> str:
    from .text import request_notes
    return request_notes(request)[0]


def detect(words: str, request: str | None, *, typed: bool = False, vocabulary: Iterable[str] = (),
           known_wrong: Iterable[str] = ()) -> Fix | None:
    """The correction `words` make to a task asked `request`, or None when they are not one."""
    if not words or not request:
        return None
    from .text import request_notes
    text, _ = request_notes(words)
    text = _clean(text)
    if not text or len(text.split()) > 16:
        return None
    vocab = list(vocabulary)
    known = [k for k in known_wrong if k]
    said = _said(request)
    led_no = bool(_LEAD_NO.match(text))
    relayed = _RELAYED.match(text)
    if relayed and relayed.group(0).strip().lower() not in {"means", "meant", "says", "said"}:
        text = text[relayed.end():].strip()
    body = _FILLER_LEAD.sub("", text).strip()
    if not body or _ANSWER.match(body):
        return None

    def fixed(wrong: str, right: str | None) -> Fix | None:
        right = _clean(right or "") or None
        if right is not None:
            if len(right.split()) > MAX_PHRASE_WORDS or _COMMAND.match(right) or not _content(right):
                return None
            right = snap(right, vocab)
            if norm(right) == norm(wrong):
                return None
        return Fix(wrong, right, rewrite(request, wrong, right) if right else request)

    def repeat(wrong: str) -> bool:
        """The words were corrected already and the request no longer has them as said."""
        exact = find_span(said, wrong, 0.95)
        return exact is None and mentions(wrong, known)

    for pattern in (_NOT_X_Y, _Y_NOT_X):
        m = pattern.match(body)
        if m and len(m.group("wrong").split()) <= MAX_PHRASE_WORDS:
            wrong = _clean(m.group("wrong"))
            if repeat(wrong):
                right = _clean(m.group("right"))
                return Fix(None, snap(right, vocab), request) if right and _content(right) else None
            span = find_span(said, wrong, WRONG_MATCH)
            if span is not None:
                return fixed(span[2], m.group("right"))
    m = _NO_SUCH.match(body) or _MISHEARD.match(body)
    if m and len(m.group("wrong").split()) <= MAX_PHRASE_WORDS:
        wrong = _clean(m.group("wrong"))
        if repeat(wrong):
            return Fix(None, None, request)  # said again after it was fixed: confirmed, not sent again
        span = find_span(said, wrong, WRONG_MATCH)
        return Fix(span[2], None, request) if span is not None else None
    m = _I_MEANT.match(body)
    right = m.group("right") if m else (body if (led_no or typed) else None)
    if right is None:
        return None
    right = _clean(right)
    limit = MAX_BARE_WORDS if not m and not led_no else MAX_PHRASE_WORDS - 2
    if not right or len(right.split()) > limit or _COMMAND.match(right) or not _content(right):
        return None
    snapped = snap(right, vocab)
    named = [t for t in vocab if _has(snapped, t)]
    if _has(said, snapped) or _has(said, right) or (named and all(_has(said, t) for t in named)):
        # Already what the task has: a repeat of a correction it took (confirmed, nothing sent again),
        # or, for a task never corrected, just more words.
        return Fix(None, snapped, request) if known else None
    need = RIGHT_MATCH if m else NO_MATCH if led_no else BARE_MATCH
    span = find_span(said, snapped, need) or find_span(said, right, need)
    if span is None:
        return None
    return fixed(span[2], right)
