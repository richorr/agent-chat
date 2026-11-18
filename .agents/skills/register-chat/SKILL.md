---
name: register-chat
description: Register the current Codex chat or verify the current Claude session for the shared agent-bridge dispatcher when the user asks to register a chat or make it reachable through the bridge.
---

# Register chat

Use the existing dispatcher; do not start another dispatcher, change its schedule,
send test messages, or alter permission policy as part of registration.
The project is the exact receiving project name, normally the repository folder
name. Use an explicitly supplied project name when provided.

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

## Claude

Claude's current dispatcher does not use a persistent chat registry. It discovers
live peer sessions through its coordinator's `ListAgents` capability.

1. Use the coordinator/session tools to identify this session's actual name and
   verify that it appears among the live peers. Names must be `<project>-<two hex
   characters>`; remove only that final suffix when checking the project.
2. If this session matches, report it as discoverable, with no route-file write.
   If multiple sessions match, explain that the current dispatcher prefers idle
   sessions, then the most recently started; this does not pin this particular chat.
   If this session is absent, misnamed, or the coordinator tools are unavailable,
   report that registration is not established and what is missing. Do not invent
   a Claude registry or modify Claude's dispatcher to work around it.
3. Never put a Claude session name in `codex-routes.json`. If the user instead asks
   Claude to register a Codex destination, use the Codex helper with a verified,
   explicitly identified Codex UUID; do not treat Claude's session as that UUID.

## Storage and portability

The tracked source is `.agents/skills/register-chat/SKILL.md`; the repository's
`.claude/skills/register-chat` symlink exposes the same skill to Claude. Keep the
helper and skill together when moving or cloning the repository. No machine-specific
checkout path belongs in the skill. Python 3 and a local filesystem are required.
Personal mappings live in `~/.agent-bridge/state/codex-routes.json`, outside Git.
Use `desktop_bridge.py --home <mailbox> register ...` only for an explicitly selected
alternate mailbox or isolated tests. Follow normal host permissions for mailbox
writes; do not change permissions to bypass a denial.
