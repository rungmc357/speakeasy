"""Display/speech sanitizers and the final-answer splitter.

Everything a client or the voice model sees from a Hermes run passes through here: bounded,
secret-looking text rejected or redacted, server-side paths never exposed.
"""
from __future__ import annotations

import ipaddress
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

from .cards import LOCAL_IMAGE_EXTS, ImageRejected, vetted_image_url, vetted_local_image
from .runstate import SETTLED, TERMINAL  # noqa: F401  (the run states live in runstate.py)

MAX_TRANSCRIPT = 64 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_CARDS = 8
MAX_RESULT_FULL = 8000
AUTHORED_PRECEDENCE_S = 20.0

# Provider non-speech annotations such as "[clear throat]", "[cough]", "[laughs]".
NON_SPEECH_TAG_RE = re.compile(r"\[[A-Za-z][A-Za-z '_-]{0,39}\]")
UNCLOSED_TAG_RE = re.compile(r"\[(?:[A-Za-z][A-Za-z '_-]{0,39})?$")

SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(?:authorization\s*:|bearer\s+|api[_ -]?key\s*[=:]|password\s*[=:]|"
    r"secret\s*[=:]|token\s*[=:]|op:/{2}|sk-[A-Za-z0-9_-]{8,})"
)
GENERIC_SHORT_STATUSES = {
    "request received", "started working", "working on it",
    "using a tool", "searching the web", "running a command", "reading a file",
    "reading a page", "searching files", "delegating a task", "processing request",
}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
HOME_PATH_RE = re.compile(r"(?:~/|/Users/|/home/|/root/|/private/var/)")
URL_SECRET_RE = re.compile(
    r"(?i)[?&#;][^=&\s#]*(?:token|key|sig|auth|code|session|secret|passw|credential)[^=&\s#]*="
)
OPAQUE_RE = re.compile(r"[A-Za-z0-9_+=]{32,}|\b[0-9a-fA-F]{24,}\b")
SECRET_FILE_RE = re.compile(r"(?i)(?:\.env\b|id_rsa|id_ed25519|\.pem\b|device-token|credentials|\.netrc|keychain)")
URL_RE = re.compile(r"https?://[^\s,'\"<>]+", re.IGNORECASE)


def clean_transcript(text: str) -> str:
    """Drop bracketed non-speech tags; keep every real word (mirrors the app)."""
    if "[" not in text:
        return text
    out = NON_SPEECH_TAG_RE.sub(" ", text)
    out = UNCLOSED_TAG_RE.sub("", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r" +([,.!?;:])", r"\1", out)
    return out.strip()


# A sentence the user needs to hear even when the answer is cut short: something broke, failed,
# changed, needs them, or is a heads-up. Without a SPOKEN line (tasks in a chat thread) the voice
# used to read only the first two sentences and drop a "the helper is broken" in sentence five.
HEADS_UP_RE = re.compile(
    r"(?i)\b(?:broke|broken|fail(?:s|ed|ing)?|error|couldn't|could not|can't|cannot|unable|didn't work|"
    r"not working|stopped working|missing|expired|heads up|one more thing|note that|warning|careful|"
    r"you(?:'ll)? (?:need|have) to|needs? your|waiting (?:for|on) you|approve|confirm|first thing to check|"
    r"may (?:have )?(?:changed|broken)|instead)\b")


# A sentence that leans on one before it ("Its one repeat error...", "That ended...") makes no sense
# read alone, so the spoken summary never lifts it out of its paragraph.
LEANS_BACK_RE = re.compile(
    r"(?i)^(?:it|its|it's|this|that|these|those|they|their|them|he|his|she|her|also|but|and|so|then|which|"
    r"if|or|otherwise|here|there's|both|either|neither|the other|another|same)\b")
# Soft heads-up words ("instead", "note that") only count in the opening paragraph; further down they
# are usually side notes, not warnings.
STRONG_HEADS_UP_RE = re.compile(
    r"(?i)\b(?:broke|broken|failed|failing|couldn't|could not|unable|didn't work|not working|"
    r"stopped working|expired|heads up|warning|needs? your|waiting (?:for|on) you|approve|confirm)\b")


def _sentences(full: str) -> list[tuple[int, str]]:
    """Plain sentences with the paragraph each sits in. Bullets and markdown read as plain sentences;
    a blank line or a heading starts a new paragraph."""
    text = re.sub(r"https?://\S*[^\s.,;:!?)]|[`*_>]+", "", full)
    out, para, gap = [], 0, False
    for raw in text.splitlines():
        line = re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", raw).strip()
        if not line or line.startswith("#") or line.endswith(":"):
            gap = True
            line = line.lstrip("#").strip() if line.startswith("#") else ""
            if not line:
                continue
        if gap and out:
            para += 1
        gap = False
        for x in re.split(r"(?<=[.!?])\s+", line):
            x = x.strip()
            if x:
                out.append((para, x if re.search(r"[.!?]$", x) else x + "."))
    return out


def spoken_from(full: str, limit: int = 3) -> str:
    """What to say aloud when the task gave no SPOKEN line: the opening sentence, then the first
    sentences that carry a problem or a heads-up, so a warning is never cut off. A heads-up further
    down is only picked when it stands on its own, and no sentence is said twice."""
    sentences = _sentences(full)
    if not sentences:
        return ""
    picked, seen = [0], {sentences[0][1].lower()}
    for i, (para, x) in enumerate(sentences[1:], 1):
        if len(picked) >= limit:
            break
        if x.lower() in seen:
            continue
        lead = para == 0
        if (HEADS_UP_RE.search(x) if lead else STRONG_HEADS_UP_RE.search(x) and not LEANS_BACK_RE.match(x)):
            picked.append(i)
            seen.add(x.lower())
    if len(picked) == 1 and len(sentences) > 1 and sentences[1][1].lower() not in seen:
        picked.append(1)
    out = " ".join(sentences[i][1] for i in sorted(picked))
    return re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s+", " ", out)).strip()


def said_once(text: str) -> str:
    """The reply once, when it arrived as the same text twice in a row (a conversation turn's final
    message has come back with its body repeated); anything else is returned unchanged."""
    body = text.strip()
    if len(body) < 80:
        return text
    head = body[:60]
    at = body.find(head, 1)
    while at != -1:
        if body[:at].strip() == body[at:].strip():
            return body[:at].strip()
        at = body.find(head, at + 1)
    return text


def safe_user_text(value: Any, limit: int = 500) -> str | None:
    """Bounded display copy, rejecting likely secrets and machine payloads."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if not text or SENSITIVE_TEXT_RE.search(text) or "```" in text:
        return None
    return text[:limit]


def valid_short_status(value: Any) -> str | None:
    text = safe_user_text(value, 80)
    if not text or text.lower().rstrip(".!?") in GENERIC_SHORT_STATUSES:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &'’/.-]*", text):
        return None
    words = re.findall(r"[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*", text)
    if not 2 <= len(words) <= 6:
        return None
    # Active status copy is deliberately narrow: the run authors a present-tense action
    # rather than the server guessing one from a tool.
    if not words[0].lower().endswith("ing"):
        return None
    return text.rstrip(".!?")


def thread_commentary(text: Any) -> str | None:
    """What a thread task wrote while it kept working, as one display line: its first plain
    sentence or two, without the "Voice:" echo line, markdown, commands or anything secret-looking."""
    if not isinstance(text, str):
        return None
    lines = [line.strip() for line in text.splitlines()
             if line.strip() and not re.match(r"(?i)^(?:voice|status|detail|done|spoken):", line.strip())
             and not line.strip().startswith("```")]
    body = re.sub(r"[*_`#>]+", "", " ".join(lines))
    body = re.sub(r"\s+", " ", body).strip()
    if not body:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", body)
    line = " ".join(sentences[:2]) if len(sentences[0]) < 60 else sentences[0]
    return safe_user_text(line[:240], 240)


def interim_progress(event: dict[str, Any]) -> tuple[str, str] | None:
    """Parse only explicitly user-facing STATUS/DETAIL commentary; never summarize tools."""
    short_status = event.get("short_status")
    detail = event.get("detail")
    text = event.get("text")
    if not isinstance(short_status, str) and isinstance(text, str):
        status_match = re.search(r"(?im)^[ \t]*STATUS:[ \t]*(?P<status>[^\r\n]+?)[ \t]*$", text)
        detail_match = re.search(r"(?im)^[ \t]*DETAIL:[ \t]*(?P<detail>[^\r\n]+?)[ \t]*$", text)
        if status_match:
            short_status = status_match.group("status")
            detail = detail_match.group("detail") if detail_match else short_status
        elif valid_short_status(text):
            short_status, detail = text, text
    safe_status = valid_short_status(short_status)
    safe_detail = safe_user_text(detail, 500)
    if not safe_status or not safe_detail:
        return None
    return safe_status, safe_detail


def _preview_is_unsafe(preview: str) -> bool:
    return bool(SENSITIVE_TEXT_RE.search(preview) or EMAIL_RE.search(preview)
                or URL_SECRET_RE.search(preview) or OPAQUE_RE.search(preview)
                or SECRET_FILE_RE.search(preview) or "```" in preview)


def derive_tool_status(tool: Any, preview: Any) -> tuple[str, str] | None:
    """Honest status from an allowlisted tool's redacted argument preview; None otherwise."""
    if not isinstance(tool, str):
        return None
    text = re.sub(r"\s+", " ", preview).strip() if isinstance(preview, str) else ""
    if text and _preview_is_unsafe(text):
        return None
    if tool in {"read_file", "search_files"}:
        return "Checking project files", "Reading files in the project."
    if tool == "chat_history_lookup":
        detail = safe_user_text(text, 200) if text and not HOME_PATH_RE.search(text) else None
        return "Checking past conversations", detail or "Looking through earlier conversations."
    if not text or HOME_PATH_RE.search(text):
        return None
    if tool == "web_search":
        words = re.findall(r"[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*", text)
        short = valid_short_status("Searching " + " ".join(words[:5])) if words else None
        detail = safe_user_text(text, 200)
        return (short, f"Searching for: {detail}") if short and detail else None
    if tool in {"web_extract", "browser_navigate"}:
        match = URL_RE.search(text)
        if not match:
            return None
        parsed = urllib.parse.urlsplit(match.group(0))
        host = (parsed.hostname or "").lower()
        host = host[4:] if host.startswith("www.") else host
        if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
            return None
        short = valid_short_status(f"Reading {host}")
        detail = safe_user_text(f"Reading {host}{parsed.path}"[:200], 200)
        return (short, detail) if short and detail else None
    return None


# A command someone must paste into a terminal. Fenced blocks count whole; a bare line counts when it
# starts with a shell command or chains/expands like one, and doesn't read as a sentence.
SHELL_START_RE = re.compile(
    r"^(?:\$ )?(?:sudo|brew|install|printf|tee|launchctl|mkdir|chmod|chown|curl|wget|ssh|scp|cd|git|gh|npm|npx|pnpm|yarn|pip3?|"
    r"python3?|uv|hermes|defaults|open|killall|export|echo|cat|cp|mv|rm|ln|tailscale|docker|colima|xcode-select|"
    r"softwareupdate|networksetup|security|op|systemctl|launchd|pmset|codesign|xattr|diskutil)\b[^\n]*$")
SHELL_MARK_RE = re.compile(r"&&|\|\||\$\(|>>|\s\|\s|~/|--[a-z]")
FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)\n?```", re.S)


def looks_like_command(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 1500:
        return False
    if SHELL_START_RE.match(line):
        # "Open Terminal on the laptop." is a sentence, not a command.
        return not (line.endswith(".") and not SHELL_MARK_RE.search(line)) or bool(SHELL_MARK_RE.search(line))
    return False


def split_commands(text: str) -> list[tuple[str, str]]:
    """Split an answer into ("text", …) and ("command", …) pieces in reading order, so each command
    can go out as its own message: long-press → Copy on a phone then copies only the command."""
    pieces: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush() -> None:
        chunk = "\n".join(buf).strip()
        if chunk:
            pieces.append(("text", chunk))
        buf.clear()

    pos = 0
    for m in FENCE_RE.finditer(text):
        for line in text[pos:m.start()].split("\n"):
            if looks_like_command(line):
                flush(); pieces.append(("command", line.strip().removeprefix("$ ")))
            else:
                buf.append(line)
        body = m.group(1).strip("\n")
        if body.strip():
            flush(); pieces.append(("command", body))
        pos = m.end()
    for line in text[pos:].split("\n"):
        if looks_like_command(line):
            flush(); pieces.append(("command", line.strip().removeprefix("$ ")))
        else:
            buf.append(line)
    flush()
    return pieces


def without_commands(text: str, placeholder: str = "(a command, shown in the app and in chat)") -> str:
    """The answer with every shell command swapped for a placeholder: what the voice may know about it.
    Other code (a function it fixed) stays, fences unwrapped: the voice answers questions about it."""
    out = []
    for kind, chunk in split_commands(text):
        is_shell = kind == "command" and all(looks_like_command(ln) or not ln.strip() for ln in chunk.split("\n"))
        out.append(placeholder if is_shell else chunk)
    return "\n".join(out)


def safe_full_text(value: Any) -> str | None:
    """Multi-line display copy: keep paragraphs, redact secret-looking lines, bound size."""
    if not isinstance(value, str):
        return None
    lines = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", line).rstrip()
        lines.append("[redacted]" if SENSITIVE_TEXT_RE.search(line) else line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:MAX_RESULT_FULL] or None


# Listening mode: what the Mac transcribed in the room before a call, as "[HH:MM] text" lines.
MAX_ROOM_CHARS = 24_000  # Unicode code points, as sent (about 6,000 tokens)
ROOM_STAMP_RE = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")
# A secret said out loud has no "password:" syntax: "the password is hunter2", "my PIN's 4412".
SPOKEN_SECRET_RE = re.compile(
    r"(?i)\b(?:pass(?:word|code|phrase)|pin(?:\s+(?:code|number))?|api\s+key|secret\s+key|"
    r"access\s+(?:code|key|token)|security\s+code|token|cvv|cvc|(?:card|account|routing|social\s+security)\s+number|ssn)"
    r"(?:\s*['’]s\b|\s*[:,=]|\s+(?:is|was|will\s+be|should\s+be)\b)"
    # "use password hunter2", "enter the pin 4321"
    r"|(?i:\b(?:use|try|enter|type)\s+(?:the\s+|my\s+|our\s+)?(?:pass(?:word|code)|pin)\s+\S)")
# Numbers that are secrets on their own, said out loud: a card-length run of digits ("4111 1111 1111
# 1111"; phone numbers, dates and [HH:MM] stamps are too short), a US social security number, and a
# card's security code.
SPOKEN_NUMBER_SECRET_RE = re.compile(
    r"(?<![\d:])(?:\d[ -]?){12,18}\d(?![\d:])"
    r"|\b\d{3}-\d{2}-\d{4}\b"
    r"|(?i:\b(?:cvv|cvc|security\s+code)\W{0,3}\d{3,4}\b)")


def safe_room_text(value: str) -> str:
    """Listening mode's room transcript, made safe to hand to the voice and Hermes.

    Like ``safe_full_text``: control characters go, and a line that looks like it holds a secret
    (a key, "password: …", a password, PIN or card number said out loud, a card-length number or a
    social security number) becomes "[redacted]"
    behind its time stamp. A long opaque token anywhere in a line is redacted on its own. Never
    shortened here: MAX_RESULT_FULL would cut a full room transcript to a third; the caller checks
    MAX_ROOM_CHARS instead."""
    lines = []
    for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", line).rstrip()
        if SENSITIVE_TEXT_RE.search(line) or SPOKEN_SECRET_RE.search(line) or SPOKEN_NUMBER_SECRET_RE.search(line):
            stamp = ROOM_STAMP_RE.match(line)
            line = (stamp.group(0) if stamp else "") + "[redacted]"
        else:
            line = OPAQUE_RE.sub("[redacted]", line)
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def valid_done_label(value: Any) -> str | None:
    """Short past-tense label for the panel's one-line "Done · <label>" status."""
    text = safe_user_text(value, 40)
    if not text or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 &'’/.-]*", text):
        return None
    words = re.findall(r"[A-Za-z0-9]+(?:['’.-][A-Za-z0-9]+)*", text)
    if not 1 <= len(words) <= 5:
        return None
    return text.rstrip(".!?")


def _public_host(url: str, scheme_re: str) -> bool:
    if not re.fullmatch(scheme_re + r"[^\s<>]{4,1500}", url):
        return False
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if (not host or parsed.username or parsed.password or host == "localhost"
            or host.endswith((".local", ".internal", ".test", ".invalid"))):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def product_cards(output: str) -> tuple[str, list[dict[str, Any]]]:
    """Strip an optional `product-cards` payload; keep only bounded, display-safe fields."""
    match = re.search(r"(?s)\n?```product-cards\s*\n(.*?)\n```", output)
    if not match:
        return output, []
    clean = output[:match.start()] + output[match.end():]
    try:
        raw = json.loads(match.group(1))
    except (ValueError, TypeError):
        return clean, []
    if not isinstance(raw, list):
        return clean, []
    cards = []
    for item in raw[:MAX_CARDS]:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not _public_host(url, r"https?://"):
            continue
        title = safe_user_text(item.get("name"), 120)
        if not title:
            continue
        card: dict[str, Any] = {"kind": "product", "name": title, "url": url}
        for key, length in (("price", 40), ("store", 60), ("rating", 30), ("image_url", 1500)):
            value = item.get(key)
            if isinstance(value, str):
                value = value.strip()[:length]
                if key == "image_url" and not _public_host(value, r"https://"):
                    continue
                if value:
                    card[key] = value
        specs = item.get("specs")
        if isinstance(specs, list):
            card["specs"] = [s.strip()[:90] for s in specs[:3] if isinstance(s, str) and s.strip()]
        cards.append(card)
    return clean, cards


MEDIA_TAG_RE = re.compile(r"""(?m)(?P<lead>^[ \t]*|[ \t]+)MEDIA:[ \t]*(?:`(?P<tick>[^`\n]+)`|"(?P<quote>[^"\n]+)"|(?P<bare>\S+))[ \t]*$""")
MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]\n]{0,120})\]\((https://[^\s)<>]{4,1500})\)")
# Hermes' API server inlines every MEDIA:<path> image as a base64 data URL (remote frontends can't read
# the host's files), so this is the form finished images actually arrive in over the API.
DATA_IMAGE_RE = re.compile(r"!\[([^\]\n]{0,120})\]\(data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=\s]{16,})\)")
DATA_IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def saved_data_images(output: str, roots: tuple[Path, ...]) -> tuple[str, list[str]]:
    """Decode inline ``data:image`` markdown into files under the first image root (the Hermes home)
    and put a ``MEDIA:<path>`` tag in its place, so these images take the same vetted path as any other.
    The type must match the bytes; oversized or undecodable images are dropped from the text."""
    import base64
    import hashlib

    from .cards import MAX_LOCAL_IMAGE_BYTES, _sniff

    if "data:image/" not in output or not roots:
        return output, []
    folder = Path(roots[0]) / "cache" / "speakeasy" / "images"
    saved: list[str] = []

    def replace(match: re.Match) -> str:
        try:
            blob = base64.b64decode(re.sub(r"\s+", "", match.group(3)), validate=True)
        except (ValueError, TypeError):
            return ""
        if not blob or len(blob) > MAX_LOCAL_IMAGE_BYTES or _sniff(blob[:16]) != match.group(2):
            return ""
        path = folder / (hashlib.sha256(blob).hexdigest()[:32] + DATA_IMAGE_EXT[match.group(2)])
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                tmp = path.with_suffix(path.suffix + ".part")
                tmp.write_bytes(blob)
                tmp.replace(path)
        except OSError:
            return ""
        saved.append(str(path))
        return f"\nMEDIA:{path}\n"

    return DATA_IMAGE_RE.sub(replace, output), saved


def media_images(output: str, roots: tuple[Path, ...]) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Image results: Hermes `MEDIA:<path>` tags and markdown images.

    Returns (display text without image MEDIA tags, image cards, every MEDIA tag for chat delivery).
    Local cards keep their path server-side only; `public_result` strips it before any client sees it.
    Paths outside the configured roots, symlinks, and non-image types never become cards.
    """
    cards: list[dict[str, Any]] = []
    tags: list[str] = []
    seen: set[str] = set()
    fences = [(m.start(), m.end()) for m in re.finditer(r"(?s)```.*?```", output)]

    def in_fence(pos: int) -> bool:
        return any(a <= pos < b for a, b in fences)

    spans = []
    for match in MEDIA_TAG_RE.finditer(output):
        if in_fence(match.start()):
            continue
        raw = (match.group("tick") or match.group("quote") or match.group("bare") or "").strip()
        tags.append(raw)
        if Path(raw.split("?", 1)[0]).suffix.lower() not in LOCAL_IMAGE_EXTS:
            continue  # audio/documents keep their tag in the text; they are not image cards
        spans.append((match.start(), match.end()))
        if raw in seen:
            continue
        seen.add(raw)
        name = Path(raw.split("?", 1)[0]).name[:120] or "Image"
        if raw.startswith("https://"):
            try:
                vetted_image_url(raw)
            except ImageRejected:
                continue
            cards.append({"kind": "image", "name": name, "image_url": raw})
        else:
            try:
                path = vetted_local_image(raw, roots)
            except ImageRejected:
                continue
            cards.append({"kind": "image", "name": name, "path": str(path)})
    for match in MARKDOWN_IMAGE_RE.finditer(output):
        if in_fence(match.start()) or match.group(2) in seen:
            continue
        try:
            vetted_image_url(match.group(2))
        except ImageRejected:
            continue
        seen.add(match.group(2))
        title = safe_user_text(match.group(1), 120) or Path(urllib.parse.urlsplit(match.group(2)).path).name or "Image"
        cards.append({"kind": "image", "name": title[:120], "image_url": match.group(2)})
    for start, end in reversed(spans):
        output = output[:start] + output[end:]
    return output, cards, tags


LIVE_MEDIA_RE = re.compile(r"""MEDIA:[ \t]*[`"']?(?P<ref>(?:/|~/|https://)[^\s`"'<>]+)""")
SCREENSHOT_PATH_RE = re.compile(r'"screenshot_path"\s*:\s*"(?P<ref>/[^"\n]{1,1000})"')


def vetted_live_ref(raw: Any, roots: tuple[Path, ...]) -> tuple[str, str, str] | None:
    """One image a running task produced or is looking at -> (kind, ref, name), vetted exactly like
    result cards: local paths only under the image roots, HTTPS only to public hosts."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip().rstrip(".,;)")
    is_url = raw.startswith("https://")
    if Path(urllib.parse.urlsplit(raw).path if is_url else raw).suffix.lower() not in LOCAL_IMAGE_EXTS:
        return None
    try:
        if is_url:
            vetted_image_url(raw)
            return "url", raw, Path(urllib.parse.urlsplit(raw).path).name[:120] or "Image"
        path = vetted_local_image(raw, roots)
    except ImageRejected:
        return None
    return "path", str(path), path.name[:120]


def live_images_in(text: Any, roots: tuple[Path, ...]) -> list[tuple[str, str, str]]:
    """Vetted images named in interim text or a tool result preview: ``MEDIA:<path>`` tags (also
    inside JSON, where the line-anchored tag pattern misses them) and a browser ``screenshot_path``."""
    if not isinstance(text, str) or not text:
        return []
    found: list[tuple[int, str]] = [(m.start(), m.group("tick") or m.group("quote") or m.group("bare") or "")
                                    for m in MEDIA_TAG_RE.finditer(text)]
    found += [(m.start(), m.group("ref")) for m in LIVE_MEDIA_RE.finditer(text)]
    found += [(m.start(), m.group("ref")) for m in SCREENSHOT_PATH_RE.finditer(text)]
    out: list[tuple[str, str, str]] = []
    for _, raw in sorted(found):
        vetted = vetted_live_ref(raw, roots)
        if vetted and vetted not in out:
            out.append(vetted)
    return out


def public_result(result: Any) -> Any:
    """Client view of a stored result: no server-side paths or internal delivery fields."""
    if not isinstance(result, dict):
        return result
    clean = {k: v for k, v in result.items() if not k.startswith("_")}
    if isinstance(clean.get("views"), list):
        from .views import settled_all
        clean["views"] = settled_all(clean["views"])   # a stored result is a finished task: no progress bars
    if isinstance(clean.get("cards"), list):
        clean["cards"] = [{k: v for k, v in card.items() if k != "path"} if isinstance(card, dict) else card
                          for card in clean["cards"]]
    return clean


def delivery_text(result: dict[str, Any] | None) -> str:
    """Chat delivery: the readable answer plus the original MEDIA tags so the chat still gets images."""
    if not result:
        return ""
    text = (result.get("full") or "").strip()
    missing = [tag for tag in result.get("_media") or [] if isinstance(tag, str) and f"MEDIA:{tag}" not in text]
    return "\n\n".join([text, *(f"MEDIA:{tag}" for tag in missing)]).strip() if missing else text


_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,120})\]\((https://[^\s)]{4,500})\)")
_BARE_URL_RE = re.compile(r"(?<![\w(\[/])(https://[^\s<>()\[\]\"']{4,500})")
MAX_LINKS = 6


def links_in(text: str) -> list[dict[str, str]]:
    """The https links an answer gives (markdown [name](url) or bare), in order, deduped, for the app's
    tappable "Links" row. Only links the agent wrote; nothing is looked up or guessed."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(title: str, url: str) -> None:
        url = url.rstrip(".,;:!?")
        if url in seen or len(out) >= MAX_LINKS:
            return
        seen.add(url)
        host = re.sub(r"^www\.", "", url.split("/")[2]) if url.count("/") >= 2 else url
        out.append({"title": " ".join(title.split())[:80] or host, "url": url, "host": host})

    spans = []
    for m in _MD_LINK_RE.finditer(text):
        spans.append((m.start(), m.group(1), m.group(2)))
    stripped = _MD_LINK_RE.sub(" ", text)
    for m in _BARE_URL_RE.finditer(stripped):
        spans.append((len(text) + m.start(), "", m.group(1)))
    for _, title, url in sorted(spans):
        add(title, url)
    return out


MAX_SUMMARY_POINTS = 4
SUMMARY_POINT_WORDS = 10
_HEADING_RE = re.compile(r"^\s{0,3}(?:#{1,4}\s+(.+?)\s*#*\s*$|\*\*([^*]{2,80})\*\*:?\s*$)")
_BULLET_RE = re.compile(r"^\s{0,3}(?:[-*•]|\d{1,2}[.)])\s+(.+)$")


def _point(text: str) -> str | None:
    """One key point: plain words, its first clause, at most SUMMARY_POINT_WORDS words."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[*_`]+", "", text).strip()
    text = re.split(r"(?<=[.!?])\s|\s[—–]\s|:\s", text, maxsplit=1)[0].strip(" .;:,-")
    words = text.split()
    if len(words) > SUMMARY_POINT_WORDS:
        words = words[:SUMMARY_POINT_WORDS]
        while len(words) > 2 and re.sub(r"[^a-z']", "", words[-1].lower()) in _TITLE_DANGLING:
            words.pop()
    point = " ".join(words).strip(" ,;:-")
    return point if len(words) >= 2 and not SHELL_MARK_RE.search(point) else None


def summary_points(full: str | None) -> list[str] | None:
    """Up to four short key points for an answer laid out in sections (headings, or a list of three
    or more): each section's first line, else the list's items. None for a plain answer."""
    lines = [ln for ln in str(full or "").splitlines() if ln.strip()]
    if any(ln.lstrip().startswith("```") for ln in lines):
        lines = [ln for ln in re.sub(r"(?s)```.*?```", "", "\n".join(lines)).splitlines() if ln.strip()]
    heads = [i for i, ln in enumerate(lines) if _HEADING_RE.match(ln)]
    bullets = [m.group(1) for ln in lines if (m := _BULLET_RE.match(ln))]
    raw: list[str] = []
    if len(heads) >= 2:
        for n, i in enumerate(heads):
            end = heads[n + 1] if n + 1 < len(heads) else len(lines)
            body = lines[i + 1:end]
            first = next((m.group(1) if (m := _BULLET_RE.match(b)) else b for b in body), None)
            head = _HEADING_RE.match(lines[i])
            raw.append(first or head.group(1) or head.group(2))
    elif len(bullets) >= 3:
        raw = bullets
    else:
        return None
    points: list[str] = []
    for item in raw:
        point = _point(item)
        if point and point.lower() not in {p.lower() for p in points}:
            points.append(point[0].upper() + point[1:])
        if len(points) == MAX_SUMMARY_POINTS:
            break
    return points if len(points) >= 2 else None


def split_result(output: Any, image_roots: tuple[Path, ...], fallback_spoken: str = "Done. The details are in the app.") -> dict[str, Any] | None:
    """Split a final answer into the spoken register and the full display register.

    Also strips `product-cards` and `email-draft` fenced payloads (drafts go to `_email_drafts`).
    """
    from .emails import extract_email_drafts

    if not isinstance(output, str) or not output.strip():
        return None
    output = said_once(output)
    label = None
    done = list(re.finditer(r"(?im)^[ \t]*DONE:[ \t]*(.+?)[ \t]*$", output))
    if done:
        label = valid_done_label(done[-1].group(1))
        output = output[:done[-1].start()] + output[done[-1].end():]
    spoken_raw = None
    matches = list(re.finditer(r"(?im)^[ \t]*SPOKEN:[ \t]*(.+?)[ \t]*$", output))
    if matches:
        spoken_raw = matches[-1].group(1)
        output = output[:matches[-1].start()] + output[matches[-1].end():]
    output, drafts = extract_email_drafts(output)
    output, _ = saved_data_images(output, image_roots)
    output, cards = product_cards(output)
    from .views import extract as extract_views
    output, views = extract_views(output)
    from .views import extract_question, number_view_images, option_text, spoken_question, view_images
    output, question = extract_question(output)
    option_images = question.pop("_option_images", []) if question else []
    for raw in option_images + view_images(views):
        if raw and raw not in output:
            output += f"\nMEDIA:{raw}"   # pictures on cards go through the same vetting as any result image
    output, images, media_tags = media_images(output, image_roots)
    cards = (cards + images)[:MAX_CARDS]

    def number(raw: str) -> int:
        if not raw:
            return 0
        want = raw if raw.startswith("https://") else str(Path(raw).expanduser().resolve())
        for i, card in enumerate(cards):
            if card.get("image_url") == want or card.get("path") == want:
                return i + 1
        return 0
    number_view_images(views, number)   # card numbers, fetched by the app from /voice/card-image
    if question and any(option_images):
        numbers = [number(r) for r in option_images]
        if any(numbers):
            question["images"] = numbers
    full = safe_full_text(output)
    spoken = safe_user_text(spoken_raw, 500) if spoken_raw else None
    if not spoken and full:
        spoken = safe_user_text(spoken_from(full), 500)
    if spoken and (any(k == "command" for k, _ in split_commands(spoken)) or SHELL_MARK_RE.search(spoken)
                   or "`" in spoken):
        # Never hand the voice a command to read out; the pasteable copy is on screen and in chat.
        spoken = "There's a command for you to run. It's in the app and in chat, ready to copy."
    if not spoken:
        spoken = fallback_spoken
    if question:
        # The decision is the point: a one-line status, then the question, so the user answers it.
        lead = re.split(r"(?<=[.!?])\s+", spoken.strip())[0] if spoken != fallback_spoken else ""
        if lead.endswith("?"):
            lead = ""
        spoken = safe_user_text((lead + " " + spoken_question(question)).strip(), 500) or spoken_question(question)
        views = [question] + [v for v in views if v.get("kind") != "question"]
    if question:
        # The written answer (chat, task details) keeps the question so it can be answered there too.
        listed = "\n".join(f"{i + 1}. {option_text(o)}" + (" (recommended)" if question.get("recommended") == i else "")
                           for i, o in enumerate(question["options"]))
        full = ((full or "") + f"\n\n**{question['question']}**\n{listed}").strip()
    result: dict[str, Any] = {"spoken": spoken, "full": full or spoken}
    if points := summary_points(full):
        result["summary_points"] = points  # the job page's key points; "full" stays behind Details
    if question:
        result["question"] = question
    if cards:
        result["cards"] = cards
    if views:
        result["views"] = views
    found_links = links_in(full or "")
    if found_links:
        result["links"] = found_links
    if media_tags:
        result["_media"] = media_tags[:MAX_CARDS]
    if drafts:
        result["_email_drafts"] = drafts
    if label:
        result["label"] = label
    if not question and (headline := outcome_headline(spoken if spoken != fallback_spoken else None, full)):
        result["headline"] = headline  # what came of it, never the voice's opener; the side model may refine it
    return result


def notice_text(value: Any, limit: int) -> str | None:
    """Chat-safe one-liner: reuse display redaction and drop paths, emails, and opaque tokens."""
    text = safe_user_text(value, 4000)
    if not text or HOME_PATH_RE.search(text) or _preview_is_unsafe(text):
        return None
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


_TITLE_FILLER = set("""a an the and or but so um uh oh okay ok yeah yes no sure hey it its it's that this
there is are was be do did like well just maybe i you we me my huh hmm right cool""".split())


_TITLE_LEAD = re.compile(
    r"(?i)^(?:(?:hey|ok|okay|so|also|and|then|plus|oh|um+|uh+|er+|please|todd|hermes|yo|alright|all right|"
    r"actually|by the way|separately|can you|could you|would you|will you|can we|could we|i need you to|"
    r"i want you to|i'?d like you to|go ahead and|let'?s|now)[\s,]+)+")
# A clause that opens with these asks for something; a later one wins over an earlier aside.
_TITLE_ASK = re.compile(r"(?i)^(?:(?:also|and|then|so|um+|uh+|todd|hermes|hey)[\s,]+)*(?:can you|could you|would you|"
                        r"will you|please|i need you to|i want you to|go ahead and)\b")
# A self-correction mid-request ("Turn on my be- er, sorry, turn off..."): the words after it count.
_TITLE_CORRECTION = re.compile(r"(?i)(?:\b(?:sorry|i mean|i meant|no wait|scratch that|or rather)\b|\ber\b,|\w+-\s)[\s,.;:!-]*")
_TITLE_CLAUSE = re.compile(r"(?i)\s*(?:[,;:]|[.?!](?:\s|$)|\s(?:and then|then|and also|but)\s)\s*")
_TITLE_DANGLING = set("""a an the my our your his her their its to of for in at with from into onto by and or but
so than as about like then this these those some any please now""".split())
# Words a cut must not end on either: a sentence stopped here is still going ("What's the weather going to").
_TITLE_UNFINISHED = _TITLE_DANGLING | set("""is are was were be been being going gonna get still will would can could
should have has had if when where which who whose that we i you they he she um uh just really also very look looking
want need try trying make let""".split())
# Trailing words a whole request may drop (a polite tail, not part of the task).
_TITLE_SOFT_TAIL = {"please", "now", "right", "um", "uh", "like", "too", "actually", "todd", "thanks"}
# A clause that opens like this is a topic aside or a condition, not the task itself.
_TITLE_ASIDE = re.compile(r"(?i)^(?:for|on|about|regarding|with|in|during|if|when|because|since|while|although|"
                          r"like|i guess|i think|i mean|i don'?t know)\b")
# Notes the call adds to a request: what the user was looking at, what it follows up.
_TITLE_NOTE = re.compile(r"\s*\((?:[^():]{0,40}\bis looking at|following up)\s*:", re.I)
# Words that point at something said before ("do both", "fix that", "ship it").
_TITLE_REFERENT = {"it", "that", "this", "them", "those", "these", "both", "others", "other", "one", "ones",
                   "there", "same", "again", "what", "her", "him", "they"}
TITLE_WORDS = 8
TITLE_WORDS_WHOLE = 10  # a clause this short is kept whole rather than cut mid-phrase
_PRODUCT_NAMES = (
    (re.compile(r"(?i)\bspeak[\s-]?easy\b"), "Speakeasy"),
    (re.compile(r"(?i)\bherm[eè]s\b"), "Hermes"),
    (re.compile(r"(?i)\btest[\s-]?flight\b"), "TestFlight"),
    (re.compile(r"(?i)\bchat[\s-]?gpt\b"), "ChatGPT"),
    (re.compile(r"(?i)\bmac[\s-]?mini\b"), "Mac mini"),
    (re.compile(r"(?i)\bclaude[\s-]?code\b"), "Claude Code"),
    (re.compile(r"(?i)\bopen[\s-]?ai\b"), "OpenAI"),
)


def fix_product_names(title: str) -> str:
    """Spell the products a voice transcript mangles the way they are written ("Speak Easy" ->
    "Speakeasy", "Hermès" -> "Hermes", "Test flight" -> "TestFlight")."""
    for pattern, name in _PRODUCT_NAMES:
        title = pattern.sub(name, title)
    return title


def _title_content(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _TITLE_FILLER]


def request_notes(request: str | None) -> tuple[str, str]:
    """A request split into what was said and the notes the call appended to it
    ("(Geo is looking at: ...)", "(following up: ...)")."""
    text = " ".join(str(request or "").split())
    match = _TITLE_NOTE.search(text)
    if not match:
        return text, ""
    return text[:match.start()].strip(), text[match.start():].strip()


def is_fragment(request: str | None) -> bool:
    """True for a request that names nothing on its own ("Fix and ship", "Do both", "Gonna win",
    "Don't worry about that"): its name has to come from what it answers or continues."""
    said, _ = request_notes(notice_text(request, 1200) or "")
    said = _TITLE_LEAD.sub("", said.strip(" ,.;:!?-")).strip()
    content = _title_content(said)
    if len(content) < 3:
        return True
    words = set(re.findall(r"[a-z']+", said.lower()))
    return len(content) <= 5 and bool(words & _TITLE_REFERENT)


def _trim_tail(words: list[str], cut: bool) -> list[str]:
    tail = _TITLE_UNFINISHED if cut else _TITLE_DANGLING
    while len(words) > 2:
        last = re.sub(r"[^a-z']", "", words[-1].lower())
        if last in tail or last in _TITLE_SOFT_TAIL or words[-1].endswith("-"):
            words.pop()
            continue
        break
    return words


def _best_clause(text: str) -> tuple[str, bool]:
    """The clause that names the task, and whether the request went on past it."""
    clauses = [c for c in _TITLE_CLAUSE.split(text) if c and c.strip(" ,.;:!?-")]
    if len(clauses) <= 1:
        return text, False
    asks = [c for c in clauses[1:] if _TITLE_ASK.match(c) and len(_title_content(c)) >= 2]
    if asks and not _TITLE_ASK.match(clauses[0]):
        chosen = asks[0]  # "They turned off again. Can you turn the lamps back on": the ask is the task
    else:
        chosen = next((c for c in clauses if len(_title_content(c)) >= 3 and not _TITLE_ASIDE.match(c.strip())),
                      None) or next((c for c in clauses if len(_title_content(c)) >= 2), clauses[0])
    return chosen, chosen is not clauses[-1]


def short_title(request: str | None) -> str | None:
    """A to-do style label from the spoken request (no classifier). The call's notes and
    self-corrections are dropped, the clause that names the task is used, a short clause is kept
    whole, and a cut never ends mid-phrase ("Turn off the office lamp", not "Turn off the office
    lamp, turn"; "What's the weather tomorrow", not "What's the weather going to look")."""
    text = notice_text(request, 400)
    if not text:
        return None
    text, _ = request_notes(text)
    parts = _TITLE_CORRECTION.split(text)
    if len(parts) > 1:
        after = parts[-1].strip(" ,.;:!?-")
        if len(_title_content(after)) >= 2:
            text = after
    text = _TITLE_LEAD.sub("", text.strip(" ,.;:!?-")).strip()
    text, cut = _best_clause(text)
    text = _TITLE_LEAD.sub("", text.strip(" ,.;:!?-")).strip()
    words = text.rstrip(".!?").split()
    if not words or len(_title_content(text)) < 2:
        return None  # "Uh, sure", "that": no name is better than a name made of filler
    if len(words) > TITLE_WORDS_WHOLE:
        words, cut = words[:TITLE_WORDS], True
    while len(" ".join(words)) > 60 and len(words) > 2:
        words, cut = words[:-1], True
    words = _trim_tail(words, cut)
    title = fix_product_names(" ".join(words).strip(" ,.;:!?-"))
    return (title[0].upper() + title[1:])[:60] if title else None


def v1_short_title(request: str | None) -> str | None:
    """The label plugin 0.3.x saved (first clause, eight words), so a task named by it still
    counts as a stand-in that a proper name may replace."""
    text = notice_text(request, 200)
    if not text:
        return None
    lead = re.compile(r"(?i)^(?:(?:hey|ok|okay|so|also|and|then|plus|oh|um+|uh+|er+|please|todd|hermes|"
                      r"can you|could you|would you|will you|can we|could we|i need you to|i want you to|"
                      r"i'?d like you to|go ahead and|let'?s|now)[\s,]+)+")
    clause = re.compile(r"(?i)\s*(?:[,;:]|\.\s|\s(?:and then|then|and also|but)\s)\s*")
    parts = _TITLE_CORRECTION.split(text)
    if len(parts) > 1:
        after = parts[-1].strip(" ,.;:!-")
        if len(_title_content(after)) >= 2:
            text = after
    text = lead.sub("", text.strip(" ,.;:!-")).strip()
    clauses = [c for c in clause.split(text) if c.strip()]
    if clauses and len(_title_content(clauses[0])) >= 2:
        text = clauses[0]
    text = lead.sub("", text).strip()
    words = text.rstrip(".!?").split()
    if not words or len(_title_content(text)) < 2:
        return None
    words = words[:8]
    while len(words) > 2 and (re.sub(r"[^a-z']", "", words[-1].lower()) in _TITLE_DANGLING or words[-1].endswith("-")):
        words.pop()
    title = " ".join(words).strip(" ,.;:!-")
    return (title[0].upper() + title[1:])[:60]


def legacy_short_title(request: str | None) -> str | None:
    """The label older versions saved (first six words), so a task named by one still counts as a
    stand-in that a proper name may replace."""
    text = notice_text(request, 200)
    if not text:
        return None
    text = re.sub(r"(?i)^(?:(?:hey|ok|okay|so|um+|uh+|please|can you|could you|would you|i need you to|"
                  r"i want you to|go ahead and|let'?s)[\s,]+)+", "", text).strip()
    words = text.rstrip(".!?").split()
    if not words or len(_title_content(text)) < 2:
        return None
    title = " ".join(words[:6])
    return (title[0].upper() + title[1:])[:60]


def stand_in_titles(request: str | None) -> set[str]:
    """Every word-cut label this or an older version may have shown for *request*."""
    asked = notice_text(request, 1200) or ""
    return {t for t in (short_title(asked), v1_short_title(asked), legacy_short_title(asked)) if t}


def title_input(request: str, *, parent: str | None = None, parent_request: str | None = None,
                recent: list[dict[str, str]] | None = None, names: "list[str] | tuple[str, ...]" = ()) -> str:
    """What the task titler reads: the request, the task it continues or answers (only the one it
    really answers or joins: never a chat thread's name or whatever ran before it on the call), the
    last few turns of the call (so "that", "both" and "ship it" can be named), and the names a
    title must leave out (the user's and the assistant's)."""
    lines = [f"Request: {' '.join(request.split())[:1000]}"]
    if parent or parent_request:
        about = " — ".join(x for x in ((parent or "").strip(), " ".join((parent_request or "").split())[:400]) if x)
        lines.append(f"It continues or answers this earlier task: {about}")
    turns = [t for t in (recent or []) if (t.get("text") or "").strip()][-6:]
    if turns:
        lines.append("Recent conversation on the call (oldest first):")
        for turn in turns:
            role = "User" if turn.get("role") == "user" else "Assistant"
            lines.append(f"{role}: {' '.join(str(turn['text']).split())[:300]}")
    left_out = [n for n in dict.fromkeys(" ".join(str(n or "").split()) for n in names) if n]
    if left_out:
        lines.append(f"{NAMES_LINE}{', '.join(left_out)}")
    return "\n".join(lines)


# A title about the conversation rather than the task ("Continue previous discussion"): never shown, and
# never handed to the titler as the name of the task a follow-up continues.
TITLE_META = re.compile(
    r"(?i)^(?:continu(?:e|ing)|resum(?:e|ing)|follow(?:ing)?[\s-]?up|discuss(?:ion|ing)?|clarif|confusion|handle|"
    r"respond|reply|answer(?:ing)? (?:the|a) |voice (?:task|request)|quick (?:question|choice)|[a-z]+'s (?:request|question))"
    r"|\b(?:previous|earlier|prior|other) (?:discussion|conversation|topic|stuff|items)\b|\bremaining items\b"
    r"|\b(?:voice|speakeasy) (?:task|request)\b|\bthis conversation\b")


# Words that name no subject: a title made only of these ("Check work status", "Decide on holding",
# "Review the update") can't be recognized a day later.
TITLE_VAGUE = set("""check checking confirm confirming decide deciding review reviewing look looking continue
continuing update updating follow up work status progress task tasks thing things stuff it this that these those
the a an on of about for to in with and or my our your earlier previous last latest holding hold wait waiting
see get got do done make handle sort out going how what is are""".split())
TITLE_REFERENCE_RE = re.compile(r"(?i)\b(?:the|that|this)\s+(?:earlier|previous|last|other|same)\s+(?:task|one|thing|job)\b")


def vague_title(title: str | None) -> str | None:
    """Why a title can't be recognized a day later (it points at another task, or names no
    subject), else None."""
    text = " ".join(str(title or "").split())
    if not text:
        return None
    if TITLE_REFERENCE_RE.search(text):
        return "it points at another task instead of naming it; say what that task was about"
    if len([w for w in re.findall(r"[A-Za-z0-9']+", text.lower()) if w not in TITLE_VAGUE]) < 2:
        return "it doesn't name the subject; say which project or thing and what was asked"
    return None


# -- outcome headlines -------------------------------------------------------------------------------
# A finished task's one-line headline is what came of it, never how the voice opened its reply:
# "Okay, I'm dropping it." or "Sure thing." says nothing a day later.
_HEADLINE_LEAD = re.compile(
    r"(?i)^(?:(?:okay|ok|alright|all right|sure(?: thing)?|got it|you got it|on it|right|yes|yeah|yep|so|well|"
    r"great|perfect|cool|understood|absolutely|of course|no problem|will do|good news|quick update|update|heads up"
    r"|done|all done|here you go)\b[\s,.!:;—–-]*)+")
_HEADLINE_PROMISE = re.compile(
    r"(?i)^(?:i'?m|i am|i'?ll|i will|i'?ve got|let me|let's|going to|gonna|i can|i'd|i would|here'?s|here is|"
    r"here are|on it|working on|looking into|checking|starting)\b")
HEADLINE_WORDS = 16


def is_opener(sentence: str | None) -> bool:
    """A sentence that only acknowledges or promises ("Okay, I'm dropping it.", "Sure thing.",
    "Let me check."), not a fact about what happened."""
    rest = _HEADLINE_LEAD.sub("", " ".join(str(sentence or "").split())).strip(" .!,;:-")
    if len(re.findall(r"[A-Za-z0-9]+", rest)) < 2:
        return True
    return bool(_HEADLINE_PROMISE.match(rest)) and len(rest.split()) <= 7


def clean_headline(raw: Any) -> str | None:
    """A model-written outcome headline, or None when it's an opener, a question or too long."""
    text = safe_user_text(raw, 200)
    if not text:
        return None
    text = " ".join(text.split()).strip(" \"'“”")
    if text.endswith("?") or is_opener(text) or len(text.split()) > HEADLINE_WORDS:
        return None
    text = _HEADLINE_LEAD.sub("", text).strip()
    return (text[0].upper() + text[1:]).rstrip(" ,;:") if text else None


def _first_outcome(text: str | None) -> str | None:
    for _, sentence in _sentences(str(text or "")):
        if sentence.endswith("?") or is_opener(sentence):
            continue
        line = _HEADLINE_LEAD.sub("", sentence).strip(" —–-")
        if line:
            return line[0].upper() + line[1:]
    return None


def outcome_headline(spoken: str | None, full: str | None = None) -> str | None:
    """The first sentence of a reply that says what came of the work: openers ("Okay, I'm dropping
    it."), questions and one-word replies are skipped, and a leading "Okay," is dropped. The written
    answer's sentence wins when it says the spoken one and more. None when every sentence is one of
    those (the side model's headline, or the task title, stands instead)."""
    said, written = _first_outcome(spoken), _first_outcome(full)
    line = said or written
    if said and written and written.lower().startswith(said.rstrip(".!").lower()) and len(written) <= 140:
        line = written
    if line and len(line) > 140:
        line = line[:139].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return line


NAMES_LINE = "Names to leave out of the title: "


def names_in(text: str | None) -> list[str]:
    """The names a titler input (``title_input``) says to leave out."""
    match = re.search(r"(?m)^" + re.escape(NAMES_LINE) + r"(.+)$", str(text or ""))
    return [n.strip() for n in match.group(1).split(",") if n.strip()] if match else []


def has_name(title: str | None, names: "list[str] | tuple[str, ...]") -> str | None:
    """The first of *names* that *title* mentions (whole word, any case, possessive too), or None."""
    for name in names:
        name = " ".join(str(name or "").split())
        if name and re.search(r"(?i)(?<![\w])" + re.escape(name) + r"(?:'s|’s)?(?![\w])", title or ""):
            return name
    return None


def drop_names(title: str | None, user: str | None = None, assistant: str | None = None) -> str | None:
    """*title* without the user's or the assistant's name: a task list is the user's own, so
    "Sam's PRs" reads "my PRs", "the weather for Ava" reads "the weather". None when nothing
    worth showing is left."""
    text = " ".join(str(title or "").split())
    if not text:
        return None
    for name, owner in ((user, "my"), (assistant, "the")):
        name = " ".join(str(name or "").split())
        if not name:
            continue
        n = re.escape(name)
        text = re.sub(rf"(?i)\b(?:the\s+|my\s+)?{n}(?:'s|’s)(?![\w])", owner, text)  # Sam's PRs -> my PRs
        text = re.sub(rf"(?i)\b(?:ask|tell|have|get)\s+{n}\s+(?:to\s+)?", "", text)  # "Ask Ava to check" -> "check"
        text = re.sub(rf"(?i)\s*\b(?:for|to|with|from|by|ask|tell|asks?|tells?)\s+{n}(?![\w])", "", text)
        text = re.sub(rf"(?i)^\s*(?:hey\s+)?{n}\s*[,:]\s*", "", text)  # "Ava, check ..."
        text = re.sub(rf"(?i)(?<![\w]){n}(?![\w])", "", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", " ".join(text.split())).strip(" ,;:-")
    text = re.sub(r"(?i)\b(my|the)\s+(?:my|the)\b", r"\1", text)
    if len(_title_content(text)) < 2:
        return None
    return text[0].upper() + text[1:]


_HOME_STATE = re.compile(
    r"(?i)^(?:ok(?:ay)?[,.]?\s+|done[,.]?\s+|all set[,.]?\s+)?(?:the\s+|your\s+)?(?P<what>[\w' -]{2,40}?)\s+"
    r"(?:is|are|was|were)\s+(?:now\s+)?(?P<state>on|off|open|closed|locked|unlocked|playing|paused|"
    r"(?:set\s+)?(?:to|at)\s+[\w%°. -]{1,20})$")


def home_title(spoken: str | None) -> str | None:
    """A card name from what the house did ("Office lamp is off. Bedroom lamps are on." ->
    "Office lamp off · Bedroom lamps on"). None unless every sentence reads as a device state."""
    text = " ".join(str(spoken or "").split())
    sentences = [s.strip() for s in re.split(r"(?<=[.!])\s+|\s+and\s+(?=(?:the\s+)?\w[\w' -]*\s+(?:is|are)\s)", text)
                 if s.strip(" .!")]
    if not sentences or len(sentences) > 3:
        return None
    parts = []
    for sentence in sentences:
        match = _HOME_STATE.match(sentence.strip(" .!"))
        if not match:
            return None
        state = re.sub(r"(?i)^set\s+", "", match.group("state")).strip()
        what = match.group("what").strip()
        parts.append(f"{what[0].upper()}{what[1:]} {state}")
    title = " · ".join(parts)
    return title if len(title) <= 60 else None
