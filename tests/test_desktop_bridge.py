import json
from pathlib import Path
import tempfile
import unittest
from desktop_bridge import DesktopBridge

class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.b = DesktopBridge(self.home, {'demo': 'chat-123'})
    def tearDown(self):
        self.b.db.close(); self.tmp.cleanup()
    def incoming(self, ident='claude-one', **overrides):
        msg = dict(id=ident, reply_to=None, **{'from': 'claude', 'to': 'codex'}, project='demo', type='request', body='hello')
        msg.update(overrides)
        (self.home / 'to-codex' / (ident + '.json')).write_text(json.dumps(msg))
    def test_round_trip_and_duplicate(self):
        self.incoming(); self.assertEqual(len(self.b.scan()['actions']), 1)
        self.assertEqual(self.b.claim('claude-one')['threadId'], 'chat-123')
        self.b.complete('claude-one', 'answer')
        replies = list((self.home / 'to-claude').glob('*.json'))
        self.assertEqual(json.loads(replies[0].read_text())['reply_to'], 'claude-one')
        self.incoming(); self.assertEqual(self.b.scan()['actions'], [])
        self.assertEqual(len(list((self.home / 'to-claude').glob('*.json'))), 1)
    def test_reply_publish_recovers_after_restart(self):
        self.incoming(); self.b.scan(); self.b.claim('claude-one')
        def fail_publish(message):
            raise OSError('simulated interrupted publication')
        self.b.emit = fail_publish
        with self.assertRaises(OSError):
            self.b.complete('claude-one', 'durable answer')
        self.b.db.close()
        self.b = DesktopBridge(self.home, {'demo': 'chat-123'})
        self.assertEqual(self.b.scan()['actions'], [])
        reply = json.loads(next((self.home / 'to-claude').glob('*.json')).read_text())
        self.assertEqual(reply['body'], 'durable answer')
        self.assertEqual(reply['reply_to'], 'claude-one')
    def test_response_project_must_match(self):
        ident = self.b.send('demo', 'question', 'origin-chat')
        self.incoming(type='response', reply_to=ident, project='different')
        self.assertEqual(self.b.scan()['actions'], [])
        self.assertIsNone(self.b.db.execute('SELECT response_id FROM outgoing').fetchone()[0])
    def test_restart_never_reclaims(self):
        self.incoming(); self.b.scan(); self.b.claim('claude-one'); self.b.db.close()
        self.b = DesktopBridge(self.home, {'demo': 'chat-123'})
        self.assertEqual(self.b.scan()['actions'][0]['status'], 'desktop-claimed')
        with self.assertRaises(ValueError): self.b.claim('claude-one')
    def test_backoff_and_reset(self):
        self.assertEqual([self.b.scan()['next_poll_minutes'] for _ in range(6)], [10, 20, 30, 30, 30, 30])
        self.incoming(); self.assertEqual(self.b.scan()['next_poll_minutes'], 5)
    def test_unanswered_outgoing_keeps_polling_after_restart(self):
        self.assertEqual([self.b.scan()['next_poll_minutes'] for _ in range(3)], [10, 20, 30])
        ident = self.b.send('demo', 'question', 'origin-chat')
        self.assertEqual([self.b.scan()['next_poll_minutes'] for _ in range(3)], [5, 5, 5])
        self.b.db.close()
        self.b = DesktopBridge(self.home, {'demo': 'chat-123'})
        self.assertEqual(self.b.scan()['next_poll_minutes'], 5)
        self.incoming(type='response', reply_to=ident)
        self.assertEqual(self.b.scan()['next_poll_minutes'], 5)
        self.b.claim('claude-one')
        self.b.complete('claude-one', 'Delivered')
        self.assertEqual([self.b.scan()['next_poll_minutes'] for _ in range(3)], [10, 20, 30])
    def test_unknown_route_gets_error(self):
        self.incoming(project='unknown'); self.assertEqual(self.b.scan()['actions'], [])
        reply = json.loads(next((self.home / 'to-claude').glob('*.json')).read_text())
        self.assertTrue(reply['body'].startswith('ERROR:'))
    def test_response_does_not_loop(self):
        ident = self.b.send('demo', 'question', 'origin-chat')
        self.incoming(type='response', reply_to=ident)
        self.b.scan(); self.assertEqual(self.b.claim('claude-one')['threadId'], 'origin-chat')
        self.b.complete('claude-one', 'Delivered')
        self.assertEqual(len(list((self.home / 'to-claude').glob('*.json'))), 1)

    def test_sender_checks_only_its_matching_reply(self):
        ident = self.b.send('demo', 'question', 'origin-chat')
        self.incoming()
        self.incoming('claude-wrong', type='response', reply_to=ident, project='different')
        self.assertEqual(self.b.check_reply(ident, 'origin-chat'), {'status': 'pending'})
        self.incoming('claude-answer', type='response', reply_to=ident, body='answer')
        with self.assertRaisesRegex(ValueError, 'no matching request'):
            self.b.check_reply(ident, 'other-chat')
        self.assertEqual(self.b.check_reply(ident, 'origin-chat'),
                         {'status': 'received', 'body': 'answer'})
        self.assertEqual(self.b.check_reply(ident, 'origin-chat'),
                         {'status': 'received', 'body': 'answer'})
        self.assertEqual({path.stem for path in (self.home / 'to-codex').glob('*.json')},
                         {'claude-one', 'claude-wrong'})
        self.assertEqual([action['id'] for action in self.b.scan()['actions']], ['claude-one'])

    def test_sender_and_dispatcher_share_reply_claim(self):
        first = self.b.send('demo', 'first', 'origin-chat')
        second = self.b.send('demo', 'second', 'origin-chat')
        self.incoming('claude-first', type='response', reply_to=first)
        self.incoming('claude-second', type='response', reply_to=second)
        dispatcher = DesktopBridge(self.home, {'demo': 'chat-123'})
        try:
            self.assertEqual({action['id'] for action in dispatcher.scan()['actions']},
                             {'claude-first', 'claude-second'})
            dispatcher.claim('claude-first')
            self.assertEqual(self.b.check_reply(first, 'origin-chat'), {'status': 'handled'})
            self.assertEqual(self.b.check_reply(second, 'origin-chat')['status'], 'received')
            self.assertEqual(dispatcher.claim('claude-second'), {'status': 'sender-received'})
        finally:
            dispatcher.db.close()
    def test_invalid_json_is_archived(self):
        (self.home / 'to-codex/bad.json').write_text('{')
        self.assertEqual(self.b.scan()['actions'], [])
        self.assertEqual(len(list((self.home / 'archive').glob('*.json'))), 1)
    def test_pending_request_timeout_reports_error(self):
        self.incoming(); self.b.scan()
        self.b.save('UPDATE incoming SET started=0', ())
        self.assertEqual(self.b.scan()['actions'], [])
        reply = json.loads(next((self.home / 'to-claude').glob('*.json')).read_text())
        self.assertTrue(reply['body'].startswith('ERROR:'))

    def test_aged_claimed_request_can_complete(self):
        self.incoming(); self.b.scan(); self.b.claim('claude-one')
        self.b.save('UPDATE incoming SET started=0', ())
        self.assertEqual(self.b.scan()['actions'][0]['status'], 'desktop-claimed')
        self.b.complete('claude-one', 'eventual answer')
        reply = json.loads(next((self.home / 'to-claude').glob('*.json')).read_text())
        self.assertEqual(reply['body'], 'eventual answer')
        self.assertEqual(reply['reply_to'], 'claude-one')

    def test_aged_pending_response_can_be_delivered(self):
        ident = self.b.send('demo', 'question', 'origin-chat')
        self.incoming(type='response', reply_to=ident, body='eventual answer')
        self.b.scan()
        self.b.save('UPDATE incoming SET started=0', ())
        self.assertEqual(self.b.scan()['actions'][0]['status'], 'pending')
        claim = self.b.claim('claude-one')
        self.assertEqual(claim['threadId'], 'origin-chat')
        self.assertIn('eventual answer', claim['prompt'])
        self.b.complete('claude-one', 'Delivered')
        self.assertEqual(self.b.scan()['actions'], [])
        self.assertEqual(len(list((self.home / 'to-claude').glob('*.json'))), 1)

if __name__ == '__main__': unittest.main()
