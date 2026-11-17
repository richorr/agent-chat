# Agent Bridge Protocol (v1)

A shared mailbox between the Claude dispatcher and the Codex dispatcher.
Each side routes messages to its own project chats; this folder is the only shared component.

## Layout

```
~/.agent-bridge/
  to-codex/    Claude writes, Codex reads
  to-claude/   Codex writes, Claude reads
  archive/     processed messages (each reader moves what it has handled here)
  tmp/         staging for atomic writes (never read from here)
  state/       per-side bookkeeping; Claude uses claude-*.{txt,json}, Codex should use codex-*
  bin/         Claude-side tooling (bridge.py)
```

## Message

One JSON file per message, named `<id>.json`:

```json
{
  "id": "codex-20261117T150000-ab12cd34",
  "reply_to": null,
  "from": "codex",
  "to": "claude",
  "project": "pyroto",
  "type": "request",
  "body": "The task to perform",
  "created_at": "2026-11-17T15:00:00Z"
}
```

- `id`: globally unique. Prefix with the sender (`claude-` / `codex-`).
- `type`: `request` or `response`.
- `reply_to`: `null` for requests; the original request's `id` for responses.
- `project`: the project the request is for (on the receiving side), or the project
  the response came from. For Claude this is a folder name under `~/Development`.
- `created_at`: optional, UTC ISO-8601.

Error responses are ordinary responses whose `body` starts with `ERROR:` (for example,
an unknown project).

## Rules

1. **Atomic writes:** write to `tmp/`, then `rename` into the inbox. Readers ignore dotfiles and anything not ending in `.json`.
2. **At most once:** a reader records the `id` as processed *before* dispatching, then moves the file to `archive/`. A file whose `id` is already recorded is archived without being dispatched.
3. **No loops:** a `response` is never dispatched as a new task. It is only delivered back to whoever sent the original request.
4. **Unanswerable requests still get a response** (an `ERROR:` body), so the sender never waits forever.
