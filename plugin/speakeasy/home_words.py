"""Home's words on real data: what a card says when the stored text was written for something else.

Real jobs carry titles written like internal handles ("ABC-12: Search panel — fixes, suites (continued)"),
status lines that are tool narration or three-clause worker updates, voice asks full of filler and
the call's own notes, near-duplicate jobs from one call, and pictures that are wide contact sheets
named ``sheet-light-v3.png``. Every function here is pure (no store, no network), so the Home builder
and any other surface can apply the same rules to whatever row it has:

- ``short_title``: at most ``TITLE_MAX`` characters of plain words, cut at a phrase boundary, never an
  ellipsis, never an issue number or a handle.
- ``status_line``: one short present-tense line from the latest real step; ``None`` once finished.
- ``clean_ask``: what the person said, without filler or the call's notes, numbers as digits.
- ``near_duplicate`` / ``fold_duplicates``: one card per thing a call asked for, however many jobs.
- ``picture_caption`` / ``hero_box``: a human caption (never a file name), and a readable crop of a
  wide contact sheet.
- ``fresh_steps``: next steps that are still true (no past times, nothing already decided).
- ``pill_text``: the tracker pill in a few words at any volume ("3 need you · 5 running").
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .text import _TITLE_UNFINISHED, fix_product_names, request_notes

TITLE_MAX = 32
STATUS_MAX = 40   # what one tracker/card status row shows on the smallest phone
PILL_MAX = 24

# -- titles --------------------------------------------------------------------------------------

# An issue/ticket number or handle anywhere ("ABC-123", "#45", "PR 12", "t_1a2b3c4d").
_HANDLE = re.compile(r"(?i)\b[A-Z]{2,6}-\d+\b:?|(?<!\w)#\d+\b|\bPR\s?\d+\b|\bt_[0-9a-f]{6,}\b")
# Run bookkeeping a title carries in brackets or as a tail: "(continued)", "— restart", "(v2)".
_BOOKKEEPING = re.compile(r"(?i)\s*(?:\((?:[^()]*\b(?:continu\w*|restart\w*|retry|again|take \d+|v\d+|part \d+|private|"
                          r"draft|wip)\b[^()]*)\)|[-\u2013\u2014]+\s*(?:continued|continuation|restart(?:ed)?|retry|again|"
                          r"private|wip|draft)\s*$)")
_PAREN = re.compile(r"\s*\([^()]*\)")
# Words a title may be cut before: what follows them is detail, not the job.
_CONNECTORS = {"for", "with", "to", "from", "after", "before", "via", "into", "on", "in", "across", "including",
               "covering", "using", "about", "because", "so", "that", "which", "and", "or", "vs", "versus",
               "through", "while", "when", "instead", "against", "by", "as", "at", "plus", "near"}
_HEAD_SPLIT = re.compile(r"\s*(?:\s[-\u2013\u2014]+\s|:\s)\s*")
# A head that is a code name, not a subject: "b70 debug", "ios", "wip", "r2 fix".
_CODE_HEAD = re.compile(r"(?i)^(?:[a-z]{1,3}\d+[a-z]?|wip|tmp|todo|ios|mac|plugin|server|qa|ci)(?:\s+\w+)?$")
_SELF_NAME = re.compile(r"(?i)\bSpeakeasy\s+")


def _cap(text: str) -> str:
    # "iPhone", "macOS", "eBay" keep their case.
    if not text or len(text) > 1 and text[0].islower() and text[1].isupper():
        return text
    return text[:1].upper() + text[1:]


def _words(text: str) -> list[str]:
    return text.split()


_ALSO_UNFINISHED = {"not", "no", "never", "without", "than", "very", "more", "most", "less", "being", "under", "over",
                    "between", "within", "after", "before", "around", "behind", "across", "against", "through",
                    "toward", "towards", "via", "vs", "versus", "including", "using", "plus", "per", "upon", "beside",
                    # a bare verb whose object was cut off ("Pictures in chat threads show")
                    "show", "shows", "appear", "appears", "display", "displays", "stay", "stays", "look", "looks"}


def _ok_end(words: list[str]) -> bool:
    if words and re.fullmatch(r"\d[\d.,]*", words[-1]):
        return True   # "build 70", "iOS 27": a number ends a phrase (a ticket number is gone before this)
    last = re.sub(r"[^a-z']", "", words[-1].lower()) if words else ""
    # a possessive waits for its noun ("Find out what time the user's")
    possessive = last.endswith("'s") or len(last) > 2 and last.endswith("s'")
    return bool(last) and not possessive and last not in _TITLE_UNFINISHED and last not in _ALSO_UNFINISHED


def _trim_end(text: str) -> str:
    """*text* without the words left dangling at its end ("Make a card for" once "DEMO-42" is out)."""
    words = _words(text)
    while len(words) > 1 and not _ok_end(words):
        words.pop()
    return " ".join(words).rstrip(" ,;:-\u2013\u2014")


# The ask's verb and a leading count, which a too-long title can lose and keep its subject:
# "Create and display an invented example Linear issue" -> "Invented example Linear issue".
_LEAD_ASK = re.compile(r"(?i)^(?:please\s+)?(?:(?:create|make|build|draft|write) and (?:display|show)|find out|look up|"
                       r"look for|search for|pull up|show me|give me|find|get|create|make|show|draft|write|build)\s+"
                       r"(?:(?:a|an|the|some|me|\d+|two|three|four|five|six|seven|eight|nine|ten|a few|few)\s+)?")
_TAIL_DETAIL = re.compile(r"(?i)\s+(?:online|today|tonight|tomorrow|now|nearby|quickly|again|for me)$")


def _fit(text: str, limit: int, phrase_only: bool = False) -> str | None:
    """*text* cut at the last phrase boundary that fits *limit* (else, unless *phrase_only*, the
    longest run of whole words that doesn't end mid-phrase), or None when no clean cut does."""
    text = text.strip(" ,;:.-\u2013\u2014")
    if len(text) <= limit:
        return text
    words = _words(text)
    best = None
    # Phrase boundaries first: before a connector word or after a comma.
    for n in range(1, len(words)):
        head = words[:n]
        nxt = re.sub(r"[^a-z]", "", words[n].lower())
        # "… in light | and dark" splits a pair: cut before the preposition instead.
        split_pair = nxt in {"and", "or"} and len(head) >= 2 and head[-2].lower() in _CONNECTORS
        if (nxt in _CONNECTORS or head[-1].endswith(",")) and len(head) >= 2 and not split_pair:
            cut = " ".join(head).rstrip(",;:")
            if len(cut) <= limit and _ok_end(cut.split()):
                best = cut
    if best or phrase_only:
        return best
    # Otherwise the longest run of whole words that does not end mid-phrase.
    for n in range(len(words) - 1, 1, -1):
        cut = " ".join(words[:n]).rstrip(",;:")
        if len(cut) <= limit and _ok_end(cut.split()):
            return cut
    return None


def short_title(title: Any, limit: int = TITLE_MAX) -> str:
    """A job's name as Home shows it: plain words, at most *limit* characters, cut where a phrase
    ends (never "…"), with issue numbers, handles and run bookkeeping taken out.

    "ABC-12: Search panel — real-model checks, fixes, suites" -> "Search panel";
    "r9 debug: tablet app walkthrough vs 8 — triage, fix" -> "Tablet app walkthrough"."""
    text = " ".join(str(title or "").split()).strip(" \"'\u201c\u201d")
    if not text:
        return ""
    text = _BOOKKEEPING.sub("", text)
    kept = _PAREN.sub("", text)
    text = _PAREN.sub("", _HANDLE.sub("", text))
    text = _trim_end(re.sub(r"\s{2,}", " ", text).strip(" ,;:-\u2013\u2014"))
    short = _words(text)
    if len(_words(kept)) > len(short) and (len(short) < 2 or len(short) <= 3 and short[-1].lower() in
                                           ("on", "at", "of", "in", "for", "to", "with", "about", "from")):
        # The number was the subject ("CI on PR 147753" left "CI on", then "CI"): keep it.
        text = _trim_end(re.sub(r"\s{2,}", " ", kept).strip(" ,;:-\u2013\u2014"))
    parts = [p for p in _HEAD_SPLIT.split(text) if p.strip()]
    if not parts:
        return ""
    head, rest = parts[0], parts[1:]
    if rest and (_CODE_HEAD.match(head) or len(_words(head)) < 2 and len(head) < 8):
        head, rest = rest[0], rest[1:]   # "b70 debug: <the job>": the job is after the code name
    head = fix_product_names(head)
    if len(head) <= limit - 6 and rest and len(_words(head)) <= 3:
        # "Home v7: every work use case" says more than "Home v7"; the detail is cut only where a
        # phrase ends ("Video attachments: send clips", never "Home coordinator: real-model").
        for lead in dict.fromkeys((head, _SELF_NAME.sub("", head))):
            for tail in dict.fromkeys((rest[0], _SELF_NAME.sub("", rest[0]))):
                tail = _fit(tail, limit - len(lead) - 2, phrase_only=True)
                if tail and len(tail) >= 6:
                    return _cap(f"{lead}: {tail}")
    variants = (head, _SELF_NAME.sub("", head))
    for phrase_only in (True, False):     # a clean phrase without "Speakeasy" beats a cut one with it
        for candidate in variants:
            cut = _fit(candidate, limit, phrase_only=phrase_only)
            if cut and (len(_words(cut)) >= 2 or len(_words(candidate)) == 1):
                if phrase_only and len(candidate) > limit:
                    # "Get the weather forecast | for Chicago": a phrase cut that spends its room on
                    # the ask verb loses the subject's place; without the verb, "Weather forecast for
                    # Chicago" fits. Take that when it keeps more of the subject.
                    stripped = _LEAD_ASK.sub("", candidate, count=1)
                    lead_n = len(_words(candidate)) - len(_words(stripped))
                    subject = _trim_end(_TAIL_DETAIL.sub("", stripped))
                    if lead_n and subject != candidate:
                        whole = subject if len(subject) <= limit else _fit(subject, limit, phrase_only=True)
                        if (whole and len(_words(whole)) >= 2 and _ok_end(_words(whole))
                                and len(_words(whole)) >= len(_words(cut)) - lead_n):
                            return _cap(whole)
                if not phrase_only and len(candidate) > limit:
                    # A cut mid-subject ("Create and display an invented") reads worse than the
                    # subject without its ask verb, when that subject fits whole in three words or more.
                    subject = _TAIL_DETAIL.sub("", _LEAD_ASK.sub("", candidate, count=1))
                    if subject != candidate:
                        whole = subject if len(subject) <= limit else _fit(subject, limit, phrase_only=True)
                        if whole and len(_words(whole)) >= 3 and _ok_end(_words(whole)):
                            return _cap(whole)
                return _cap(cut)
    words, out = _words(head), []
    for w in words:
        if len(" ".join(out + [w])) > limit:
            break
        out.append(w)
    return _cap(" ".join(out or [head[:limit]]))


# -- status lines --------------------------------------------------------------------------------

# A run's bookkeeping before the real step: "Resuming after an earlier run died: reviewing …".
_META_LEAD = re.compile(r"(?i)^(?:resum\w*|restart\w*|continu\w*|picking (?:it )?(?:back )?up|back)\b[^:\u2014;]*[:\u2014;]\s*")
_CLAUSE = re.compile(r"\s*(?:;|\s[-\u2013\u2014]\s|,\s(?=(?:and|then|now|next|waiting|so)\b)|\.\s+|:\s)\s*")
_ING = re.compile(r"^[A-Za-z]+ing\b")
_PAST_LEAD = re.compile(r"(?i)^(?:fixed|finished|done|built|ran|passed|verified|posted|sent|wrote|added|made|found|"
                        r"shipped|merged|committed|completed|checked|tested)\b")
# Tool narration that is never a status: commands, paths, flags, function calls, "+ 3 commands".
_RAW = re.compile(r"[`$|<>{}\[\]\\=]|(?<!\w)/\w|\+ ?\d+ (?:more )?(?:commands?|calls?|tools?)\b|(?:^|\s)--?[A-Za-z]"
                  r"|\b[a-z]+_[a-z_]+\b|\w\(|\b(?:sudo|bash|zsh|curl|ssh|pkill|xcodebuild|pytest|npm|pip3?|git)\b"
                  r"|^(?:Running|Calling|Executing|Using tool)\b")


def status_line(line: Any, finished: bool = False, limit: int = STATUS_MAX) -> str | None:
    """One short present-tense line from a job's latest real step, or None.

    Finished work has no status line (its result speaks; a stale "Resuming…" must not survive the
    finish). Tool narration is never a line. A multi-clause update keeps its present-tense clause:
    "Fixed on the branch with a test; waiting for the release step to pick it up." ->
    "Waiting for the release step"."""
    if finished:
        return None
    text = " ".join(str(line or "").split()).strip()
    if not text or _RAW.search(text):
        return None
    text = _META_LEAD.sub("", text)
    clauses = [c.strip(" ,.;:") for c in _CLAUSE.split(text) if c and c.strip(" ,.;:")]
    if not clauses:
        return None
    pick = next((c for c in clauses if _ING.match(c)), None)
    if pick is None:
        pick = clauses[0]
        if _PAST_LEAD.match(pick) and len(pick) > limit:
            # A long report of what already happened: its first phrase, or nothing.
            return _fit(_cap(pick), limit, phrase_only=True)
    pick = _cap(pick)
    cut = _fit(pick, limit) if len(pick) > limit else pick
    return cut or None


# -- what the person asked -----------------------------------------------------------------------

_FILLER_LEAD = re.compile(r"(?i)^[\s:;,.-]*(?:(?:got it|so|okay|ok|alright|all right|yeah|yes|yep|um+|uh+|er+|hmm+|oh|hey|yo|cool|"
                          r"right|well|also|and|like|actually|listen|look|anyway|basically)\b[\s,.!-]*)+")
_FILLER_MID = re.compile(r"(?i)(?:,\s*|\s)(?:um+|uh+|er+|hmm+|you know|i mean|like)(?=[\s,.!?])(?:,(?=\s))?")
_STUTTER = re.compile(r"(?i)\b((?:[\w']+\s+){0,3}[\w']+)(?:[\s,]+\1\b)+")
_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_NUM_WORD = "|".join(sorted({*_ONES, *_TENS}, key=len, reverse=True))
_NUMBER = re.compile(rf"(?i)\b((?:{'|'.join(_TENS)})(?:[\s-](?:{'|'.join(k for k in _ONES if 0 < _ONES[k] < 10)}))?"
                     rf"|(?:{_NUM_WORD}))(?:\s+(hundred|thousand))?\b")
# "one" stays a word unless a unit or count follows it: "the one on the left", "one more thing".
_COUNTED = re.compile(r"(?i)^\s+(?:[a-z]+s\b|percent|dollars?|hours?|minutes?|seconds?|days?|weeks?|months?|years?|"
                      r"am\b|pm\b|o'clock|times?|degrees?|miles?|gigs?|gb|tb|mb|k\b|x\b)")


def digits(text: str) -> str:
    """Spelled-out numbers as digits ("three studios" -> "3 studios", "twenty five" -> "25"). A lone
    "one" stays a word unless a unit or plural follows it ("one more thing" stays)."""
    def swap(m: re.Match[str]) -> str:
        word, scale = m.group(1), m.group(2)
        parts = re.split(r"[\s-]", word.lower())
        value = _TENS.get(parts[0], _ONES.get(parts[0], 0)) + (_ONES.get(parts[1], 0) if len(parts) > 1 else 0)
        if scale:
            value *= 100 if scale.lower() == "hundred" else 1000
        after = m.string[m.end():]
        if parts == ["one"] and not scale and not _COUNTED.match(after):
            return m.group(0)
        if value == 1 and re.match(r"(?i)^\s+(?:more|of|another|day|thing|other|by)\b", after):
            return m.group(0)
        if parts == ["zero"] and not _COUNTED.match(after):
            return m.group(0)
        return f"{value:,}" if value >= 10000 else str(value)
    return _NUMBER.sub(swap, text)


def clean_ask(text: Any, assistant: str | None = None) -> str:
    """What the person asked, as their thread shows it: without the call's appended notes ("(… is
    looking at: …)", "(following up: …)"), wrapping quotes, leading filler, mid-sentence ums and
    stutters, or the assistant's name used as a greeting; spelled-out numbers as digits. Nothing
    is reworded beyond that."""
    said, _notes = request_notes(str(text or ""))
    said = said.strip().lstrip(":;,.-\u2013\u2014 ").strip("\"\u201c\u201d").strip()
    if assistant:
        said = re.sub(rf"(?i)^(?:hey|hi|ok|okay|yo)?[\s,]*{re.escape(assistant)}\b[\s,.!-]*", "", said)
    said = _FILLER_LEAD.sub("", said)
    said = _FILLER_MID.sub("", said)
    said = _STUTTER.sub(lambda m: m.group(1), said)
    # "have a, like, some sort of job" -> without the filler, "a some" is a restart: keep the second word.
    said = re.sub(r"(?i)\b(?:a|an|the)\s+(?=(?:a|an|the|some|any|this|that|these|those|my|your|our)\b)", "", said)
    said = re.sub(r"\s+([,.!?])", r"\1", said)
    said = re.sub(r",\s*,", ",", said)
    said = re.sub(r"\s{2,}", " ", said).strip(" ,")
    said = digits(said)
    if not said:   # "hey" on its own is the whole message, not filler
        said, _ = request_notes(str(text or ""))
        said = said.strip().strip("\"\u201c\u201d").strip()
    return _cap(fix_product_names(said))


# -- one call, one card per thing asked ---------------------------------------------------------

_STOP = set("""a an the and or but so to of for in on at with from into my our your this that these those it its
is are be do does did can could would should will please now just also some any all both then than via
fix check find look make get show add update see not no""".split())


def _key_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return {w[:-1] if len(w) > 4 and w.endswith("s") else w for w in words if w not in _STOP}


def near_duplicate(a: Any, b: Any, threshold: float = 0.6) -> bool:
    """Two job titles (or asks) that name the same thing: most of their subject words are shared
    ("Fix picture sending and carousels" / "Fix picture sending and the carousel")."""
    wa, wb = _key_words(a), _key_words(b)
    if not wa or not wb:
        return False
    small = min(len(wa), len(wb))
    shared = len(wa & wb)
    return small >= 2 and shared / small >= threshold and shared / len(wa | wb) >= threshold * 0.7


def fold_duplicates(items: list[dict[str, Any]], *, key: str = "title",
                    group: str | None = "call_key") -> list[dict[str, Any]]:
    """Keep the first of each run of near-duplicate items (same *group* value when given) and count
    the rest on it as ``also`` (their ids in ``also_ids``). Order is kept."""
    out: list[dict[str, Any]] = []
    for item in items:
        same = next((o for o in out if (group is None or o.get(group) == item.get(group))
                     and (group is None or item.get(group) is not None)
                     and near_duplicate(o.get(key), item.get(key))), None)
        if same is None:
            out.append(item)
            continue
        same["also"] = int(same.get("also") or 0) + 1
        same.setdefault("also_ids", []).append(item.get("id"))
    return out


def collapse(things: list[Any], keep: int = 3) -> tuple[list[Any], int]:
    """A list shown in full up to *keep* + 1 rows; longer, the first *keep* and "N more"."""
    if len(things) <= keep + 1:
        return list(things), 0
    return list(things[:keep]), len(things) - keep


# -- pictures ------------------------------------------------------------------------------------

_FILE_NAME = re.compile(r"(?i)^[\w.~-]*\.(?:png|jpe?g|gif|webp|heic|tiff?|bmp|pdf|mov|mp4)$|^(?:img|image|screenshot|"
                        r"screen ?shot|photo|shot|untitled|frame)[\s_-]*\d*$|[\\/]|^[\w-]*_[\w-]*$|^[\w]+(?:-[\w]+){2,}$")


def picture_caption(*candidates: Any) -> str | None:
    """The first candidate that reads as words a person wrote (a card's caption, its name, the
    option it shows): never a file name, path, or slug. None when there is no such text: no caption
    beats "sheet-light-v3.png"."""
    for raw in candidates:
        text = " ".join(str(raw or "").split()).strip(" \"'")
        if not text or _FILE_NAME.search(text) or len(_words(text)) == 1 and len(text) > 14:
            continue
        return _cap(text[:80])
    return None


WIDE = 1.9          # wider than this (w/h) is a contact sheet a portrait tile can't show whole
HERO_ASPECT = 0.8   # the readable crop: a little taller than wide, like the tiles that show it


def hero_box(width: int, height: int, energy: list[float] | None = None) -> tuple[int, int, int, int] | None:
    """For a wide picture, the crop (left, top, width, height) a portrait tile shows readably: full
    height, ``HERO_ASPECT`` wide, placed on the busiest columns (*energy*: one detail value per
    column, e.g. edge strength) or at the left (where a sheet's first frame is). None when the
    picture is not wide enough to need one."""
    if width <= 0 or height <= 0 or width / height <= WIDE:
        return None
    w = max(1, min(width, round(height * HERO_ASPECT)))
    left = 0
    if energy and len(energy) == width:
        run = sum(energy[:w])
        best = run
        for x in range(1, width - w + 1):
            run += energy[x + w - 1] - energy[x - 1]
            if run > best * 1.15:       # only move off the first frame for clearly more detail
                best, left = run, x
    return left, 0, w, height


def hero_crop(data: bytes) -> tuple[bytes, str] | None:
    """The hero crop of a wide picture as JPEG, or None (not wide, not decodable, no Pillow)."""
    try:
        from io import BytesIO
        from PIL import Image, ImageFilter, ImageOps
    except Exception:
        return None
    try:
        img = ImageOps.exif_transpose(Image.open(BytesIO(data)))
        img.load()
    except Exception:
        return None
    box = hero_box(*img.size)
    if box is None:
        return None
    small = img.convert("L").resize((max(1, img.width // 8), max(1, img.height // 8)))
    edges = small.filter(ImageFilter.FIND_EDGES)
    cols = [0.0] * small.width
    px = edges.load()
    for x in range(small.width):
        cols[x] = float(sum(px[x, y] for y in range(small.height)))  # type: ignore[index,misc]
    energy = [cols[min(len(cols) - 1, x // 8)] for x in range(img.width)]
    left, top, w, h = hero_box(img.width, img.height, energy) or box
    out = img.convert("RGB").crop((left, top, left + w, top + h))
    if out.height > 1600:
        out = out.resize((round(out.width * 1600 / out.height), 1600))
    buf = BytesIO()
    out.save(buf, "JPEG", quality=85)
    return buf.getvalue(), "image/jpeg"


# -- staleness -----------------------------------------------------------------------------------

def fresh_steps(steps: Iterable[dict[str, Any]], now: float, decided: set[str] | None = None) -> list[dict[str, Any]]:
    """Next steps that are still true: drops one whose time (``due_at`` / ``at``) has passed, one
    already decided (its ``id`` or ``decides`` in *decided*, or ``done``/``decided`` set), and a
    repeat of a step already kept."""
    decided = decided or set()
    out: list[dict[str, Any]] = []
    for s in steps:
        at = s.get("due_at") if s.get("due_at") is not None else s.get("at")
        if isinstance(at, (int, float)) and at < now:
            continue
        if s.get("done") or s.get("decided") or s.get("id") in decided or s.get("decides") in decided:
            continue
        if any(near_duplicate(s.get("title"), o.get("title"), 0.7)
               or " ".join(str(s.get("title") or "").lower().split()) == " ".join(str(o.get("title") or "").lower().split())
               for o in out):
            continue
        out.append(s)
    return out


_ISO_WHEN = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{1,2}):(\d{2}))?")


def _when_ts(when: Any, tz: Any = None) -> float | None:
    """An absolute time from a step's ``when`` ("2026-11-07", "2026-11-07T19:00"), else None:
    relative words ("Tonight", "7 PM") say nothing about which day, so they never expire a step."""
    import datetime as dt
    m = _ISO_WHEN.match(str(when or "").strip())
    if not m:
        return None
    try:
        day = dt.date.fromisoformat(m.group(1))
        if m.group(2) is None:   # a whole day is past only once it has ended
            at = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 0), tzinfo=tz)
        else:
            at = dt.datetime.combine(day, dt.time(int(m.group(2)), int(m.group(3))), tzinfo=tz)
    except ValueError:
        return None
    return at.timestamp()


def expire_steps(views: Any, finished: bool, now: float, tz: Any = None) -> Any:
    """A stored result's timeline as it reads *now*: a finished job has no "now" step, and a "next"
    step whose absolute time has passed is no longer next (both keep their row, without the state).
    Other views pass through; the view count never changes."""
    if not isinstance(views, list):
        return views
    out = []
    for view in views:
        if not (isinstance(view, dict) and view.get("kind") == "timeline" and isinstance(view.get("days"), list)):
            out.append(view)
            continue
        days = []
        for day in view["days"]:
            items = []
            for it in (day.get("items") or []) if isinstance(day, dict) else []:
                state = it.get("state") if isinstance(it, dict) else None
                stale = (state == "now" and finished) or (
                    state == "next" and (lambda t: t is not None and t < now)(_when_ts(it.get("when"), tz)))
                items.append({k: v for k, v in it.items() if k != "state"} if stale else it)
            days.append({**day, "items": items} if isinstance(day, dict) else day)
        out.append({**view, "days": days})
    return out


def replaced_questions(questions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{id of a superseded question: {"by", "at"}}: an unanswered question is replaced when a newer
    question in the same thread (``thread``) was asked after it. *questions*: dicts with ``id``,
    ``thread``, ``asked_at`` and ``answered``."""
    out: dict[str, dict[str, Any]] = {}
    by_thread: dict[Any, list[dict[str, Any]]] = {}
    for q in questions:
        if q.get("thread") is not None:
            by_thread.setdefault(q["thread"], []).append(q)
    for group in by_thread.values():
        group.sort(key=lambda q: float(q.get("asked_at") or 0))
        for older, newer in zip(group, group[1:]):
            if not older.get("answered") and float(newer.get("asked_at") or 0) > float(older.get("asked_at") or 0):
                out[older["id"]] = {"by": newer["id"], "at": float(newer.get("asked_at") or 0)}
    return out


# -- tracker pill --------------------------------------------------------------------------------

def pill_text(needs: int, running: int, *, one_title: str | None = None, limit: int = PILL_MAX) -> str:
    """The tracker pill's words at any volume: counts, never a title cut mid-way. A single thing
    waiting on you names itself only when its short title fits."""
    if needs == 1 and running == 0 and one_title:
        t = short_title(one_title, limit)
        if t and len(t) <= limit:
            return t
    parts = []
    if needs:
        parts.append(f"{needs} need{'s' if needs == 1 else ''} you")
    if running:
        parts.append(f"{running} running")
    text = " \u00b7 ".join(parts) or "All quiet"
    return text if len(text) <= limit else parts[0]
