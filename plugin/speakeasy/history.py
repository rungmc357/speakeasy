"""Every Hermes session and Speakeasy task, as a history the apps can browse and write to
(docs/API.md "History"; capability ``history``).

Read side: ``<HERMES_HOME>/state.db``, read-only, the same way continuity and the thread watcher read
it. The list mirrors Hermes Desktop's sidebar rules: no subagent/tool children, nothing hidden or
archived, one row per conversation (the newest segment of a compression chain), scheduled (cron)
sessions only when asked for. Search covers message text through Hermes' own full-text index
(``messages_fts``), plus titles; a plain LIKE scan when the index is missing.

Write side (``POST /voice/history/{id}/message``) always targets the session named, by id, and never
goes through the call router. Which door it uses depends on who owns the session (see service.py):

* a Speakeasy task's session: the task_messages machinery (steer, answer, or follow-up run);
* another API-server session: a new Speakeasy job in that same session (``/v1/runs`` + session_id);
* anything else (a Discord or Telegram thread, the terminal, Desktop): a Speakeasy task *linked*
  to that conversation, seeded with its recent messages. Hermes has no door that writes into such a
  session without the reply going back to the chat (``/v1/runs`` refuses a foreign session; the
  chat-stream door and the gateway socket's viewer submit both deliver the reply to the platform),
  and Speakeasy never posts into a chat. Later messages from that page go to the same linked task.
  The chat's page shows the linked task's messages merged in by time (``merge_linked``).
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import activity as A

logger = logging.getLogger(__name__)

PAGE = 40                  # sessions per page
MAX_PAGE = 100
MESSAGES_PAGE = 300        # transcript rows per page (newest first, oldest shown first)
PREVIEW_CHARS = 140
TEXT_CHARS = 20000         # one message's text, as shown
SEARCH_HITS = 600          # most recent matching messages a search looks at
BUSY_WINDOW_S = 900        # a non-Speakeasy session whose last row is a pending turn this recent "runs"
MAX_TEXT = 4000
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_:@.+=-]{1,128}$")
SPEAKEASY_PREFIX = "speakeasy_task_"

# Internal sessions never listed (their work shows inside the session that started them).
HIDDEN_SOURCES = ("subagent", "tool", "oneshot", "acp", "recovered")
LABELS = {"discord": "Discord", "telegram": "Telegram", "slack": "Slack", "whatsapp": "WhatsApp",
          "signal": "Signal", "matrix": "Matrix", "email": "Email", "sms": "SMS",
          "cli": "Terminal", "tui": "Terminal", "gui": "Desktop", "local": "Desktop", "desktop": "Desktop",
          "api_server": "Hermes API", "cron": "Scheduled", "kanban": "Kanban", "webhook": "Webhook", "subagent": "Helper"}
# The source filter (``?source=``): id -> the Hermes sources it covers (None: Speakeasy's own).
FILTERS: dict[str, tuple[str, ...] | None] = {
    "speakeasy": None,
    "discord": ("discord",), "telegram": ("telegram",), "slack": ("slack",),
    "terminal": ("cli", "tui"), "desktop": ("gui", "local", "desktop"),
    "scheduled": ("cron",), "kanban": ("kanban",), "api": ("api_server",), "helpers": ("subagent",),
}
FILTER_LABELS = {"speakeasy": "Speakeasy", "discord": "Discord", "telegram": "Telegram", "slack": "Slack",
                 "terminal": "Terminal", "desktop": "Desktop", "scheduled": "Scheduled", "kanban": "Kanban",
                 "api": "Hermes API", "helpers": "Helpers"}
# Automated work: sessions nobody typed into (Kanban board workers, scheduled jobs). Listed like any
# other unless ``?hide=kanban,scheduled`` (or the older ``?automated=0``: both) leaves them out (the
# apps' filter menu, remembered per device); never unread and never "needs you" (they belong to their
# board card / job, not to the person's inbox).
AUTOMATED_SOURCES = ("kanban", "cron")
AUTOMATED_FILTERS = ("kanban", "scheduled")
# Background helpers (``delegate_task`` children): left out unless ``?helpers=1`` asks for them (their
# work also shows inside the session that started them); automated like the others when listed.
HELPER_SOURCE = "subagent"
# The filter menu's kinds, in its order: id -> label (each id is also its source filter's id).
AUTOMATED_KINDS = {"kanban": "Board workers", "scheduled": "Scheduled jobs", "helpers": "Background helpers"}

# Earlier's filter menu (SPK-124, ``?kinds=``): checkboxes in Hermes' own words, each with its count.
# Conversations (everything a person talks in), scheduled jobs (cron), Kanban workers, subagents (a
# task's helpers, otherwise never listed) and archived conversations (Hermes' own archived flag).
KINDS = ("conversations", "scheduled", "kanban", "subagents", "archived")
KIND_LABELS = {"conversations": "Conversations", "scheduled": "Scheduled jobs", "kanban": "Kanban",
               "subagents": "Subagents", "archived": "Archived"}
DEFAULT_KINDS = ("conversations",)
_KIND_SQL = ("(CASE WHEN s.source='cron' THEN 'scheduled' WHEN s.source='kanban' THEN 'kanban' "
             "WHEN s.source='subagent' THEN 'subagents' ELSE 'conversations' END)")


def parse_kinds(raw: str | None) -> tuple[str, ...] | None:
    """``?kinds=conversations,kanban``: the checked boxes (None: the parameter wasn't sent, the list
    behaves as it did before Earlier). Empty or unknown names: 400."""
    if raw is None:
        return None
    kinds = tuple(dict.fromkeys(k.strip() for k in str(raw).split(",") if k.strip()))
    if not kinds or any(k not in KINDS for k in kinds):
        raise HistoryError(400, f"kinds is a comma-separated list of: {', '.join(KINDS)}")
    return kinds

# A context-compaction handoff (as a user or an assistant row): one quiet divider per page at most.
COMPACTED = "Earlier messages were summarized"
# The row Hermes writes to close a turn that failed before any reply (a provider refusal, an error):
# ``messages.display_kind = 'failed_turn'``, or on older Hermes only its exact text
# (agent/turn_failure_copy.py FAILED_TURN_NOTICE and friends). It is Hermes' bookkeeping, not the
# assistant's words: never a message bubble or a preview. A resend right after it reads as one
# message; a failed turn nothing followed yet shows as this quiet note.
FAILED_TURN_NOTICES = frozenset({
    "Your request was not processed. Send it again if you still want me to carry it out.",
    "This turn did not complete. Some actions may already have run; verify their effects before resending.",
    "An automated delivery was interrupted and was not run again. Some actions may already have run; "
    "verify their effects.",
})
FAILED_TURN_NOTE = "That didn't go through"
_COMPACTION = re.compile(r"^\s*(?:\[[^\]\n]{1,60}\]\s*)?\[(?:CONTEXT COMPACTION|PRIOR CONTEXT)|_compressed_summary")


def is_compaction(text: str) -> bool:
    """A context-compaction handoff (Hermes' model-facing summary of earlier turns)."""
    return bool(_COMPACTION.match(text or ""))


# The same handoff appended after a user's words in one row: everything from it on is Hermes'.
_COMPACTION_TAIL = re.compile(r"\n\s*\[(?:CONTEXT COMPACTION|PRIOR CONTEXT)\b")


def _before_compaction(text: str) -> str:
    m = _COMPACTION_TAIL.search(text or "")
    return text[:m.start()].rstrip() if m and text[:m.start()].strip() else text
# User rows that are Hermes talking to itself, never the person: hidden. (kind, step) where ``step``
# is the plain line the reply's work shows for it ("A helper finished"), or None.
_HIDDEN_RES = (
    (re.compile(r"^\s*\[ASYNC DELEGATION"), "A helper finished"),
    (re.compile(r"^\s*\[IMPORTANT: \d+ background subagent delegations? completed"), "Helpers finished"),
    (re.compile(r"^\s*\[IMPORTANT: Background process"), "A background job finished"),
    (re.compile(r"^\s*\[IMPORTANT: \d+ background process results arrived"), "A background job finished"),
    (re.compile(r"(?m)^\s*\[INTERNAL NOTIFICATION\b"), None),
    (re.compile(r"^\s*\[Cron delivery", re.I), None),
)
# A background-process report Hermes appended after the person's own words in one row (SPK-244):
# "…words\n[IMPORTANT: Background process proc_1 completed normally (exit code 0).\nStarted by
# subagent …\nCommand: …\nOutput:\n…]" (tools/process_registry_notifications.py), when the gateway
# merges a report into a turn the person sent. (A row ending in the gateway's "[INTERNAL NOTIFICATION
# — …]" footer is a notification row as a whole, never the person's: it stays hidden.) It starts a
# line (or follows a space after the words, with Hermes' exact head), never one quoted inside a
# sentence. Its output is arbitrary text, so brackets inside it don't balance: the report ends at the
# first "]" before a blank line, the next notice, or the end of the row.
_BG_NOTICE = re.compile(r"(?m)(?:^[ \t]*|(?<=[ \t]))\[(?:IMPORTANT: Background process \S+ (?=[^\n]*\(exit code|matched watch "
                        r"pattern)|Background process \S+ heartbeat #)")
_BG_NOTICE_STRICT = re.compile(r"\[IMPORTANT: Background process proc_\w+ [^\n]*\(exit code|\[IMPORTANT: Background process "
                               r"proc_\w+ matched watch pattern|\[Background process proc_\w+ heartbeat #")
_BG_NOTICE_END = re.compile(r"\][ \t]*(?=\n[ \t]*\n|\n[ \t]*\[(?:INTERNAL NOTIFICATION|IMPORTANT: |Background process )|\s*$)")
BACKGROUND_STEP = "A background job finished"
# What's left of a chat row once its report is cut can be only gateway tags (a sender tag "[pebble42]",
# a trigger-id line): every line one bracketed block. That's no words of the person's.
_SENDER_ONLY = re.compile(r"(?:[ \t]*\[[^\n]*\][ \t]*(?:\n|$)|[ \t]*\n)+")


def drop_background_notices(text: str) -> tuple[str, list[str]]:
    """(the person's words, the steps) of a row whose words carry Hermes' background-process reports
    after them: each report is cut out (with a gateway notification line right after it) and becomes
    one plain step ("A background job finished", once however many) for the reply's work. A row
    without one is unchanged."""
    steps: list[str] = []
    for _ in range(6):
        m = next((m for m in _BG_NOTICE.finditer(text) if text[:m.start()].strip()), None)
        if not m:
            break
        start = m.start()
        bracket = text.index("[", start)
        line_start = not text[text.rfind("\n", 0, start) + 1:bracket].strip()
        if not line_start and not _BG_NOTICE_STRICT.match(text, bracket):
            break   # mid-line: only Hermes' exact head counts (a sentence quoting the words stays)
        end = _BG_NOTICE_END.search(text, bracket)
        stop = end.end() if end else len(text)
        rest = text[stop:].strip()
        text = (text[:start].rstrip() + ("\n\n" + rest if rest else "")).strip()
        steps.append(BACKGROUND_STEP)
    return text, steps[:1]


# A Speakeasy task prompt: the person's words are the last "User:" line of its transcript.
# A Speakeasy task's brief: a call's ("gave you by voice", over the call transcript) or a message
# typed in the app ("sent you as a message in the Speakeasy app", over the typed messages).
_SPEAKEASY_BRIEF = re.compile(r"Recent timestamped voice transcript|Recent messages typed in the Speakeasy app|"
                              r"handling one task .{0,80} (?:gave you by voice|sent you as a message in the Speakeasy app)", re.S)
_USER_LINE = re.compile(r"^(?:\[[^\]]{1,40}\]\s*)?(?:User|You)\s*:\s*(.+)$", re.M)
_ADDED = re.compile(r"^.{1,60}? (?:just added to this task(?: by voice| by typing)?|typed to this task):?\s*(.+?)"
                    r"(?:\s*Fold it into the work.*)?$", re.S)
# A correction steer (prompt.builder.correction_steer_text): only the person's own words show.
_CORRECTION = re.compile(r'^Correction from .{1,60}?, who (?:said|typed): "(.+?)"\. ', re.S)
# A later turn in a task's session (prompt.builder.build_follow_up_prompt): its first line holds the
# person's words; everything under it (what it continues, the screen, the call) is context. The
# "[Speakeasy follow-up]" head may already be gone (strip_gateway takes "[sender]"-style prefixes), so
# the closing rules line is what tells it apart from someone's own words.
_FOLLOW_UP = re.compile(r"^(?:\[Speakeasy follow-up\] )?.{1,60}? (?:said on the call|typed in the Speakeasy app|"
                        r"answered by tapping): ([^\n]+)(?=.*\nSame rules as the first message in this session)", re.S)
# What a steer says about pictures/files the person shared (saved copies the task opens itself).
_SHARED = re.compile(r"(?s)\s*(?:The user|\S+) shared (?:the |a )?(?:picture|pictures|file|files|screenshot|images?)\b.*?"
                     r"Use them to understand what they mean\.\s*"
                     # Video preparation is appended to this generated block, never user speech.
                     # Only consume it after the complete shared-block signature, not a quoted
                     # 'Video MIME' phrase in an ordinary message.
                     r"(?:Video MIME: video/(?:mp4|quicktime)\. Original retained at .*\Z)?")


# -- what a message shows: never Speakeasy's own plumbing ----------------------------------------
# A task's machine blocks (```speakeasy-question / -views ...): the app draws them as cards.
_MACHINE_BLOCK = re.compile(r"(?s)\n?```speakeasy-[\w-]+[ \t]*\n.*?(?:\n```|\Z)")
# A relayed voice request written into a chat: "[sender] [Voice request from Geo, relayed by Speakeasy] <words>".
_RELAYED = re.compile(r"^\s*(?:\[[^\]\n]{1,60}\]\s*)?\[Voice(?: request)?,? from [^\]\n]{1,60}\]\s*", re.I)
_RELAY_TAIL = re.compile(r"\n\s*\n\s*(?:Begin your reply with exactly one line|Recent voice conversation for context)", re.I)
# A later relayed follow-up in the same row (a steer that folded several together).
_RELAYED_NEXT = re.compile(r"\s*\n\s*(?:\[[^\]\n]{1,60}\]\s*)?\[Voice(?: request)?,? from [^\]\n]{1,60}, relayed by Speakeasy\]\s*",
                           re.I)
# What a follow-up relayed into a task's thread tells the task (prompt/builder.thread_follow_up): a
# "(They want to SEE this: …MEDIA:<absolute path>.)" note and "This is about the task in this thread: …".
_FOLLOW_UP_TAIL = re.compile(r"(?s)\s*\(They want to SEE this\b[^)]*?\)|\s*This is about the task in this thread: "
                             r"apply it to that work now\.(?: If it answers a question you asked [^\n]{1,60}?, act on the "
                             r"answer\.)?")
# The note Speakeasy adds to a request: "(Geo is looking at: Find previous sessions)", "(following up: ...)".
# A follow-up of a follow-up nests them, cut short before any ")": "(following up: A (following up: B…".
_LOOKING_AT = re.compile(r"\s*\((?:(?:[^():\n]{0,40}\bis looking at|following up)\s*:[^\n]*\)|following up\s*:[^\n]*)\s*$",
                         re.I | re.M)
# Speakeasy's own notes to the task after the person's words (prompt.builder SHOW_IT_FOCUS,
# thread_follow_up, PLUGIN_UPDATE_FOCUS): "(They want to SEE this…MEDIA:<absolute path>.)",
# "This is about the task in this thread: apply it to that work now. If it answers…act on the answer.",
# "(This is an update of the Speakeasy voice plugin…switches over after the call.)".
_TASK_NOTES = re.compile(
    r"\s*\(They want to SEE this\b[^\n]*?MEDIA:<absolute path>\.?\)"
    r"|\s*This is about the task in this thread: apply it to that work now\."
    r"(?: If it answers a question you asked [^\n]{1,60}?, act on the answer\.)?"
    r"|\s*\(This is an update of the Speakeasy voice plugin\b.*?switches over after the call\.\)", re.S)
# A later relayed request inside the same row (two steers the gateway delivered together).
_RELAYED_LINE = re.compile(r"(?m)^[ \t]*(?:\[[^\]\n]{1,60}\][ \t]*)?\[Voice(?: request)?,? from [^\]\n]{1,60}\][ \t]*", re.I)
# The echo line a relayed request asks for: "Voice: <the request>" first.
_VOICE_LINE = re.compile(r"^\s*Voice:[^\n]*(?:\n+|$)")
_STATUS = re.compile(r"(?im)^[ \t]*STATUS:[ \t]*(.+?)[ \t]*$")
_DETAIL = re.compile(r"(?im)^[ \t]*DETAIL:[ \t]*(.+?)[ \t]*$")
# A voice report: "DONE: <what happened>" / "SPOKEN: <what the voice says>" lines. SPOKEN is the
# person-facing sentence (it may run on over several lines until the next label).
_REPORT_LABEL = re.compile(r"(?im)^[ \t]*(DONE|SPOKEN)[ \t]*:[ \t]*")
_SPOKEN = re.compile(r"(?is)^[ \t]*SPOKEN[ \t]*:[ \t]*(.+?)(?=^[ \t]*(?:DONE|SPOKEN|STATUS|DETAIL)[ \t]*:|\Z)", re.M)

# -- gateway plumbing on user rows (Discord, Telegram, ...) ------------------------------------
_TRIGGER_NOTE = re.compile(r"\[Triggering message id:[^\]\n]{0,300}\]\s*", re.I)
_MEMORY_CONTEXT = re.compile(r"(?s)<memory-context>.*?(?:</memory-context>|\Z)\s*")
_ORIGIN_HEADER = re.compile(
    r"(?s)(?:^|(?<=\n))[ \t]*Gateway message origin \(JSON data[^\n{]*?(?::|\n)\s*(?:```[a-z]*\n)?\s*\{[^{}]*\}\s*(?:```\s*)?"
    r"(?:Do not guess a reply destination[^\n]*?(?:insufficient\.|\n|$)\s*)?")
_REPLY_DEST = re.compile(r"(?m)^[ \t]*Do not guess a reply destination when these fields are insufficient\.[ \t]*\n?")
_IMAGE_ATTACHED = re.compile(r"\[Image attached at:[^\]\n]{0,500}\]\s*", re.I)
_NO_TEXT = re.compile(r"(?:\[[A-Za-z0-9_.\- ]{1,40}\][ \t]*)?\(The user sent a message with no text content\)\s*", re.I)
_SENDER_PREFIX = re.compile(r"^\s*\[[A-Za-z0-9_.\- ]{1,40}\]\s+(?=\S)")
# A steer into a running turn: "[OUT-OF-BAND USER MESSAGE — …]\n<words>\n[/OUT-OF-BAND USER MESSAGE]".
_OOB_OPEN = re.compile(r"\[OUT-OF-BAND USER MESSAGE\b[^\]\n]*\]\s*", re.I)
_OOB_CLOSE = re.compile(r"\s*\[/OUT-OF-BAND USER MESSAGE\]", re.I)
# A chat reply: '[Replying to( your previous message)?: "<quoted>"]' first, then the person's words,
# then the chat around it ("[Context around the replied-to message]", "[Recent channel messages]",
# the unverified-sender note), and "[New message]" before the person's newest line.
_REPLYING = re.compile(r'^\s*\[Replying to(?: your previous message)?: "', re.I)
_REPLY_END = re.compile(r'"\]\s*(?:\n|$)')
_CONTEXT_BLOCK = re.compile(r"(?m)^[ \t]*\[(?:Context around the replied-to message|Recent channel messages|"
                            r"Messages prefixed with \[unverified\][^\n]*)\][ \t]*$")
_NEW_MESSAGE = re.compile(r"(?m)^[ \t]*\[New message\][ \t]*\n?")
# Hermes' notes to the model in front of (or instead of) the person's words: "[System: …]",
# "[System note: …]", "[SYSTEM] …", "[Note: model was just switched…]", "[Runtime note: …]", the run time a
# scheduled job is told, a skipped-skill notice.
_MODEL_NOTE = re.compile(r"^\[(?:system\b[^\]\n]{0,20}?:?|note:\s*(?:model|the model)\b|runtime note:|current run time:|"
                         r"important: the following skill)", re.I)
# A /skill command or a session started with skills: Hermes sends the whole skill file. The person's
# words are the instruction after it (``/name words``).
_SKILL_OPEN = re.compile(r'^\[IMPORTANT: The user (?:has invoked the "([^"\n]{1,80})" skill|has invoked the ([^,\]\n]{1,80}?) '
                         r'skill bundle|launched this (?:CLI )?session with the "([^"\n]{1,80})" skill)', re.I)
_SKILL_INSTRUCTION = "The user has provided the following instruction alongside the skill invocation: "
_SKILL_BLOCK_ENDS = ("/scripts/foo.js`).", "with the terminal tool using the absolute path.")
_SKILL_BUNDLE_INSTRUCTION = re.compile(r"(?m)^User instruction:[ \t]*(.+)$")
# A scheduled job's prompt: Hermes' delivery hint, then the job's own prompt, then its run context.
_CRON_HINT = re.compile(r"^\[IMPORTANT: You are running as a scheduled cron job\.", re.I)
_RUN_CONTEXT = re.compile(r"\n\s*## Run Context\b")
# A Kanban worker's card: "# Kanban task t_123: <title>".
_KANBAN_TASK = re.compile(r"(?m)^# Kanban task \S+:\s*(.+)$")
# A voice note the gateway transcribed: the words in plain quotes.
_QUOTED = re.compile(r'(?s)^"(.+)"$')


def _lead_bracket(text: str) -> int:
    """End index of the ``[ … ]`` block ``text`` starts with (nesting allowed), else 0."""
    if not text.startswith("["):
        return 0
    depth = 0
    for i, ch in enumerate(text[:20000]):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i + 1
    return 0


_MODEL_NOTE_LINE = re.compile(r"(?m)^[ \t]*(?=\[(?:system\b[^\]\n]{0,20}?:|note:\s*(?:model|the model)\b|runtime note:))", re.I)


def _drop_trailing_notes(text: str) -> str:
    """The text without Hermes notes to the model that start a later line ("…words\n[System: The
    skills available…]")."""
    for _ in range(6):
        m = next((m for m in _MODEL_NOTE_LINE.finditer(text) if m.start() > 0), None)
        if not m:
            break
        end = _lead_bracket(text[m.end():])
        text = text[:m.start()] + (text[m.end() + end:] if end else "")
    return text.strip()


def _drop_model_notes(text: str) -> str:
    """The text without the Hermes notes to the model it starts with (several may stack)."""
    for _ in range(6):
        text = text.lstrip()
        if not _MODEL_NOTE.match(text):
            if text.startswith("[SYSTEM]"):
                text = text[len("[SYSTEM]"):]
                continue
            break
        end = _lead_bracket(text)
        if not end:
            return ""   # an unclosed note runs to the end: nothing of the person's in it
        text = text[end:]
    return text.strip()


def _skill_words(text: str) -> str | None:
    """``/name words`` for a /skill command (or session started with skills), else None."""
    m = _SKILL_OPEN.match(text)
    if not m:
        return None
    name = (m.group(1) or m.group(2) or m.group(3) or "").strip()
    head = text.find(_SKILL_INSTRUCTION)
    if head >= 0:
        words = text[head + len(_SKILL_INSTRUCTION):]
        words = words.split("\n\n[Runtime note:", 1)[0]
    elif m.group(2):
        got = _SKILL_BUNDLE_INSTRUCTION.search(text)
        words = got.group(1) if got else ""
    else:
        # Skills a session was started with, then whatever was asked after the last skill file.
        end = max((text.rfind(e) + len(e) for e in _SKILL_BLOCK_ENDS if text.rfind(e) >= 0), default=-1)
        words = text[end:] if end > 0 else ""
        nxt = _SKILL_OPEN.search(words.lstrip()) if words else None
        if nxt:
            words = ""
    words = words.strip()
    if _CRON_HINT.match(words):
        return words   # a scheduled job that loads skills: the job's prompt, not something typed
    if m.group(3):
        return words   # started with skills: the person's words only (no /name they never typed)
    return f"/{name} {words}".strip() if name else words


def _reply_parts(text: str) -> tuple[str | None, str]:
    """(the quoted message replied to, the rest) of a chat reply; (None, text) when it isn't one."""
    m = _REPLYING.match(text)
    if not m:
        return None, text
    end = _REPLY_END.search(text, m.end())
    if not end:
        return None, text
    return text[m.end():end.start()], text[end.end():]


def _drop_chat_context(text: str) -> str:
    """The person's words without the chat lines a gateway adds around them (the replied-to
    message's neighbours, recent channel messages): what comes before the first block, and the
    newest message after ``[New message]``."""
    block = _CONTEXT_BLOCK.search(text)
    newest = list(_NEW_MESSAGE.finditer(text))
    if not block and not newest:
        return text
    before = text[:block.start()] if block else text[:newest[0].start()]
    after = text[newest[-1].end():] if newest and (not block or newest[-1].start() > block.start()) else ""
    after = _SENDER_PREFIX.sub("", after.strip(), count=1) if after.strip() else ""
    after = re.sub(r"^\s*\[[A-Za-z0-9_.\- ]{1,40}\]\s*$", "", after)   # a sender tag with nothing after it
    return "\n\n".join(p.strip() for p in (before, after) if p.strip())


def strip_gateway(content: str) -> str:
    """A gateway user row without Hermes' wrappers: no steer wrapper, triggering-message note,
    memory context, origin header, reply quote and chat context, image-path note, no-text placeholder
    or ``[sender]`` prefix, and a transcribed voice note without its quotes."""
    text = _OOB_CLOSE.sub("", _OOB_OPEN.sub("", content or ""))
    text = _MEMORY_CONTEXT.sub("", text)
    text = _ORIGIN_HEADER.sub("", text)
    text = _REPLY_DEST.sub("", text)
    text = _TRIGGER_NOTE.sub("", text)
    text = _reply_parts(text.strip())[1]
    text = _drop_chat_context(text)
    text = _IMAGE_ATTACHED.sub("", text)
    text = _NO_TEXT.sub("", text)
    text = text.strip()
    if not text.startswith(("[Voice request", "[SYSTEM", "[Cron")):
        text = _SENDER_PREFIX.sub("", text, count=1)
    text = text.strip()
    quoted = _QUOTED.match(text)
    if quoted and '"' not in quoted.group(1):
        text = quoted.group(1).strip()
    return text


def reply_quote(content: str) -> str | None:
    """The message a chat reply answers, as one short plain line (for a small "Replying to" quote),
    else None."""
    if not isinstance(content, str):
        return None
    text = _OOB_OPEN.sub("", content)
    text = _TRIGGER_NOTE.sub("", _ORIGIN_HEADER.sub("", _MEMORY_CONTEXT.sub("", text))).strip()
    quoted, _ = _reply_parts(text)
    if quoted is None:
        return None
    shown = gateway_reply_text(quoted.replace('\\"', '"'))
    shown = re.sub(r'^\s*🎙️?\s*', "", shown)
    plain = _QUOTED.match(shown.strip())
    shown = plain.group(1) if plain else shown
    line = preview_text(redact_text(shown), 100)
    return line or None


# -- what a chat delivery adds around an answer (when one is quoted or stored) ---------------------
# "Cronjob Response: <job>\n(job_id: …)\n-------------\n\n" and the "To stop or manage this job…" footer.
_CRON_HEADER = re.compile(r"(?s)^\s*Cronjob Response:[^\n]*\n(?:\(job_id:[^\n]*\)\n)?-{3,}\s*\n")
_CRON_FOOTER = re.compile(r'(?s)\s*To stop or manage this job, send me a new message \(e\.g\. "[^"\n]*"\)\.?\s*$')
# The gateway's own status lines: "⏩ Steered into current run…", "ℹ️ Context compression deferred…".
_GATEWAY_LINE = re.compile(r"(?m)^[ \t]*(?:\[[^\]\n]{1,40}\][ \t]*)?(?:⏩[ \t]*Steered into|ℹ️?[ \t]*Context compression)[^\n]*\n?")
# A redelivered reply's prefix: "♻️ Recovered reply — the messaging platform reconnected …, so this may
# be a duplicate:" (restart / reconnect / rate-limit variants), on its own line before the answer.
_RECOVERED = re.compile(r"(?m)^[ \t]*♻️?[ \t]*Recovered reply —[^\n]*?(?:duplicate|arrived above):[ \t]*\n*")
# The runtime footer ("model · 22% · 23s", maybe a folder and a split marker after it).
_FOOTER = re.compile(r"(?m)^[ \t]*[\w.\-/:]{2,80}(?: · [^·\n]{1,60}){1,4}[ \t]*(?:\(\d{1,2}/\d{1,2}\))?[ \t]*$")
_FOOTER_SHAPE = re.compile(r"· \d{1,3}% ·")
_SPLIT_MARK = re.compile(r"[ \t]*\(\d{1,2}/\d{1,2}\)[ \t]*$")
_CRON_FAILURE = re.compile(r"^\s*\[CRON_FAILURE\][ \t]*\n?")
SILENT = "[SILENT]"


def gateway_reply_text(text: str) -> str:
    """An answer without what the chat delivery wrapped it in: cron header/footer, the runtime
    footer, "(1/2)" split markers, the gateway's own steer/compression lines."""
    out = _RECOVERED.sub("", text or "", count=1)
    out = _CRON_FAILURE.sub("", _CRON_FOOTER.sub("", _CRON_HEADER.sub("", out)))
    out = _GATEWAY_LINE.sub("", out)
    out = "\n".join(line for line in out.split("\n") if not (_FOOTER.match(line) and _FOOTER_SHAPE.search(line)))
    return _SPLIT_MARK.sub("", out.rstrip()).strip()
_MD_MARKS = re.compile(r"(\*\*|__|~~|`+)")
_MD_LEAD = re.compile(r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d{1,2}[.)]\s+)")
_MD_LINK = re.compile(r"!?\[([^\]\n]{1,200})\]\([^)\s]{1,500}\)")


def _status_line(status: str, detail: str) -> str:
    """A progress note's STATUS and DETAIL as one line, never the same words twice ("Checking the
    wiki — Checking the wiki for the trip"): a DETAIL that restates its STATUS shows alone."""
    status, detail = status.strip().rstrip("."), detail.strip().rstrip(".")
    s, d = _norm(status), _norm(detail)
    if not d or d == s or d in s:
        return status or detail
    if not s or s in d:
        return detail
    return f"{status} — {detail}"


def assistant_text(text: str, *, brief: bool = False) -> str:
    """An assistant message as shown: no machine blocks (question/views JSON), no "Voice: …" echo line,
    a progress note (STATUS:/DETAIL:) as its plain words.

    A task's final report (a written answer, then ``DONE:`` / ``SPOKEN:`` lines) shows its written
    answer: the prose, commands and code blocks the voice points at ("paste the line on screen"),
    never only the spoken sentence (SPK-187). *brief* (a list row's preview) takes the spoken
    sentence; a report with no written part shows the spoken sentence either way."""
    if not isinstance(text, str):
        return ""
    if text.strip() == SILENT or _COMPACTION.match(text):
        return ""   # a scheduled job's "nothing to report", a compaction handoff (shown as a divider)
    if text.strip() in FAILED_TURN_NOTICES:
        return ""   # Hermes' "your request was not processed" boundary: never the assistant's reply
    out = _MACHINE_BLOCK.sub("", gateway_reply_text(text))
    status, detail = _STATUS.search(out), _DETAIL.search(out)
    if status or detail:
        rest = _DETAIL.sub("", _STATUS.sub("", out)).strip()
        said = _status_line(status.group(1) if status else "", detail.group(1) if detail else "")
        out = (said + ("\n\n" + rest if rest else "")) if said else rest
    if label := _REPORT_LABEL.search(out):
        spoken = _SPOKEN.search(out)
        said = spoken.group(1).strip() if spoken and spoken.group(1).strip() else ""
        written = _VOICE_LINE.sub("", out[:label.start()], count=1).strip()
        if written and not brief:
            out = written
        else:
            out = said or _REPORT_LABEL.sub("", out[label.start():]).strip() or written
    out = _VOICE_LINE.sub("", out, count=1)
    return out.strip()


def relayed_words(text: str) -> str | None:
    """The person's own words in a request Speakeasy relayed into a chat, else None."""
    m = _RELAYED.match(text or "")
    if not m:
        return None
    body = text[m.end():]
    tail = _RELAY_TAIL.search(body)
    if tail:
        body = body[:tail.start()]
    # Several follow-ups Hermes folded into one steer: one line each, without what each one tells the task
    # or the notes Speakeasy appended for it.
    parts = [_FOLLOW_UP_TAIL.sub("", p) for p in _RELAYED_NEXT.split(body)]
    return "\n".join(w for w in (drop_task_notes(_LOOKING_AT.sub("", drop_task_notes(p))) for p in parts) if w)


def drop_task_notes(text: str) -> str:
    """The person's words without the notes Speakeasy appends for the task ("They want to SEE this…",
    "This is about the task in this thread…", the plugin-update instructions)."""
    return _TASK_NOTES.sub("", text).strip() if isinstance(text, str) else ""


def preview_text(text: str, limit: int = PREVIEW_CHARS) -> str:
    """One plain line for a list row: no markdown marks, links as their words, no notes."""
    line = _MD_LINK.sub(r"\1", text or "")
    line = _MD_LEAD.sub("", line)
    line = _MD_MARKS.sub("", line)
    line = drop_task_notes(_LOOKING_AT.sub("", line))
    return _one_line(line, limit)


def tidy_title(title: str) -> str:
    """A title as shown: an all-lowercase one ("telegram general") gets a capital first letter."""
    t = " ".join((title or "").split())
    if t and t == t.lower() and t[0].isalpha():
        t = t[0].upper() + t[1:]
    return t


class HistoryError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# -- reading state.db ----------------------------------------------------------------------------

def _open(state_db: Path) -> sqlite3.Connection | None:
    if not Path(state_db).exists():
        return None
    try:
        db = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    db.row_factory = sqlite3.Row
    return db


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _flag(cols: set[str], name: str) -> str:
    return f"coalesce(s.{name},0)=0" if name in cols else "1=1"


# What moves a conversation in the list (SPK-319): the person's own messages and finished replies.
# A running task's tool calls, tool results and progress lines are not: they update its row's
# preview and state where it sits, or every step of 4-5 running tasks reshuffled the list.
_ORDER_ROW = ("(m.role = 'user' OR (m.role = 'assistant' "
              "AND (m.tool_calls IS NULL OR m.tool_calls IN ('', '[]', 'null'))))")


def _last_expr(cols: set[str]) -> str:
    """A session's place in the list, and the time its row shows: the newest of its newest
    message from the person or finished reply (``_ORDER_ROW``) and its start (a task starting).
    Hermes' ``last_activity_at`` is left out: it is a heartbeat a running turn keeps moving, and on
    its own it lags (a short turn soon after the previous one never moved it, SPK-69). The lookup
    walks ``messages(session_id, timestamp)`` newest first and stops at the first matching row."""
    del cols
    last_msg = (f"coalesce((SELECT m.timestamp FROM messages m WHERE m.session_id = s.id AND {_ORDER_ROW} "
                "ORDER BY m.timestamp DESC LIMIT 1), 0)")
    return f"max({last_msg}, coalesce(s.started_at, 0))"


def redact_text(text: Any) -> str:
    """A message as shown: credentials and tokens out (the person's words otherwise kept as written)."""
    if not isinstance(text, str):
        return ""
    for pattern in A._TOKEN_RES:
        text = pattern.sub("…", text)
    return text[:TEXT_CHARS]


def _one_line(text: str, limit: int = PREVIEW_CHARS) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def said_key(text: str) -> str:
    """A short hash of a message's words (case and spacing ignored): how the page knows which of
    its messages were voice messages without keeping the words twice."""
    norm = " ".join((text or "").lower().split())
    if len(norm) >= 2 and norm[0] == norm[-1] == '"':
        norm = norm[1:-1].strip()  # a voice note reaches the agent quoted; the page shows it unquoted
    norm = norm[:400]
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


_LINKED_HEAD = "The user is continuing the conversation"
_CALL_TRANSCRIPT = re.compile(r"Recent timestamped voice transcript[^\n]*:\n", re.I)
_CALL_LINE = re.compile(r"^(?:\[[^\]]{1,40}\]\s*)?(User|You|Assistant)\s*:\s*(.+)$")
CALL_LINES = 40


def call_transcript(content: str) -> list[dict[str, str]] | None:
    """What was said on the call a Speakeasy task came from (its brief's transcript), as
    ``[{"who": "you"|"agent", "text": …}]``: the page shows the call as one card with this behind a
    tap. None when the row isn't a call brief."""
    if not isinstance(content, str) or not _SPEAKEASY_BRIEF.search(content[:2000]):
        return None
    m = _CALL_TRANSCRIPT.search(content)
    if not m or content[m.end():m.end() + 200].lstrip().startswith(_LINKED_HEAD):
        return None   # a message typed on a chat's page (older briefs carried the voice header too)
    lines: list[dict[str, str]] = []
    for raw in content[m.end():].splitlines():
        got = _CALL_LINE.match(raw.strip())
        if got and got.group(2).strip():
            who = "agent" if got.group(1) == "Assistant" else "you"
            lines.append({"who": who, "text": redact_text(_one_line(got.group(2), 400))})
    return lines[-CALL_LINES:] or None


_MESSAGE_ID = re.compile(r"\n?\[Speakeasy message id: ([A-Za-z0-9_-]+)\]")


def message_id(content: str) -> str | None:
    found = _MESSAGE_ID.search(content or "")
    if not found:
        return None
    try:
        cid = base64.urlsafe_b64decode(found[1] + "=" * (-len(found[1]) % 4)).decode()
        return cid if SAFE_ID_RE.fullmatch(cid) else None
    except (ValueError, UnicodeError):
        return None


def user_words(content: str) -> tuple[str, str]:
    """(kind, text) for a user row: ``user`` with the person's words; ``note`` for the one thing
    worth a quiet line (earlier messages summarized, a Kanban card); ``hidden`` for Hermes talking to
    itself (helper and background-job reports, internal notifications, system notes), whose text is
    the plain step the reply's work shows for it ("A helper finished") or empty. A background-job
    report appended after the person's words is cut out of them (``user_parts`` gives its step)."""
    kind, text, _ = user_parts(content)
    return kind, text


def user_parts(content: str) -> tuple[str, str, list[str]]:
    """``user_words`` plus the steps of Hermes reports cut out of the person's words (SPK-244: a
    background job that finished, appended to the same row): a page shows them as one quiet step in
    the reply's work, never as the person's words."""
    cut: list[str] = []
    kind, text = _user_words(content, cut)
    if not cut:
        return kind, text, []
    if kind == "hidden" or not text.strip() or _SENDER_ONLY.fullmatch(text.strip()):
        return "hidden", (text if kind == "hidden" else "") or cut[0], []
    return kind, text, cut[:1] if kind == "user" else []


def _user_words(content: str, cut: list[str] | None = None) -> tuple[str, str]:
    if isinstance(content, str):
        content = _MESSAGE_ID.sub("", content)
    if not isinstance(content, str):
        return "user", ""
    card = _kanban_head(content)
    if card:
        return "note", kanban_heading(card.group(1))
    text = strip_gateway(content)
    if _COMPACTION.match(text):
        return "note", COMPACTED
    text = _before_compaction(text)
    for _ in range(4):   # a scheduled job's prompt, model notes and skill files can stack
        before = text
        if _CRON_HINT.match(text):
            return "note", "Scheduled run"   # the job's own prompt, not something the person said
        text = _drop_trailing_notes(_drop_model_notes(text))
        skill = _skill_words(text)
        if skill is not None:
            text = _RUN_CONTEXT.split(skill, 1)[0]
        text = strip_gateway(text) if text != before else text
        if text == before:
            break
    # A background-job report merged after the person's words (SPK-244): cut out of them, one step for
    # the reply's work. A row that is itself Hermes talking (a report, a batch header, a notification
    # with the gateway's footer) or a Speakeasy brief stays as it was.
    if cut is not None and not any(p.match(text) for p, _ in _HIDDEN_RES) \
            and not _SPEAKEASY_BRIEF.search(text[:2000]):
        text, steps = drop_background_notices(text)
        cut.extend(steps)
    for pattern, step in _HIDDEN_RES:
        if pattern.search(text[:6000]):
            return "hidden", step or ""
    if not text.strip():
        return "hidden", ""
    card = _KANBAN_TASK.match(text.lstrip())
    if card:
        return "note", kanban_heading(card.group(1))
    relayed = relayed_words(text[:TEXT_CHARS])
    if relayed is not None:
        return ("user", relayed) if relayed else ("note", "Request from Speakeasy")
    follow = _FOLLOW_UP.match(text.lstrip())
    if follow:
        return "user", _LOOKING_AT.sub("", follow.group(1)).strip() or follow.group(1).strip()
    if _SPEAKEASY_BRIEF.search(text[:2000]) or (len(text) > 3000 and _USER_LINE.search(text)):
        said = _USER_LINE.findall(text)
        return (("user", _LOOKING_AT.sub("", said[-1]).strip() or said[-1].strip()) if said
                else ("note", "Task brief from Speakeasy"))
    corrected = _CORRECTION.match(text.strip())
    if corrected:
        return "user", corrected.group(1).strip()
    added = _ADDED.match(text.strip())
    if added:
        words = _SHARED.sub("", added.group(1))
        return "user", drop_task_notes(_LOOKING_AT.sub("", drop_task_notes(words)))
    return "user", drop_task_notes(text) or text


# -- a Kanban worker's session: its card as a compact header, its updates as messages -------------
_ISSUE_ID = re.compile(r"^\s*([A-Z][A-Z0-9]{1,9}-\d{1,6})\s*[:\-\u2013\u2014\u00b7]\s*(.+)$")
_CARD_BODY = re.compile(r"(?m)^## Body[ \t]*$")
_CARD_URL = re.compile(r"https://[^\s<>()\"'`]{4,300}")
_CARD_FIELD = re.compile(r"(?m)^[ \t]*(?:Assignee|Status|Workspace|Max runtime|Terminal timeout|Tenant|Priority|Skills)\s*:.*$")
_CARD_TAIL = re.compile(r"(?s)\n\s*work kanban task \S+\s*$")
_ISSUE_LINE = re.compile(r"(?im)^[ \t]*(?:\w+ )?issue [A-Z][A-Z0-9]{1,9}-\d{1,6}\b.*$")
INSTRUCTIONS_CHARS = 8000
# Kanban lifecycle calls: bookkeeping, never a step of the work.
KANBAN_QUIET = {"kanban_show", "kanban_heartbeat", "kanban_list", "kanban_attachments", "kanban_link"}
# Lifecycle calls whose words are the worker's own update to the person: (tool, the argument).
KANBAN_UPDATES = {"kanban_comment": "body", "kanban_complete": "summary", "kanban_block": "reason",
                  "kanban_request_review": "summary"}
# A tracker CLI update in a shell command: `linear comment|status|waiting ABC-12 "words"` (the CLI may
# sit in a shell variable: `$L comment ABC-12 "words"`). The command stays a step of the work too.
_SHELL_EXPANSION = re.compile(r"\$\(|`[^`]*`|\$\{?[A-Za-z_]\w*\}?")
_TRACKER_UPDATE = re.compile(r"""(?:\blinear(?:\.py)?["']?|\$\{?[A-Za-z_]\w{0,20}\}?)\s+(?:comment|status|waiting)\s+[A-Z][A-Z0-9]{1,9}-\d{1,6}\s+"""
                             r"""(?:"((?:[^"\\]|\\.){4,4000})"|'([^']{4,4000})')""")


def _kanban_head(content: str) -> re.Match[str] | None:
    """The ``# Kanban task …`` heading of a worker's dispatch prompt: at the start, or right after the
    skill files the worker was started with (never one quoted later in a conversation)."""
    head = _KANBAN_TASK.search(content, 0, 400) if not _SKILL_OPEN.match(content) else None
    if head is None and _SKILL_OPEN.match(content):
        head = _KANBAN_TASK.search(content)
        if head is not None and _SKILL_OPEN.search(content[head.start():]):
            head = None
    if head is None or content[:head.start()].strip() and not _SKILL_OPEN.match(content):
        return None
    return head


def kanban_heading(title: str) -> str:
    """A Kanban card's one-line heading: "SPK-43 · Keyboard sometimes covers content"."""
    title = _one_line(title, 160)
    m = _ISSUE_ID.match(title)
    return f"{m.group(1)} \u00b7 {m.group(2).strip()}" if m else title


def kanban_card(content: str) -> dict[str, Any] | None:
    """The card a Kanban worker was dispatched with, for a compact header: {title, issue?, url?, goal?,
    instructions}. ``instructions`` is the card body (the brief, bases, board snapshot), redacted,
    for a collapsed disclosure; the skill files loaded with it are never included."""
    if not isinstance(content, str):
        return None
    head = _kanban_head(content)
    if not head:
        return None
    title = _one_line(head.group(1), 160)
    rest = content[head.end():]
    body_at = _CARD_BODY.search(rest)
    body = rest[body_at.end():] if body_at else _CARD_FIELD.sub("", rest)
    body = _CARD_TAIL.sub("", body).strip()
    issue = _ISSUE_ID.match(title)
    out: dict[str, Any] = {"title": issue.group(2).strip() if issue else title}
    if issue:
        out["issue"] = issue.group(1)
    url = _CARD_URL.search(body[:2000])
    if url:
        out["url"] = url.group(0).rstrip(".,;:")
    goal = ""
    for para in re.split(r"\n\s*\n", body[:6000]):
        para = _ISSUE_LINE.sub("", _CARD_URL.sub("", para)).strip()
        if para and not para.startswith(("#", "---", "Integration bases", "Follow the")):
            goal = para
            break
    if goal:
        first = re.split(r"(?<=[.!?])\s+", " ".join(goal.split()), maxsplit=1)[0]
        out["goal"] = _one_line(redact_text(first), 200)
    out["instructions"] = redact_text(body)[:INSTRUCTIONS_CHARS].strip()
    return out


def tracker_update(name: str, args: Any) -> str | None:
    """The worker's own words to the person in one tool call (a Kanban comment / completion summary,
    a tracker comment or status line run from the shell), else None."""
    if not isinstance(args, dict):
        return None
    key = KANBAN_UPDATES.get(name)
    if key:
        text = args.get(key) or args.get("result")
        return text.strip() if isinstance(text, str) and text.strip() else None
    if name in {"terminal", "bash", "shell", "run_command", "exec_command"}:
        cmd = args.get("command") or args.get("cmd")
        m = _TRACKER_UPDATE.search(cmd) if isinstance(cmd, str) else None
        if m:
            text = m.group(1) if m.group(1) is not None else m.group(2)
            text = text.replace('\\"', '"').replace("\\n", "\n").strip()
            if m.group(1) is not None and _SHELL_EXPANSION.search(text):
                return None   # the shell filled part of it in ("Checked at $(date)"): not the words posted
            return text or None
    return None


def source_label(source: str, display_name: str | None, chat_type: str | None, speakeasy: bool) -> str:
    if speakeasy:
        return "Speakeasy"
    base = LABELS.get(source, source.replace("_", " ").title() if source else "Hermes")
    name = (display_name or "").strip()
    if source in {"discord", "slack"} and name:
        channel = next((p.strip() for p in name.split(" / ") if p.strip().startswith("#")), None)
        if channel:
            return f"{base} · {channel}"
    return base


def _title_from(row: sqlite3.Row, first_user: str | None) -> str:
    title = (row["title"] or "").strip() if "title" in row.keys() else ""
    if title and "source" in row.keys() and row["source"] == HELPER_SOURCE:
        title = re.sub(r"^\s*subagent\s*:\s*", "", title, flags=re.I)   # the source line already says "Helper"
    if title:
        return tidy_title(_one_line(title, 90))
    name = (row["display_name"] or "").strip() if "display_name" in row.keys() else ""
    if name and " / " in name:
        return tidy_title(_one_line(name.split(" / ")[-1], 90))
    if first_user:
        return tidy_title(preview_text(first_user, 60))
    return "Untitled conversation"


def snippet_text(snip: str) -> str:
    """A search snippet as shown: the same cleaning as a message (no gateway wrappers, no voice-report
    labels); an internal notification's snippet is left out."""
    # A cut-off wrapper the snippet window started inside of ("…message id: `123` — use as …]").
    snip = re.sub(r"^\s*…?[^\[\]\n]{0,200}(?:discord tools|reply/react/pin)[^\]\n]*\]\s*", "", snip or "")
    snip = re.sub(r"^\s*…?[^<\n]{0,200}</memory-context>\s*", "", snip)
    kind, text = user_words(snip)
    if kind != "user":
        return ""
    text = _REPORT_LABEL.sub("", _STATUS.sub(r"\1", _DETAIL.sub(r"\1", text)))
    return _one_line(preview_text(text, 200), 120)


def shown_text(role: str, content: Any) -> str:
    """What a page shows of one message row (the person's words or the answer), "" for plumbing."""
    if role == "user":
        kind, text = user_words(content)
        return text if kind == "user" else ""
    return assistant_text(content) if role == "assistant" else ""


def message_snippet(role: str, content: Any, q: str) -> str | None:
    """A search hit's snippet from what the page shows of the message, around the first query word:
    None when the words only matched plumbing the page never shows (a quoted chat line, a skill file,
    a cron header), so that row is no hit."""
    line = " ".join(preview_text(redact_text(shown_text(role, content)), TEXT_CHARS).split())
    if not line:
        return None
    words = [w.lower() for w in re.findall(r"[\w'’-]+", q) if w]
    low = line.lower()
    at = min((i for i in (low.find(w) for w in words) if i >= 0), default=-1)
    if words and at < 0:
        return None
    start = 0
    if at > 40:
        start = line.rfind(" ", 0, at - 30) + 1 or at - 30
    piece = line[start:]
    return ("…" if start else "") + _one_line(piece, 120 - (1 if start else 0))


def _fts_query(q: str) -> str:
    words = [w for w in re.findall(r"[\w'’-]+", q) if w]
    return " ".join('"' + w.replace('"', "") + '"' for w in words[:8])


def _search_ids(db: sqlite3.Connection, q: str) -> tuple[dict[str, str], bool]:
    """Sessions whose messages or title match ``q``: {session_id: snippet}, and whether the full-text
    index answered (False: the LIKE fallback did)."""
    hits: dict[str, str] = {}
    query = _fts_query(q)
    used_fts = False
    if query:
        try:
            rows = db.execute(
                "SELECT m.session_id, m.role, substr(m.content, 1, ?) FROM messages_fts "
                "JOIN messages m ON m.id = messages_fts.rowid WHERE messages_fts MATCH ? "
                "AND m.role IN ('user','assistant') ORDER BY messages_fts.rowid DESC LIMIT ?",
                (TEXT_CHARS, query, SEARCH_HITS)).fetchall()
            used_fts = True
        except sqlite3.Error:
            like = f"%{q.strip()}%"
            try:
                rows = db.execute(
                    "SELECT session_id, role, substr(content, 1, ?) FROM messages WHERE role IN ('user','assistant') "
                    "AND content LIKE ? ORDER BY id DESC LIMIT ?", (TEXT_CHARS, like, SEARCH_HITS)).fetchall()
            except sqlite3.Error:
                rows = []
        for sid, role, content in rows:
            if sid not in hits:
                snip = message_snippet(role, content, q)
                if snip is not None:   # a match only in plumbing the page never shows is no hit
                    hits[sid] = snip
    try:
        for (sid,) in db.execute("SELECT id FROM sessions WHERE title LIKE ? OR display_name LIKE ? LIMIT 400",
                                 (f"%{q.strip()}%", f"%{q.strip()}%")):
            hits.setdefault(sid, "")
    except sqlite3.Error:
        try:
            for (sid,) in db.execute("SELECT id FROM sessions WHERE title LIKE ? LIMIT 400", (f"%{q.strip()}%",)):
                hits.setdefault(sid, "")
        except sqlite3.Error:
            pass
    return hits, used_fts


def _tips(db: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    """Each session's newest compression continuation (same source, child of it): {id: tip}."""
    out = {i: i for i in ids}
    frontier = set(ids)
    for _ in range(12):
        if not frontier:
            break
        marks = ",".join("?" * len(frontier))
        try:
            rows = db.execute(f"SELECT c.id, c.parent_session_id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id "
                              f"WHERE c.parent_session_id IN ({marks}) AND c.source = p.source", tuple(frontier)).fetchall()
        except sqlite3.Error:
            break
        frontier = set()
        for child, parent in rows:
            for k, v in out.items():
                if v == parent:
                    out[k] = child
            frontier.add(child)
    return out


def _chain(db: sqlite3.Connection, session_id: str) -> list[str]:
    """Root..tip ids of one conversation (compression segments share a source)."""
    chain = [session_id]
    try:
        for _ in range(20):
            row = db.execute("SELECT p.id FROM sessions s JOIN sessions p ON p.id = s.parent_session_id "
                             "WHERE s.id=? AND p.source = s.source", (chain[0],)).fetchone()
            if not row or row[0] in chain:
                break
            chain.insert(0, row[0])
    except sqlite3.Error:
        pass
    return chain


def _pending_turn(db: sqlite3.Connection, session_id: str, now: float) -> bool:
    """A turn is under way: the newest row is the person's, a tool call or a tool result, and recent."""
    try:
        row = db.execute("SELECT role, tool_calls, timestamp FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1",
                         (session_id,)).fetchone()
    except sqlite3.Error:
        return False
    if not row or not isinstance(row["timestamp"], (int, float)) or now - row["timestamp"] > BUSY_WINDOW_S:
        return False
    return row["role"] in {"user", "tool"} or (row["role"] == "assistant" and row["tool_calls"] not in (None, "", "[]"))


def _preview(db: sqlite3.Connection, session_id: str, linked: Sequence[str] = ()) -> tuple[str, str | None]:
    """(preview line, first user words) of a session. *linked*: the sessions of its linked Speakeasy
    tasks. The preview is the newest real message in the chat or any of them, by time (as the page
    merges them), so a chat continued from Speakeasy previews the task's reply instead of the chat's
    older last message. The first user words (the title fallback) stay the chat's own."""
    preview, first = "", None
    try:
        from .history_events import event_for
        columns = _columns(db, "messages")
        display = ", display_kind" if "display_kind" in columns else ", NULL AS display_kind"
        display += ", display_metadata" if "display_metadata" in columns else ", NULL AS display_metadata"
        query = (f"SELECT id, role, content, timestamp{display} FROM messages WHERE session_id=? AND role IN ('user','assistant') "
                 "AND content IS NOT NULL AND content != '' ORDER BY id DESC LIMIT 12")
        rows = db.execute(query, (session_id,)).fetchall()
        others = [sid for sid in dict.fromkeys(linked) if sid != session_id]
        if others:
            # Each session's newest few, merged newest first by time (row id breaks ties).
            for sid in others:
                rows += db.execute(query, (sid,)).fetchall()
            rows.sort(key=lambda r: (r["timestamp"] if isinstance(r["timestamp"], (int, float)) else 0.0, r["id"]),
                      reverse=True)
        for row in rows:
            notice = event_for(row)
            if notice:
                preview = preview_text(redact_text(f"{notice['label']}: {notice['title']} — {notice['summary']}"))
                break
            kind, text = user_words(row["content"]) if row["role"] == "user" else ("assistant", assistant_text(row["content"], brief=True))
            if kind in ("user", "assistant") and text.strip():
                preview = preview_text(redact_text(text))
                break
        head = db.execute(f"SELECT id, role, content{display} FROM messages WHERE session_id=? AND role='user' AND content IS NOT NULL "
                          "ORDER BY id LIMIT 5", (session_id,)).fetchall()
        for row in head:
            if event_for(row):
                continue
            kind, text = user_words(row["content"])
            if kind == "user" and text.strip():
                first = redact_text(text)
                break
    except sqlite3.Error:
        pass
    return preview, first


@dataclasses.dataclass
class Linked:
    """What Speakeasy knows of a session: its tasks (newest first) and their state.

    *current*: the newest task's own name (what the session is doing now). *ours*: every name
    Speakeasy gave the session's tasks (and so may have set as its Hermes title). *user_title*: the
    name the user gave it from the app, which always wins (SPK-320)."""
    keys: list[str]
    name: str | None
    running: bool
    needs_you: bool
    run_ids: list[str]
    current: str | None = None
    ours: frozenset[str] = frozenset()
    user_title: str | None = None


# session_titles.candidates: a name Hermes already had gets the day, then the day and time.
_DAY_SUFFIX = re.compile(r"\s+\([A-Z][a-z]{2} \d{1,2}(?:, \d{1,2}:\d{2}[ap]m)?\)$")


def title_key(title: str | None) -> str:
    return " ".join(str(title or "").split())[:100].rstrip().casefold()


def shown_title(session_id: str, stored: str | None, link: Linked | None) -> str | None:
    """The name a conversation shows, when Speakeasy's tasks decide it (else None: the stored title).

    A Speakeasy task's session is named once, after its first task, and later requests run in the
    same session; the row kept that first name while the work moved on (SPK-320). So a session
    Speakeasy minted shows its newest task's own name, unless its title is one nobody but the user
    could have given it: a rename from the app (always wins) or any name that isn't one of its tasks'
    (another Hermes app's rename). A chat Speakeasy only continues keeps its own name."""
    if link is None:
        return None
    title = (stored or "").strip()
    if not title:
        return link.current or link.name
    if not session_id.startswith(SPEAKEASY_PREFIX) or not link.current:
        return None
    if link.user_title and title_key(link.user_title) == title_key(title):
        return None
    base = title_key(_DAY_SUFFIX.sub("", title))
    if base in link.ours or title_key(title) in link.ours:
        return link.current
    return None


def parse_cursor(cursor: str | None) -> tuple[float, str] | None:
    """``next_cursor`` is "<last activity>|<session id>" (exact; ties broken by id)."""
    if not cursor:
        return None
    last, _, sid = cursor.partition("|")
    try:
        return float(last), sid
    except ValueError:
        raise HistoryError(400, "cursor is not one this server gave out") from None


def list_sessions(state_db: Path, *, cursor: str | None = None, q: str = "", source: str = "",
                  limit: int = PAGE, linked: Callable[[str], Linked | None] = lambda sid: None,
                  speakeasy_ids: Callable[[], set[str]] = set, live: Callable[[str], bool] = lambda sid: False,
                  folded: Callable[[], dict[str, str]] = dict,
                  marks: Callable[[], dict[str, dict[str, Any]]] = dict, archived: bool = False,
                  include_archived: bool | None = None, automated: bool = True,
                  hide: Iterable[str] = (), helpers: bool = False,
                  now: float | None = None, only: set[str] | None = None, kinds: Sequence[str] | None = None,
                  on_now: Callable[[], set[str]] | None = None, exclude_now: bool = False) -> dict[str, Any]:
    """``GET /voice/history``: conversations newest first, ``limit`` per page; pass ``next_cursor`` back
    as ``cursor`` for the next page.

    *folded*: {session of a linked task: the chat it continues}. A chat continued from Speakeasy stays ONE
    row: its linked tasks' sessions are never listed on their own, and the chat sorts by its newest
    activity in either (so a reply from its page moves it to the top instead of adding a row).

    *marks*: {session: {pinned, read_at}} (the plugin's, shared by every device): rows carry ``pinned``
    and ``unread``; the first page lists pinned rows first. *archived*: list only archived conversations
    (Hermes' own ``archived`` flag, set by any Hermes app). *include_archived*: list archived ones too,
    marked ``archived`` (default: when searching, so an archived thread is still found).

    *hide*: automated kinds to leave out (``kanban``: board workers, ``scheduled``: scheduled jobs) of
    the list, the search and the ``sources`` facet, unless ``source`` asks for one of them by name;
    *automated* False is the older spelling of hiding both. *helpers*: also list background helpers
    (``subagent`` sessions; left out by default). The reply's ``automated`` says how many automated
    sessions there are and whether both are hidden, and ``kinds`` gives the apps' filter menu each
    kind's id, label, count and whether it's shown. Automated rows are never unread or needs-you.

    *kinds* (SPK-124 Earlier, ``parse_kinds``): list only those kinds, with ``kinds`` in the reply
    ({id, label, count, on} for every kind, counted with the same search) and no pinned-first order
    (Home has no pins). It replaces *source*, *archived* and *automated*. *on_now*: the sessions Home
    shows; rows carry ``on_now``, and *exclude_now* leaves them out (Earlier never repeats Home)."""
    hidden_kinds = {k for k in hide if k in AUTOMATED_FILTERS} | (set() if automated else set(AUTOMATED_FILTERS))
    automated = not set(AUTOMATED_FILTERS) <= hidden_kinds
    if kinds is not None:  # Earlier's checkboxes decide on their own (below)
        hidden_kinds, automated, helpers = set(), True, True
    after = parse_cursor(cursor)
    now = time.time() if now is None else now
    limit = max(1, min(int(limit or PAGE), MAX_PAGE))
    if source and source not in FILTERS:
        raise HistoryError(400, f"source must be one of: {', '.join(FILTERS)}")
    if kinds is not None:
        kinds = tuple(k for k in kinds if k in KINDS) or DEFAULT_KINDS
        source, archived, automated, include_archived = "", False, True, True
    db = _open(state_db)
    if db is None:
        return {"sessions": [], "next_cursor": None, "filters": _filters(), "sources": [],
                "automated": {"count": 0, "hidden": not automated, "kinds": []}, "search": "none" if q else None,
                **({"kinds": _kind_list({}, kinds)} if kinds is not None else {})}
    try:
        cols = _columns(db, "sessions")
        fold = {k: v for k, v in (folded() or {}).items() if k != v and SAFE_ID_RE.fullmatch(k) and SAFE_ID_RE.fullmatch(v)}
        last = _last_expr(cols)
        if fold:
            # The chat's own activity, or its linked tasks' (sessions named inline: ids are SAFE_ID_RE-checked).
            pairs = ",".join(f"('{src}','{dst}')" for dst, src in fold.items())
            linked_last = _last_expr(cols).replace("s.", "l.")
            last = (f"max({last}, coalesce((SELECT max({linked_last}) FROM (VALUES {pairs}) AS f "
                    f"JOIN sessions l ON l.id = f.column2 WHERE f.column1 = s.id), 0))")
        if include_archived is None:
            include_archived = bool(q.strip())
        # Helpers are counted with the rest (for the filter menu) and left out below unless asked for.
        hidden_sources = tuple(s for s in HIDDEN_SOURCES if s != HELPER_SOURCE)
        where = [f"s.source NOT IN ({','.join('?' * len(hidden_sources))})", _flag(cols, "hidden")]
        if archived:
            where.append("coalesce(s.archived,0)!=0" if "archived" in cols else "1=0")
        elif not include_archived:
            where.append(_flag(cols, "archived"))
        params: list[Any] = list(hidden_sources)
        if "parent_session_id" in cols:
            # The newest segment of a conversation only; children of another source are helpers
            # (listed only under Earlier's Subagents box).
            where.append("NOT EXISTS (SELECT 1 FROM sessions c WHERE c.parent_session_id = s.id AND c.source = s.source)")
            where.append("(s.parent_session_id IS NULL OR EXISTS (SELECT 1 FROM sessions p "
                         f"WHERE p.id = s.parent_session_id AND p.source = s.source) OR s.source = '{HELPER_SOURCE}')")
        if "message_count" in cols:
            where.append("(coalesce(s.message_count,0) > 0 OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id))")
        if fold:
            where.append(f"s.id NOT IN ({','.join('?' * len(fold))})")
            params += list(fold)
        links: dict[str, list[str]] = {}  # chat: its linked tasks' sessions
        for task_sid, chat_sid in fold.items():
            links.setdefault(chat_sid, []).append(task_sid)
        if only is not None:
            # Another agent's state.db (agents.py): only the chats Speakeasy started there.
            ids = sorted(i for i in only if SAFE_ID_RE.fullmatch(i))[:500]
            where.append(f"s.id IN ({','.join('?' * len(ids))})" if ids else "1=0")
            params += ids
        mine = speakeasy_ids()
        shown_now = (on_now() if on_now is not None else set()) or set()
        if exclude_now and shown_now:
            ids_now = sorted(sid for sid in shown_now if SAFE_ID_RE.fullmatch(sid))
            if ids_now:
                where.append(f"s.id NOT IN ({','.join('?' * len(ids_now))})")
                params += ids_now
        kind_counts: dict[str, int] = {}
        if kinds is not None:
            arch_col = "coalesce(s.archived,0)" if "archived" in cols else "0"
            kind_where, kind_params = list(where), list(params)
            if q.strip():
                hits, _ = _search_ids(db, q)
                tips = {fold.get(t, t) for t in _tips(db, set(hits)).values()}
                kind_where.append(f"s.id IN ({','.join('?' * len(tips))})" if tips else "1=0")
                kind_params += sorted(tips)
            for kind, arch, n in db.execute(
                    f"SELECT {_KIND_SQL}, {arch_col} != 0, count(*) FROM sessions s WHERE {' AND '.join(kind_where)} "
                    f"GROUP BY 1, 2", kind_params).fetchall():
                key = "archived" if arch else kind
                kind_counts[key] = kind_counts.get(key, 0) + int(n)
            cats = [k for k in kinds if k != "archived"]
            shown = [f"({arch_col} = 0 AND {_KIND_SQL} IN ({','.join('?' * len(cats))}))"] if cats else []
            if "archived" in kinds:
                shown.append(f"{arch_col} != 0")
            where.append("(" + " OR ".join(shown) + ")")
            params += cats
        sources = _source_counts(db, where, params, mine)
        counts = {c["source"]: c["count"] for c in sources}
        shown = {"kanban": "kanban" not in hidden_kinds, "scheduled": "scheduled" not in hidden_kinds, "helpers": helpers}
        auto = {"count": sum(counts.get(k, 0) for k in AUTOMATED_FILTERS), "hidden": not automated,
                "kinds": [{"id": k, "label": label, "count": counts.get(k, 0), "shown": shown[k]}
                          for k, label in AUTOMATED_KINDS.items()]}
        for kind, on in shown.items():
            if on:
                continue
            sources = [c for c in sources if c["source"] != kind]
            if source != kind:
                covered = FILTERS[kind] or ()
                where.append(f"s.source NOT IN ({','.join('?' * len(covered))})")
                params += list(covered)
        if source == "speakeasy":
            ids = sorted(mine | set())
            where.append(f"(s.id LIKE '{SPEAKEASY_PREFIX}%'" + (f" OR s.id IN ({','.join('?' * len(ids))}))" if ids else ")"))
            params += ids
        elif source:
            covered = FILTERS[source] or ()
            where.append(f"s.source IN ({','.join('?' * len(covered))})")
            params += list(covered)
            if source == "api":
                where.append(f"s.id NOT LIKE '{SPEAKEASY_PREFIX}%'")
        elif kinds is None:
            where.append("s.source != 'cron'")
        snippets: dict[str, str] = {}
        search_kind = None
        if q.strip():
            hits, used_fts = _search_ids(db, q)
            search_kind = "fts" if used_fts else "like"
            tips = _tips(db, set(hits))
            tips = {sid: fold.get(tip, tip) for sid, tip in tips.items()}  # a linked task's hit is its chat's
            snippets = {}
            for sid, tip in tips.items():
                if hits.get(sid) and not snippets.get(tip):
                    snippets[tip] = hits[sid]
                snippets.setdefault(tip, hits.get(sid, ""))
            if not snippets:
                return {"sessions": [], "next_cursor": None, "filters": _filters(), "sources": sources,
                        "automated": auto, "search": search_kind,
                        **({"kinds": _kind_list(kind_counts, kinds)} if kinds is not None else {})}
            where.append(f"s.id IN ({','.join('?' * len(snippets))})")
            params += list(snippets)
        if after is not None:
            where.append(f"({last} < ? OR ({last} = ? AND s.id < ?))")
            params += [after[0], after[0], after[1]]
        marked = marks() or {}
        pins = sorted((sid for sid, m in marked.items() if m.get("pinned") and SAFE_ID_RE.fullmatch(sid)),
                      key=lambda sid: -float(marked[sid]["pinned"]))
        pin_rank = "0"
        if pins and not archived and kinds is None:
            # Pinned first (newest pin on top), then everything else by last activity. The cursor
            # pages only the unpinned rest: every pinned row comes on the first page.
            pin_rank = "(CASE s.id " + " ".join(f"WHEN '{sid}' THEN {i + 1}" for i, sid in enumerate(pins[:50])) + " ELSE 0 END)"
            if after is not None:
                where.append(f"s.id NOT IN ({','.join('?' * len(pins[:50]))})")
                params += pins[:50]
        display = "s.display_name" if "display_name" in cols else "NULL"
        chat_type = "s.chat_type" if "chat_type" in cols else "NULL"
        title = "s.title" if "title" in cols else "NULL"
        arch = "coalesce(s.archived,0)" if "archived" in cols else "0"
        rows = db.execute(f"SELECT s.id, s.source, {title} AS title, {display} AS display_name, {chat_type} AS chat_type, "
                          f"s.started_at, {last} AS last, {pin_rank} AS pin, {arch} AS arch FROM sessions s WHERE {' AND '.join(where)} "
                          f"ORDER BY (pin = 0), pin, last DESC, s.id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        out = []
        tail = rows[limit - 1] if more else None
        if tail is not None and tail["pin"]:
            # The page ended inside the pinned rows: the next page starts at the newest unpinned row.
            tail = {"last": 1e18, "id": "~"}
        for row in rows[:limit]:
            sid = row["id"]
            link = linked(sid)
            # A chat continued from Speakeasy (linked tasks, no keys of its own) stays the chat's row.
            speakeasy = bool(link and link.keys) or sid.startswith(SPEAKEASY_PREFIX)
            preview, first = _preview(db, sid, links.get(sid, ()))
            running = bool(link and link.running) or live(sid) or (link is None and _pending_turn(db, sid, now))
            automated_row = row["source"] in (*AUTOMATED_SOURCES, HELPER_SOURCE) and not speakeasy
            entry: dict[str, Any] = {
                "id": sid, "title": shown_title(sid, row["title"], link) or _title_from(row, first),
                "source": "speakeasy" if speakeasy else row["source"],
                "source_label": source_label(row["source"], row["display_name"], row["chat_type"], speakeasy),
                "last_activity": round(float(row["last"] or 0), 3), "preview": preview,
                "state": "needs_you" if link and link.needs_you and not automated_row else ("running" if running else "idle"),
            }
            if automated_row:
                entry["automated"] = True
            mark = marked.get(sid) or {}
            if row["pin"]:
                entry["pinned"] = True
            if archived or row["arch"]:
                entry["archived"] = True
            read_at = mark.get("read_at")
            if (read_at is not None and float(row["last"] or 0) > float(read_at) + 1 and entry["state"] != "running"
                    and not automated_row):
                entry["unread"] = True
            if link and link.run_ids:
                entry["run_ids"] = link.run_ids[:5]
            if snippets.get(sid):
                entry["match"] = snippets[sid]
            if sid in shown_now:
                entry["on_now"] = True
            out.append(entry)
        next_cursor = f"{float(tail['last'] or 0)!r}|{tail['id']}" if tail is not None else None
        return {"sessions": out, "next_cursor": next_cursor, "filters": _filters(), "sources": sources,
                "automated": auto, "search": search_kind,
                **({"kinds": _kind_list(kind_counts, kinds)} if kinds is not None else {})}
    except sqlite3.Error as exc:
        logger.warning("speakeasy: history list failed (%s)", type(exc).__name__)
        raise HistoryError(502, "I couldn't read the Hermes history.") from None
    finally:
        db.close()


def _kind_list(counts: dict[str, int], kinds: Sequence[str] | None) -> list[dict[str, Any]]:
    """Earlier's filter menu: every kind in order, its label, how many it holds and whether it's on."""
    on = set(kinds or DEFAULT_KINDS)
    return [{"id": k, "label": KIND_LABELS[k], "count": int(counts.get(k, 0)), "on": k in on} for k in KINDS]


def _source_counts(db: sqlite3.Connection, where: list[str], params: list[Any], mine: set[str]) -> list[dict[str, Any]]:
    """``sources``: each filter id the person has listable sessions under, with how many, over every
    visible session (not just this page, and ignoring the search and the chosen filter), so the apps
    only offer filters that would show something. Counted the same way ``?source=`` filters."""
    counts: dict[str, int] = {}
    by_source = {s: k for k, covered in FILTERS.items() for s in (covered or ())}
    for sid, src in db.execute(f"SELECT s.id, s.source FROM sessions s WHERE {' AND '.join(where)}", params):
        speakeasy = sid.startswith(SPEAKEASY_PREFIX) or sid in mine
        if speakeasy:
            counts["speakeasy"] = counts.get("speakeasy", 0) + 1
        key = by_source.get(src)
        # ``api`` leaves out Speakeasy's own task sessions; the rest count wherever ?source= lists them.
        if key and not (key == "api" and sid.startswith(SPEAKEASY_PREFIX)):
            counts[key] = counts.get(key, 0) + 1
    return [{"source": k, "count": counts[k]} for k in FILTERS if counts.get(k)]


def _filters() -> list[dict[str, str]]:
    return [{"id": k, "label": FILTER_LABELS[k]} for k in FILTERS]


def fingerprint(state_db: Path, session_ids: list[str] | None = None) -> tuple[Any, ...] | None:
    """A cheap "has anything changed" stamp for cached history replies (indexed lookups only, a few ms
    on a multi-GB state.db): the newest message row of these sessions (or of all, for the list), and
    the sessions table's newest row and last activity. None when it can't be read (no caching)."""
    db = _open(state_db)
    if db is None:
        return None
    try:
        if session_ids:
            ids = list(dict.fromkeys(session_ids))[:40]
            marks = ",".join("?" * len(ids))
            msg = db.execute(f"SELECT max(id), count(*) FROM messages WHERE session_id IN ({marks})", ids).fetchone()
            cols = _columns(db, "sessions")
            extra = ", ".join(c for c in ("title", "archived", "hidden", "last_activity_at") if c in cols) or "id"
            meta = db.execute(f"SELECT {extra} FROM sessions WHERE id IN ({marks})", ids).fetchall()
            kids = db.execute(f"SELECT count(*) FROM sessions WHERE parent_session_id IN ({marks})", ids).fetchone() \
                if "parent_session_id" in cols else (0,)
            return (tuple(msg), tuple(tuple(r) for r in meta), tuple(kids))
        msg = db.execute("SELECT max(id) FROM messages").fetchone()
        sess = db.execute("SELECT max(rowid), count(*) FROM sessions").fetchone()
        return (tuple(msg), tuple(sess))
    except sqlite3.Error:
        return None
    finally:
        db.close()


def rules_in_session(state_db: Path, session_id: str | None, mark: str) -> bool:
    """True when a user row carrying *mark* (the full task brief's own sentence) is still in the live
    context of this session: its newest compression segment, active rows only. A compaction that
    summarized the brief away (a new segment without it, or the row marked inactive) answers False,
    and so does anything unreadable, so the caller sends the full brief again: never a follow-up
    with no rules behind it."""
    if not session_id or not mark:
        return False
    db = _open(state_db)
    if db is None:
        return False
    try:
        tip = _tips(db, {session_id}).get(session_id, session_id) \
            if "parent_session_id" in _columns(db, "sessions") else session_id
        active = " AND coalesce(active,1)=1" if "active" in _columns(db, "messages") else ""
        row = db.execute(f"SELECT 1 FROM messages WHERE session_id=? AND role='user'{active} "
                         "AND instr(content, ?) > 0 LIMIT 1", (tip, mark)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        db.close()


def session_meta(state_db: Path, session_id: str) -> dict[str, Any] | None:
    """{id, source, title, display_name, chat_type, model} of a session (its newest segment), or None."""
    db = _open(state_db)
    if db is None:
        return None
    try:
        cols = _columns(db, "sessions")
        tip = _tips(db, {session_id}).get(session_id, session_id) if "parent_session_id" in cols else session_id
        display = "display_name" if "display_name" in cols else "NULL AS display_name"
        chat = "chat_type" if "chat_type" in cols else "NULL AS chat_type"
        model = "model" if "model" in cols else "NULL AS model"  # the model Hermes ran the session on
        arch = "coalesce(archived,0) AS archived" if "archived" in cols else "0 AS archived"
        config = "model_config" if "model_config" in cols else "NULL AS model_config"
        row = db.execute(f"SELECT id, source, title, {display}, {chat}, {model}, {arch}, {config} FROM sessions WHERE id=?",
                         (tip,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["model_session_ids"] = _chain(db, tip)
        from .reply_models import object_value, clean
        config_value = object_value(result.pop("model_config", None))
        # Return only selection metadata, never endpoints or credentials in the config.
        result["model_provider"] = clean(config_value.get("provider"), 80)
        if not result["model_provider"] and "billing_provider" in cols:
            # Real Hermes records the serving provider here; its model_config is bookkeeping only.
            billed = db.execute("SELECT billing_provider FROM sessions WHERE id=?", (tip,)).fetchone()
            result["model_provider"] = clean(billed[0], 80) if billed and isinstance(billed[0], str) else None
        effort = config_value.get("reasoning_effort")
        result["model_reasoning"] = effort if effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max"} else None
        return result
    except sqlite3.Error:
        return None
    finally:
        db.close()


def session_messages(state_db: Path, session_id: str, *, before: int | None = None,
                     limit: int = MESSAGES_PAGE, model_runs: dict[str, list[dict]] | None = None,
                     run_lookup: Callable[[str], dict[str, list[dict]]] | None = None) -> dict[str, Any]:
    """``GET /voice/history/{id}``'s transcript: entries oldest first. ``user`` / ``assistant`` text,
    ``note`` for system rows, and ``tools`` groups: the calls made between two messages, each item in
    the job tool log's shape (``activity.public(detail=True)``: text, tool, args, result, status,
    started_at, ended_at), redacted the same way. ``before`` (a row id) pages back."""
    db = _open(state_db)
    if db is None:
        raise HistoryError(404, "I couldn't find that conversation.")
    try:
        found = db.execute("SELECT source FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not found:
            raise HistoryError(404, "I couldn't find that conversation.")
        chain = _chain(db, session_id)
        marks = ",".join("?" * len(chain))
        if run_lookup:
            model_runs = {key: value for sid in chain for key, value in run_lookup(sid).items()}
        cols = _columns(db, "messages")
        active = " AND coalesce(active,1)=1" if "active" in cols else ""
        hidden = " AND coalesce(display_kind,'') != 'hidden'" if "display_kind" in cols else ""
        extra = ''.join(', ' + name for name in ('model', 'provider', 'reasoning_effort', 'fallback', 'display_metadata', 'display_kind') if name in cols)
        rows = db.execute(
            f"SELECT id, session_id, role, content, tool_calls, tool_call_id, tool_name, timestamp{extra} FROM messages "
            f"WHERE session_id IN ({marks}){active}{hidden} AND id < ? ORDER BY id DESC LIMIT ?",
            (*chain, int(before) if before else 1 << 62, int(limit) + 1)).fetchall()
        scols = _columns(db, 'sessions')
        fields = ', '.join(['id'] + [c for c in ('model', 'model_config', 'billing_provider') if c in scols])
        session_models = {}
        for item in db.execute(f'SELECT {fields} FROM sessions WHERE id IN ({marks})', chain):
            item = dict(item)
            # A session's current model cannot identify older replies after switches.
            usage = set()
            if _columns(db, 'session_model_usage'):
                usage = {r[0] for r in db.execute('SELECT DISTINCT model FROM session_model_usage WHERE session_id=?', (item['id'],)) if r[0]}
            if len(usage) > 1:
                continue
            session_models[item['id']] = {'model': item.get('model'), 'provider': item.get('billing_provider')}
    except sqlite3.Error as exc:
        logger.warning("speakeasy: history read failed (%s)", type(exc).__name__)
        raise HistoryError(502, "I couldn't read that conversation.") from None
    finally:
        db.close()
    more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    from .reply_models import attribute
    return {"entries": attribute(rows, build_entries(rows, worker=found[0] == "kanban"), session_models, model_runs), "has_more": more,
            "before": rows[0]["id"] if more and rows else None}


def _reply_entry(row: Any, at: float | None, *, with_tools: bool = False) -> dict[str, Any]:
    """Mark recorded commentary additively; preserve the sanitized text for older clients."""
    content = row["content"] or ""
    clean = _MACHINE_BLOCK.sub("", content)
    status, detail = _STATUS.search(clean), _DETAIL.search(clean)
    rest = _DETAIL.sub("", _STATUS.sub("", clean)).strip()
    narration = with_tools or (bool(status or detail) and not rest)
    entry: dict[str, Any] = {"kind": "assistant", "id": f"m{row['id']}", "at": at,
                             "text": redact_text(assistant_text(content))}
    if narration:
        entry["narration"] = True
        if status:
            entry["text"] = redact_text(status.group(1).strip())
        if detail:
            entry["work_detail"] = redact_text(detail.group(1).strip())
    return entry


def build_entries(rows: list[Any], *, worker: bool = False) -> list[dict[str, Any]]:
    """*worker*: a Kanban worker's session, whose tracker updates (comments, status lines) are its
    words to the person and show as messages; anywhere else they stay steps of the work."""
    entries: list[dict[str, Any]] = []
    tracker: A.JobActivity | None = None
    group_id: int | None = None
    names: dict[str, str] = {}
    # Helper/background reports are never shown as messages: each is one plain step in the work of
    # the reply it fed (the next tool group, or a small one right before an answer with no tools).
    pending: list[tuple[str, float | None, int]] = []
    compacted = False
    quiet: set[str] = set()          # lifecycle/update calls: never steps (their results neither)
    updates_seen: set[str] = set()   # one update per text (a status line re-posted as is)
    card_seen = False

    def flush() -> None:
        nonlocal tracker, group_id
        if tracker is not None:
            items = A.public(tracker.state, detail=True).get("activity") or []
            if items:
                entries.append({"kind": "tools", "id": f"t{group_id}", "items": items})
        tracker, group_id = None, None

    def fold_pending() -> None:
        nonlocal tracker, group_id
        if not pending:
            return
        if tracker is None:
            tracker, group_id = A.JobActivity(), pending[0][2]
        for step, at, _ in pending:
            tracker._add("message", step, at if at is not None else 0.0)
        pending.clear()

    def divider(row_id: int, at: float | None) -> None:
        nonlocal compacted
        if not compacted:   # one quiet divider per page, however many times the context was compacted
            compacted = True
            entries.append({"kind": "note", "id": f"m{row_id}", "at": at, "text": COMPACTED})

    for row in rows:
        role, content = row["role"], row["content"] or ""
        at = float(row["timestamp"]) if isinstance(row["timestamp"], (int, float)) else None
        calls = row["tool_calls"]
        from .history_events import event_for
        notice = event_for(row)
        if notice:
            flush()
            pending.clear()
            notice = {key: redact_text(value) if isinstance(value, str) and key in {"title", "summary"} else value
                      for key, value in notice.items()}
            entries.append({"kind": "note", "id": f"event:{notice['id']}", "at": at,
                            "text": f"{notice['label']}: {notice['title']} — {notice['summary']}", "event": notice})
            continue
        if role == "assistant" and _failed_turn_row(row, content):
            flush()
            pending.clear()
            # Hermes' failed-turn boundary: kept only while nothing came after it (dropped below).
            entries.append({"kind": "note", "id": f"m{row['id']}", "at": at, "text": FAILED_TURN_NOTE,
                            "_failed_turn": True})
            continue
        if role == "assistant" and isinstance(content, str) and _COMPACTION.match(content):
            flush()
            divider(row["id"], at)
            continue
        if role == "assistant" and calls not in (None, "", "[]", "null"):
            shown = assistant_text(content)
            if shown:
                if pending:
                    flush()
                    fold_pending()
                flush()
                entries.append(_reply_entry(row, at, with_tools=True))
            try:
                parsed = json.loads(calls)
            except ValueError:
                parsed = []
            for call in parsed if isinstance(parsed, list) else []:
                if not isinstance(call, dict):
                    continue
                fn = call["function"] if isinstance(call.get("function"), dict) else call
                name = fn.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    args = raw
                from .threads import _arg_preview
                cid = call.get("id") or call.get("call_id")
                # A tracker update is the worker's own words to the person only in a Kanban worker's
                # session; in a chat it is a step of the work, never a reply (SPK-104).
                said = tracker_update(name, args) if worker or card_seen else None
                if said and said not in updates_seen:
                    updates_seen.add(said)
                    flush()
                    fold_pending()
                    flush()
                    entries.append({"kind": "assistant", "id": f"m{row['id']}-u{len(entries)}", "at": at,
                                    "text": redact_text(assistant_text(said)), "update": True})
                if name in KANBAN_QUIET or name in KANBAN_UPDATES:
                    if isinstance(cid, str) and cid:
                        quiet.add(cid)
                    continue
                fold_pending()
                if tracker is None:
                    tracker, group_id = A.JobActivity(), row["id"]
                event: dict[str, Any] = {"event": "tool.started", "tool": name, "preview": _arg_preview(name, args)}
                if isinstance(cid, str) and cid:
                    event["tool_call_id"] = cid
                    names[cid] = name
                tracker.observe(event, now=at)
        elif role == "tool":
            name = row["tool_name"] or names.get(row["tool_call_id"] or "") or ""
            if tracker is None or not name or (row["tool_call_id"] or "") in quiet or name in KANBAN_QUIET:
                continue
            from .threads import TOOL_RESULT_CHARS, _tool_failed
            event = {"event": "tool.completed", "tool": name, "preview": content[:TOOL_RESULT_CHARS],
                     "error": _tool_failed(name, content)}
            if row["tool_call_id"]:
                event["tool_call_id"] = row["tool_call_id"]
            tracker.observe(event, now=at)
        elif role == "user":
            flush()
            kind, text, cut = user_parts(content)
            if kind == "hidden":
                if text:
                    pending.append((text, at, row["id"]))
                continue
            pending.clear()   # a report nothing answered stays out
            # A report cut from the person's words (SPK-244): one step in the reply that follows.
            pending.extend((step, at, row["id"]) for step in cut)
            if kind == "note" and text == COMPACTED:
                divider(row["id"], at)
            elif text.strip():
                entry: dict[str, Any] = {"kind": kind, "id": f"m{row['id']}", "at": at,
                                         "text": redact_text(text) if kind == "user" else text}
                if kind == "user" and (cid := message_id(content)):
                    entry["client_id"] = cid
                card = kanban_card(content) if kind == "note" else None
                if card and card_seen:
                    entry["text"] = "Picked up again"   # a retry's dispatch: the header shows once
                elif card:
                    entry["task"] = card
                    card_seen = True
                quote = reply_quote(content) if kind == "user" else None
                if quote:
                    entry["reply_to"] = quote
                said = call_transcript(content) if kind == "user" else None
                if said:
                    entry["call"] = {"lines": said}
                entries.append(entry)
        elif role == "assistant" and assistant_text(content):
            flush()
            fold_pending()
            flush()
            entries.append(_reply_entry(row, at))
    flush()
    from .history_events import dedupe_events
    return dedupe_entries(dedupe_events(_drop_answered_failures(entries)))


def _failed_turn_row(row: Any, content: str) -> bool:
    """Hermes' row closing a failed turn (``display_kind`` failed_turn, or its exact notice text)."""
    try:
        kind = row["display_kind"]
    except (IndexError, KeyError):
        kind = None
    return kind == "failed_turn" or (isinstance(content, str) and content.strip() in FAILED_TURN_NOTICES)


def _drop_answered_failures(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A failed turn followed by anything (a resend, a retry's reply) leaves no trace: its note goes,
    and a resend of the same words then folds into one message (dedupe_entries). Only a failed turn
    nothing has followed yet keeps its quiet note."""
    out: list[dict[str, Any]] = []
    for i, e in enumerate(entries):
        if e.pop("_failed_turn", False) and any(
                later.get("kind") in ("user", "assistant") for later in entries[i + 1:]):
            continue
        out.append(e)
    return out


# A raw shell command in a step's words ("Running cd app && make"): never a live line.
_SHELLISH = re.compile(r"&&|;|(?:^|\s)--?[A-Za-z]|=|^Running (?:cd|git|python3?|nice|env|ls|grep|sed|cat|rm|mkdir)\b")


def session_live(state_db: Path, session_id: str, entries: list[dict[str, Any]],
                 now: float | None = None) -> dict[str, str] | None:
    """A running session's live line without a Speakeasy task (a Kanban worker, a chat mid-turn):
    {now, so_far?} from its real steps (``live_status.deterministic``), or None when no turn is under way."""
    from . import live_status
    now = time.time() if now is None else now
    db = _open(state_db)
    if db is None:
        return None
    try:
        tip = _tips(db, {session_id}).get(session_id, session_id)
        if not _pending_turn(db, tip, now):
            return None
        started = db.execute("SELECT min(timestamp) FROM messages WHERE session_id=?", (tip,)).fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        db.close()
    items = [i for e in entries if e.get("kind") == "tools" for i in (e.get("items") or [])]
    line = live_status.deterministic({"items": items[-200:]}, now=now,
                                     started=float(started) if isinstance(started, (int, float)) else None)
    said = next((e for e in reversed(entries) if e.get("kind") in ("assistant", "user", "note")), None)
    if (said and said.get("kind") == "assistant" and not said.get("update") and isinstance(said.get("at"), (int, float))
            and now - float(said["at"]) <= live_status.WINDOW_S and (not line or line.get("source") != "idle")):
        # What the worker just said it's doing ("Fixing the test.") beats "Running a command"; a
        # STATUS — DETAIL note gives its STATUS. Talk about a hiccup ("Using patch instead") never.
        text = " ".join(_MD_MARKS.sub("", said.get("text") or "").split()).split(" — ", 1)[0]
        first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
        mine = None if live_status.recovery_talk(first) else live_status._clean(first.rstrip("."), live_status.NOW_CHARS)
        if mine:
            line = {**(line or {}), "now": mine}
    if not line or not line.get("now"):
        return None
    now_line = (None if _SHELLISH.search(str(line["now"])) else live_status._clean(line["now"], live_status.NOW_CHARS)) or (
        "Running a command" if str(line["now"]).startswith("Running") else "Working on it")
    so_far = live_status._clean(line.get("so_far"), live_status.SO_FAR_CHARS) if line.get("so_far") else None
    return {"now": now_line, **({"so_far": so_far} if so_far else {})}


def _norm(text: str) -> str:
    return " ".join(_MD_MARKS.sub("", text or "").lower().split())


def dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """No redundant bubbles: the same assistant text twice in a row (a progress note repeated as the
    answer, a retried final), a bare "Done." right before or after the answer, and a note repeated, show
    once. Tool groups between them don't count as a break (the answer is the same answer)."""
    out: list[dict[str, Any]] = []
    last_text: dict[str, str] = {}
    for e in entries:
        kind = e.get("kind")
        if e.get("event"):
            out.append(e)  # producer identity, not matching prose, owns event deduplication
            continue
        if kind in {"assistant", "note", "user"}:
            text = _norm(e.get("text") or "")
            prev = last_text.get(kind)
            if prev is not None and text and (text == prev or (kind == "assistant" and (
                    text in {"done", "done.", "all done", "finished"} or prev in {"done", "done.", "all done", "finished"}))):
                if kind == "assistant" and prev in {"done", "done.", "all done", "finished"}:
                    # Keep the real answer, not the bare "Done" before it.
                    for i in range(len(out) - 1, -1, -1):
                        if out[i].get("kind") == "assistant":
                            out[i] = e
                            break
                    last_text[kind] = text
                continue
            last_text[kind] = text
            if kind == "user":
                last_text.pop("assistant", None)   # a new turn: the same answer again is a real answer
        out.append(e)
    return out


def _units(entries: list[dict[str, Any]]) -> list[tuple[float, list[dict[str, Any]]]]:
    """Entries as (time, [message, the untimed entries after it]): a tool group stays with the
    message before it (one before any message takes the first message's time)."""
    units: list[tuple[float, list[dict[str, Any]]]] = []
    lead: list[dict[str, Any]] = []
    for e in entries:
        at = e.get("at")
        if isinstance(at, (int, float)):
            units.append((float(at), lead + [e]))
            lead = []
        elif units:
            units[-1][1].append(e)
        else:
            lead.append(e)
    if lead:
        units.insert(0, (float("-inf"), lead))
    return units


def merge_linked(own: list[dict[str, Any]], linked: list[dict[str, Any]], *, lower: float | None,
                 upper: float | None, note: str | None = None) -> list[dict[str, Any]]:
    """A chat's page with what was said to it from Speakeasy (its linked tasks' entries) merged in by
    time, so the page always ends with the newest activity. Only linked entries in this page's span
    come in: ``lower`` <= at < ``upper`` (None: open-ended; the newest page has no upper bound and the
    oldest no lower one), so each one shows on exactly one page. No divider between them (the page
    reads as one conversation); a ``note`` given starts each run of linked entries (ids ``linked``,
    ``linked-2``, …)."""
    lo = float("-inf") if lower is None else lower
    hi = float("inf") if upper is None else upper
    # The page keeps at most one "earlier messages were summarized" divider: the chat's own, if any.
    one = any(e.get("kind") == "note" and e.get("text") == COMPACTED for e in own)
    linked = [e for e in linked if not (one and e.get("kind") == "note" and e.get("text") == COMPACTED)]
    picked = [(at, items) for at, items in _units(linked) if lo <= at < hi]
    if not picked:
        return own
    picked.sort(key=lambda u: u[0])  # stable: a linked session's own order on ties
    # The chat's order is kept as written; each linked unit goes in before the first chat message
    # newer than it (ties: the chat's first).
    merged: list[tuple[int, list[dict[str, Any]]]] = []
    i = 0
    for at, items in _units(own):
        while i < len(picked) and picked[i][0] < at:
            merged.append((1, picked[i][1]))
            i += 1
        merged.append((0, items))
    merged += [(1, items) for _, items in picked[i:]]
    out: list[dict[str, Any]] = []
    runs, prev = 0, 0
    for origin, items in merged:
        if origin == 1 and prev != 1 and note:
            runs += 1
            out.append({"kind": "note", "id": "linked" if runs == 1 else f"linked-{runs}", "at": None, "text": note})
        out += items
        prev = origin
    return out


def message_time(state_db: Path, row_id: int | None) -> float | None:
    """The timestamp of one message row (a page boundary), or None."""
    if not row_id:
        return None
    db = _open(state_db)
    if db is None:
        return None
    try:
        row = db.execute("SELECT timestamp FROM messages WHERE id=?", (int(row_id),)).fetchone()
        return float(row[0]) if row and isinstance(row[0], (int, float)) else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


def moved_on_after(state_db: Path, session_id: str, after: float, *, replies: bool) -> bool:
    """Whether a conversation went on after *after* (a question was asked then): a newer message with
    words in it from the user, or with ``replies`` from either side (a chat's own later turns)."""
    db = _open(state_db)
    if db is None:
        return False
    roles = ("user", "assistant") if replies else ("user",)
    try:
        row = db.execute(
            f"SELECT 1 FROM messages WHERE session_id=? AND timestamp>? AND role IN ({','.join('?' * len(roles))}) "
            "AND length(trim(COALESCE(content,'')))>0 LIMIT 1", (session_id, float(after), *roles)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        db.close()


def last_words_at(state_db: Path, session_ids: Sequence[str]) -> float | None:
    """The newest message with words in it (either role) across these conversations, each followed to
    its newest compression segment, or None when none has one."""
    db = _open(state_db)
    if db is None:
        return None
    try:
        ids = {s for s in session_ids if s}
        tips = _tips(db, ids) if ids else {}
        every = sorted(ids | set(tips.values()))
        if not every:
            return None
        row = db.execute(
            f"SELECT max(timestamp) FROM messages WHERE session_id IN ({','.join('?' * len(every))}) "
            "AND role IN ('user','assistant') AND length(trim(COALESCE(content,'')))>0", tuple(every)).fetchone()
        return float(row[0]) if row and isinstance(row[0], (int, float)) else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


# -- continuing a chat Speakeasy can't write into ------------------------------------------------

def linked_context(state_db: Path, session_id: str, title: str, label: str, text: str) -> str:
    """What a linked task sees: where the conversation lives, its recent messages, then the request."""
    from .continuity import recent_exchange
    recent = recent_exchange(state_db, session_id)
    head = (f"{_LINKED_HEAD} \"{title}\" ({label}, Hermes session {session_id}) from the "
            "Speakeasy app. Work here; your answer is shown in Speakeasy only, never posted to that chat. "
            "Look up more of that session's history if you need it.")
    return head + ("\n\nIts recent messages:\n" + recent if recent else "") + f"\n\nUser: {text}"


def valid_session_id(session_id: str) -> bool:
    return bool(SAFE_ID_RE.fullmatch(session_id or ""))
