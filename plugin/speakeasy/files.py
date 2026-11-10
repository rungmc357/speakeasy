"""Files your agent made: every file Hermes handed you in a reply, across all chats
(docs/API.md "Files"; capability ``files``).

Hermes has no list of the files its agent produced. They are announced in replies as ``MEDIA:<path>``
tags, which the gateway turns into attachments on Discord, Telegram and so on. This module builds
that list from Hermes' own ``state.db`` (read-only, like history.py): assistant messages carrying a
``MEDIA:`` tag, newest first, one row per file.

What may be listed or downloaded is exactly what Hermes itself would deliver as an attachment:
Hermes' own extractor and delivery check (``gateway.platforms.base.extract_media`` and
``validate_media_delivery_path``: credential stores, ``~/.ssh``, system paths and the like are
refused), and only file types Hermes delivers. Without those (an older Hermes) only files inside
Hermes' own media caches count. Clients never see or send a path: every file is an opaque id that
is an HMAC of the resolved path under a key kept beside Speakeasy's state, and a download re-checks
the file on every read.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PAGE = 60
MAX_PAGE = 200
SCAN_NEWEST = 4000           # first scan: the newest messages with a MEDIA tag
SCAN_STEP = 2000             # later scans: new messages since the last one, at most this many per pass
RESCAN_S = 5.0               # list calls closer together than this reuse the index
FIRST_WAIT_S = 2.5           # how long a list call waits for the first (background) scan; then
                             # it answers ``loading: true`` and the app asks again (never near its timeout)
MAX_DOWNLOAD_BYTES = 200_000_000
THUMB_PX = 360
THUMB_MAX_SOURCE = 40_000_000
ID_RE = re.compile(r"^[a-z2-7]{26}$")
HIDDEN_SOURCES = ("subagent", "tool", "oneshot", "acp", "recovered")

_IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic", ".svg")
_VIDEO = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp")
_AUDIO = (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac", ".aac", ".m2a")
_DOCS = (".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".epub", ".xlsx", ".xls", ".ods", ".csv",
         ".tsv", ".json", ".xml", ".yaml", ".yml", ".pptx", ".ppt", ".odp", ".html", ".htm",
         ".kmz", ".kml", ".geojson", ".gpx")
_OTHER = (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z")
# ".key" (Keynote) is left out on purpose: the same extension names private keys.
KINDS = {**{e: "image" for e in _IMAGES}, **{e: "video" for e in _VIDEO}, **{e: "audio" for e in _AUDIO},
         **{e: "document" for e in _DOCS}, **{e: "other" for e in _OTHER}}
KIND_LABELS = {"image": "Images", "video": "Videos", "audio": "Audio", "document": "Docs", "other": "Other"}
_EXTRA_MIME = {".md": "text/markdown", ".heic": "image/heic", ".m4a": "audio/mp4", ".opus": "audio/ogg",
               ".kml": "application/vnd.google-earth.kml+xml", ".kmz": "application/vnd.google-earth.kmz",
               ".geojson": "application/geo+json", ".gpx": "application/gpx+xml", ".yaml": "application/yaml",
               ".yml": "application/yaml", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
               ".webp": "image/webp", ".webm": "video/webm"}
_FALLBACK_TAG = re.compile(r"""MEDIA:[ \t]*[`"']?(?P<p>(?:~/|/)[^\s`"'<>]+?)[`"',.;:)\]]*(?=\s|$)""")
# A MEDIA tag that ends its line (Hermes' delivery form), bare, in backticks or quoted.
_TAG_LINE = re.compile(r"""(?m)(?:^[ \t]*|[ \t]+)MEDIA:[ \t]*(?:`(?P<tick>[^`\n]+)`|"(?P<quote>[^"\n]+)"|(?P<bare>(?:~/|/)\S+))[ \t]*$""")
_CACHE_DIRS = ("cache/images", "cache/audio", "cache/videos", "cache/documents", "cache/screenshots",
               "cache/generated", "image_cache", "audio_cache", "video_cache", "document_cache",
               "browser_screenshots")


class FilesError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def kind_of(name: str) -> str | None:
    return KINDS.get(os.path.splitext(name)[1].lower())


def mime_of(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return _EXTRA_MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


# -- Hermes' own rules, with a narrow fallback ------------------------------------------------------

def hermes_extract(content: str) -> list[str]:
    """Every ``MEDIA:`` path in one assistant message, through Hermes' own extractor when present."""
    try:
        from gateway.platforms.base import BasePlatformAdapter
        found, _ = BasePlatformAdapter.extract_media(content)
        return [str(p) for p, _voice in found if isinstance(p, str)]
    except Exception:
        return [m.group("p") for m in _FALLBACK_TAG.finditer(content or "")]


def _fallback_check(raw: str, hermes_home: Path) -> str | None:
    """Without Hermes' delivery check: only a regular file inside one of Hermes' own media caches."""
    try:
        resolved = Path(os.path.expanduser(raw)).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    for sub in _CACHE_DIRS:
        try:
            root = (Path(hermes_home) / sub).resolve()
            resolved.relative_to(root)
        except (OSError, ValueError, RuntimeError):
            continue
        return str(resolved)
    return None


def _worth_checking(raw: str) -> bool:
    """Cheap first look before Hermes' delivery check, which resolves every denied prefix per call
    (about 6 ms each; a first scan of a big history made ~1,300 of them and took 12 s, longer than
    the app waited): a type we list that is still a regular file on disk."""
    if kind_of(raw) is None:
        return False
    try:
        return stat.S_ISREG(os.stat(os.path.expanduser(raw)).st_mode)
    except (OSError, ValueError):
        return False


def deliverable(raw: str, hermes_home: Path) -> Path | None:
    """The resolved file if Hermes would hand it out as an attachment (and it's a type we list)."""
    if not isinstance(raw, str) or not raw or len(raw) > 1024 or "\x00" in raw:
        return None
    if not _worth_checking(raw):
        return None
    try:
        from gateway.platforms.base import validate_media_delivery_path
        safe = validate_media_delivery_path(raw)
    except ImportError:
        safe = _fallback_check(raw, hermes_home)
    except Exception:
        return None
    if not safe:
        return None
    path = Path(safe)
    if kind_of(path.name) is None:
        return None
    try:
        info = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return path


# -- the index -------------------------------------------------------------------------------------

class FileIndex:
    """Files named in Hermes replies, kept in memory and topped up from ``state.db`` as it grows."""

    def __init__(self, state_db: Path, hermes_home: Path, key_path: Path):
        self.state_db = Path(state_db)
        self.home = Path(hermes_home)
        self.key = self._load_key(Path(key_path))
        self.lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.last_id = 0
        self.scanned_at = 0.0
        # id -> {"path", "session_id", "at", "message_id"}; newest mention wins
        self.entries: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.ready = threading.Event()
        self.scanner: threading.Thread | None = None

    @staticmethod
    def _load_key(path: Path) -> bytes:
        try:
            raw = path.read_bytes()
            if len(raw) >= 32:
                return raw[:32]
        except OSError:
            pass
        raw = secrets.token_bytes(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
        except OSError:
            logger.warning("speakeasy: couldn't save the files key; file ids change on restart")
        return raw

    def file_id(self, path: Path) -> str:
        import base64
        digest = hmac.new(self.key, str(path).encode("utf-8", "surrogatepass"), hashlib.sha256).digest()
        return base64.b32encode(digest[:16]).decode().lower().rstrip("=")

    def message_files(self, text: str, session_id: str = "", at: float | None = None,
                      ) -> tuple[str, list[dict[str, Any]]]:
        """A chat message's files, for its thread (SPK-214): the text without its ``MEDIA:`` tag lines,
        and each file Hermes would deliver, in ``listing``'s row shape (an opaque id, never a path), so
        the app shows the picture in place and fetches it from ``/voice/files/{id}``. A tag Hermes
        wouldn't deliver (refused path, gone, an unlisted type) stays as text; tags in code blocks too."""
        if not isinstance(text, str) or "MEDIA:" not in text:
            return text, []
        fences = [(m.start(), m.end()) for m in re.finditer(r"(?s)```.*?```", text)]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        spans: list[tuple[int, int, str]] = []
        gone: list[tuple[int, int, str]] = []
        for match in _TAG_LINE.finditer(text):
            if any(a <= match.start() < b for a, b in fences):
                continue
            raw = (match.group("tick") or match.group("quote") or match.group("bare") or "").strip()
            raw = raw.rstrip("`\"',.;:)]")
            path = deliverable(raw, self.home)
            if path is None:
                name = Path(raw).name
                if kind_of(name) in {"image", "video"}:
                    gone.append((match.start(), match.end(), "\n"))
                    continue
                if kind_of(name) and not os.path.lexists(os.path.expanduser(raw)):
                    # Gone (a pruned scratch folder): one plain line, never a dead path.
                    gone.append((match.start(), match.end(), f"\n({'Picture' if kind_of(name) == 'image' else 'File'}"
                                                             f" no longer available: {name[:120]})\n"))
                continue
            spans.append((match.start(), match.end(), "\n"))
            fid = self.file_id(path)
            if fid in seen:
                continue
            seen.add(fid)
            try:
                info = path.stat()
            except OSError:
                continue
            with self.lock:   # so /voice/files/{id} finds it even before the next scan reaches it
                self.entries.setdefault(fid, {"path": path, "session_id": session_id, "at": at, "message_id": 0})
            rows.append({"id": fid, "name": path.name, "kind": kind_of(path.name) or "other",
                         "mime": mime_of(path.name), "bytes": int(info.st_size), "at": at or info.st_mtime,
                         "session_id": session_id})
        for start, end, put in sorted(spans + gone, reverse=True):
            text = text[:start] + put + text[end:]
        return re.sub(r"\n{3,}", "\n\n", text).strip(), rows

    def _open(self) -> sqlite3.Connection | None:
        if not self.state_db.exists():
            return None
        try:
            db = sqlite3.connect(f"file:{self.state_db}?mode=ro", uri=True, timeout=2)
            db.row_factory = sqlite3.Row
            return db
        except sqlite3.Error:
            return None

    def refresh(self, *, force: bool = False) -> None:
        # scan_lock serializes scans; self.lock only guards the entries, so a list call never waits
        # on a long scan.
        with self.scan_lock:
            if not force and time.monotonic() - self.scanned_at < RESCAN_S:
                return
            db = self._open()
            if db is None:
                self.scanned_at = time.monotonic()
                return
            try:
                top = int(db.execute("SELECT max(id) FROM messages").fetchone()[0] or 0)
                if self.last_id == 0:
                    rows = db.execute(
                        "SELECT id, session_id, content, timestamp FROM messages WHERE id <= ? AND role='assistant' "
                        "AND content LIKE '%MEDIA:%' ORDER BY id DESC LIMIT ?", (top, SCAN_NEWEST)).fetchall()
                    rows = list(reversed(rows))
                    next_id = top
                else:
                    rows = db.execute(
                        "SELECT id, session_id, content, timestamp FROM messages WHERE id > ? AND id <= ? "
                        "AND role='assistant' AND content LIKE '%MEDIA:%' ORDER BY id LIMIT ?",
                        (self.last_id, top, SCAN_STEP)).fetchall()
                    next_id = int(rows[-1]["id"]) if len(rows) >= SCAN_STEP else max(self.last_id, top)
                found: list[tuple[str, dict[str, Any]]] = []
                fresh = set()
                checked: dict[str, Path | None] = {}   # one file named in many replies: check it once
                for row in rows:
                    at = float(row["timestamp"]) if isinstance(row["timestamp"], (int, float)) else None
                    for raw in hermes_extract(row["content"] or ""):
                        if raw not in checked:
                            checked[raw] = deliverable(raw, self.home)
                        path = checked[raw]
                        if path is None:
                            continue
                        found.append((self.file_id(path), {"path": path, "session_id": row["session_id"],
                                                           "at": at, "message_id": int(row["id"])}))
                        fresh.add(row["session_id"])
                self._load_sessions(db, fresh - set(self.sessions))
                with self.lock:
                    for fid, entry in found:
                        self.entries.pop(fid, None)  # re-insert: dict order = newest mention last
                        self.entries[fid] = entry
                self.last_id = next_id
            except sqlite3.Error as exc:
                logger.warning("speakeasy: files scan failed (%s)", type(exc).__name__)
            finally:
                db.close()
                self.scanned_at = time.monotonic()

    def _load_sessions(self, db: sqlite3.Connection, ids: set[str]) -> None:
        """Each file's chat: the visible conversation (a helper's file belongs to the chat that started it)."""
        from . import history as H
        cols = H._columns(db, "sessions")
        pick = ", ".join(c for c in ("id", "source", "title", "display_name", "chat_type", "parent_session_id")
                         if c in cols)
        pending = list(ids)
        for _ in range(4):  # a helper of a helper: a few hops at most
            if not pending:
                break
            marks = ",".join("?" * len(pending))
            found = {r["id"]: dict(r) for r in db.execute(f"SELECT {pick} FROM sessions WHERE id IN ({marks})",
                                                          tuple(pending)).fetchall()}
            nxt = []
            for sid in pending:
                row = found.get(sid)
                if row is None:
                    self.sessions[sid] = {"open": sid, "title": "", "source": ""}
                    continue
                parent = row.get("parent_session_id")
                if row.get("source") in HIDDEN_SOURCES and parent:
                    self.sessions[sid] = {"via": parent}
                    if parent not in self.sessions:
                        nxt.append(parent)
                    continue
                speakeasy = str(sid).startswith(H.SPEAKEASY_PREFIX)
                self.sessions[sid] = {
                    "open": sid, "title": H.tidy_title(row.get("title") or row.get("display_name") or ""),
                    "source": H.source_label(row.get("source") or "", row.get("display_name"),
                                             row.get("chat_type"), speakeasy)}
            pending = nxt

    def _chat(self, sid: str) -> dict[str, Any]:
        seen = 0
        info = self.sessions.get(sid) or {}
        while "via" in info and seen < 5:
            info = self.sessions.get(info["via"]) or {}
            seen += 1
        return info if "open" in info else {"open": sid, "title": "", "source": ""}

    # -- reads -----------------------------------------------------------------------------------

    def listing(self, *, kind: str = "", q: str = "", session_id: str = "", cursor: str | None = None,
                limit: int = PAGE, chat_titles: Any = None) -> dict[str, Any]:
        """``GET /voice/files``: newest first. ``kinds`` lists only the kinds this person has (with counts).
        The first scan of a big ``state.db`` takes a while: it runs in the background, and until it's
        done the reply carries ``loading: true`` (ask again shortly)."""
        loading = not self._first_scan(wait=FIRST_WAIT_S)
        if not loading:
            self.refresh()
        try:
            start = max(0, int(cursor or 0))
        except ValueError:
            raise FilesError(400, "cursor must be a number") from None
        limit = max(1, min(int(limit or PAGE), MAX_PAGE))
        q = (q or "").strip().lower()[:200]
        with self.lock:
            items = list(reversed(list(self.entries.items())))
        rows: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        gone = []
        for fid, entry in items:
            path: Path = entry["path"]
            try:
                info = path.stat()
            except OSError:
                gone.append(fid)
                continue
            if not stat.S_ISREG(info.st_mode):
                gone.append(fid)
                continue
            chat = self._chat(entry["session_id"])
            if session_id and session_id not in (chat["open"], entry["session_id"]):
                continue
            k = kind_of(path.name) or "other"
            if q and q not in path.name.lower() and q not in (chat.get("title") or "").lower():
                continue
            counts[k] = counts.get(k, 0) + 1
            if kind and k != kind:
                continue
            title = chat.get("title") or ""
            if not title and callable(chat_titles):
                try:
                    title = chat_titles(chat["open"]) or ""
                except Exception:
                    title = ""
            rows.append({"id": fid, "name": path.name, "kind": k, "mime": mime_of(path.name),
                         "bytes": int(info.st_size), "at": entry["at"] or info.st_mtime,
                         "session_id": chat["open"], "chat": title, "source": chat.get("source") or ""})
            if entry.get("message_id") and entry["session_id"] == chat["open"]:
                # The reply it came in, as the session page names its entries ("m<row id>"): Open chat
                # scrolls there. A helper's file opens its parent chat with no message to point at.
                rows[-1]["message_id"] = f"m{entry['message_id']}"
        if gone:
            with self.lock:
                for fid in gone:
                    self.entries.pop(fid, None)
        page = rows[start:start + limit]
        more = start + limit < len(rows)
        kinds = [{"id": k, "label": KIND_LABELS[k], "count": counts[k]} for k in KIND_LABELS if counts.get(k)]
        return {"files": page, "kinds": kinds, "total": len(rows),
                "next": str(start + limit) if more else None, "loading": loading}

    def _first_scan(self, *, wait: float) -> bool:
        """Start the first scan in the background (once); True when it has finished."""
        if self.ready.is_set():
            return True
        with self.lock:
            if self.scanner is None:
                def run() -> None:
                    try:
                        self.refresh(force=True)
                    finally:
                        self.ready.set()
                self.scanner = threading.Thread(target=run, name="speakeasy-files-scan", daemon=True)
                self.scanner.start()
        return self.ready.wait(wait)

    def locate(self, file_id: str) -> tuple[Path, str, int]:
        """A listed file, re-checked: (path, mime, size). 404 for anything unknown, gone or now refused."""
        if not isinstance(file_id, str) or not ID_RE.fullmatch(file_id):
            raise FilesError(404, "file not found")
        with self.lock:
            entry = self.entries.get(file_id)
        if entry is None:
            self.refresh(force=True)
            with self.lock:
                entry = self.entries.get(file_id)
        if entry is None:
            raise FilesError(404, "file not found")
        path = deliverable(str(entry["path"]), self.home)
        if path is None or self.file_id(path) != file_id:
            raise FilesError(404, "file not found")
        size = path.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            raise FilesError(413, "That file is too big to send to this device.")
        return path, mime_of(path.name), int(size)

    def thumbnail(self, file_id: str) -> tuple[bytes, str]:
        """A small JPEG of an image file (for the grid); the image itself when it can't be shrunk here."""
        path, mime, size = self.locate(file_id)
        if kind_of(path.name) != "image":
            raise FilesError(404, "no preview for this file")
        try:
            from PIL import Image, ImageOps
        except ImportError:
            Image = ImageOps = None  # type: ignore[assignment]
        if Image is not None and ImageOps is not None and size <= THUMB_MAX_SOURCE and mime != "image/svg+xml":
            try:
                with Image.open(path) as im:
                    im = ImageOps.exif_transpose(im)
                    im.thumbnail((THUMB_PX, THUMB_PX))
                    if im.mode not in ("RGB", "L"):
                        im = im.convert("RGB")
                    out = io.BytesIO()
                    im.save(out, "JPEG", quality=78)
                    return out.getvalue(), "image/jpeg"
            except Exception:
                pass
        if size > 8_000_000:
            raise FilesError(404, "no preview for this file")
        return path.read_bytes(), mime
