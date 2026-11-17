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
        self.assertEqual(len(replies), 1)
    def test_restart_never_reclaims(self):
        self.incoming(); self.b.scan(); self.b.claim('claude-one'); self.b.db.close()
        self.b = DesktopBridge(self.home, {'demo': 'chat-123'})
        self.assertEqual(self.b.scan()['actions'][0]['status'], 'desktop-claimed')
        with self.assertRaises(ValueError): self.b.claim('claude-one')
    def test_backoff_and_reset(self):
        self.assertEqual([self.b.scan()['next_poll_minutes'] for _ in range(6)], [10, 20, 30, 30, 30, 30])
        self.incoming(); self.assertEqual(self.b.scan()['next_poll_minutes'], 5)
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
    def test_invalid_json_is_archived(self):
        (self.home / 'to-codex/bad.json').write_text('{')
        self.assertEqual(self.b.scan()['actions'], [])
        self.assertEqual(len(list((self.home / 'archive').glob('*.json'))), 1)
    def test_claim_timeout_reports_error(self):
        self.incoming(); self.b.scan(); self.b.claim('claude-one')
        self.b.save('UPDATE incoming SET started=0', ())
        self.assertEqual(self.b.scan()['actions'], [])
        reply = json.loads(next((self.home / 'to-claude').glob('*.json')).read_text())
        self.assertTrue(reply['body'].startswith('ERROR:'))

if __name__ == '__main__': unittest.main()
