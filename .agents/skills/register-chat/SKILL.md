---
name: register-chat
description: Register the current Codex chat for the shared agent-bridge dispatcher when the user asks to register a chat or make it reachable through the bridge.
---

# Register chat

Use the existing dispatcher; do not start another dispatcher, change its schedule,
send test messages, or alter permission policy as part of registration.
In Codex, the project is the incoming route name. Use the name supplied with
`/register-chat NAME`, or the repository folder name when no name is supplied.
This opts the current chat into incoming dispatch for that name. A sending chat
can discover it with `/chat-with`.

## Codex

1. Discover the current chat UUID from the available Codex chat tools and their
   current-chat metadata. Do not substitute a title, project name, or another chat
   sharing the same folder. If the tools cannot identify it unambiguously, ask for
   the current chat UUID. Never infer it from a temporary file path.
2. Locate this skill's repository root (three levels above this skill directory).
   Run `python3 <repo>/desktop_bridge.py register --project <project> --chat-id <uuid>`.
   Quote arguments as shell data. The helper preserves other routes and refuses
   to overwrite a different destination. If replacement was explicitly requested,
   use `--replace-chat-id <previous-uuid>` with the existing value; otherwise ask
   whether to replace the destination reported by the helper. A repeated identical
   registration succeeds without changing the mapping.
3. Report the project and verified destination from the helper receipt. Registration
   does not prove the dispatcher is running or that end-to-end delivery works.

## Storage and portability

The tracked source is `.agents/skills/register-chat/SKILL.md`. Keep the
helper and skill together when moving or cloning the repository. No machine-specific
checkout path belongs in the skill. Python 3 and a local filesystem are required.
Personal mappings live in `~/.agent-bridge/state/codex-routes.json`, outside Git.
Use `desktop_bridge.py --home <mailbox> register ...` only for an explicitly selected
alternate mailbox or isolated tests. Follow normal host permissions for mailbox
writes; do not change permissions to bypass a denial.
