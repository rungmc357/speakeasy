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

MAX_TRANSCRIPT = 64 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
SETTLED = TERMINAL | {"waiting_for_approval"}
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


def spoken_from(full: str, limit: int = 3) -> str:
    """What to say aloud when the task gave no SPOKEN line: the opening sentence, then the first
    sentences that carry a problem or a heads-up, so a warning is never cut off. Bullets and
    markdown read as separate plain sentences."""
    text = re.sub(r"https?://\S*[^\s.,;:!?)]|[`*_#>]+", "", full)
    lines = [re.sub(r"^\s*(?:[-•]|\d+[.)])\s+", "", l).strip() for l in text.splitlines()]
    sentences = []
    for line in lines:
        if line:
            sentences += [x.strip() for x in re.split(r"(?<=[.!?])\s+", line) if x.strip()]
    sentences = [x if re.search(r"[.!?]$", x) else x + "." for x in sentences]
    if not sentences:
        return ""
    picked = [0]
    flagged = [i for i, x in enumerate(sentences[1:], 1) if HEADS_UP_RE.search(x)]
    picked += flagged[: limit - 1]
    if len(picked) == 1 and len(sentences) > 1:
        picked.append(1)
    out = " ".join(sentences[i] for i in sorted(picked))
    return re.sub(r"\s+([.,;:!?])", r"\1", re.sub(r"\s+", " ", out)).strip()


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


def split_result(output: Any, image_roots: tuple[Path, ...], fallback_spoken: str = "Done. The details are in the app.") -> dict[str, Any] | None:
    """Split a final answer into the spoken register and the full display register.

    Also strips `product-cards` and `email-draft` fenced payloads (drafts go to `_email_drafts`).
    """
    from .emails import extract_email_drafts

    if not isinstance(output, str) or not output.strip():
        return None
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
    from .views import extract_question, spoken_question
    output, question = extract_question(output)
    option_images = question.pop("_option_images", []) if question else []
    for raw in option_images:
        if raw and raw not in output:
            output += f"\nMEDIA:{raw}"   # one picture per option, through the same vetting as any result image
    output, images, media_tags = media_images(output, image_roots)
    cards = (cards + images)[:MAX_CARDS]
    if question and any(option_images):
        def number(raw: str) -> int:
            if not raw:
                return 0
            want = raw if raw.startswith("https://") else str(Path(raw).expanduser().resolve())
            for i, card in enumerate(cards):
                if card.get("image_url") == want or card.get("path") == want:
                    return i + 1
            return 0
        numbers = [number(r) for r in option_images]
        if any(numbers):
            question["images"] = numbers   # card numbers, fetched by the app from /voice/card-image
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
        listed = "\n".join(f"{i + 1}. {o}" + (" (recommended)" if question.get("recommended") == i else "")
                           for i, o in enumerate(question["options"]))
        full = ((full or "") + f"\n\n**{question['question']}**\n{listed}").strip()
    result: dict[str, Any] = {"spoken": spoken, "full": full or spoken}
    if question:
        result["question"] = question
    if cards:
        result["cards"] = cards
    if views:
        result["views"] = views
    if media_tags:
        result["_media"] = media_tags[:MAX_CARDS]
    if drafts:
        result["_email_drafts"] = drafts
    if label:
        result["label"] = label
    return result


def notice_text(value: Any, limit: int) -> str | None:
    """Chat-safe one-liner: reuse display redaction and drop paths, emails, and opaque tokens."""
    text = safe_user_text(value, 4000)
    if not text or HOME_PATH_RE.search(text) or _preview_is_unsafe(text):
        return None
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


_TITLE_FILLER = set("""a an the and or but so um uh oh okay ok yeah yes no sure hey it its it's that this
there is are was be do did like well just maybe i you we me my huh hmm right cool""".split())


def short_title(request: str | None) -> str | None:
    """A to-do style label from the spoken request (no classifier): first few content words."""
    text = notice_text(request, 200)
    if not text:
        return None
    text = re.sub(r"(?i)^(?:(?:hey|ok|okay|so|um+|uh+|please|can you|could you|would you|i need you to|"
                  r"i want you to|go ahead and|let'?s)[\s,]+)+", "", text).strip()
    words = text.rstrip(".!?").split()
    content = [w for w in re.findall(r"[a-z0-9']+", text.lower()) if w not in _TITLE_FILLER]
    if not words or len(content) < 2:
        return None  # "Uh, sure", "that": no name is better than a name made of filler
    title = " ".join(words[:6])
    return (title[0].upper() + title[1:])[:60]
