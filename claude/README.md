# Claude side of the bridge

The Claude half of the shared mailbox in [PROTOCOL.md](../PROTOCOL.md). It routes Codex
requests into running Claude Code sessions and Codex's answers back to the session that asked.

## How it works

- `bridge.py serve` is a plain Python loop with no model of its own. It sleeps until a file
  lands in `~/.agent-bridge/to-claude/` (kqueue on macOS, 1-second polling elsewhere).
- Delivering into a running Claude session requires Claude Code's built-in cross-session
  messaging, which only exists inside a Claude session. So for each message, `serve` starts
  one fresh, single-use `claude -p` run on Claude Haiku 5.5 that may only call `ListAgents`
  and `SendMessage`. Each run starts with an empty context, so cost per message stays flat
  (about $0.003) and nothing ever needs compacting. An idle bridge costs nothing.
- Incoming dispatch is opt-in. A session claims a chat name with `/register-chat <name>`, and
  Codex requests addressed to that name (in the `project` field) go to that session only. For
  compatibility, a request addressed to a project folder name reaches the one chat registered
  from a session of that project. Unregistered sessions never receive requests. An unknown or
  ambiguous name, or a registered session that is offline, gets an `ERROR:` response; the bridge
  never picks another session instead.
- In the sending session, `/chat-with <codex name>` plus plain-language text saves when to ask
  that Codex chat something. When that applies, the session shows the destination and exact
  message and asks before sending. If Codex later points that name at a different chat, `send`
  refuses until the user reconnects.
- Project sessions answer by running `bridge.py reply <id> --body-file <file>`. The exact
  command is included in every delivered request, so sessions need no setup to answer.
- At most once: a message id is recorded as processed before dispatch, and a re-delivered
  file is archived without running again. Unanswered Codex requests get an `ERROR:`
  response after 2 hours (`serve --reply-timeout`).

## Install (macOS)

Run these commands from the root of the Agent Chat clone. The Claude dispatcher
requires Python 3.8+ (standard library only) and Claude Code (`claude`) signed in.
For the Codex side, follow the [Codex Desktop setup guide](../SETUP.md).

```sh
mkdir -p ~/.agent-bridge/bin ~/.agent-bridge/state ~/Library/LaunchAgents
cp claude/bridge.py ~/.agent-bridge/bin/bridge.py
cp PROTOCOL.md ~/.agent-bridge/PROTOCOL.md
cp claude/claude-policy.example.json ~/.agent-bridge/claude-policy.json   # then edit
```

1. **Run it as a launch agent** so it starts at login and restarts if it crashes:
   ```sh
   sed "s#HOME_DIR#$HOME#g" claude/local.agent-bridge.serve.plist.example \
     > ~/Library/LaunchAgents/local.agent-bridge.serve.plist
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.agent-bridge.serve.plist
   tail -f ~/.agent-bridge/state/serve.log
   ```
   Make sure the plist's `PATH` includes the folder holding `claude` (`command -v claude`).
2. **Teach your Claude sessions** how to send and answer: append
   [`global-CLAUDE.md`](global-CLAUDE.md) to `~/.claude/CLAUDE.md`, and install the two skills
   for every project:
   ```sh
   mkdir -p ~/.claude/skills
   cp -R claude/skills/register-chat claude/skills/chat-with ~/.claude/skills/
   ```
   Then run `/register-chat <name>` in each session that should receive Codex requests.
3. **Let sessions run the bridge without a prompt each time** by adding these rules to
   `permissions.allow` in `~/.claude/settings.json`. Replace `HOME_DIR` with
   the absolute path to your home directory, for example `/Users/you`.
   ```json
   "Bash(python3 ~/.agent-bridge/bin/bridge.py:*)",
   "Bash(python3 HOME_DIR/.agent-bridge/bin/bridge.py:*)"
   ```

After changing `bridge.py`, copy it again and run
`launchctl kickstart -k gui/$(id -u)/local.agent-bridge.serve`.

## Self-initiated requests (per-project policy)

Agents may send to Codex without being asked only for the purposes your policy allows,
by default `code-review`, `pr-fix` and `design-feedback`. The same policy decides which
self-initiated Codex requests (`purpose` field) each Claude project accepts. Edit it in a
terminal; the edit commands refuse to run without one, so agents can't change it through
their shell tool. This is a guardrail for cooperative agents, not a security boundary.

```sh
python3 ~/.agent-bridge/bin/bridge.py policy show
python3 ~/.agent-bridge/bin/bridge.py policy deny my-project pr-fix
python3 ~/.agent-bridge/bin/bridge.py policy reset my-project
```

## Files

| Path | Purpose |
| --- | --- |
| `~/.agent-bridge/bin/bridge.py` | installed copy of `claude/bridge.py` |
| `~/.agent-bridge/claude-policy.json` | your per-project policy |
| `~/.agent-bridge/router/` | working folder for the Haiku routing runs (created automatically) |
| `~/.agent-bridge/state/claude-*.{txt,json}` | processed ids and open routes; keep when restarting |
| `~/.agent-bridge/state/claude-chats.json` | registered chat names (private, mode 600) |
| `~/.agent-bridge/state/claude-connections.json` | each session's saved `/chat-with` instructions (private) |
| `~/.agent-bridge/state/serve.log` | one line per routed message, with its cost |

None of these belong in Git.

## Tests

```sh
python3 -m unittest tests.test_claude_bridge -v
```
