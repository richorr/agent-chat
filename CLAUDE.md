# Agent Chat

- Claude maintains the Claude side only: everything under `claude/` plus `tests/test_claude_bridge.py`. Codex owns the Codex side (`codex_bridge.py`, `desktop_bridge.py`, `DISPATCHER.md`); propose changes to it rather than editing it.
- `PROTOCOL.md` is the shared contract; change it only in agreement with the Codex side.
- The installed copy runs from `~/.agent-bridge/bin/bridge.py`; after changing `claude/bridge.py`, copy it there and restart the launch agent (see `claude/README.md`).
- Keep routes, mailbox contents, policy, logs and state in `~/.agent-bridge/`, never in Git.
- Run `python3 -m unittest discover -s tests -v` before opening a PR.
