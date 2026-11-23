"""The Mac app's Apple Intelligence rename sweep: stand-in titles are offered, renames stick."""
from __future__ import annotations

from fakes import http, wait_for
from test_routing import start_call, tasks
from test_run_failures import VENICE_402


def test_stand_in_titles_are_offered_and_renames_stick(server, service, hermes):
    _, worker = start_call(server, service)
    worker.delegate("call_t", "Um, book a train to Boston for tomorrow morning please")
    # Only finished tasks are offered: a running one may still get the cloud titler's name.
    wait_for(lambda: any(t.get("task_id") == "call_t" and t.get("title") and t["status"] == "completed"
                         for t in tasks(server)))

    status, pending = http(server.base_url, "GET", "/voice/titles", token=server.token)
    assert status == 200, pending
    offered = [t for t in pending["tasks"] if t["task_id"] == "call_t"]
    assert offered, pending
    assert offered[0]["request"].startswith("Um, book a train")
    assert offered[0]["title"]  # the stand-in label made from the request's own words

    status, renamed = http(server.base_url, "POST", "/voice/titles",
                           {"task_id": "call_t", "title": "book train boston"}, server.token)
    assert status == 200 and renamed["renamed"] is True and renamed["title"] == "book train boston"

    # Gone from the offer list (a real title now) and shown on the task.
    status, pending = http(server.base_url, "GET", "/voice/titles", token=server.token)
    assert status == 200
    assert not [t for t in pending["tasks"] if t["task_id"] == "call_t"]
    assert [t for t in tasks(server) if t.get("task_id") == "call_t"][0]["title"] == "book train boston"

    # The job the task first named took the new title too.
    with service.store._lock:   # the server's threads share this connection (see test_job_names_never_generic)
        (job_name,) = service.store._db.execute(
            "SELECT name FROM jobs WHERE from_key=(SELECT idem_key FROM runs WHERE delegation_id='call_t')"
        ).fetchone()
    assert job_name == "book train boston"


def test_rename_keeps_a_job_name_that_is_not_the_stand_in(server, service, hermes):
    """Only the task's stand-in title is replaced; a job already named some other way keeps its name."""
    _, worker = start_call(server, service)
    worker.delegate("call_j", "Um, book a train to Boston for tomorrow morning please")
    wait_for(lambda: any(t.get("task_id") == "call_j" and t.get("title") and t["status"] == "completed"
                         for t in tasks(server)))
    key = service.store.key_for_task("call_j")
    job = [t for t in tasks(server) if t.get("task_id") == "call_j"][0]["job"]
    with service.store._lock, service.store._db:
        service.store._db.execute("INSERT OR REPLACE INTO jobs (job_id,name,from_key,updated) VALUES (?,?,?,?)",
                                  (job["id"], "Boston weekend", key, 1.0))

    status, renamed = http(server.base_url, "POST", "/voice/titles",
                           {"task_id": "call_j", "title": "book train boston"}, server.token)
    assert status == 200 and renamed["renamed"] is True
    assert service.store.title(key) == "book train boston"
    with service.store._lock:
        (name,) = service.store._db.execute("SELECT name FROM jobs WHERE from_key=?", (key,)).fetchone()
    assert name == "Boston weekend"


def test_rename_validation_and_auth(server, service, hermes):
    status, _ = http(server.base_url, "GET", "/voice/titles")
    assert status == 401
    status, _ = http(server.base_url, "POST", "/voice/titles",
                     {"task_id": "nope", "title": "nothing here"}, server.token)
    assert status == 404

    _, worker = start_call(server, service)
    worker.delegate("call_v", "Find a dentist near the office")
    wait_for(lambda: any(t.get("task_id") == "call_v" and t.get("title") and t["status"] == "completed"
                         for t in tasks(server)))

    too_short = http(server.base_url, "POST", "/voice/titles", {"task_id": "call_v", "title": "  "}, server.token)
    assert too_short[0] == 400
    extra_key = http(server.base_url, "POST", "/voice/titles",
                     {"task_id": "call_v", "title": "find a dentist", "extra": 1}, server.token)
    assert extra_key[0] == 400
    too_long = http(server.base_url, "POST", "/voice/titles",
                    {"task_id": "call_v", "title": "x" * 91}, server.token)
    assert too_long[0] == 400
    not_a_string = http(server.base_url, "POST", "/voice/titles", {"task_id": "call_v", "title": 7}, server.token)
    assert not_a_string[0] == 400

    # A renamed title is whitespace-collapsed, not rejected outright.
    status, renamed = http(server.base_url, "POST", "/voice/titles",
                           {"task_id": "call_v", "title": "  find  a dentist  "}, server.token)
    assert status == 200 and renamed["title"] == "find a dentist"


def test_finished_and_unsettled_tasks_are_handled(server, service, hermes):
    """A failed task keeps its stand-in title offer; every offer carries the fields the app reads."""
    hermes.fail_with = VENICE_402
    _, worker = start_call(server, service)
    worker.delegate("call_f", "Show me the weather in Lisbon this weekend")
    wait_for(lambda: any(t.get("task_id") == "call_f" and t["status"] == "failed" for t in tasks(server)))
    status, pending = http(server.base_url, "GET", "/voice/titles", token=server.token)
    assert status == 200
    assert [t for t in pending["tasks"] if t["task_id"] == "call_f"]
    assert all(set(t) == {"request", "task_id", "title", "updated"} and t["request"] for t in pending["tasks"])


def test_chat_thread_tasks_are_never_offered(service):
    """A task in a chat thread shows the thread's name: renaming it here would split it from the thread."""
    from speakeasy.text import short_title
    store = service.store
    request = "Draft the weekly update for the team"
    store._db.execute("INSERT INTO runs (idem_key, interaction_id, delegation_id, revision, status, title, updated) "
                      "VALUES ('k_th', 'vi_x', 'call_th', 1, 'completed', ?, 2)", (short_title(request),))
    store.progress("k_th", "request", request)
    assert [t for t in store.stand_in_titles() if t["task_id"] == "call_th"]
    store.set_thread("k_th", "discord", "123", "weekly update")
    assert not [t for t in store.stand_in_titles() if t["task_id"] == "call_th"]
    assert store.rename_stand_in("k_th", "weekly team update") is False


def test_a_real_title_is_never_offered_or_clobbered(server, service, hermes):
    """The cloud titler (or a chat thread) named the task after the app asked: its rename is refused."""
    _, worker = start_call(server, service)
    worker.delegate("call_r", "Compare three espresso machines under 500 dollars")
    wait_for(lambda: any(t.get("task_id") == "call_r" and t["status"] == "completed" for t in tasks(server)))
    status, pending = http(server.base_url, "GET", "/voice/titles", token=server.token)
    assert status == 200 and [t for t in pending["tasks"] if t["task_id"] == "call_r"]

    key = service.store.key_for_task("call_r")
    service.store.set_title(key, "Espresso machine shortlist")  # the cloud titler lands meanwhile

    status, pending = http(server.base_url, "GET", "/voice/titles", token=server.token)
    assert status == 200 and not [t for t in pending["tasks"] if t["task_id"] == "call_r"]
    status, _ = http(server.base_url, "POST", "/voice/titles",
                     {"task_id": "call_r", "title": "compare espresso machines"}, server.token)
    assert status == 409
    assert service.store.title(key) == "Espresso machine shortlist"


def test_running_tasks_are_not_offered(service):
    """A task still working may yet get the cloud titler's name: never offered until it finishes."""
    from speakeasy.text import short_title
    store = service.store
    request = "Plan the garden beds for spring"
    store._db.execute("INSERT INTO runs (idem_key, interaction_id, delegation_id, revision, status, title, updated) "
                      "VALUES ('k_run', 'vi_x', 'call_run', 1, 'running', ?, 2)", (short_title(request),))
    store.progress("k_run", "request", request)
    assert not [t for t in store.stand_in_titles() if t["task_id"] == "call_run"]
    store._db.execute("UPDATE runs SET status='completed' WHERE idem_key='k_run'")
    assert [t for t in store.stand_in_titles() if t["task_id"] == "call_run"]
