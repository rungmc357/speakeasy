"""Server side of the app's audio relay: a minimal RFC 6455 WebSocket over the HTTP handler's socket.

Used by voice providers whose realtime socket the plugin owns (Grok): the app streams mic audio
here and plays back what comes down. Binary frames are raw PCM16LE 24 kHz mono; text frames are
small JSON control messages (docs/API.md, "Audio relay"). No extensions, no subprotocols.
"""
from __future__ import annotations

import base64
import hashlib
import json
import queue
import socket
import struct
import threading
import time
from typing import Any

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_MESSAGE = 1024 * 1024   # one app message (audio chunks are a few KB)
PING_EVERY_S = 5.0          # server pings keep the link honest while the app is muted
READ_TIMEOUT_S = 20.0       # nothing (not even a pong) for this long: the app is gone
SEND_TIMEOUT_S = 5.0
OUT_QUEUE = 2000            # frames waiting for the app (about 40 s of 20 ms audio)


class RelayProtocolError(Exception):
    pass


def accept_value(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key.strip() + GUID).encode()).digest()).decode()


def upgrade_problem(headers: Any) -> str | None:
    """Why these request headers are not a WebSocket upgrade we take (None when they are)."""
    if (headers.get("Upgrade") or "").lower() != "websocket":
        return "WebSocket upgrade required"
    if "upgrade" not in (headers.get("Connection") or "").lower():
        return "WebSocket upgrade required"
    if (headers.get("Sec-WebSocket-Version") or "").strip() != "13":
        return "WebSocket version 13 required"
    key = (headers.get("Sec-WebSocket-Key") or "").strip()
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            return "invalid Sec-WebSocket-Key"
    except ValueError:
        return "invalid Sec-WebSocket-Key"
    return None


class RelaySocket:
    """One accepted connection. ``recv`` runs on the handler thread; sends may come from any thread."""

    def __init__(self, sock: socket.socket, rfile: Any = None):
        self.sock = sock
        self.rfile = rfile or sock.makefile("rb")
        self.wlock = threading.Lock()
        self.closed = False

    # -- reading ---------------------------------------------------------------------------
    def _exact(self, n: int) -> bytes:
        data = self.rfile.read(n) if n else b""
        if len(data) != n:
            raise EOFError
        return data

    def _frame(self) -> tuple[bool, int, bytes]:
        b1, b2 = self._exact(2)
        fin, opcode, masked, length = bool(b1 & 0x80), b1 & 0x0F, bool(b2 & 0x80), b2 & 0x7F
        if b1 & 0x70:
            raise RelayProtocolError("reserved bits set")
        if not masked:
            raise RelayProtocolError("client frames must be masked")
        if length == 126:
            length = struct.unpack("!H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._exact(8))[0]
        if length > MAX_MESSAGE:
            raise RelayProtocolError("frame too large")
        mask = self._exact(4)
        payload = self._exact(length)
        if length:
            key = (mask * (length // 4 + 1))[:length]
            payload = (int.from_bytes(payload, "big") ^ int.from_bytes(key, "big")).to_bytes(length, "big")
        return fin, opcode, payload

    def recv(self) -> tuple[str, Any] | None:
        """The next message as ("binary", bytes) or ("text", str); None once the app closed."""
        parts: list[bytes] = []
        kind = 0
        while True:
            try:
                fin, opcode, payload = self._frame()
            except EOFError:
                return None
            if opcode == 0x8:  # close: answer it and stop
                self.close(1000)
                return None
            if opcode == 0x9:
                self._send(0xA, payload[:125])
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2):
                if parts:
                    raise RelayProtocolError("new message inside a fragmented one")
                kind = opcode
            elif opcode != 0x0 or not parts and not kind:
                raise RelayProtocolError("unexpected opcode")
            parts.append(payload)
            if sum(len(p) for p in parts) > MAX_MESSAGE:
                raise RelayProtocolError("message too large")
            if fin:
                data = b"".join(parts)
                if kind == 0x1:
                    return "text", data.decode("utf-8", errors="replace")
                return "binary", data

    # -- writing ---------------------------------------------------------------------------
    def _send(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, n)
        elif n < 65536:
            head = struct.pack("!BBH", 0x80 | opcode, 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 127, n)
        with self.wlock:
            if self.closed and opcode != 0x8:
                raise OSError("relay closed")
            self.sock.sendall(head + payload)

    def send_bytes(self, data: bytes) -> None:
        self._send(0x2, data)

    def send_text(self, text: str) -> None:
        self._send(0x1, text.encode())

    def ping(self) -> None:
        self._send(0x9, b"")

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        try:
            self._send(0x8, struct.pack("!H", code) + reason.encode()[:120])
        except OSError:
            pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class RelayClient:
    """What the worker sees of the app: queued, non-blocking sends from the event loop. A writer
    thread drains the queue (and pings when idle), so a slow app never stalls the voice socket.
    ``flush`` drops queued assistant audio (barge-in)."""

    def __init__(self, ws: RelaySocket):
        self.ws = ws
        self.out: queue.Queue[tuple[int, str, Any] | None] = queue.Queue(OUT_QUEUE)
        self.generation = 0
        self.closed = False
        self.audio_frames = 0
        self.audio_bytes = 0
        self.writer = threading.Thread(target=self._write, daemon=True, name="speakeasy-relay-out")
        self.writer.start()

    def audio(self, data: bytes) -> None:
        self._put((self.generation, "audio", data))

    def event(self, payload: dict[str, Any]) -> None:
        self._put((-1, "text", json.dumps(payload, separators=(",", ":"))))

    def flush(self) -> None:
        self.generation += 1

    def _put(self, item: tuple[int, str, Any]) -> None:
        if self.closed:
            return
        try:
            self.out.put_nowait(item)
        except queue.Full:
            pass  # the app stopped reading; the read timeout ends it

    def close(self, reason: str | None = None) -> None:
        """Say why (when given) after anything already queued, then close the socket."""
        if self.closed:
            return
        if reason is not None:
            self.event({"type": "closed", "reason": reason})
        self.closed = True
        try:
            self.out.put_nowait(None)
        except queue.Full:
            self.ws.close(1000)

    def _write(self) -> None:
        last = time.monotonic()
        try:
            while True:
                try:
                    item = self.out.get(timeout=PING_EVERY_S)
                except queue.Empty:
                    self.ws.ping()
                    last = time.monotonic()
                    continue
                if item is None:
                    break
                generation, kind, data = item
                if kind == "audio":
                    if generation != self.generation:
                        continue  # flushed by a barge-in
                    self.ws.send_bytes(data)
                    self.audio_frames += 1
                    self.audio_bytes += len(data)
                else:
                    self.ws.send_text(data)
                if time.monotonic() - last >= PING_EVERY_S:
                    self.ws.ping()
                    last = time.monotonic()
        except OSError:
            pass
        finally:
            self.closed = True
            self.ws.close(1000)
