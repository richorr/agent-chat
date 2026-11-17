#!/usr/bin/env python3
"""Shared durable mailbox primitives; no network or model calls."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone

DEFAULT_HOME = Path.home() / '.agent-bridge'


def atomic_json(path, value, staging):
    staging.mkdir(parents=True, exist_ok=True)
    temp = staging / ('.' + uuid.uuid4().hex)
    with temp.open('x') as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


class Bridge:
    def __init__(self, home, routes, adapter, timeout=3600):
        self.home, self.routes, self.adapter, self.timeout = Path(home), routes, adapter, timeout
        for name in ['to-codex', 'to-claude', 'archive', 'tmp', 'state']:
            (self.home / name).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.home / 'state/codex-state.sqlite3'))
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS incoming (
            id TEXT PRIMARY KEY, message TEXT, status TEXT, thread TEXT, turn TEXT,
            started REAL, response TEXT);
          CREATE TABLE IF NOT EXISTS outgoing (id TEXT PRIMARY KEY, thread TEXT, project TEXT,
            response_id TEXT);
        ''')

    def save(self, query, args):
        self.db.execute(query, args)
        self.db.commit()

    def emit(self, msg):
        atomic_json(self.home / 'to-claude' / (msg['id'] + '.json'), msg, self.home / 'tmp')

    def send(self, project, body, origin):
        msg = self.message(project, body)
        self.save('INSERT INTO outgoing(id,thread,project) VALUES (?,?,?)',
                  (msg['id'], origin, project))
        self.emit(msg)
        return msg['id']

    @staticmethod
    def message(project, body, reply_to=None, ident=None):
        return {'id': ident or 'codex-' + uuid.uuid4().hex, 'reply_to': reply_to,
                'from': 'codex', 'to': 'claude', 'project': project,
                'type': 'response' if reply_to else 'request', 'body': body,
                'created_at': datetime.now(timezone.utc).isoformat()}

    @staticmethod
    def validate(msg):
        if not isinstance(msg, dict):
            raise ValueError('message must be an object')
        for key in ['id', 'project', 'body']:
            if not isinstance(msg.get(key), str) or not msg[key]:
                raise ValueError('missing or empty ' + key)
        if not re.fullmatch(r'claude-[A-Za-z0-9_-]{1,180}', msg['id']):
            raise ValueError('invalid message id')
        if msg.get('from') != 'claude' or msg.get('to') != 'codex':
            raise ValueError('invalid sender or recipient')
        if msg.get('type') not in ['request', 'response']:
            raise ValueError('invalid message type')
        if msg['type'] == 'request' and msg.get('reply_to') is not None:
            raise ValueError('request cannot have reply_to')
        if msg['type'] == 'response' and not isinstance(msg.get('reply_to'), str):
            raise ValueError('response requires reply_to')

    def ingest(self):
        for path in sorted((self.home / 'to-codex').glob('*.json')):
            if path.name.startswith('.') or path.is_symlink():
                continue
            try:
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError('message exceeds 1 MiB')
                msg = json.loads(path.read_text())
                self.validate(msg)
            except (ValueError, OSError) as exc:
                logging.error('Invalid inbox file %s: %s', path.name, exc)
                path.rename(self.home / 'archive' / ('codex-invalid-' + uuid.uuid4().hex + '.json'))
                continue
            self.save('INSERT OR IGNORE INTO incoming(id,message,status,started) VALUES (?,?,?,?)',
                      (msg['id'], json.dumps(msg), 'pending', time.time()))
            path.rename(self.home / 'archive' / ('codex-' + msg['id'] + '.json'))

    def finish(self, row, body):
        msg = json.loads(row['message'])
        # Persist the response before publishing. A crash can repeat delivery of the
        # SAME response ID, which the peer deduplicates, never repeat the task.
        if msg['type'] == 'request':
            response = self.message(msg['project'], body, msg['id'],
                'codex-reply-' + hashlib.sha256(msg['id'].encode()).hexdigest())
            self.save('UPDATE incoming SET status=?,response=? WHERE id=?',
                      ('reply', json.dumps(response), row['id']))
        else:
            self.save('UPDATE incoming SET status=? WHERE id=?', ('done', row['id']))
            if body.startswith('ERROR:'):
                logging.error('Response delivery %s: %s', row['id'], body)
