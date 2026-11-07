import json
import pytest
from speakeasy import history
from speakeasy.history_events import event_for

WAKE = ('[kanban] Task t_example01 completed.\nTitle: Build native reminders (Codex continuation)\n'
        'Assignee: @default\nBoard: example\n\nCheck the result or decide the next step.\n'
        'Result: Candidate built and simulator-tested; not shipped.\n\n'
        'This is an automatic task-status notification. Inspect the current board.\nkanban_create()')

def row(text=WAKE, kind='internal_notification', role='user', id=1, metadata=None):
    return dict(id=id, role=role, content=text, display_kind=kind, display_metadata=metadata,
                timestamp=100, tool_calls=None, tool_name=None, tool_call_id=None)

@pytest.mark.parametrize('kind', [None, '', 'human', 'unknown'])
def test_human_lookalike_is_never_an_event(kind):
    assert event_for(row(kind=kind)) is None
    assert history.build_entries([row(kind=kind)])[0]['kind'] == 'user'

def test_real_wake_is_neutral_readable_and_not_shipped():
    entry = history.build_entries([row()])[0]
    assert entry['kind'] == 'note'
    assert entry['event']['title'] == 'Build native reminders'
    assert entry['event']['label'] == 'Task completed'
    assert entry['event']['summary'] == 'Candidate built and simulator-tested; not shipped.'
    assert all(s not in entry['text'] for s in ['t_example', 'Assignee', 'kanban_create', 'Inspect'])
    assert row()['content'] == WAKE

@pytest.mark.parametrize('text', ['', '[kanban] Task t_example01 completed.', WAKE[:70]])
def test_missing_truncated_metadata_has_safe_visible_fallback(text):
    entry = history.build_entries([row(text=text, metadata='{broken')])[0]
    assert entry['kind'] == 'note'
    assert entry['text']
    assert 'Inspect' not in entry['text']

def test_source_attribution_not_role_guess():
    assert event_for(row(role='assistant')) is None
    assert event_for(row(role='tool')) is None
    assert event_for(row(role='system'))

def test_structured_metadata_preferred():
    meta = json.dumps({'task_event': {'task_id':'t_example01', 'title':'Fix the search field',
        'state':'blocked', 'summary':'Needs a device check.', 'url':'https://example.com/task'}})
    event = event_for(row(metadata=meta))
    assert event['title'] == 'Fix the search field'
    assert event['label'] == 'Task needs attention'
    assert event['url'] == 'https://example.com/task'

def test_duplicate_and_exact_mirror_fold_not_new_reply():
    rows = [row(), row(id=2), row('Candidate built and simulator-tested; not shipped.', None, 'assistant', 3),
            row('Next, inspect the keyboard.', None, 'assistant', 4)]
    entries = history.build_entries(rows)
    assert len(entries) == 2
    assert entries[-1]['text'] == 'Next, inspect the keyboard.'

@pytest.mark.parametrize('url', ['file:///private/data', 'javascript:bad', 'http://example.com'])
def test_unsafe_link_omitted(url):
    assert 'url' not in event_for(row(metadata=json.dumps({'task_event':{'url':url}})))

@pytest.mark.parametrize('kind', ['async_delegation_complete','process_complete'])
def test_other_automatic_sources_do_not_leak_payload(kind):
    event = event_for(row(text='INSTRUCTIONS: private internal payload', kind=kind))
    assert event['summary'] == 'A recorded update is available.'


@pytest.mark.parametrize('status,state', [('completed','completed'), ('blocked; needs attention','blocked'),
    ('gave up (retries exhausted)','failed'), ('gave up (retries exhausted), crashed (worker exited); dispatcher will retry','failed')])
def test_producer_wrapper_on_same_line_and_terminal_states(status, state):
    event = event_for(row(text='<internal-notification>' + WAKE.replace('completed.', status + '.', 1)))
    assert event['state'] == state
    assert event['title'] == 'Build native reminders'


def test_wrapped_internal_event_and_truncated_excerpt():
    event = event_for(row(text='[Internal delivery]\n' + WAKE.replace('not shipped.', 'not ship')))
    assert event['title'] == 'Build native reminders'
    assert event['summary'].endswith('not ship…')


def test_malformed_structured_fields_are_bounded():
    event = event_for(row(text='', metadata=json.dumps({'task_event':{'state':{}, 'title':[], 'task_id':[]}})))
    assert event['state'] == 'unknown'
    assert event['title'] == 'Automatic task update'


def test_distinct_tasks_with_identical_words_remain_distinct():
    assert len(history.build_entries([row(), row(WAKE.replace('t_example01', 't_example02'), id=2)])) == 2


def test_preview_uses_event_not_original_instructions():
    import sqlite3
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE messages (id INTEGER, role TEXT, content TEXT, timestamp REAL, session_id TEXT, display_kind TEXT, display_metadata TEXT)')
    db.execute('INSERT INTO messages VALUES (1,?,?,?,?,?,?)', ('user', WAKE, 100, 'example', 'internal_notification', None))
    preview, first = history._preview(db, 'example')
    assert preview.startswith('Task completed: Build native reminders')
    assert 't_example' not in preview and 'Inspect' not in preview
    assert first is None
    db.close()


def test_server_returns_typed_event_and_safe_older_client_note(server, service):
    import sqlite3
    from test_history import make_db, add_session, add_msg, get
    path = service.rt.state_db
    make_db(path)
    add_session(path, 'example-event', 'api', title='Background update')
    add_msg(path, 'example-event', 'user', WAKE)
    db = sqlite3.connect(path)
    db.execute('ALTER TABLE messages ADD COLUMN display_metadata TEXT')
    db.execute("UPDATE messages SET display_kind='internal_notification'")
    db.commit(); db.close()
    status, body = get(server, '/voice/history/example-event')
    assert status == 200, body
    entry = body['session']['entries'][0]
    assert entry['kind'] == 'note'
    assert entry['event']['state'] == 'completed'
    assert entry['event']['summary'].endswith('not shipped.')
    assert 'Assignee:' not in entry['text']
