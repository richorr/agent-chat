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
- `project`: the destination name on the receiving side for a request, or the name the
  response came from. For Codex it is a key in `codex-routes.json`. For Claude it is a chat name
  registered with `/register-chat`, or a project folder name, which reaches the single chat
  registered from that project. Claude delivers only to registered chats.
- `created_at`: optional, UTC ISO-8601.
- `purpose` (optional, requests only): `user-requested` when a human asked for the request
  (this is the default when the field is missing), or, when an agent sends on its own initiative,
  one of `code-review`, `pr-fix`, `design-feedback`. Each side's human decides, per project,
  which self-initiated purposes their agents may send and their projects will accept.
  Claude side: `~/.agent-bridge/claude-policy.json` (view with `bridge.py policy show`).
  A self-initiated request a project doesn't accept gets an `ERROR:` response.

Error responses are ordinary responses whose `body` starts with `ERROR:` (for example,
an unknown project).

## Rules

1. **Atomic writes:** write to `tmp/`, then `rename` into the inbox. Readers ignore dotfiles and anything not ending in `.json`.
2. **At most once:** a reader records the `id` as processed *before* dispatching, then moves the file to `archive/`. A file whose `id` is already recorded is archived without being dispatched.
3. **No loops:** a `response` is never dispatched as a new task. It is only delivered back to whoever sent the original request.
4. **Unanswerable requests still get a response** (an `ERROR:` body), so the sender never waits forever.
