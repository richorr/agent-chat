# Agent Chat

A shared-mailbox bridge between Claude sessions and existing Codex Desktop chats.
Claude maintains its own dispatcher. This repository implements the Codex mailbox
helper and instructions for a scheduled dispatcher inside Codex Desktop.

```text
Claude project chat ⇄ Claude dispatcher ⇄ ~/.agent-bridge/
                                      ⇄ Codex dispatcher ⇄ Codex Desktop chat
```

## How it works

Python handles mailbox files, durable SQLite state, duplicate suppression and
reply correlation without model calls. A scheduled Codex agent uses Desktop's
`send_message_to_thread` and `read_thread` tools to deliver requests and collect
answers. It routes only explicitly configured project names; it does no project work.

The dispatcher starts at five minutes, doubles its interval on empty checks
(10, 20, then 30 minutes), and resets to five minutes on activity. Outstanding
requests keep the interval at five minutes. At the idle cap there are approximately
48 model checks per day. This is low-cost polling, not free or immediate delivery.
The app and local automation scheduler must be available; sleep or scheduler delays
can delay delivery. Each destination uses its own model and permissions.

Use one persistent dispatcher chat with a heartbeat automation. Routes and
delivery state live on disk; chat history is not delivery state. Heartbeat checks
reuse the dispatcher chat instead of creating a new sidebar chat per check.

## Setup

Requires Python 3.9+ (standard library only), Codex Desktop with local automations
and cross-chat tools, and a separately configured Claude dispatcher implementing
[PROTOCOL.md](PROTOCOL.md). No external Python packages are required.

Follow [SETUP.md](SETUP.md) for clone and verification commands, destination
registration, a reusable dispatcher prompt, permissions, and troubleshooting.
Use one heartbeat attached to your dispatcher chat, an available low-cost model,
and notifications for failed runs only. Keep private mailbox data outside Git.
The Claude dispatcher must be installed separately; this repository does not
provide a complete two-sided installer.

## Commands

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
  answer in a chat. Busy chats remain pending. Unresolved messages expire after
  one hour with an error; this does not cancel work already running in a chat.
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
[CLAUDE.md](CLAUDE.md) for Claude's separately maintained dispatcher instructions.
Claude's referenced `bridge.py` tool is not included here.
