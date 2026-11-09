"""Screens, pictures and files shared with a spoken request ("look at this").

Limits the Mac and the plugin agree on; ``/voice/status`` reports them under ``attachments``.
Images are sized for being re-sent: Hermes sends every image in a session again on each later
model call, and its whole request body is capped at 10 MB (base64 adds a third).

Two pieces live here:

- ``CallAttachments``: one call's sharing state, in memory only. Whether the client can share
  (its ``screen`` declaration), the screen toggle, pictures and files waiting for the next
  request, which handoff they are bound to, and the screen capture requests the plugin opens.
- ``SharedFolder``: Speakeasy's copies of shared files on the Hermes machine, pruned after
  ``SHARED_KEEP_S`` and deleted when the task that carried them is cleared.

Image and file bytes are never logged, and ``Attachment`` keeps them out of its repr.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote

MAX_ATTACHMENTS = 3                 # pictures and files waiting for the next request
MAX_REQUEST_IMAGES = 4              # the attachments plus one screen capture
MAX_IMAGE_BYTES = 2_500_000         # one re-encoded JPEG (the Mac aims for about 300 KB)
MAX_REQUEST_IMAGE_BYTES = 6_500_000  # every image in one request, before base64
MAX_FILE_BYTES = 10_000_000         # a non-image file, saved on the Hermes machine, never inlined
MAX_VIDEO_BYTES = 25_000_000        # an MP4/MOV clip after the phone compresses it (up to 2 minutes)


def max_upload_bytes(kind: str, mime: str | None) -> int:
    """The cap for one upload: pictures, videos (compressed on the phone first), other files."""
    if kind != "file":
        return MAX_IMAGE_BYTES
    return MAX_VIDEO_BYTES if mime in {"video/mp4", "video/quicktime"} else MAX_FILE_BYTES

KINDS = ("screen", "picture", "file")
IMAGE_KINDS = frozenset({"screen", "picture"})
IMAGE_TYPES = frozenset({"image/jpeg", "image/png"})  # the Mac re-encodes every image to one of these
DECLARATIONS = frozenset({"ready", "no_permission"})  # ``screen`` on POST /voice/sessions
# Why the Mac couldn't capture (``capture-status: failed``); the same strings as its AttachmentFailure.
FAILURE_REASONS = frozenset({"permission", "no_window", "speakeasy_window", "password_manager", "secure_input",
                             "blank", "too_large", "timeout", "unsupported"})

CAPTURE_WAIT_S = 2.5          # how long a request waits for its capture
CAPTURE_UPLOAD_WAIT_S = 6.0   # ... while the Mac reports it is uploading
CAPTURE_OPEN_TTL_S = 20.0     # an open request nobody answered closes (a stale event never captures)
SHARED_KEEP_S = 7 * 86400     # Speakeasy's copies of shared files go after a week at most
MAX_NAME_CHARS = 120
MAX_NAME_BYTES = 200          # well under the 255-byte file name limit
MAX_APP_CHARS = 60

_OPEN = frozenset({"open", "capturing", "uploading"})  # capture requests the Mac may still answer
_HELD = _OPEN | {"delivered"}                          # ... plus a capture waiting to be taken
_HASH_DIR = re.compile(r"^[0-9a-f]{32}$")
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$")


class Refused(Exception):
    """Something the call can't take. ``status`` is the HTTP status; ``reason`` a short code the app
    maps to its own line: too_many, too_large, ended, closed, sent, unknown, not_declared, permission."""

    def __init__(self, status: int, reason: str, message: str):
        super().__init__(message)
        self.status, self.reason, self.message = status, reason, message


@dataclasses.dataclass
class Attachment:
    """One shared screen capture, picture or file. Images keep their bytes in memory until they
    are sent; files are saved under the shared folder at once and keep only their ``path``."""
    id: str
    kind: str                   # screen | picture | file
    mime: str
    size: int
    name: str | None = None     # the sanitized file name (files; pictures when the app sent one)
    app: str | None = None      # the app the screen capture shows (pictures when the app sent one)
    data: bytes | None = dataclasses.field(default=None, repr=False)  # images only, never logged
    path: str | None = None     # files: Speakeasy's copy on the Hermes machine
    capture_id: str | None = None
    created: float = dataclasses.field(default_factory=time.time)
    media: dict | None = None

    @property
    def is_image(self) -> bool:
        return self.kind in IMAGE_KINDS

    def file_paths(self) -> list[str]:
        from . import video
        return ([self.path] if self.path else []) + video.paths(self.media)

    def public(self) -> dict[str, str | None]:
        """What the app may see: never bytes or paths."""
        return {"id": self.id, "kind": self.kind, "name": self.name, "app": self.app}

    def without_data(self) -> "Attachment":
        return dataclasses.replace(self, data=None)


@dataclasses.dataclass(frozen=True)
class CaptureResult:
    """What ``wait_capture`` hands back: the capture, or why there is none. ``reason`` is one of
    FAILURE_REASONS (from the Mac), or timeout, sharing_off, ended, paused, cancelled, taken, unknown."""
    attachment: Attachment | None
    reason: str | None = None


@dataclasses.dataclass
class _Item:
    attachment: Attachment
    state: str = "pending"          # pending | bound | sent
    delegation_id: str | None = None


@dataclasses.dataclass
class _Capture:
    capture_id: str
    delegation_id: str
    state: str = "open"             # open | capturing | uploading | delivered | taken | closed
    reason: str | None = None       # why it closed
    touched: float = dataclasses.field(default_factory=time.monotonic)
    attachment: Attachment | None = None


class CallAttachments:
    """One call's sharing state, in memory only; thread-safe (HTTP threads and the call's event loop).

    **Declaration.** ``declared`` is what the client said on ``POST /voice/sessions``: ``ready`` (it
    can capture the screen), ``no_permission`` (it could, but Screen Recording is off) or None (an
    older app, the iPhone). Only ``ready`` calls ever get capture requests.

    **Sharing.** ``set_screen(on, seq)`` mirrors the panel's button. Older sequence numbers are
    ignored, so a late request can't undo a newer one. Turning sharing off closes every capture
    request (reason ``sharing_off``) and drops any capture not yet taken. ``stop_sharing`` turns it
    off from the plugin's side (a spoken "stop looking"), taking the next sequence number.

    **Pictures and files** wait as *pending* until a handoff claims them: ``bind_pending`` moves them
    to that handoff, ``release`` puts them back (the request ended up needing no attachments),
    ``take_bound`` hands them over and marks them *sent*. Pending ones count against
    MAX_ATTACHMENTS, and pending pictures always leave room for one capture in a ``ready`` call.

    **Captures.** ``open_capture`` opens a request for one handoff; the app announces ``capturing``
    (answered 410 once closed: it never captures for a closed request), may report ``uploading``
    (extends the wait) or ``failed`` (closes it with the app's reason), then uploads by capture id
    (``deliver_capture``, idempotent). ``wait_capture`` hands the capture to its first waiter, once.
    An open request nobody answers closes after CAPTURE_OPEN_TTL_S.

    **End.** ``end`` closes every request, drops pending things (the app re-sends them to a resumed
    call, which starts with a fresh CallAttachments) and refuses new ones. Bound ones stay, so a
    request already on its way still carries them.
    """

    def __init__(self, declared: str | None = None):
        if declared is not None and declared not in DECLARATIONS:
            raise ValueError("screen must be ready or no_permission")
        self.declared = declared
        self._cond = threading.Condition(threading.Lock())
        self._on = False
        self._seq = -1
        self._items: dict[str, _Item] = {}        # in drop order
        self._captures: dict[str, _Capture] = {}
        self._ended: str | None = None
        self._noted_on = False                    # what the voice was last told
        self._noted_counts = (0, 0)
        self._note_waiter = False

    # -- state -----------------------------------------------------------------------------
    @property
    def ended(self) -> bool:
        return self._ended is not None

    @property
    def screen_on(self) -> bool:
        return self._on

    @property
    def can_capture(self) -> bool:
        """A capture may be requested now: a ``ready`` call with sharing on."""
        with self._cond:
            return self._can_capture()

    def _can_capture(self) -> bool:
        return self.declared == "ready" and self._on and self._ended is None

    def screen_state(self) -> dict[str, object]:
        with self._cond:
            return {"on": self._on, "seq": max(self._seq, 0)}

    def snapshot(self) -> dict[str, object]:
        """The interaction snapshot's share of this: sharing, capture requests the app should answer,
        and the pictures and files not sent yet (``sending`` once a request has claimed them)."""
        with self._cond:
            self._expire()
            return {
                "screen": {"on": self._on, "seq": max(self._seq, 0), "declared": self.declared},
                "captures": [{"capture_id": c.capture_id} for c in self._captures.values() if c.state in _OPEN],
                "attachments": [{**i.attachment.public(), "state": "pending" if i.state == "pending" else "sending"}
                                for i in self._items.values() if i.state in {"pending", "bound"}],
            }

    def file_paths(self) -> set[str]:
        """Saved files this call still holds (not sent yet): never delete these."""
        with self._cond:
            return {path for i in self._items.values() if i.state in {"pending", "bound"}
                    for path in i.attachment.file_paths()}

    # -- sharing ---------------------------------------------------------------------------
    def set_screen(self, on: bool, seq: int) -> bool:
        """Apply the panel's toggle; True when sharing actually changed. A sequence number at or
        below the last one applied is ignored. Turning it on needs a ``ready`` call."""
        with self._cond:
            if self._ended is not None:
                raise Refused(409, "ended", "the call has ended")
            if on and self.declared != "ready":
                if self.declared == "no_permission":
                    raise Refused(409, "permission", "Screen Recording is off for Speakeasy on this Mac")
                raise Refused(409, "not_declared", "this call didn't say it can share the screen")
            if seq <= self._seq:
                return False
            self._seq = seq
            changed, self._on = on != self._on, on
            if not on:
                for capture in self._captures.values():
                    if capture.state in _HELD:
                        self._close(capture, "sharing_off")
            self._cond.notify_all()
            return changed

    def stop_sharing(self) -> dict[str, object] | None:
        """Turn sharing off from the plugin's side ("stop looking at my screen"), the same as the button
        would: capture requests close (reason ``sharing_off``). It takes the next sequence number, so the
        app adopts the change (``screen.state``) and its next toggle comes after it. Returns the new
        ``{on, seq}``, or None when sharing wasn't on."""
        with self._cond:
            if not self._on or self._ended is not None:
                return None
            self._seq, self._on = max(self._seq, 0) + 1, False
            for capture in self._captures.values():
                if capture.state in _HELD:
                    self._close(capture, "sharing_off")
            self._cond.notify_all()
            return {"on": False, "seq": self._seq}

    # -- pictures and files ----------------------------------------------------------------
    def check_room(self, kind: str, size: int) -> None:
        """Raise Refused unless one more picture or file of this size fits the next request."""
        with self._cond:
            self._check_room(kind, size)

    def _check_room(self, kind: str, size: int) -> None:
        if self._ended is not None:
            raise Refused(409, "ended", "the call has ended")
        pending = [i.attachment for i in self._items.values() if i.state == "pending"]
        if len(pending) >= MAX_ATTACHMENTS:
            raise Refused(409, "too_many", f"{MAX_ATTACHMENTS} things are already waiting to go with the next request")
        if kind != "picture":
            return
        images = [a for a in pending if a.is_image]
        reserve = 1 if self.declared == "ready" else 0  # a ready call keeps room for one capture
        if (len(images) + 1 + reserve > MAX_REQUEST_IMAGES
                or sum(a.size for a in images) + size + reserve * MAX_IMAGE_BYTES > MAX_REQUEST_IMAGE_BYTES):
            raise Refused(409, "too_large", "no room for this picture next to the others"
                          + (" and a screen capture" if reserve else ""))

    def add(self, kind: str, *, mime: str, size: int, data: bytes | None = None, path: str | None = None,
            name: str | None = None, app: str | None = None) -> Attachment:
        """A picture (``data``) or file (``path``, already saved) dropped on the panel: pending."""
        if kind == "picture" and data is None or kind == "file" and path is None or kind not in {"picture", "file"}:
            raise ValueError("a picture needs data, a file needs its saved path")
        with self._cond:
            self._check_room(kind, size)
            attachment = Attachment("att_" + secrets.token_hex(8), kind, mime, size, name=name, app=app,
                                    data=data, path=path)
            self._items[attachment.id] = _Item(attachment)
            return attachment

    def remove(self, attachment_id: str) -> Attachment:
        """The ✕ on a thumbnail: allowed until the attachment has been sent (then 409)."""
        with self._cond:
            item = self._items.get(attachment_id)
            if item is None:
                raise Refused(404, "unknown", "no such attachment")
            if item.state == "sent":
                raise Refused(409, "sent", "already sent with a request")
            del self._items[attachment_id]
            return item.attachment

    def pending(self) -> list[Attachment]:
        """Pictures and files waiting for the next request, in drop order."""
        with self._cond:
            return [i.attachment for i in self._items.values() if i.state == "pending"]

    # -- per handoff -----------------------------------------------------------------------
    def bind_pending(self, delegation_id: str) -> list[Attachment]:
        """This handoff claims everything pending; returns all it holds. Later drops wait for the next."""
        with self._cond:
            for item in self._items.values():
                if item.state == "pending":
                    item.state, item.delegation_id = "bound", delegation_id
            return self._bound(delegation_id)

    def bound(self, delegation_id: str) -> list[Attachment]:
        with self._cond:
            return self._bound(delegation_id)

    def _bound(self, delegation_id: str) -> list[Attachment]:
        return [i.attachment for i in self._items.values() if i.state == "bound" and i.delegation_id == delegation_id]

    def release(self, delegation_id: str, reason: str = "cancelled") -> list[Attachment]:
        """The handoff won't carry anything (home control, a quick answer, a dropped handoff): its
        pictures and files go back to pending (dropped instead once the call has ended) and its
        capture request closes. Returns what went back."""
        with self._cond:
            back = []
            for key, item in list(self._items.items()):
                if item.state == "bound" and item.delegation_id == delegation_id:
                    if self._ended is not None:
                        del self._items[key]
                        continue
                    item.state, item.delegation_id = "pending", None
                    back.append(item.attachment)
            for capture in self._captures.values():
                if capture.delegation_id == delegation_id and capture.state in _HELD:
                    self._close(capture, reason)
            self._cond.notify_all()
            return back

    def take_bound(self, delegation_id: str) -> list[Attachment]:
        """Hand this handoff's pictures and files over (with their bytes) and mark them sent; only
        their metadata stays here."""
        with self._cond:
            taken = []
            for item in self._items.values():
                if item.state == "bound" and item.delegation_id == delegation_id:
                    taken.append(item.attachment)
                    item.state, item.attachment = "sent", item.attachment.without_data()
            return taken

    # -- screen capture requests -----------------------------------------------------------
    def open_capture(self, delegation_id: str) -> str | None:
        """Open a capture request for this handoff and return its id (the same one while it is
        still open or waiting to be taken). None when this call can't capture now."""
        with self._cond:
            self._expire()
            if not self._can_capture():
                return None
            live = self._live_capture(delegation_id)
            if live is not None:
                return live.capture_id
            capture_id = "cap_" + secrets.token_hex(8)
            self._captures[capture_id] = _Capture(capture_id, delegation_id)
            return capture_id

    def capture_for(self, delegation_id: str) -> str | None:
        """The handoff's capture request that is still open or waiting to be taken, if any."""
        with self._cond:
            self._expire()
            live = self._live_capture(delegation_id)
            return live.capture_id if live else None

    def _live_capture(self, delegation_id: str) -> _Capture | None:
        return next((c for c in self._captures.values()
                     if c.delegation_id == delegation_id and c.state in _HELD), None)

    def open_captures(self) -> list[str]:
        with self._cond:
            self._expire()
            return [c.capture_id for c in self._captures.values() if c.state in _OPEN]

    def capture_status(self, capture_id: str, status: str, reason: str | None = None) -> bool:
        """The app's report: ``capturing``, ``uploading`` (extends the wait) or ``failed`` with one of
        FAILURE_REASONS (closes the request). False when the request is closed or unknown (410)."""
        if status not in {"capturing", "uploading", "failed"} or status == "failed" and reason not in FAILURE_REASONS:
            raise ValueError("unknown capture status")
        with self._cond:
            self._expire()
            capture = self._captures.get(capture_id)
            if capture is None or capture.state not in _OPEN:
                return False
            if status == "failed":
                self._close(capture, reason or "unsupported")
            else:
                capture.state, capture.touched = status, time.monotonic()
            self._cond.notify_all()
            return True

    def check_capture(self, capture_id: str) -> None:
        """Before reading an upload's body: raise Refused (410) unless the request still takes it."""
        with self._cond:
            self._expire()
            capture = self._captures.get(capture_id)
            if capture is None or capture.state not in _OPEN | {"delivered", "taken"}:
                raise Refused(410, "closed", "that capture request is closed")

    def deliver_capture(self, capture_id: str, *, data: bytes, mime: str, app: str | None = None) -> Attachment:
        """The app's upload for an open request. A repeat upload for the same request returns the
        same attachment; a closed request raises Refused (410) and nothing is kept."""
        with self._cond:
            self._expire()
            capture = self._captures.get(capture_id)
            if capture is not None and capture.state in {"delivered", "taken"} and capture.attachment is not None:
                return capture.attachment
            if capture is None or capture.state not in _OPEN:
                raise Refused(410, "closed", "that capture request is closed")
            capture.attachment = Attachment("att_" + secrets.token_hex(8), "screen", mime, len(data), app=app,
                                            data=data, capture_id=capture_id)
            capture.state = "delivered"
            self._cond.notify_all()
            return capture.attachment

    def move_capture(self, capture_id: str, delegation_id: str) -> bool:
        """Hand a capture request that is still open (or its capture, not taken yet) to another
        handoff: the request asked a question first, and the answer carries what was on screen when
        it was asked. False when it has closed meanwhile."""
        with self._cond:
            capture = self._captures.get(capture_id)
            if capture is None or capture.state not in _HELD:
                return False
            capture.delegation_id = delegation_id
            return True

    def close_capture(self, capture_id: str, reason: str = "cancelled") -> bool:
        """Close a request (and drop its capture if not taken yet); False when it was already closed."""
        with self._cond:
            capture = self._captures.get(capture_id)
            if capture is None or capture.state not in _HELD:
                return False
            self._close(capture, reason)
            self._cond.notify_all()
            return True

    async def wait_capture(self, capture_id: str, timeout: float = CAPTURE_WAIT_S,
                           extended_timeout: float = CAPTURE_UPLOAD_WAIT_S) -> CaptureResult:
        """Wait for the capture (``timeout``, or ``extended_timeout`` once the app reports uploading),
        counted from this call. The first waiter takes it, bytes and all; a wait that runs out closes
        the request (reason ``timeout``), so a late upload is refused."""
        return await asyncio.to_thread(self.wait_capture_blocking, capture_id, timeout, extended_timeout)

    def wait_capture_blocking(self, capture_id: str, timeout: float = CAPTURE_WAIT_S,
                              extended_timeout: float = CAPTURE_UPLOAD_WAIT_S) -> CaptureResult:
        start = time.monotonic()
        with self._cond:
            while True:
                self._expire()
                capture = self._captures.get(capture_id)
                if capture is None:
                    return CaptureResult(None, "unknown")
                if capture.state == "delivered" and capture.attachment is not None:
                    taken = capture.attachment
                    capture.state, capture.attachment = "taken", taken.without_data()
                    return CaptureResult(taken)
                if capture.state not in _OPEN:
                    return CaptureResult(None, capture.reason or "taken")
                limit = extended_timeout if capture.state == "uploading" else timeout
                remaining = start + limit - time.monotonic()
                if remaining <= 0:
                    self._close(capture, "timeout")
                    self._cond.notify_all()
                    return CaptureResult(None, "timeout")
                self._cond.wait(min(remaining, 0.5))

    def _close(self, capture: _Capture, reason: str) -> None:
        capture.state, capture.reason = "closed", reason
        capture.attachment = None  # an untaken capture is discarded

    def _expire(self) -> None:
        """Requests the app never answered close after CAPTURE_OPEN_TTL_S (since the last report)."""
        now = time.monotonic()
        for capture in self._captures.values():
            if capture.state in _OPEN and now - capture.touched > CAPTURE_OPEN_TTL_S:
                self._close(capture, "timeout")

    # -- notes to the voice ----------------------------------------------------------------
    def take_notes(self) -> list[tuple[str, object]]:
        """What changed since the voice was last told: ``("screen", on)`` and/or
        ``("pending", (pictures, files))``. Coalesces: a change undone before it was told is no news."""
        with self._cond:
            if self._ended is not None:
                return []
            notes: list[tuple[str, object]] = []
            if self._on != self._noted_on:
                self._noted_on = self._on
                notes.append(("screen", self._on))
            pending = [i.attachment for i in self._items.values() if i.state == "pending"]
            counts = (sum(1 for a in pending if a.kind == "picture"), sum(1 for a in pending if a.kind == "file"))
            if counts != self._noted_counts:
                self._noted_counts = counts
                notes.append(("pending", counts))
            return notes

    def claim_note_waiter(self) -> bool:
        """One thread at a time waits for a connecting call to deliver its notes."""
        with self._cond:
            if self._note_waiter or self._ended is not None:
                return False
            self._note_waiter = True
            return True

    def release_note_waiter(self) -> None:
        with self._cond:
            self._note_waiter = False

    def attach_bound(self, delegation_id: str, attachments: list[Attachment]) -> None:
        """Pictures and files staged on a device (``StagedPool``) that a typed message or new job
        carries: held for this handoff, as if it had bound them, so ``take_bound`` hands them over.
        They never count against what waits on a call's panel."""
        with self._cond:
            for attachment in attachments:
                self._items[attachment.id] = _Item(attachment, "bound", delegation_id)

    # -- end -------------------------------------------------------------------------------
    def end(self, reason: str = "ended") -> list[Attachment]:
        """The call ended or paused: sharing off, every capture request closed (captures not taken are
        discarded), pending pictures and files dropped and returned (so their saved copies can go).
        Bound ones stay for the request already carrying them. Idempotent."""
        with self._cond:
            if self._ended is not None:
                return []
            self._ended, self._on = reason, False
            for capture in self._captures.values():
                if capture.state in _HELD:
                    self._close(capture, reason)
            dropped = [i.attachment for i in self._items.values() if i.state == "pending"]
            self._items = {k: i for k, i in self._items.items() if i.state != "pending"}
            self._cond.notify_all()
            return dropped


# -- staged on a device: pictures and files for a typed message or a new job ------------------------

MAX_STAGED = 6                # waiting on one device across its composers (each message takes at most 3)
STAGED_TTL_S = 30 * 60        # an attachment nobody sent goes after half an hour


STAGED_FILE_TYPES = frozenset({"application/pdf", "text/plain", "text/markdown", "text/csv", "application/json",
                               "video/mp4", "video/quicktime"})


def staged_file_ok(mime: str, data: bytes) -> bool:
    """A staged file is what it says: a PDF starts %PDF-, text is UTF-8 with no NUL bytes."""
    if mime in {"video/mp4", "video/quicktime"}:
        from .video import sniff
        return sniff(data)
    if mime == "application/pdf":
        return data.startswith(b"%PDF-")
    if mime in STAGED_FILE_TYPES:
        if b"\x00" in data:
            return False
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True
    return False


def strip_metadata(data: bytes, mime: str) -> bytes:
    """Remove what can say where a picture was taken (and by what): a JPEG's APP1 (Exif, XMP),
    APP13 (IPTC) and comment segments, a PNG's eXIf, tEXt, zTXt and iTXt chunks. The pixels are
    untouched. The phone strips location before upload; this makes sure nothing slips through.
    Anything that doesn't parse cleanly is returned as it came (it was already sniffed)."""
    try:
        if mime == "image/jpeg":
            return _strip_jpeg(data)
        if mime == "image/png":
            return _strip_png(data)
    except (IndexError, ValueError):
        pass
    return data


def _strip_jpeg(data: bytes) -> bytes:
    if not data.startswith(b"\xff\xd8"):
        return data
    out, i = bytearray(b"\xff\xd8"), 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            raise ValueError("not a marker")
        marker = data[i + 1]
        if marker == 0xDA:          # start of scan: the rest is image data
            out += data[i:]
            return bytes(out)
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            out += data[i:i + 2]
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2 or i + 2 + length > len(data):
            raise ValueError("bad segment")
        if marker not in (0xE1, 0xED, 0xFE):  # APP1 Exif/XMP, APP13 IPTC, COM
            out += data[i:i + 2 + length]
        i += 2 + length
    raise ValueError("no scan")


_PNG_DROP = {b"eXIf", b"tEXt", b"zTXt", b"iTXt"}


def _strip_png(data: bytes) -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(sig):
        return data
    out, i = bytearray(sig), len(sig)
    while i + 12 <= len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        end = i + 12 + length
        if end > len(data):
            raise ValueError("bad chunk")
        if kind not in _PNG_DROP:
            out += data[i:end]
        i = end
        if kind == b"IEND":
            return bytes(out)
    raise ValueError("no IEND")


class StagedPool:
    """Pictures and files a paired device uploaded before the message that carries them exists
    (``POST /voice/attachments``): the iPhone's job-page reply bar and its new-job composer. Held per
    device, in memory (pictures) or as saved copies in the shared folder (files), until a typed
    message or new job names them (``claim``), the device removes one, or STAGED_TTL_S passes.
    One device never sees or sends another's. Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Attachment]] = {}  # device id -> attachment id -> attachment

    def check_room(self, device_id: str, kind: str, size: int) -> None:
        with self._lock:
            self._check_room(device_id, kind, size)

    def _check_room(self, device_id: str, kind: str, size: int) -> None:
        if kind not in {"picture", "file"}:
            raise Refused(400, "unsupported", "only pictures and files can be staged")
        if len(self._items.get(device_id, {})) >= MAX_STAGED:
            raise Refused(409, "too_many", f"{MAX_STAGED} things are already waiting to be sent from this device")

    def add(self, device_id: str, kind: str, *, mime: str, size: int, data: bytes | None = None,
            path: str | None = None, name: str | None = None) -> Attachment:
        if kind == "picture" and data is None or kind == "file" and path is None:
            raise ValueError("a picture needs data, a file needs its saved path")
        with self._lock:
            self._check_room(device_id, kind, size)
            attachment = Attachment("att_" + secrets.token_hex(8), kind, mime, size, name=name, data=data, path=path)
            self._items.setdefault(device_id, {})[attachment.id] = attachment
            return attachment

    def remove(self, device_id: str, attachment_id: str) -> Attachment:
        with self._lock:
            found = self._items.get(device_id, {}).pop(attachment_id, None)
        if found is None:
            raise Refused(404, "unknown", "no such attachment")
        return found

    def preview(self, device_id: str, attachment_id: str) -> tuple[bytes, str]:
        """A device-owned staged poster, without claiming the original or exposing its path."""
        with self._lock:
            item = self._items.get(device_id, {}).get(attachment_id)
            if item is None:
                raise Refused(404, "unknown", "no such attachment")
            if item.is_image and item.data:
                return item.data, item.mime
            frames = (item.media or {}).get("frames", [])
            if frames:
                try:
                    data = Path(frames[0]["path"]).read_bytes()
                    if len(data) <= 500_000:
                        return data, "image/jpeg"
                except OSError:
                    pass
            raise Refused(404, "unknown", "no preview available")

    def describe(self, device_id: str | None, ids: list[str]) -> list[dict[str, object]]:
        """What a thread may show of these staged items (read-only, in order; unknown ids skipped):
        ``{id, kind: video|picture|file, mime, name?, duration?}``. Never a path or bytes."""
        with self._lock:
            mine = dict(self._items.get(device_id or "", {}))
        out: list[dict[str, object]] = []
        for i in ids:
            item = mine.get(i) if isinstance(i, str) else None
            if item is None:
                continue
            duration = (item.media or {}).get("duration")
            is_video = item.mime.startswith("video/") or isinstance(duration, (int, float))
            row: dict[str, object] = {"id": item.id, "mime": item.mime,
                                      "kind": "video" if is_video else "picture" if item.is_image else "file"}
            if item.name:
                row["name"] = item.name
            if is_video and isinstance(duration, (int, float)) and duration > 0:
                row["duration"] = round(float(duration), 1)
            out.append(row)
        return out

    def claim(self, device_id: str, ids: list[str]) -> list[Attachment]:
        """Take these (in this order) for one message: all or nothing. Refused when one is unknown
        (404, maybe expired), more than MAX_ATTACHMENTS (409 too_many) or the images break one
        request's budget (409 too_large)."""
        if len(set(ids)) != len(ids):
            raise Refused(400, "duplicate", "an attachment is named twice")
        if len(ids) > MAX_ATTACHMENTS:
            raise Refused(409, "too_many", f"at most {MAX_ATTACHMENTS} pictures or files go with one message")
        with self._lock:
            mine = self._items.get(device_id, {})
            if any(i not in mine for i in ids):
                raise Refused(404, "unknown", "an attachment is gone (it was sent, removed or expired); add it again")
            taken = [mine[i] for i in ids]
            images = [a for a in taken if a.is_image]
            if len(images) > MAX_REQUEST_IMAGES or sum(a.size for a in images) > MAX_REQUEST_IMAGE_BYTES:
                raise Refused(409, "too_large", "those pictures are too much for one message together")
            for i in ids:
                del mine[i]
            return taken

    def restore(self, device_id: str, attachments: list[Attachment]) -> None:
        """A message that claimed these never went: they wait again (a retry may send them)."""
        with self._lock:
            mine = self._items.setdefault(device_id, {})
            for attachment in attachments:
                mine[attachment.id] = attachment

    def expire(self, now: float | None = None) -> list[Attachment]:
        """Drop what waited longer than STAGED_TTL_S; returns it (so saved copies can go)."""
        cutoff = (time.time() if now is None else now) - STAGED_TTL_S
        gone: list[Attachment] = []
        with self._lock:
            for mine in self._items.values():
                for key, attachment in list(mine.items()):
                    if attachment.created < cutoff:
                        gone.append(mine.pop(key))
        return gone

    def file_paths(self) -> set[str]:
        """Saved files still waiting: never delete these."""
        with self._lock:
            return {path for mine in self._items.values() for a in mine.values() for path in a.file_paths()}


# -- names and header values ---------------------------------------------------------------------

def header_text(raw: str | None) -> str:
    """A header value the Mac sends percent-encoded (UTF-8). Raw UTF-8 that http.server read as
    Latin-1 is repaired too."""
    if not raw:
        return ""
    text = raw
    try:
        text = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return unquote(text, errors="replace")


def _printable(text: str) -> str:
    """Drop control, format (direction overrides), private-use and unassigned characters."""
    return "".join(ch for ch in unicodedata.normalize("NFC", text) if unicodedata.category(ch)[0] != "C")


def safe_filename(raw: str | None, fallback: str = "file") -> str:
    """A shared file's name as saved and shown: the last path component only (never ``..``, never a
    folder), percent-encoding undone, no control or direction-override characters, no leading dot
    (never hidden), at most MAX_NAME_CHARS characters, extension kept."""
    text = re.split(r"[\\/]", header_text(raw))[-1]
    text = " ".join(_BAD_NAME_CHARS.sub("_", _printable(text)).split()).strip(". ")
    if not text:
        return fallback
    stem, dot, ext = text.rpartition(".")
    if not dot or not stem or len(ext) > 16 or not ext.isalnum():
        stem, ext = text, ""
    suffix = f".{ext}" if ext else ""
    stem = stem[:MAX_NAME_CHARS - len(suffix)]
    while stem and len((stem + suffix).encode()) > MAX_NAME_BYTES:
        stem = stem[:-1]
    return (stem.rstrip(". ") or fallback) + suffix


def safe_app(raw: str | None) -> str | None:
    """The source app's name for display and the voice note (``X-Speakeasy-App``): short, plain."""
    text = " ".join(_printable(header_text(raw)).split())[:MAX_APP_CHARS].strip()
    return text or None


def media_type(raw: str | None) -> str:
    """``Content-Type`` without parameters, lowercased; a malformed one reads as octet-stream."""
    value = (raw or "").split(";", 1)[0].strip().lower()
    return value if _MIME.fullmatch(value) else "application/octet-stream"


# -- the shared folder ---------------------------------------------------------------------------

class SharedFolder:
    """Speakeasy's copies of shared files: ``<HERMES_HOME>/cache/speakeasy/shared``, inside the image
    roots (a saved screenshot passes ``cards.vetted_local_image``). Each copy is
    ``<sha256[:32]>/<safe name>``: the same file shared twice is one copy and names never collide.
    Owner-only (folders 0700, files 0600, never executable). ``lock`` serializes saves and deletes,
    so a copy being saved for one call is never deleted for another."""

    def __init__(self, hermes_home: Path):
        self.root = Path(hermes_home) / "cache" / "speakeasy" / "shared"
        self.lock = threading.RLock()

    def save(self, data: bytes, name: str) -> Path:
        """Write atomically (``.part``, then rename). Sharing the same file again keeps the copy and
        restarts its week."""
        folder = self.root / hashlib.sha256(data).hexdigest()[:32]
        path = folder / safe_filename(name)
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.mkdir(exist_ok=True, mode=0o700)
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                info = None
            if info is not None and stat.S_ISREG(info.st_mode) and info.st_size == len(data):
                os.utime(path)
                return path
            tmp = folder / f".{secrets.token_hex(8)}.part"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return path

    def owns(self, raw: str | os.PathLike[str]) -> bool:
        """A path this folder wrote: exactly ``<root>/<hash>/<name>``."""
        path = Path(raw)
        return path.parent.parent == self.root and bool(_HASH_DIR.fullmatch(path.parent.name)) \
            and path.name not in {"", ".", ".."}

    def delete(self, paths: Iterable[str]) -> int:
        """Remove these copies (anything outside the folder is ignored) and their emptied folders."""
        removed = 0
        with self.lock:
            for raw in paths:
                if not raw or not self.owns(raw):
                    continue
                path = Path(raw)
                try:
                    os.unlink(path)  # a symlink itself, never its target
                    removed += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
                try:
                    path.parent.rmdir()
                except OSError:
                    pass  # another name for the same content is still there
        return removed

    def prune(self, before: float) -> int:
        """Remove copies last saved before ``before`` (epoch seconds), leftovers of interrupted
        writes included, then the folders they leave empty. Never follows a symlink."""
        removed = 0
        with self.lock:
            try:
                entries = list(os.scandir(self.root))
            except FileNotFoundError:
                return 0
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False) or not _HASH_DIR.fullmatch(entry.name):
                    continue
                try:
                    children = list(os.scandir(entry.path))
                except OSError:
                    continue
                for child in children:
                    try:
                        if child.is_dir(follow_symlinks=False) or child.stat(follow_symlinks=False).st_mtime >= before:
                            continue
                        os.unlink(child.path)
                        removed += 1
                    except OSError:
                        continue
                try:
                    os.rmdir(entry.path)
                except OSError:
                    pass
        return removed
