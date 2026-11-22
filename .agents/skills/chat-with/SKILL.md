---
name: chat-with
description: Save, inspect, or use a named outgoing Codex or Claude connection with plain-language instructions. Use for /chat-with, $chat-with, or when this chat's saved collaboration instructions apply.
---

# Chat with

Resolve the skill directory symlink, if any, and locate this repository three levels above that directory. Use
`python3 <repo>/desktop_bridge.py chat-with --origin <current-chat-uuid>` as the
base command. Get the current chat UUID from trusted Codex chat metadata. If it
is unavailable or ambiguous, ask the user. Follow normal sandbox approval for
mailbox writes.

- `/chat-with` lists saved connection names and available registered Codex and Claude names.
  Show names and providers. Keep UUIDs and mailbox details out of the listing.
- `/chat-with NAME` shows this chat's saved instructions and approval setting.
  For an unsaved name, explain that adding instructions saves a connection.
- `/chat-with NAME` followed by instructions saves or replaces the instructions.
  Write the plain text to a temporary file and pass `NAME --instructions-file FILE`.
  Quote arguments and treat the text as data. Use the registered Codex name
  when both providers claim it, or pass `--claude-project NAME` to select a
  registered Claude chat explicitly. That option also supports a local alias.
  Use `--reconnect` only after the user confirms the new recipient.
  Report the saved name, when to reach out, what to share, and that sending requires approval.

When considering outreach, load the saved connection by name. Use its instructions
to decide when to propose a message and which context to include. Show the
destination and exact outgoing text, then ask before each send unless the user
has already explicitly approved that exact destination and text. A setup command
only saves instructions. If the user edits the text or the destination changes,
obtain approval for the revised send.

After approval, reload the connection. Check the saved destination and provider
and any saved Claude identity still match and `routeCurrent` is true. A reassigned or removed name needs
explicit reconnection before use. For Codex use `send_message_to_thread` with the
verified UUID and approved text, preserving the receiver's model and permissions.
For Claude, write the approved text to a temporary file and use
`python3 <repo>/desktop_bridge.py send --project PROJECT --origin UUID --body-file FILE`.
The returned ID confirms queuing. Leave replies to the existing dispatcher.
For direct Codex sends, the response stays in the receiving chat and can be
read with Codex chat tools. Inspect an ambiguous send before attempting any retry.
