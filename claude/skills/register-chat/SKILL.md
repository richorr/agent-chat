---
name: register-chat
description: Claim a chat name for this Claude session so Codex can send it requests through the agent bridge. Use only when the user runs /register-chat <name> or asks to register, move or unregister this session's chat name.
argument-hint: <chat_name> | unregister <chat_name> | list
---

# Register this session as a named chat

Arguments: `$ARGUMENTS`

A registered name opts **this** session into incoming dispatch: Codex requests addressed to that name
are delivered here and nowhere else. Sessions without a registered name receive nothing. Registering
sends nothing and does not change permissions, the bridge policy, or any saved `/chat-with` connections.

`B` below means `python3 ~/.agent-bridge/bin/bridge.py`.

1. Call `ListAgents`. Its first line is "This session is <name> [<ref>] …". Note `<name>` and `<ref>`.
2. Depending on the arguments:
   - **`list`**: run `B chats` and show the result. Stop.
   - **`unregister <chat>`**: run `B unregister <chat> --session <name>`, report the result. Stop.
   - **`<chat>`**: run `B register <chat> --session <name> --ref <ref>`.
   - **Nothing**: ask the user which name to claim. Suggest the project folder name.
3. Read the result:
   - `REGISTERED` / `UNCHANGED` / `MOVED`: report which name now delivers to this session.
   - `CLAIMED: '<chat>' is registered to session <other>`: the name belongs to another session.
     Check whether `<other>` appears in the `ListAgents` output from step 1, and tell the user
     "`<chat>` is claimed by `<other>`, which is online/offline." Then ask whether to move it to this
     session. Only if they say yes, run `B register <chat> --session <name> --ref <ref> --replace <other>`.
     Never pass `--replace` without that yes.
4. Tell the user: Codex sends to this chat by putting the name in a request's `project` field. For
   now, Codex requests sent to the project folder name also reach this chat, as long as it is the
   only chat registered from this project. Registration does not prove that delivery works end to end.

If this session restarts and gets a new session name, the registration still points to the old
session. Requests then come back to Codex as "offline" errors until the user runs /register-chat again.
