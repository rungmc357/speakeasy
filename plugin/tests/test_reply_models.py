"""Invented reply attribution, including databases without message model columns."""
import json
import sqlite3

from speakeasy import history as H
from speakeasy import reply_models as R
from test_history import make_db, add_session, add_msg


def test_message_metadata_beats_run_and_session(tmp_path):
    path = tmp_path / 'history.sqlite'
    make_db(path, fts=False)
    add_session(path, 's', 'cli')
    add_msg(path, 's', 'assistant', 'The report is ready.', at=15)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE messages ADD COLUMN display_metadata TEXT')
        db.execute('ALTER TABLE sessions ADD COLUMN model TEXT')
        db.execute("UPDATE sessions SET model='session-model'")
        db.execute('UPDATE messages SET display_metadata=?', (json.dumps({'model_attribution': {
            'model': 'reply-model', 'provider': 'example', 'reasoning_effort': 'high'}}),))
    runs = {'s': [{'start': 10, 'end': 20, 'record': {'model': 'example/run-model'}}]}
    reply = H.session_messages(path, 's', model_runs=runs)['entries'][0]
    assert reply['model']['id'] == 'example/reply-model'
    assert reply['model']['reasoning'] == 'high'
    assert reply['model']['source'] == 'message'


def test_two_runs_keep_their_own_model_and_fallback(tmp_path):
    path = tmp_path / 'history.sqlite'
    make_db(path, fts=False)
    add_session(path, 's', 'cli')
    for at in (15, 35, 55):
        add_msg(path, 's', 'assistant', f'Report at {at}.', at=at)
    runs = {'s': [
        {'start': 10, 'end': 20, 'record': {'model': 'example/first', 'reasoning': 'high',
                                         'served': {'provider': 'example', 'model': 'first'}}},
        {'start': 30, 'end': 40, 'record': {'model': 'example/first', 'reasoning': 'high',
                                         'served': {'provider': 'backup', 'model': 'second'}}}]}
    entries = H.session_messages(path, 's', model_runs=runs)['entries']
    assert [e.get('model', {}).get('id') for e in entries] == ['example/first', 'backup/second', None]
    assert entries[1]['model']['fallback'] is True
    assert entries[1]['model']['reasoning'] is None
    assert entries[0]['model']['reasoning'] is None  # requested effort != served effort
    older = H.session_messages(path, 's', before=3, limit=1, model_runs=runs)['entries'][0]
    assert older['model'] == entries[1]['model']


def test_unknown_never_inherits_current_picker_or_reasoning_text(tmp_path):
    path = tmp_path / 'history.sqlite'
    make_db(path, fts=False)
    add_session(path, 's', 'cli')
    add_msg(path, 's', 'assistant', 'Ready.', at=15)
    assert 'model' not in H.session_messages(path, 's')['entries'][0]
    assert R.badge({'model': 'unknown'}, 'session') is None
    assert R.message_badge({'reasoning': 'private thought text'}) is None
    assert R.badge({'model': 'https://invalid.example/model'}, 'message') is None


def test_session_fallback_suppressed_when_models_mixed(tmp_path):
    path = tmp_path / 'history.sqlite'
    make_db(path, fts=False)
    add_session(path, 's', 'cli')
    add_msg(path, 's', 'assistant', 'Ready.', at=15)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE sessions ADD COLUMN model TEXT')
        db.execute("UPDATE sessions SET model='latest-model'")
        db.execute('CREATE TABLE session_model_usage (session_id TEXT, model TEXT)')
        db.execute("INSERT INTO session_model_usage VALUES ('s', 'latest-model')")
    assert H.session_messages(path, 's')['entries'][0]['model']['id'] == 'latest-model'
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO session_model_usage VALUES ('s', 'older-model')")
    assert 'model' not in H.session_messages(path, 's')['entries'][0]


def test_switch_is_not_fallback():
    badge = R.badge({'model': 'example/second', 'served': {'provider': 'example', 'model': 'second'}}, 'run')
    assert badge is not None and badge['fallback'] is False


def test_compaction_segments_keep_own_session_models(tmp_path):
    path = tmp_path / 'history.sqlite'
    make_db(path, fts=False)
    add_session(path, 'root', 'cli')
    add_session(path, 'tip', 'cli', parent='root')
    add_msg(path, 'root', 'assistant', 'First reply.', at=15)
    add_msg(path, 'tip', 'assistant', 'Second reply.', at=35)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE sessions ADD COLUMN model TEXT')
        db.execute("UPDATE sessions SET model='first' WHERE id='root'")
        db.execute("UPDATE sessions SET model='second' WHERE id='tip'")
    assert [e['model']['id'] for e in H.session_messages(path, 'tip')['entries']] == ['first', 'second']


def test_run_timeline_from_store(service):
    store = service.store
    # Store fixture has the real migration/schema; add invented historical runs.
    with store._lock, store._db:
        for key, model, start in [('a', 'example/first', 10), ('b', 'example/second', 30)]:
            store._db.execute('INSERT INTO runs (idem_key, interaction_id, delegation_id, revision, status, created, updated, session_id, model) VALUES (?,?,?,?,?,?,?,?,?)',
                              (key, 'call', key, 1, 'completed', start, start + 10, 's', json.dumps({'model': model})))
    runs = store.reply_model_runs('s')['s']
    assert [(r['start'], r['end']) for r in runs] == [(10, 20), (30, 40)]
    assert runs[0]['record']['model'] == 'example/first'
