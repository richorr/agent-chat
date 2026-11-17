# Agent Chat

- Implement the Codex side only; Claude maintains its own dispatcher and CLAUDE.md.
- The shared filesystem protocol is PROTOCOL.md. Mailbox bookkeeping uses no model; Desktop routing uses a low-cost scheduled agent.
- Keep personal routes, chat IDs, mailbox contents, logs, and state outside Git.
- Never automatically approve Codex permissions or override a destination chat's model/sandbox.
- Prefer at-most-once task dispatch over retrying ambiguous delivery.
- Desktop-owned chats require the in-app send_message_to_thread tool; the external CLI daemon cannot own them concurrently.
- Idle polling doubles from 5 to 30 minutes and resets on activity.
- Run `python3 -m unittest discover -s tests -v` before opening or merging a PR.
- Standalone dispatcher runs start fresh; keep durable memory in SQLite, not chat history.
