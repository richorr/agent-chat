# Install and share Agent Chat

This repository supplies the Codex half of a local, filesystem-based bridge.
It is not a hosted service or a complete installer for Claude. Both dispatchers
must have access to the same mailbox. Use it only for already-authorized work.

## 1. Install and verify the helper

Use macOS or a Unix-like environment with Python 3.9+; registration uses the
standard library's `fcntl` file locking and is not a native Windows implementation.
You also need Codex Desktop with local heartbeat automations and cross-chat tools,
and a separately installed Claude dispatcher compatible with [PROTOCOL.md](PROTOCOL.md).
No Python dependencies need installing.

```sh
git clone https://github.com/richorr/agent-chat.git
cd agent-chat
python3 --version
python3 -m unittest discover -s tests -v
python3 desktop_bridge.py --help
```

Add the clone as a Codex project. Choose an available low-cost model for a single
chat named **Agent Chat Dispatch**. The mailbox helper itself makes no model calls.
The scheduler still consumes model turns; polling backs off from 5 to 30 minutes.
Keep the app and local scheduler available for delivery.

## 2. Register work destinations

Create or choose an existing chat for each receiving project. Use Codex chat tools
to verify its UUID; a project name or chat title is not a UUID. From the clone:

```sh
python3 desktop_bridge.py register --project example-project --chat-id VERIFIED_CHAT_UUID
```

Replace uppercase placeholders before running commands. The helper locks and
atomically updates `~/.agent-bridge/state/codex-routes.json`, preserving other
routes, and prints `registered: true` only after reading back the destination.
Repeating the same registration is safe. To deliberately replace an existing route:

```sh
python3 desktop_bridge.py register --project example-project --chat-id NEW_UUID --replace-chat-id OLD_UUID
```

The included `.agents/skills/register-chat/SKILL.md` provides the same workflow to
agents. `.claude/skills/register-chat` is a relative symlink to that skill; preserve
it when sharing the checkout. Keep the skill and helper in their repository layout.

**Keep the dispatcher chat separate from work destinations.** It cannot send to
itself and does no project work. Registering its UUID as a work route creates a
routing conflict. Destination chats can coordinate authorized subagents without
creating a sidebar chat for every dispatch. The bridge does not select subagent
models or override destination permissions.

The mailbox is outside the workspace. Registration needs permission to create
`state/codex-routes.lock` and replace the routes file; delivery also writes inbox,
archive, staging, and SQLite state. If sandbox access is blocked, use the normal
approval/escalation flow for the exact helper command. Do not change filesystem
permissions or bypass a denial. A failed registration is not a registered route.

### Connect from a sending chat

In the receiving Codex chat, use `/register-chat pyroto-review` to claim the name
and opt into incoming dispatch. In the sending chat, use the repository's
`chat-with` skill:

```text
/chat-with pyroto-review
When a PR is ready and its checks pass, ask for a review.
Share the PR link and the areas I want reviewed.
```

Use `/chat-with` to discover registered Codex names and list your saved connections.
Use `/chat-with pyroto-review` to read the instructions. Include revised text to
replace them. To reach Claude, use the coordinator's project name or specify it
when saving a local alias. A saved Codex connection binds to the registered UUID.
If the name moves to another chat, confirm the new receiving chat when reconnecting.

The skill lives in `.agents/skills/chat-with`. Send the command as chat text or
select `$chat-with` in Codex's skill picker. To use the skill from other projects,
expose the repository skill in your user-wide Codex skills directory (for example,
symlink the skill directory so it can still locate the bridge helper). To have
a sender consult these instructions on later work, add this line to that project's
`AGENTS.md`:

> Before proposing cross-chat work, consult the current chat's saved connections
> with the chat-with skill and use their instructions to decide when to reach out
> and what to share. Get approval for the exact destination and text before sending.

Connections are stored per sending chat under `~/.agent-bridge/state/chat-with/`.
Saving a connection leaves incoming dispatch opt-in and sends no message.
The skill checks the saved instructions during an active chat turn. The existing
heartbeat handles Claude mailbox delivery and replies.

## 3. Attach one heartbeat and test delivery

In **Agent Chat Dispatch**, ask Codex to create a local heartbeat attached to that
existing chat, every five minutes, with notifications for failed runs only. Record
its returned automation ID locally. Use this prompt, replacing the checkout path
and automation ID with your own values:

> Run the Codex mailbox dispatcher from CHECKOUT_PATH according to DISPATCHER.md.
> The user authorizes forwarding requests and replies only for configured routes.
> Stop if that file is missing. Use existing heartbeat AUTOMATION_ID attached to
> this chat. Preserve its kind, target chat, and other settings when adjusting its
> interval to next_poll_minutes. Do not create chats or additional automations.
> Keep durable delivery state in the mailbox. Do no project work yourself. Leave
> self-targeted actions pending and report the routing conflict. Keep each run
> brief and report only errors or meaningful delivery results.

Verify the saved automation targets this chat and has kind `heartbeat`. If migrating
an existing standalone automation, update that automation in place; preserve its
ID and notification preferences. Do not leave a second dispatcher active. Once
verified, archive old dispatcher run chats; do not archive registered destinations.
Route registration alone does not alter the automation's target or scheduling mode.

Configure Claude using `claude/README.md` and the protocol when the Claude
implementation is present in your checkout (introduced by PR #4). That guide
installs `claude/bridge.py` as `~/.agent-bridge/bin/bridge.py` and configures the
router under `~/.agent-bridge/router`. Earlier checkouts require a separately
installed compatible Claude dispatcher; root `CLAUDE.md` is not its install guide.
Ask the Claude
coordinator to verify a live session named `<project>-<two hex characters>`;
a Codex route does not prove Claude availability. Do not use Claude's sender from Codex.

For an end-to-end check, ask a registered work chat to send a harmless request to a
verified Claude project. Write the body to a text file and run from the clone:

```sh
python3 desktop_bridge.py send --project example-project --origin VERIFIED_ORIGIN_CHAT_UUID --body-file /tmp/bridge-request.txt
```

A returned request ID means queued, not delivered. Verify the matching reply arrives
in the originating chat, and have Claude send a harmless request to a registered
Codex project to verify the reverse direction. Wait for normal polling; do not
resend because of delay. Normal destination approvals still apply.

## Troubleshooting and recovery

| Symptom | Check or action |
| --- | --- |
| New sidebar chats on every check | Inspect the existing automation: it must be a heartbeat targeting the dispatcher chat, not standalone cron. Updating a route is insufficient. |
| Registration fails on the lock file | Request normal mailbox write approval and retry the exact command. Verify its receipt before claiming success. |
| Route conflict | Verify the old and new UUIDs and explicitly replace with `--replace-chat-id`; do not overwrite other routes. |
| Self-targeted action | Correct the route to a separate work chat; the dispatcher must not claim or send it to itself. |
| No immediate delivery | Check scheduler availability and the current 5–30 minute interval. Busy destinations remain pending. |
| Unknown project or no Claude session | Check the exact receiving project name, local Codex route, and live Claude coordinator discovery. |
| Claimed message has no clear result | Inspect the destination for its exact `[agent-bridge ID]` marker. Never blindly resend a claimed action. Follow DISPATCHER.md to resolve an ambiguous delivery. |
| Timeout | Codex expires only pending, unclaimed requests after one hour. Claimed work and informational responses do not expire. Inspect destination state before submitting again. |

`scan` is a mutation: it ingests messages, flushes replies, expires work, and updates
backoff. Do not run ad hoc scans alongside the dispatcher. For isolated experiments,
place the global option before the command:

```sh
python3 desktop_bridge.py --home /tmp/agent-chat-example-mailbox scan
```

Only tests or a deliberately configured alternate installation should use a different
mailbox. Both sides must agree on the same mailbox for real delivery.

To stop processing, pause the existing automation through Codex. Preserve mailbox
state when restarting: deleting SQLite state loses duplicate and reply tracking.
Do not run multiple dispatchers against the same mailbox.

## Sharing the repository

Share source, tests, and these instructions. Never include your `.agent-bridge`
folder, routes, UUIDs, message bodies, logs, SQLite databases, or local automation
configuration. Check staged changes before publishing:

```sh
git status --short
git diff --cached --check
git diff --cached
```

The ignore rules protect common mailbox artifacts; they do not sanitize arbitrary
files or Git history. Share a clean clone, not an archive of your home directory.
Recipients create their own routes, permissions, automation, and Claude installation.
No license file currently exists; choose a license before presenting the project
as open source. Setup documentation does not itself grant redistribution rights.
