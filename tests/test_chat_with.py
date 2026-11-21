import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from desktop_bridge import chat_with, register_chat


SENDER = '11111111-1111-4111-8111-111111111111'
RECEIVER = '22222222-2222-4222-8222-222222222222'
OTHER = '33333333-3333-4333-8333-333333333333'


class ChatWithTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def test_cli_saves_instructions_privately_without_dispatch_or_incoming_registration(self):
        register_chat(self.home, 'review', RECEIVER)
        routes = self.home / 'state/codex-routes.json'
        before = routes.read_bytes()
        command = [sys.executable, 'desktop_bridge.py', '--home', str(self.home),
                   'chat-with', '--origin', SENDER]
        instructions = self.home / 'instructions.txt'
        instructions.write_text('When checks pass, ask for review.\nShare only the PR link.\n')
        receipt = subprocess.check_output(command + ['review', '--instructions-file', str(instructions)], text=True)
        self.assertEqual(json.loads(receipt)['approval'], 'ask')
        read_back = json.loads(subprocess.check_output(command + ['review'], text=True))
        self.assertEqual(read_back['instructions'], instructions.read_text())
        self.assertEqual(read_back['destination'], RECEIVER)
        self.assertTrue(read_back['routeCurrent'])
        self.assertEqual(routes.read_bytes(), before)
        self.assertEqual({path.name for path in self.home.iterdir()}, {'state', 'instructions.txt'})
        self.assertFalse(chat_with(self.home, OTHER, 'review')['saved'])

    def test_discovery_and_reassignment(self):
        register_chat(self.home, 'review', RECEIVER)
        register_chat(self.home, 'my-chat', SENDER)
        self.assertEqual(chat_with(self.home, SENDER)['registeredCodexNames'], ['review'])
        with self.assertRaises(ValueError):
            chat_with(self.home, SENDER, 'my-chat', 'Send a review')
        chat_with(self.home, SENDER, 'review', 'Share the PR link')
        register_chat(self.home, 'review', OTHER, RECEIVER)
        saved = chat_with(self.home, SENDER, 'review')
        self.assertFalse(saved['routeCurrent'])
        self.assertEqual(saved['destination'], RECEIVER)
        with self.assertRaisesRegex(ValueError, '--reconnect'):
            chat_with(self.home, SENDER, 'review', 'Share the new PR link')
        reconnected = chat_with(self.home, SENDER, 'review', 'Share the PR link', reconnect=True)
        self.assertEqual(reconnected['destination'], OTHER)

    def test_claude_connection_is_explicit_and_persists_across_instruction_updates(self):
        with self.assertRaises(ValueError):
            chat_with(self.home, SENDER, 'review', 'Share the PR link')
        chat_with(self.home, SENDER, 'review', 'Share the PR link', 'pyroto')
        register_chat(self.home, 'review', RECEIVER)
        chat_with(self.home, SENDER, 'review', 'Share the check results')
        saved = chat_with(self.home, SENDER, 'review')
        self.assertEqual(saved['provider'], 'claude')
        self.assertEqual(saved['destination'], 'pyroto')
        self.assertEqual(saved['instructions'], 'Share the check results')
