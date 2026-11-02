"""Windows Task Tracker -> ARS bridge; durable intent precedes all network writes."""
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import uuid


class AdapterError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ArsJobs:
    def __init__(self, path, config, client_factory):
        validate_config(config)
        self.path, self.config, self.client_factory = Path(path), config, client_factory
        if (not self.path.is_absolute() or any(p.is_symlink() for p in [self.path, *self.path.parents]) or
                not isinstance(config, dict) or not isinstance(config.get('bindings'), dict)):
            raise AdapterError('ARS_CONFIG_INVALID')
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, parent TEXT, task TEXT, prompt TEXT, '
                'control TEXT, control_hash TEXT, create_key TEXT, request_id TEXT, launch TEXT, remote TEXT, '
                'status TEXT, session TEXT, cursor INTEGER DEFAULT 0, answer TEXT, events TEXT DEFAULT "[]", truncated INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS commands (job TEXT, number INTEGER, key TEXT UNIQUE, request_id TEXT, '
                'text TEXT, phase TEXT, PRIMARY KEY(job,number))')
            db.execute('CREATE TABLE IF NOT EXISTS replies (job TEXT, operation TEXT, text TEXT, response TEXT, PRIMARY KEY(job,operation))')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS active_parent ON jobs(parent) WHERE status NOT IN ("stopped","completed","failed")')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def text(args, name):
        value = args.get(name)
        if not isinstance(value, str) or not value.strip() or len(json.dumps(value).encode()) > 12000:
            raise AdapterError('INVALID_REQUEST')
        return value

    def job(self, args):
        identity, control = self.text(args, 'job_id'), self.text(args, 'control_key')
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone()
        if row is None or not hmac.compare_digest(row['control_hash'], hashlib.sha256(control.encode()).hexdigest()):
            raise AdapterError('JOB_NOT_FOUND')
        return dict(row)

    def public(self, row, summary=False):
        result = {'job_id': row['id'], 'thread_id': row['parent'], 'status': row['status'],
            'result': row['answer'], 'rights_version': 0, 'codex_thread_id': row['session']}
        if not summary:
            result.update(events=row['events'], events_truncated=bool(row['truncated']))
        return result

    def pending(self, db, job, text):
        latest = db.execute('SELECT * FROM commands WHERE job=? ORDER BY number DESC LIMIT 1', (job,)).fetchone()
        if latest and latest['phase'] not in ('completed', 'failed'):
            if latest['text'] != text:
                raise AdapterError('BUSY')
            return
        number = latest['number'] + 1 if latest else 1
        if number > 256:
            raise AdapterError('LIMIT_EXCEEDED')
        db.execute('INSERT INTO commands VALUES (?,?,?,?,?,?)',
            (job, number, 'tt-' + uuid.uuid4().hex, str(uuid.uuid4()), text, 'intent'))
        db.execute('UPDATE jobs SET status="running",answer=NULL WHERE id=?', (job,))

    def send(self, row, client):
        with self.db() as db:
            pending = db.execute('SELECT * FROM commands WHERE job=? AND phase="intent" ORDER BY number LIMIT 1', (row['id'],)).fetchone()
        if pending:
            client.command(row['remote'], 'message', {'text': pending['text']}, pending['key'], pending['request_id'])
            with self.db() as db:
                db.execute('UPDATE commands SET phase="sent" WHERE key=? AND phase="intent"', (pending['key'],))

    def refresh(self, row, client):
        if not row['remote']:
            return row
        status = client.status(row['remote'])
        events = client.events(row['remote'], row['cursor'])
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = dict(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())
            recorded = json.loads(current['events'])
            latest = db.execute('SELECT * FROM commands WHERE job=? ORDER BY number DESC LIMIT 1', (row['id'],)).fetchone()
            for event in events:
                if type(event.get('seq')) is not int or event['seq'] <= current['cursor']:
                    raise AdapterError('EVENT_ORDER_INVALID')
                current['cursor'] = event['seq']
                if event.get('session_id'):
                    current['session'] = event['session_id']
                recorded.append(event)
                if latest and event.get('request_id') == latest['key']:
                    payload = event.get('payload') or {}
                    if event.get('event') == 'answer':
                        current['answer'] = payload.get('text')
                    if event.get('event') == 'turn/completed':
                        complete = payload.get('status') == 'completed' and isinstance(current['answer'], str) and bool(current['answer'])
                        current['status'] = 'waiting' if complete else 'unknown'
                        db.execute('UPDATE commands SET phase=? WHERE key=?',
                            ('completed' if complete else 'failed', latest['key']))
                    if event.get('event') == 'error':
                        current['status'], current['answer'] = 'unknown', None
                        db.execute('UPDATE commands SET phase="failed" WHERE key=?', (latest['key'],))
            while len(recorded) > 256 or len(json.dumps(recorded).encode()) > 65536:
                recorded.pop(0)
                current['truncated'] = 1
            remote_state = status.get('status')
            if remote_state in ('failed', 'indeterminate', 'stopped', 'succeeded'):
                current['status'] = {'indeterminate': 'unknown', 'succeeded': 'completed'}.get(remote_state, remote_state)
                if remote_state in ('failed', 'indeterminate'):
                    current['answer'] = None
            current['session'] = status.get('session_id') or current['session']
            current['events'] = json.dumps(recorded)
            db.execute('UPDATE jobs SET status=?,session=?,cursor=?,answer=?,events=?,truncated=? WHERE id=?',
                (current['status'], current['session'], current['cursor'], current['answer'], current['events'], current['truncated'], row['id']))
        return current

    def call(self, method, args):
        if method not in ('start', 'status', 'reply', 'stop'):
            raise AdapterError('UNSUPPORTED_CAPABILITY')
        fields = {'start': {'thread_id', 'task_key', 'prompt'}, 'status': {'job_id', 'control_key', 'summary_only'},
            'reply': {'job_id', 'control_key', 'reply', 'idempotency_key'}, 'stop': {'job_id', 'control_key'}}
        if not isinstance(args, dict) or set(args) - fields[method]:
            raise AdapterError('INVALID_REQUEST')
        if method == 'start':
            parent, task, prompt = (self.text(args, key) for key in ('thread_id', 'task_key', 'prompt'))
            if len(parent) > 128 or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-[0-9]+', task):
                raise AdapterError('INVALID_REQUEST')
            launch = self.config['bindings'].get(task)
            if not isinstance(launch, dict):
                raise AdapterError('TASK_NOT_CONFIGURED')
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                previous = db.execute('SELECT * FROM jobs WHERE parent=? ORDER BY rowid DESC LIMIT 1', (parent,)).fetchone()
                if previous and previous['task'] != task:
                    raise AdapterError('TASK_BINDING_CONFLICT')
                if previous and previous['status'] not in ('stopped', 'completed', 'failed'):
                    if previous['prompt'] != prompt:
                        raise AdapterError('THREAD_BUSY')
                    row = dict(previous)
                else:
                    if db.execute('SELECT count(*) FROM jobs').fetchone()[0] >= 128:
                        raise AdapterError('JOB_LIMIT')
                    selected = dict(launch)
                    if previous and previous['session']:
                        selected['thread_id'] = previous['session']
                    identity, control = uuid.uuid4().hex, secrets.token_urlsafe(32)
                    db.execute('INSERT INTO jobs(id,parent,task,prompt,control,control_hash,create_key,request_id,launch,status) VALUES(?,?,?,?,?,?,?,?,?,"queued")',
                        (identity,parent,task,prompt,control,hashlib.sha256(control.encode()).hexdigest(),
                         'tt-create-' + identity,str(uuid.uuid4()),json.dumps(selected)))
                    self.pending(db, identity, prompt)
                    row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (identity,)).fetchone())
            try:
                client = self.client_factory()
                client.ensure_available(seconds=10)
                if not row['remote']:
                    remote = client.create(json.loads(row['launch']), row['create_key'], row['request_id'])
                    if not isinstance(remote.get('job_id'), str) or not re.fullmatch(r'[a-f0-9]{32}', remote['job_id']):
                        raise AdapterError('INVALID_RESPONSE')
                    with self.db() as db:
                        db.execute('UPDATE jobs SET remote=? WHERE id=?', (remote['job_id'], row['id']))
                    row['remote'] = remote['job_id']
                self.send(row, client)
            except AdapterError:
                raise
            except Exception as error:
                if not row['remote'] and getattr(error, 'status', None) in (400,401,403,404,422):
                    with self.db() as db:
                        db.execute('UPDATE jobs SET status="failed" WHERE id=? AND remote IS NULL', (row['id'],))
                code = getattr(error, 'code', 'ARS_UNAVAILABLE')
                raise AdapterError(code if re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', str(code)) else 'ARS_UNAVAILABLE') from None
            return {'job_id': row['id'], 'thread_id': parent, 'control_key': row['control'], 'status': row['status']}
        row = self.job(args)
        try:
            client = self.client_factory()
            if method == 'stop':
                if row['remote'] is None:
                    raise AdapterError('CREATE_OUTCOME_UNKNOWN')
                # Persist the exact stop identity before the first attempt; it is stable across restart.
                stopped = client.stop(row['remote'], 'tt-stop-' + row['id'], row['request_id'])
                state = 'stopped' if stopped.get('status') == 'stopped' else 'unknown'
                with self.db() as db:
                    db.execute('UPDATE jobs SET status=? WHERE id=?', (state, row['id']))
                return {'job_id': row['id'], 'status': state}
            self.send(row, client)
            row = self.refresh(row, client)
            if method == 'reply':
                operation = args.get('idempotency_key')
                if operation is None:
                    raise AdapterError('IDEMPOTENCY_KEY_REQUIRED')
                if not isinstance(operation, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', operation):
                    raise AdapterError('INVALID_REQUEST')
                text = self.text(args, 'reply')
                with self.db() as db:
                    db.execute('BEGIN IMMEDIATE')
                    previous = db.execute('SELECT * FROM replies WHERE job=? AND operation=?', (row['id'], operation)).fetchone()
                    if previous:
                        if previous['text'] != text:
                            raise AdapterError('IDEMPOTENCY_CONFLICT')
                        response = json.loads(previous['response'])
                    else:
                        if row['status'] != 'waiting':
                            raise AdapterError('JOB_NOT_WAITING')
                        self.pending(db, row['id'], text)
                        response = {'job_id': row['id'], 'status': 'queued'}
                        db.execute('INSERT INTO replies VALUES (?,?,?,?)', (row['id'], operation, text, json.dumps(response)))
                self.send(row, client)
                return response
            return self.public(row, args.get('summary_only') is True)
        except AdapterError:
            raise
        except Exception as error:
            code = getattr(error, 'code', 'ARS_UNAVAILABLE')
            raise AdapterError(code if re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', str(code)) else 'ARS_UNAVAILABLE') from None


def from_service(config):
    from ars_client import Client
    selected=config['arsJobs']
    if not isinstance(selected.get('credentialPath'), str) or not Path(selected['credentialPath']).is_absolute():
        raise AdapterError('ARS_CONFIG_INVALID')
    cache=[]
    def client():
        if not cache:
            cache.append(Client(credential_path=selected['credentialPath'],
                powershell_helper=Path(__file__).with_name('Ars.Client.ps1'), port=selected.get('port',48758),
                expected_version=selected.get('expectedVersion','0.3.0-dev'), startup=selected.get('startup'),timeout=20))
        return cache[0]
    return ArsJobs(Path(config['protectedRoot'])/'state/ars-jobs.sqlite',selected,client)


def validate_config(config):
    if (not isinstance(config, dict) or set(config) - {'bindings', 'port', 'expectedVersion', 'startup', 'credentialPath'} or
            not isinstance(config.get('bindings'), dict) or not 1 <= len(config['bindings']) <= 128 or
            type(config.get('port', 48758)) is not int or not 1024 <= config.get('port', 48758) <= 65535 or
            not isinstance(config.get('expectedVersion', '0.3.0-dev'), str)):
        raise AdapterError('ARS_CONFIG_INVALID')
    allowed = {'schema_version', 'profile_id', 'image_digest', 'lock_sha256', 'mounts', 'state_root', 'auth_file', 'context_files', 'model', 'thread_id'}
    for task, launch in config['bindings'].items():
        if (not isinstance(task, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*-[0-9]+', task) or
                not isinstance(launch, dict) or set(launch) - allowed or launch.get('schema_version') != 1 or
                not isinstance(launch.get('profile_id'), str) or not isinstance(launch.get('state_root'), str)):
            raise AdapterError('ARS_CONFIG_INVALID')
    startup = config.get('startup')
    if startup is not None and (not isinstance(startup, dict) or set(startup) != {'task_name', 'agent_sid'} or
            not isinstance(startup['task_name'], str) or not re.fullmatch(r'AgentRuntime-Restart-[A-Za-z0-9_-]+', startup['task_name']) or
            not isinstance(startup['agent_sid'], str) or not re.fullmatch(r'S-1-5-21-[0-9-]+', startup['agent_sid'])):
        raise AdapterError('ARS_CONFIG_INVALID')
