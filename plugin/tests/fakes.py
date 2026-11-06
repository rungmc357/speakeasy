"""Test doubles: a fake Hermes API server (real HTTP on loopback), a fake Codex app-server
(stdio JSON-RPC in a subprocess), and a fake live-call worker. No network, no paid calls."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = Path(os.environ.get("HERMES_AGENT_SRC", Path.home() / ".hermes" / "hermes-agent"))
for p in (str(PLUGIN_ROOT), str(HERMES_SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from speakeasy.calls import SidebandWorker  # noqa: E402

class ModuleClock:
    """A stand-in for one module's ``time``: its own monotonic/time, everything else real.

    Tests must never patch ``time.monotonic`` itself (``monkeypatch.setattr(calls.time, ...)`` does,
    since ``calls.time`` IS the time module): every asyncio loop in the process reads it, so a loop left
    by an earlier test schedules its next timer against the fake clock and sleeps in select() for about
    the machine's uptime. That parked loop never finished a Hermes event its stream thread waited on,
    and that thread (a non-daemon executor worker) kept pytest from exiting after the last test."""

    def __init__(self, monotonic, wall=None):
        import time as _time
        self._real = _time
        self.monotonic = monotonic
        if wall is not None:
            self.time = wall

    def __getattr__(self, name):
        return getattr(self._real, name)


SDP = "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
FAKE_API_KEY = "test-api-server-key-not-real"


class FakeHermes(BaseHTTPRequestHandler):
    """Implements /health, /v1/runs, /v1/runs/{id}, /events, /stop, /steer, /approval.

    ``responder(prompt, session_id)`` decides each run's final output (default: echo)."""

    def log_message(self, *a: Any) -> None:
        pass

    @property
    def state(self) -> "FakeHermesServer":
        return self.server.state  # type: ignore[attr-defined]

    def _json(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self) -> bool:
        if self.headers.get("Authorization") != f"Bearer {FAKE_API_KEY}":
            self._json(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(200, {"status": "ok"})
            return
        if not self._authed():
            return
        parts = self.path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["v1", "runs"] and parts[3] == "events":
            run = self.state.runs.get(parts[2])
            if not run:
                self._json(404, {"error": "no run"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in run.get("pre_events", []):
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
                self.wfile.flush()
            run["done"].wait(10)
            for event in run["events"]:
                self.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            self.wfile.flush()
            return
        if self.path == "/api/model/options":
            with self.state.lock:
                self.state.options_reads += 1
            if self.state.model_options is None:
                self._json(500, {"error": {"code": "model_options_failed"}})
            else:
                self._json(200, self.state.model_options)
            return
        if len(parts) == 3 and parts[:2] == ["api", "sessions"]:
            with self.state.lock:
                title = self.state.session_titles.get(parts[2], False)
            if title is False:
                self._json(404, {"error": {"code": "session_not_found"}})
            else:
                self._json(200, {"object": "hermes.session", "session": {"id": parts[2], "title": title}})
            return
        if len(parts) == 3 and parts[:2] == ["v1", "runs"]:
            run = self.state.runs.get(parts[2])
            if not run:
                self._json(404, {"error": "no run"})
                return
            status = {"run_id": parts[2], "status": run["status"], "output": run["output"]}
            if run.get("error"):
                status["error"] = run["error"]
            self._json(200, status)
            return
        self._json(404, {"error": "not found"})

    def do_PATCH(self) -> None:
        """PATCH /api/sessions/{id} {"title"}: Hermes' rename; titles are unique (400 when taken)."""
        if not self._authed():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "sessions"]:
            self._json(404, {"error": "not found"})
            return
        with self.state.lock:
            db_path = self.state.state_db
            if db_path is not None:
                # Like Hermes: the PATCH writes its state.db (title, archived, pinned), which every
                # Hermes app (and Speakeasy's history) reads.
                import sqlite3
                db = sqlite3.connect(db_path)
                try:
                    if db.execute("SELECT 1 FROM sessions WHERE id=?", (parts[2],)).fetchone() is None:
                        self._json(404, {"error": {"code": "session_not_found"}})
                        return
                    title = body.get("title")
                    if title and db.execute("SELECT 1 FROM sessions WHERE title=? AND id!=?", (title, parts[2])).fetchone():
                        self._json(400, {"error": {"code": "invalid_title"}})
                        return
                    for col in ("title", "archived", "pinned"):
                        if col in body:
                            db.execute(f"UPDATE sessions SET {col}=? WHERE id=?",
                                       (body[col] if col == "title" else int(bool(body[col])), parts[2]))
                    db.commit()
                finally:
                    db.close()
                self.state.session_patches.append((parts[2], dict(body)))
                self._json(200, {"object": "hermes.session", "session": {"id": parts[2], **body}})
                return
            if parts[2] not in self.state.session_titles:
                self._json(404, {"error": {"code": "session_not_found"}})
                return
            title = body.get("title")
            if title and any(t == title for sid, t in self.state.session_titles.items() if sid != parts[2]):
                self._json(400, {"error": {"code": "invalid_title"}})
                return
            self.state.session_titles[parts[2]] = title
            self.state.title_patches.append((parts[2], title))
        self._json(200, {"object": "hermes.session", "session": {"id": parts[2], "title": title}})

    def do_POST(self) -> None:
        if not self._authed():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        parts = self.path.strip("/").split("/")
        if parts == ["v1", "runs"]:
            idem = self.headers.get("Idempotency-Key", "")
            # Hermes replays a run only for the same key AND the same body; a changed body is a 409.
            fingerprint = json.dumps([body, self.headers.get("X-Hermes-Session-Key", "")], sort_keys=True)
            with self.state.lock:
                if idem and idem in self.state.by_idem:
                    if self.state.idem_bodies.get(idem) != fingerprint:
                        self._json(409, {"error": {"code": "idempotency_key_conflict"}})
                        return
                    self._json(202, {"run_id": self.state.by_idem[idem], "status": "started"})
                    return
                self.state.counter += 1
                run_id = f"run_{self.state.counter:04d}"
                self.state.by_idem[idem] = run_id
                self.state.idem_bodies[idem] = fingerprint
            # ``input`` is recorded as sent: a string, or a list of messages with text and image parts.
            prompt, session_id = body.get("input", ""), body.get("session_id")
            self.state.calls.append({"run_id": run_id, "input": prompt, "session_id": session_id, "idem": idem,
                                     **{k: body[k] for k in ("provider", "model", "model_options") if k in body}})
            # Hermes' served runtime (api_server_runs): the requested pair, else its configured default.
            fallback = self.state.served_default or {"provider": "anthropic", "model": "claude-opus-5-5"}
            served = {"provider": body.get("provider") or fallback["provider"],
                      "model": body.get("model") or fallback["model"],
                      "route_source": "raw_request" if body.get("model") else "global"}
            if session_id and self.state.create_sessions:
                with self.state.lock:
                    # Hermes' auto-titler names a new session from the prompt's preamble.
                    self.state.session_titles.setdefault(session_id, self.state.auto_title)
            output = self.state.responder(prompt, session_id)
            run = {"status": "completed", "output": output, "done": threading.Event(),
                   "events": [{"event": "tool.started", "tool": "web_search", "preview": "weather"},
                              # Hermes' terminal fields (terminal_run_status) ride on every run.* event.
                              {"event": "run.completed", "output": output, "completed": True, "partial": False,
                               "interrupted": False, "runtime": served}]}
            if self.state.fail_with is not None:
                # Hermes' shape for a turn that failed (api_server_runs._execute_run): no output, the
                # provider's redacted error line in ``error`` on the event and on GET /v1/runs/{id}.
                # ``fail_reply`` instead gives the session chat stream's shape: the reply is Hermes'
                # failure message and there is no error field.
                reply = self.state.fail_reply
                run.update(status="failed", output=reply or "", error=None if reply else self.state.fail_with,
                           events=[{"event": "run.failed", "completed": False, "partial": False,
                                    "interrupted": False, **({"output": reply} if reply else
                                                             {"error": self.state.fail_with})}])
            if self.state.drop_terminal:
                run["events"] = [e for e in run["events"] if not e["event"].startswith("run.")]
            if self.state.approval_first:
                run["pre_events"] = [{"event": "approval.request", "request_id": "apr_" + run_id,
                                      "description": "Delete an old file", "choices": list(self.state.approval_choices)}]
            if self.state.live_events:
                run["pre_events"] = run.get("pre_events", []) + list(self.state.live_events)
            if self.state.hold or self.state.approval_first:
                run["status"] = "running"
            else:
                run["done"].set()
            self.state.runs[run_id] = run
            self._json(202, {"run_id": run_id, "status": "started"})
            return
        if len(parts) == 4 and parts[:2] == ["v1", "runs"]:
            run = self.state.runs.get(parts[2])
            if not run:
                self._json(404, {"error": "no run"})
                return
            if parts[3] == "stop":
                run["status"] = "cancelled"
                run["events"] = [{"event": "run.cancelled"}]
                run["done"].set()
                self._json(200, {"status": "stopping"})
            elif parts[3] == "steer":
                self.state.steers.append((parts[2], body.get("input", "")))
                self._json(200, {"accepted": run["status"] == "running"})
            elif parts[3] == "approval":
                self.state.approvals.append(body)
                if body.get("choice") not in self.state.approval_choices:
                    self._json(400, {"error": {"code": "invalid_approval_choice"}})
                    return
                run["status"] = "completed"
                run["done"].set()
                self._json(200, {"resolved": 1})
            else:
                self._json(404, {"error": "not found"})
            return
        self._json(404, {"error": "not found"})


class FakeHermesServer:
    def __init__(self, responder: Callable[[str, str | None], str] | None = None):
        self.lock = threading.Lock()
        self.runs: dict[str, dict[str, Any]] = {}
        self.by_idem: dict[str, str] = {}
        self.idem_bodies: dict[str, str] = {}  # what each idempotency key was first sent with
        self.calls: list[dict[str, Any]] = []
        self.approvals: list[dict[str, Any]] = []
        self.counter = 0
        self.session_titles: dict[str, str | None] = {}  # session rows the runs created: id -> title
        self.title_patches: list[tuple[str, str | None]] = []
        self.state_db: Any = None  # set: PATCH /api/sessions writes this state.db, as Hermes does
        self.session_patches: list[tuple[str, dict[str, Any]]] = []
        self.create_sessions = True
        self.served_default: dict[str, str] | None = None  # what Hermes serves when a run names no model
        self.auto_title: str | None = "Handle voice task from Speakeasy"
        self.hold = False
        self.approval_first = False
        self.approval_choices = ("once", "deny")  # the approval.request ``choices`` Hermes offers
        self.live_events: list[dict[str, Any]] = []  # streamed while a held run is still working
        self.steers: list[tuple[str, str]] = []
        self.fail_with: str | None = None  # every run fails with this Hermes error line
        self.fail_reply: str | None = None  # …or with this failure reply and no error (session chat shape)
        self.drop_terminal = False          # the event stream ends without its run.* event
        self.model_options: dict[str, Any] | None = model_options_payload()  # GET /api/model/options (None = 500)
        self.options_reads = 0
        self.responder = responder or (lambda prompt, sid: "Done.\nDONE: Finished\nSPOKEN: All done.")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHermes)
        self.httpd.daemon_threads = True
        self.httpd.state = self  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def model_options_payload() -> dict[str, Any]:
    """An invented /api/model/options payload (hermes_cli.inventory.build_model_options_payload's shape)."""
    return {
        "provider": "anthropic", "model": "claude-opus-5-5", "base_url": "",
        "providers": [
            {"slug": "anthropic", "name": "Anthropic", "is_current": True, "is_user_defined": False,
             "authenticated": True, "models": ["claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5"],
             "featured_models": [], "capabilities": {"claude-opus-5-5": {"reasoning": True},
                                                     "claude-haiku-4-5": {"reasoning": True}}},
            {"slug": "openai-codex", "name": "OpenAI Codex", "is_current": False, "is_user_defined": False,
             "authenticated": True, "models": ["gpt-6.1-sol", "gpt-5.5", "gpt-5.4-mini"],
             "capabilities": {}},
            {"slug": "studio-qwen", "name": "Studio Qwen", "is_current": False, "is_user_defined": True,
             "authenticated": True, "models": ["qwen3.8-coder"],
             "capabilities": {"qwen3.8-coder": {"reasoning": False}}},
            {"slug": "nous", "name": "Nous Portal", "is_current": False, "authenticated": False,
             "models": ["hermes-5-405b"]},
        ]}


def read_profile_env(home: Path) -> dict[str, str]:
    """A test's view of a profile .env, parsed by Hermes' own tokenizer (the plugin never reads it)."""
    from agent.secret_scope import load_env_file
    return dict(load_env_file(Path(home) / ".env"))


def make_home(api_port: int | None = None) -> Path:
    home = Path(tempfile.mkdtemp(prefix="speakeasy-test-"))
    env = [f"API_SERVER_KEY={FAKE_API_KEY}", "SPEAKEASY_OPENAI_API_KEY=test-openai-key-not-real"]
    if api_port:
        env.append(f"API_SERVER_PORT={api_port}")
    (home / ".env").write_text("\n".join(env) + "\n")
    return home


class FakeTransport:
    """Stands in for CodexTransport in service tests (no subprocess)."""

    def __init__(self) -> None:
        self.thread_id = "thr_fake"
        self.closed = False
        self.started_at = time.monotonic()
        self.max_call_s = 60
        self.requests: list[tuple[str, dict]] = []
        self.started: tuple | None = None

    def start(self, sdp: str, instructions: str, seed: list, voice: str) -> str:
        self.started = (sdp, instructions, seed, voice)
        return "v=0\r\nanswer\r\n"

    def request(self, method: str, params: dict | None = None, timeout: float = 20) -> dict:
        self.requests.append((method, params or {}))
        return {}

    def stop(self) -> None:
        self.closed = True


class FakeLiveWorker(SidebandWorker):
    """Live-call worker whose provider is the test: it records everything sent to the call."""

    def __init__(self, rt: Any, interaction: Any):
        super().__init__(rt, interaction)
        self.sent: list[tuple[str, str | None, str]] = []
        self.ready = threading.Event()
        self.connected = True

    def start(self) -> None:
        def runner() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.ready.set()
            self.loop.run_forever()
        threading.Thread(target=runner, daemon=True).start()
        self.ready.wait(5)

    async def _send(self, kind: str, delegation_id: str | None, content: str) -> None:
        self.sent.append((kind, delegation_id, content))

    def feed(self, event: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(self.handle_event(event), self.loop).result(10)  # type: ignore[arg-type]

    def delegate(self, delegation_id: str, words: str, **extra: Any) -> None:
        self.feed({"type": "session.input_transcript.delta", "delta": words, "start_ms": 1, "end_ms": 2})
        self.feed({"type": "session.delegation.created", "offset_ms": 5,
                   "delegation": {"id": delegation_id, "target": "client", **extra}})


def upload_attachment(base: str, token: str, interaction_id: str, data: bytes, kind: str = "picture",
                      ctype: str = "image/jpeg", headers: dict[str, str] | None = None) -> tuple[int, Any]:
    """POST raw bytes to /voice/interactions/{id}/attachments the way the Mac app does."""
    req = urllib.request.Request(f"{base}/voice/interactions/{interaction_id}/attachments", data=data, method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("X-Speakeasy-Kind", kind)
    req.add_header("Authorization", f"Bearer {token}")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class FakeMac:
    """The Mac app's side of screen sharing: it answers every ``capture`` event on a call's feed over
    real HTTP, as the app does (``capturing`` first; only on 200 the upload or a failure).
    ``reply(capture_id)`` decides: ``{"data": bytes, "app": "Xcode", "delay": s}`` uploads,
    ``{"fail": reason}`` reports a failed capture, None ignores the event."""

    def __init__(self, base: str, token: str, interaction: Any, reply: Callable[[str], dict[str, Any] | None]):
        self.base, self.token, self.interaction, self.reply = base, token, interaction, reply
        self.seen: list[str] = []                  # capture ids, in the order the events arrived
        self.answers: dict[str, tuple[int, int]] = {}  # capture id -> (capturing status, upload/fail status)
        self._stop = threading.Event()
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        while not self._stop.is_set():
            for _, kind, payload in list(self.interaction.feed.ring):
                capture_id = payload.get("capture_id") if kind == "capture" else None
                if capture_id and capture_id not in self.seen:
                    self.seen.append(capture_id)
                    threading.Thread(target=self._answer, args=(capture_id,), daemon=True).start()
            time.sleep(0.02)

    def _answer(self, capture_id: str) -> None:
        how = self.reply(capture_id)
        if how is None:
            return
        time.sleep(how.get("delay", 0))
        iid = self.interaction.interaction_id
        url = f"/voice/interactions/{iid}/captures/{capture_id}"
        status, _ = http(self.base, "POST", url, {"status": "capturing"}, self.token)
        if status != 200:
            self.answers[capture_id] = (status, 0)
            return
        if "fail" in how:
            done, _ = http(self.base, "POST", url, {"status": "failed", "reason": how["fail"]}, self.token)
        else:
            headers = {"X-Speakeasy-Capture-Id": capture_id}
            if how.get("app"):
                headers["X-Speakeasy-App"] = how["app"]
            done, _ = upload_attachment(self.base, self.token, iid, how["data"], "screen", headers=headers)
        self.answers[capture_id] = (status, done)

    def stop(self) -> None:
        self._stop.set()


def wait_for(predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def http(base: str, method: str, path: str, body: Any = None, token: str | None = None,
         headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
            return r.status, json.loads(raw) if "json" in ctype else raw
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
