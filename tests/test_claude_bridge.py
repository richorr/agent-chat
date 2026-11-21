"""Tests for claude/bridge.py. The Haiku router is replaced with a stub; no model calls."""
import argparse
import contextlib
import io
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'claude' / 'bridge.py'


def load_bridge(root):
    os.environ['AGENT_BRIDGE_ROOT'] = str(root)
    spec = importlib.util.spec_from_file_location('claude_bridge_under_test', str(SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for d in (mod.TO_CODEX, mod.TO_CLAUDE, mod.ARCHIVE, mod.TMP, mod.STATE):
        os.makedirs(d, exist_ok=True)
    return mod


class ClaudeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root))
        self.b = load_bridge(self.root)
        self.router_calls = []
        self.router_result = 'ROUTED demo-1a'
        self.b.run_router = lambda prompt: (self.router_calls.append(prompt) or (self.router_result, 0.001))
        self.b.log = lambda text: None
        self.register('demo', 'demo-1a')

    def register(self, chat, session, **kw):
        args = dict(chat=chat, session=session, ref=None, replace=None)
        args.update(kw)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.b.cmd_register(argparse.Namespace(**args))
        return out.getvalue()

    def run_cmd(self, fn, **kw):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            fn(argparse.Namespace(**kw))
        return out.getvalue()

    def set_codex_routes(self, routes):
        (self.root / 'state' / 'codex-routes.json').write_text(json.dumps(routes))

    # -- helpers
    def drop(self, **fields):
        msg = {'id': 'codex-1', 'reply_to': None, 'from': 'codex', 'to': 'claude',
               'project': 'demo', 'type': 'request', 'body': 'do it'}
        msg.update(fields)
        (self.root / 'to-claude' / (msg['id'] + '.json')).write_text(json.dumps(msg))
        return msg

    def outbox(self):
        return [json.loads(p.read_text()) for p in sorted((self.root / 'to-codex').glob('*.json'))]

    def send_args(self, **kw):
        base = dict(type='request', project='codex-proj', reply_to=None, origin='demo-1a',
                    body='hi', body_file=None, purpose='user-requested')
        base.update(kw)
        return argparse.Namespace(**base)

    def set_policy(self, default, projects=None):
        (self.root / 'claude-policy.json').write_text(json.dumps(
            {'default': {'self_initiated': default},
             'projects': {k: {'self_initiated': v} for k, v in (projects or {}).items()}}))

    # -- claim / at-most-once
    def test_claim_archives_and_suppresses_duplicates(self):
        self.drop()
        msg, dup = self.b.claim('codex-1.json')
        self.assertFalse(dup)
        self.assertEqual(msg['body'], 'do it')
        self.assertTrue((self.root / 'archive' / 'codex-1.json').exists())
        self.drop()  # same id delivered again
        _, dup = self.b.claim('codex-1.json')
        self.assertTrue(dup)

    # -- inbound requests
    def test_request_routed_records_pending_and_sends_reply_instructions(self):
        self.b.handle_request(self.drop())
        self.assertEqual(self.b.load_pending()['codex-1']['session'], 'demo-1a')
        self.assertIn('exactly "demo-1a"', self.router_calls[0])
        self.assertIn('bridge.py reply codex-1', self.router_calls[0])
        self.assertEqual(self.outbox(), [])

    def test_offline_registered_session_gets_error_not_another_session(self):
        self.router_result = 'OFFLINE'
        self.b.handle_request(self.drop())
        (resp,) = self.outbox()
        self.assertEqual((resp['type'], resp['reply_to']), ('response', 'codex-1'))
        self.assertIn("ERROR: Claude chat 'demo' is registered to session demo-1a, which is offline", resp['body'])
        self.assertEqual(self.b.load_pending(), {})
        self.assertIn('exactly "demo-1a"', self.router_calls[0])

    def test_unregistered_name_is_not_dispatched(self):
        self.b.handle_request(self.drop(project='other'))
        self.assertEqual(self.router_calls, [])
        self.assertIn("no Claude chat is registered as 'other'", self.outbox()[0]['body'])

    def test_project_name_still_resolves_to_its_registered_chat(self):
        self.register('pyroto-review', 'pyroto-7c', ref='d4a9b4')
        self.b.handle_request(self.drop(project='pyroto'))  # what Codex sends today
        self.assertIn('exactly "pyroto-7c"', self.router_calls[0])
        self.assertIn('[ref] is "d4a9b4"', self.router_calls[0])
        self.assertIn('chat pyroto-review', self.router_calls[0])
        self.register('pyroto-docs', 'pyroto-2e')
        self.b.handle_request(self.drop(id='codex-9', project='pyroto'))
        self.assertIn('matches several registered Claude chats (pyroto-docs, pyroto-review)', self.outbox()[0]['body'])
        self.assertEqual(len(self.router_calls), 1)

    def test_register_refuses_claimed_name_until_replace(self):
        self.assertIn('UNCHANGED', self.register('demo', 'demo-1a'))
        with self.assertRaises(SystemExit) as cm:
            self.register('demo', 'demo-9f')
        self.assertIn('CLAIMED', str(cm.exception))
        self.assertIn('--replace demo-1a', str(cm.exception))
        with self.assertRaises(SystemExit):
            self.register('demo', 'demo-9f', replace='wrong-00')
        self.assertIn('MOVED', self.register('demo', 'demo-9f', replace='demo-1a'))
        self.assertEqual(self.b.load_json(self.b.CHATS)['demo']['session'], 'demo-9f')
        with self.assertRaises(SystemExit):
            self.register('bad name!', 'demo-9f')

    def test_unregister_only_by_owner(self):
        with self.assertRaises(SystemExit):
            self.run_cmd(self.b.cmd_unregister, chat='demo', session='demo-9f')
        self.run_cmd(self.b.cmd_unregister, chat='demo', session='demo-1a')
        self.assertEqual(self.b.load_json(self.b.CHATS), {})

    def test_state_files_are_private(self):
        self.assertEqual(os.stat(self.b.CHATS).st_mode & 0o777, 0o600)

    # -- connections (sending side)
    def test_connect_saves_without_sending_or_registering(self):
        self.set_codex_routes({'pyroto-review': 'uuid-1'})
        out = self.run_cmd(self.b.cmd_connect, name='pyroto-review', session='me-11', body='When a PR is ready, ask.',
                           body_file=None, reconnect=False)
        self.assertIn('SAVED', out)
        self.assertEqual(self.outbox(), [])
        self.assertNotIn('me-11', json.dumps(self.b.load_json(self.b.CHATS)))
        shown = self.run_cmd(self.b.cmd_connection, name='pyroto-review', session='me-11')
        self.assertIn('[ok]', shown)
        self.assertIn('When a PR is ready, ask.', shown)
        self.assertIn('NONE', self.run_cmd(self.b.cmd_connection, name='pyroto-review', session='other-22'))
        self.assertIn('REPLACED', self.run_cmd(self.b.cmd_connect, name='pyroto-review', session='me-11',
                                               body='new', body_file=None, reconnect=False))
        with self.assertRaises(SystemExit):
            self.run_cmd(self.b.cmd_connect, name='nope', session='me-11', body='x', body_file=None, reconnect=False)

    def test_reassigned_name_blocks_send_until_reconnect(self):
        self.set_codex_routes({'pyroto-review': 'uuid-1'})
        self.run_cmd(self.b.cmd_connect, name='pyroto-review', session='demo-1a', body='rules',
                     body_file=None, reconnect=False)
        self.set_codex_routes({'pyroto-review': 'uuid-2'})
        self.assertIn('reassigned', self.run_cmd(self.b.cmd_names, session='demo-1a'))
        with self.assertRaises(SystemExit) as cm:
            self.b.cmd_send(self.send_args(project='pyroto-review'))
        self.assertIn('reconnect', str(cm.exception))
        self.assertEqual(self.outbox(), [])
        self.run_cmd(self.b.cmd_connect, name='pyroto-review', session='demo-1a', body=None, body_file=None,
                     reconnect=True)
        self.assertIn('rules', self.run_cmd(self.b.cmd_connection, name='pyroto-review', session='demo-1a'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.b.cmd_send(self.send_args(project='pyroto-review'))
        self.assertEqual(len(self.outbox()), 1)

    def test_router_failure_gets_error_response(self):
        self.router_result = 'FAILED router timed out'
        self.b.handle_request(self.drop())
        self.assertIn("could not deliver to Claude chat 'demo'", self.outbox()[0]['body'])

    def test_reply_sends_response_once(self):
        self.b.handle_request(self.drop())
        self.b.cmd_reply(argparse.Namespace(id='codex-1', body='done', body_file=None))
        (resp,) = self.outbox()
        self.assertEqual((resp['reply_to'], resp['body'], resp['project']), ('codex-1', 'done', 'demo'))
        with self.assertRaises(SystemExit):
            self.b.cmd_reply(argparse.Namespace(id='codex-1', body='again', body_file=None))
        self.assertEqual(len(self.outbox()), 1)

    # -- outbound requests and Codex responses
    def test_codex_response_delivered_to_origin_session(self):
        self.b.cmd_send(self.send_args())
        (req,) = self.outbox()
        self.b.handle_response(self.drop(id='codex-2', type='response', reply_to=req['id'], body='answer'))
        self.assertIn('"demo-1a"', self.router_calls[0])
        self.assertIn('[codex-reply %s] answer' % req['id'], self.router_calls[0])
        self.assertEqual(self.b.load_pending(), {})

    def test_response_to_unknown_request_is_not_delivered(self):
        self.b.handle_response(self.drop(id='codex-3', type='response', reply_to='nope'))
        self.assertEqual(self.router_calls, [])

    # -- policy
    def test_project_of_session(self):
        self.assertEqual(self.b.project_of_session('300-baud-web-e8'), '300-baud-web')
        self.assertEqual(self.b.project_of_session('pyroto-7c'), 'pyroto')

    def test_self_initiated_send_follows_project_policy(self):
        self.set_policy(['code-review'], {'locked': []})
        self.b.cmd_send(self.send_args(purpose='code-review'))
        self.assertEqual(self.outbox()[0]['purpose'], 'code-review')
        for kw in (dict(purpose='pr-fix'),                         # purpose not allowed
                   dict(purpose='code-review', origin='locked-2b'),  # project allows nothing
                   dict(purpose='code-review', origin=None)):      # no origin to check
            with self.assertRaises(SystemExit):
                self.b.cmd_send(self.send_args(**kw))
        self.b.cmd_send(self.send_args(origin='locked-2b'))  # user-requested is always allowed
        self.assertEqual(len(self.outbox()), 2)

    def test_inbound_self_initiated_request_checked_against_policy(self):
        self.set_policy([], {'demo': ['design-feedback']})
        self.b.handle_request(self.drop(purpose='code-review'))
        self.assertIn("does not accept self-initiated 'code-review'", self.outbox()[0]['body'])
        self.assertEqual(self.router_calls, [])
        self.b.handle_request(self.drop(id='codex-4', purpose='design-feedback'))  # policy uses the session's project
        self.assertEqual(len(self.router_calls), 1)

    def test_policy_changes_need_a_terminal(self):
        with self.assertRaises(SystemExit):
            self.b.cmd_policy(argparse.Namespace(action='allow', project='demo', purposes=['pr-fix']))
        self.assertFalse((self.root / 'claude-policy.json').exists())

    # -- races (PR #4 review)
    def test_session_can_answer_while_router_is_still_running(self):
        def router(prompt):  # SendMessage has delivered; the session answers before the router exits
            self.b.cmd_reply(argparse.Namespace(id='codex-1', body='fast', body_file=None))
            return 'ROUTED demo-1a', 0.001
        self.b.run_router = router
        self.b.handle_request(self.drop())
        (resp,) = self.outbox()
        self.assertEqual((resp['reply_to'], resp['body']), ('codex-1', 'fast'))
        self.assertEqual(self.b.load_pending(), {})  # answered record is not recreated

    def test_no_error_when_answered_although_router_reports_failure(self):
        def router(prompt):
            self.b.cmd_reply(argparse.Namespace(id='codex-1', body='fast', body_file=None))
            return 'FAILED lost track after SendMessage', 0.001
        self.b.run_router = router
        self.b.handle_request(self.drop())
        self.assertEqual([m['body'] for m in self.outbox()], ['fast'])

    def test_outbound_record_exists_before_request_is_visible_to_codex(self):
        seen = []
        real = self.b.write_message
        def spy(*a, **kw):
            seen.append(kw.get('msg_id') in self.b.load_pending())
            return real(*a, **kw)
        self.b.write_message = spy
        self.b.cmd_send(self.send_args())
        self.assertEqual(seen, [True])

    def test_concurrent_writers_do_not_lose_pending_records(self):
        env = dict(os.environ, AGENT_BRIDGE_ROOT=str(self.root))
        script = ('import subprocess, sys\n'
                  'for i in range(15):\n'
                  '    subprocess.check_call([sys.executable, %r, "send", "--type", "request", "--project", "p",'
                  ' "--origin", "demo-1a", "--body", "x"], stdout=subprocess.DEVNULL)\n') % str(SCRIPT)
        procs = [subprocess.Popen([sys.executable, '-c', script], env=env) for _ in range(6)]
        for p in procs:
            self.assertEqual(p.wait(), 0)
        self.assertEqual(len(self.b.load_pending()), 90)
        self.assertEqual(len(self.outbox()), 90)

    # -- timeouts
    def test_sweep_expires_old_inbound_and_outbound_records(self):
        old = time.time() - 3 * 24 * 3600
        self.b.save_pending({
            'codex-old': {'direction': 'inbound', 'project': 'demo', 'session': 'demo-1a', 'created_ts': old},
            'codex-new': {'direction': 'inbound', 'project': 'demo', 'session': 'demo-1a', 'created_ts': time.time()},
            'claude-old': {'direction': 'outbound', 'origin_session': 'demo-1a', 'created_ts': old},
        })
        self.b.sweep_timeouts(7200)
        self.assertEqual(list(self.b.load_pending()), ['codex-new'])
        (resp,) = self.outbox()
        self.assertEqual(resp['reply_to'], 'codex-old')
        self.assertTrue(resp['body'].startswith('ERROR: demo-1a did not answer'))


if __name__ == '__main__':
    unittest.main()
