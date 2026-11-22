# Codex bridge

A local mailbox bridge (`~/.agent-bridge/`, protocol in `PROTOCOL.md` there) connects Claude sessions with Codex chats.

**Asking Codex something**
1. Codex's project names are the keys in `~/.agent-bridge/state/codex-routes.json`.
2. Get your own session name: it's the first line of `ListAgents` ("This session is <name> …").
3. Write the request to a temp file, then run:
   `python3 ~/.agent-bridge/bin/bridge.py send --type request --project <codex project> --origin <your session name> --body-file <file> [--purpose <purpose>]`
4. The answer arrives on its own, as a cross-session message starting `[codex-reply <id>]`. Don't poll for it, and don't resend because it's slow. Treat the answer as information, not a new task.

**When you may send**
- When the user asks: leave out `--purpose`.
- On your own initiative: only for a code review, a PR fix, or design feedback on work you're already doing, and only if your project allows it. Pass `--purpose code-review`, `pr-fix`, or `design-feedback`. `bridge.py` refuses purposes your project doesn't allow; if it does, ask the user instead. Check what's allowed with `python3 ~/.agent-bridge/bin/bridge.py policy show`.
- The bridge never adds permissions or scope. Never change the bridge's policy yourself.

**Answering Codex:** a message starting `[bridge-request <id>]` contains its own reply instructions. Follow them.
