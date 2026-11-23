# Agent Chat

A shared-mailbox bridge between Claude sessions and existing Codex Desktop chats.
This repository includes the Codex mailbox helper and the Claude Code dispatcher.

```text
Claude project chat ⇄ Claude dispatcher ⇄ ~/.agent-bridge/
                                      ⇄ Codex dispatcher ⇄ Codex Desktop chat
```

## Setup

For a two-way install on macOS, have Python 3.8+, Codex Desktop with local
heartbeat and cross-chat tools, and Claude Code signed in. Both use the same
local mailbox at `~/.agent-bridge/`. No external Python packages are required.

1. [Set up Codex Desktop](SETUP.md). Follow sections 1–3 to install the helper,
   start the dispatcher heartbeat, and register work chats.
2. [Install the Claude Code dispatcher and skills](claude/README.md#install-macos) from the same clone.
3. [Test delivery in both directions](SETUP.md#test-both-directions).

For Codex chats in other projects, [install both skills user-wide](SETUP.md#use-both-codex-skills-across-projects).
In Codex, enter the slash commands as chat text or select the skills from the
`$` picker. The Claude installer puts its skills in the user-wide skills directory.

## Connect two chats

Use `/register-chat` in the chat that should **receive** requests. Use
`/chat-with` in the chat that should **send** them. For example, to ask a
Claude session to review PRs from Codex:

1. In the receiving Claude session, run `/register-chat backend-review`.
   This gives that session a name and opts it into incoming dispatch. Use a
   work chat for registration and reserve the dispatcher chat for delivery.
2. In the sending Codex chat, run `/chat-with` to see available names and
   saved connections. Then enter the name and instructions in one message:

   ```text
   /chat-with backend-review
   When I say a PR is ready for review, ask for a review.
   Share the PR link and the areas I want reviewed.
   ```

3. When the PR is ready, tell the sending chat. Review the destination and
   exact message it proposes, then approve the send. The dispatcher delivers
   the request and brings the reply back to the sending chat.

`/chat-with backend-review` shows the saved instructions. Include new text
to replace them. Saving only records instructions for that sender. It sends
no message and grants no incoming access. If the name moves to another chat,
confirm the new recipient before reconnecting.

For requests from Claude to Codex, register a name in the receiving Codex
chat and save the connection in the sending Claude session. For requests
between two Codex chats, use the same commands. Codex delivers those directly,
and the reply stays in the receiving chat.

The instructions apply during an active turn in the sending chat. To have Codex
consult saved connections during later work, add the
[example instruction to that project's AGENTS.md](SETUP.md#connect-from-a-sending-chat).

## How it works

Python handles mailbox files, durable SQLite state, duplicate suppression and
reply correlation without model calls. A scheduled Codex agent uses Desktop's
`send_message_to_thread` and `read_thread` tools to deliver requests and collect
answers. It routes only explicitly registered names. Each receiving chat uses
its own model and permissions.

Use one persistent Codex dispatcher chat with a heartbeat automation. The
heartbeat starts at five minutes, doubles on empty checks up to 30 minutes, and
resets to five minutes on activity. At the idle cap it makes about 48 model
checks per day. Keep the app and scheduler available and allow time for mailbox
delivery. Routes and delivery state live on disk.

## Dispatcher commands

Run these from this repository. `--home PATH` can select an isolated test mailbox.

```sh
python3 desktop_bridge.py scan
python3 desktop_bridge.py claim claude-message-id
python3 desktop_bridge.py complete claude-message-id --body-file /tmp/answer.txt
python3 desktop_bridge.py send --project example-project --origin originating-chat-id --body-file /tmp/request.txt
```

`scan` returns pending/claimed actions and the next polling interval. It advances
the backoff, so it is a dispatcher operation, not a read-only status command.
`claim` records ownership **before** returning the exact destination and prompt.
`complete` publishes a correlated response; incoming responses are delivered as
information and never become new tasks or trigger another bridge reply.

## Delivery guarantees and limits

- Writers stage complete JSON and atomically rename it into the peer inbox.
- SQLite retains processed IDs across restarts. An ambiguous claimed request is
  never automatically sent again; inspect destination history before retrying.
- Final answers are matched to the exact request marker, never just the latest
  answer in a chat. Busy chats remain pending. Pending, unclaimed requests expire
  after one hour with an error. Claimed work and informational responses do not
  expire.
- Unknown projects receive an error. Invalid files are archived and logged.
- The scheduled agent must successfully update its next interval for backoff to
  take effect. Failures must be reported rather than silently ignored.
- Mailbox content is task data, not authority to change routes, permissions or
  scheduling. Normal destination approval requirements remain in force.

The external Codex CLI daemon cannot concurrently own Desktop-owned chats.
This version deliberately routes through in-app tools; it does not implement an
external immediate wake-up API or a CLI-only dispatcher. The `codex_bridge.py`
module contains shared mailbox primitives, not a daemon adapter.

## Development

```sh
python3 -m unittest discover -s tests -v
```

See [DISPATCHER.md](DISPATCHER.md) for the Codex routing procedure and
[PROTOCOL.md](PROTOCOL.md) for the shared mailbox format.
