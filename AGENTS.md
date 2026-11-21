# Agent Chat

- Implement the Codex side only; Claude maintains its own dispatcher and CLAUDE.md.
- The shared filesystem protocol is PROTOCOL.md. Mailbox bookkeeping uses no model; Desktop routing uses a low-cost scheduled agent.
- Keep personal routes, chat IDs, mailbox contents, logs, and state outside Git.
- Never automatically approve Codex permissions or override a destination chat's model/sandbox.
- Prefer at-most-once task dispatch over retrying ambiguous delivery.
- Desktop-owned chats require the in-app send_message_to_thread tool; the external CLI daemon cannot own them concurrently.
- Idle polling doubles from 5 to 30 minutes and resets on activity.
- Run `python3 -m unittest discover -s tests -v` before opening or merging a PR.
- Use one persistent dispatcher chat with a heartbeat automation; do not create sidebar chats per poll. Keep durable delivery state in SQLite, not chat history. Preserve heartbeat kind and target chat when rescheduling.
- Keep the dispatcher separate from registered work destinations: it cannot send to itself. Destination chats may use subagents for authorized work; the mailbox dispatcher only routes messages.
- The portable registration skill is tracked in `.agents/skills/register-chat`, with a tracked `.claude/skills/register-chat` symlink. Codex registration uses `desktop_bridge.py register`; Claude verifies live coordinator discovery without changing its dispatcher. Personal registrations remain outside Git.
- Use the chat-with skill for named outgoing connections. Consult this chat's saved instructions before proposing cross-chat work and obtain approval for the destination and exact text before sending. Keep connections private and separate from incoming route registration.
