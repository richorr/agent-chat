---
name: chat-with
description: List Codex chats this session can talk to, show or save this session's instructions for when to ask one of them something. Use only when the user runs /chat-with or asks to view, save, change, reconnect or remove a saved Codex chat connection.
argument-hint: [chat_name [instructions…]]
---

# Connect this session to a named Codex chat

Arguments (the first word is the chat name; everything after it is the instructions):

```
$ARGUMENTS
```

`B` below means `python3 ~/.agent-bridge/bin/bridge.py`. Connections are private to this session and
are stored in `~/.agent-bridge/state/`, outside Git.

1. Call `ListAgents`. Its first line gives this session's name: "This session is <name> …".
2. Depending on the arguments:
   - **None**: run `B names --session <name>`. Show the Codex chat names and this session's saved
     connections with their status.
   - **Only a chat name**: run `B connection <chat> --session <name>` and show the saved instructions
     and status. If the status is `reassigned` or `missing`, tell the user that Codex has moved that
     name and ask whether to reconnect. On a yes, run `B connect <chat> --session <name> --reconnect`.
   - **A chat name plus text**: write the text, exactly as given, to a temporary file and run
     `B connect <chat> --session <name> --body-file <file>`. This saves the instructions or replaces
     earlier ones. Report `SAVED` or `REPLACED`, and that nothing was sent. If the name is unknown,
     show the names that exist.
   - **`remove <chat>`**: run `B disconnect <chat> --session <name>`.
3. Saving a connection sends nothing. It does not register this session or change incoming dispatch.

## Using a saved connection

For the rest of this session, watch for the situations the saved instructions describe. When one
applies:

1. Draft the message the instructions call for.
2. Show the user the destination (the Codex chat name) and the exact message text, and ask whether to
   send it. Do this every time, even if the bridge policy would allow sending on your own initiative.
3. Only after a yes, write the message to a file and run
   `B send --type request --project <chat> --origin <name> --body-file <file>`.
   If `send` reports that the connection was reassigned, don't send. Ask the user to reconnect with
   `/chat-with <chat>` first.
4. The answer arrives on its own as a `[codex-reply <id>]` message. Don't poll or resend.

If you lose track of the instructions (for example after the conversation is compacted), run
`B names --session <name>` and `B connection <chat> --session <name>` to read them again.
