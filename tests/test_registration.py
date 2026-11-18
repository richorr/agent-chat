import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from desktop_bridge import register_chat, DesktopBridge

FIRST = '11111111-1111-4111-8111-111111111111'
SECOND = '22222222-2222-4222-8222-222222222222'


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.config = self.home / 'state/codex-routes.json'

    def test_register_read_back_and_dispatcher_can_load(self):
        result = register_chat(self.home, 'demo', FIRST)
        self.assertEqual(result['threadId'], FIRST)
        self.assertTrue(result['changed'])
        bridge = DesktopBridge(self.home)
        self.addCleanup(bridge.db.close)
        self.assertEqual(bridge.routes, {'demo': FIRST})

    def test_preserves_other_routes_and_is_idempotent(self):
        register_chat(self.home, 'other', SECOND)
        register_chat(self.home, 'demo', FIRST)
        before = self.config.read_bytes()
        self.assertFalse(register_chat(self.home, 'demo', FIRST)['changed'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(json.loads(before), {'other': SECOND, 'demo': FIRST})

    def test_conflict_and_stale_replacement_preserve_file(self):
        register_chat(self.home, 'demo', FIRST)
        before = self.config.read_bytes()
        for expected in (None, SECOND):
            with self.assertRaises(ValueError):
                register_chat(self.home, 'demo', SECOND, expected)
            self.assertEqual(self.config.read_bytes(), before)
        self.assertTrue(register_chat(self.home, 'demo', SECOND, FIRST)['changed'])
        self.assertEqual(json.loads(self.config.read_text())['demo'], SECOND)

    def test_invalid_input_does_not_create_registration(self):
        for project, ident in [('demo', 'claude-aa'), (' ', FIRST), (' demo', FIRST)]:
            with self.assertRaises(ValueError):
                register_chat(self.home, project, ident)
        self.assertFalse(self.config.exists())

    def test_malformed_routes_are_preserved(self):
        self.config.parent.mkdir()
        for content in ('{broken', '[]', '{"demo": 42}'):
            self.config.write_text(content)
            with self.assertRaises(ValueError):
                register_chat(self.home, 'demo', FIRST)
            self.assertEqual(self.config.read_text(), content)

    def test_cli_registers_without_creating_delivery_database(self):
        script = Path(__file__).resolve().parents[1] / 'desktop_bridge.py'
        command = [sys.executable, str(script), '--home', str(self.home),
                   'register', '--project', 'demo', '--chat-id', FIRST]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(result.stdout)['registered'])
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ['state'])
        conflict = subprocess.run(command[:-1] + [SECOND], capture_output=True, text=True)
        self.assertEqual(conflict.returncode, 1)
        self.assertIn('route conflict', conflict.stderr)
        self.assertEqual(json.loads(self.config.read_text())['demo'], FIRST)
