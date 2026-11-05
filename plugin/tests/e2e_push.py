"""Local end to end: a real plugin (server + PushNotifier) -> the relay Worker (node host,
relay/push/test/e2e.mjs) -> a fake APNs. Run relay/push/test/e2e.mjs first, then:

    E2E_OUT=<same file> python plugin/tests/e2e_push.py

Checks: the plugin registers with the relay, the notify arrives at "APNs" signed with a valid
ES256 provider token and a generic payload with no words from the task, the event id in it lets
the paired phone read the real text back, and a token APNs calls dead is forgotten.
Not collected by pytest (no test_ prefix).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fakes import FakeHermesServer, http, make_home, wait_for  # noqa: E402

out = Path(os.environ["E2E_OUT"])
hermes = FakeHermesServer()
home = make_home(int(hermes.base.rsplit(":", 1)[1]))
os.environ["HERMES_HOME"] = str(home)
from speakeasy.service import VoiceService  # noqa: E402
from speakeasy.server import SpeakeasyServer  # noqa: E402

service = VoiceService(home, notifier=None, start_threads=False)  # inline sends, so the run is deterministic
service.settings.patch({"push": {"enabled": True, "relay_url": "http://127.0.0.1:8787", "presence_s": 5}})
srv = SpeakeasyServer(service, "127.0.0.1", 0)
srv.start()
_, paired = http(srv.base_url, "POST", "/voice/pair", {"code": service.devices.new_pairing_code(), "device_name": "iPhone"})
token = paired["token"]
APNS = "0f" * 32
DEAD = "de" * 32
assert http(srv.base_url, "POST", "/voice/push", {"apns_token": APNS, "environment": "sandbox"}, token)[0] == 200

device = service.devices.devices()[0].id
service.push._seen.clear()  # the app "closes"
title, answer = "Speed up the dashboard", "The slow query now uses the date index."
sent = service.push.notify("finished", key="e2e1", title=title, body=answer, run_id="run_e2e")
assert sent == [device], sent
lines = wait_for(lambda: out.exists() and out.read_text().splitlines())
apns = json.loads(lines[-1])
print("APNs got:", json.dumps({"path": apns["path"][:24] + "…", "push-type": apns["headers"]["apns-push-type"],
                                "topic": apns["headers"]["apns-topic"], "body": apns["body"]}))
raw = json.dumps(apns)
assert "Speed" not in raw and "dashboard" not in raw and "query" not in raw and "run_e2e" not in raw
assert apns["path"] == f"/3/device/{APNS}" and apns["body"]["aps"]["mutable-content"] == 1
assert apns["headers"]["authorization"].startswith("bearer ")
status, text = http(srv.base_url, "GET", f"/voice/push/events/{apns['body']['e']}", token=token)
assert status == 200 and text["title"] == f"Done: {title}", text
print("Extension reads back:", text["title"], "|", text["body"])

# A dead token: APNs says 410, the relay says invalid_token, the plugin forgets it.
assert http(srv.base_url, "POST", "/voice/push", {"apns_token": DEAD, "environment": "sandbox"}, token)[0] == 200
service.push._seen.clear()
service.push.notify("failed", key="e2e2", title="Another job")
wait_for(lambda: service.devices.push_targets() == {})
print("Dead token forgotten:", service.devices.push_targets() == {})
install = json.loads((home / "speakeasy" / "push_install.json").read_text())
print("Install registered:", install["install_id"][:8] + "…", "(secret kept 0600, not printed)")
srv.httpd.shutdown()
hermes.close()
print("E2E OK")
