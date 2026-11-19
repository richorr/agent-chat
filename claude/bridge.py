#!/usr/bin/env python3
"""Claude side of the Claude <-> Codex mailbox bridge.

Mailbox layout (~/.agent-bridge):
  to-codex/   Claude writes, Codex reads
  to-claude/  Codex writes, Claude reads
  archive/    processed inbound messages (moved here by the reader)
  tmp/        staging area; files are renamed into an inbox only when complete
  state/      Claude-side bookkeeping (processed ids, pending routes, log)

Commands:
  serve    run the dispatcher loop. Routing is plain Python; delivering a message into
           a Claude session is done by a fresh, single-use Haiku run (no context build-up).
  reply    a project session answers a bridge request:  reply <id> --body-file F
  send     a Claude session asks Codex something:       send --type request --project P --origin <my session> --body-file F
           (add --purpose code-review|pr-fix|design-feedback when sending on your own initiative)
  policy   show / allow / deny / reset which self-initiated purposes each project may use (humans only)
  claim    archive one inbox file by hand (marks it processed, prints it)
  pending  list/get/done routing records
"""
import argparse
import datetime
import json
import os
import re
import select
import subprocess
import sys
import time
import uuid

ROOT = os.path.expanduser(os.environ.get("AGENT_BRIDGE_ROOT", "~/.agent-bridge"))
TO_CODEX = os.path.join(ROOT, "to-codex")
TO_CLAUDE = os.path.join(ROOT, "to-claude")
ARCHIVE = os.path.join(ROOT, "archive")
TMP = os.path.join(ROOT, "tmp")
STATE = os.path.join(ROOT, "state")
PROCESSED = os.path.join(STATE, "claude-processed.txt")
PENDING = os.path.join(STATE, "claude-pending.json")

POLICY = os.path.join(ROOT, "claude-policy.json")  # human-owned config, edited with `policy` in a terminal
USER_REQUESTED = "user-requested"
KNOWN_PURPOSES = ["code-review", "pr-fix", "design-feedback"]
DEFAULT_POLICY = {
    "_doc": "Which requests Claude agents may send to Codex on their own initiative (without the user asking), "
            "and which self-initiated Codex requests each Claude project accepts. Edit with "
            "`python3 ~/.agent-bridge/bin/bridge.py policy ...` in a terminal.",
    "default": {"self_initiated": []},
    "projects": {},
}

ROUTER_MODEL = os.environ.get("AGENT_BRIDGE_MODEL", "claude-haiku-5-5")
ROUTER_CWD = os.path.expanduser(os.environ.get("AGENT_BRIDGE_ROUTER_CWD", os.path.join(ROOT, "router")))
ROUTER_TIMEOUT = 300  # seconds for one Haiku routing run
ROUTER_CLAUDE_MD = """# Bridge router

Single-use routing runs started by `bridge.py serve` work in this folder.
Follow the prompt exactly: route the one message you are given, then stop. Never do project work here.
"""
OUTBOUND_TTL = 24 * 3600  # forget an unanswered Claude -> Codex request after this long

REQUEST_TEMPLATE = """[bridge-request {id}] Request from Codex for project {project}.

{body}

When done, send your answer back by running this command (write the answer to a file first):
  python3 ~/.agent-bridge/bin/bridge.py reply {id} --body-file <file>
For a short answer you can use --body "<answer>" instead. If you cannot do the task, start the answer with "ERROR:".
Do not answer with SendMessage: the router that delivered this has already exited."""

ROUTE_PROMPT = """You are a message router. Do exactly these steps and nothing else.
1. Call ListAgents.
2. Find the peer session for project "{project}". A session name is "<project>-<2 hex chars>": strip only the final "-xx"; the rest must equal "{project}" exactly (so "300-baud" does NOT match "300-baud-web-e8"). If several match, prefer "idle" over busy, then the most recently started.
3. If one matches, call SendMessage once with "to" set to that exact name and "message" set to the text between the markers below, copied exactly.
4. Your final output must be one line and nothing else: "ROUTED <name>", "NO_SESSION", or "FAILED <reason>".

<<<MESSAGE
{message}
MESSAGE>>>"""

DELIVER_PROMPT = """You are a message router. Call SendMessage once with "to" set to "{to}" and "message" set to the text between the markers below, copied exactly. Do nothing else.
Your final output must be one line: "ROUTED {to}" or "FAILED <reason>".

<<<MESSAGE
{message}
MESSAGE>>>"""


# ---------------------------------------------------------------- helpers

def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def log(text):
    print("%s %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), text), flush=True)


def atomic_write_json(path, data):
    tmp = os.path.join(TMP, ".%s.%s.tmp" % (os.path.basename(path), uuid.uuid4().hex[:6]))
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, path)


def load_processed():
    try:
        with open(PROCESSED) as f:
            return set(line.strip() for line in f if line.strip())
    except IOError:
        return set()


def mark_processed(msg_id):
    with open(PROCESSED, "a") as f:
        f.write(msg_id + "\n")


def load_pending():
    try:
        with open(PENDING) as f:
            return json.load(f)
    except (IOError, ValueError):
        return {}


def save_pending(data):
    atomic_write_json(PENDING, data)


def inbox_files():
    try:
        names = os.listdir(TO_CLAUDE)
    except OSError:
        return []
    return sorted(n for n in names if n.endswith(".json") and not n.startswith("."))


def read_body(args):
    if args.body is not None:
        return args.body
    if args.body_file:
        with open(args.body_file) as f:
            return f.read()
    return sys.stdin.read()


def claim(name):
    """Archive an inbox file and record its id. Returns (msg, duplicate)."""
    path = os.path.join(TO_CLAUDE, os.path.basename(name))
    with open(path) as f:
        msg = json.load(f)
    msg_id = msg.get("id")
    if not msg_id:
        raise ValueError("message has no id")
    duplicate = msg_id in load_processed()
    if not duplicate:
        mark_processed(msg_id)  # record before dispatch: at-most-once
    os.rename(path, os.path.join(ARCHIVE, os.path.basename(name)))
    return msg, duplicate


def load_policy():
    try:
        with open(POLICY) as f:
            return json.load(f)
    except (IOError, ValueError):
        return json.loads(json.dumps(DEFAULT_POLICY))


def allowed_purposes(project):
    pol = load_policy()
    rule = pol.get("projects", {}).get(project) or pol.get("default", {})
    return list(rule.get("self_initiated", []))


def project_of_session(name):
    """'300-baud-web-e8' -> '300-baud-web' (session names are <project folder>-<2 hex>)."""
    m = re.match(r"^(.*)-[0-9a-f]{2}$", name or "")
    return m.group(1) if m else name


def write_message(msg_type, project, body, reply_to=None, purpose=None):
    msg_id = "claude-%s-%s" % (datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:8])
    msg = {"id": msg_id, "reply_to": reply_to, "from": "claude", "to": "codex",
           "project": project, "type": msg_type, "body": body, "created_at": now_iso()}
    if purpose:
        msg["purpose"] = purpose
    atomic_write_json(os.path.join(TO_CODEX, msg_id + ".json"), msg)
    return msg_id


def run_router(prompt):
    """One fresh, single-use Haiku run with only ListAgents/SendMessage. Returns (result_line, cost)."""
    cmd = ["claude", "-p", "--model", ROUTER_MODEL, "--output-format", "json",
           "--allowedTools", "ListAgents", "SendMessage", "--", prompt]
    try:
        p = subprocess.run(cmd, cwd=ROUTER_CWD, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=ROUTER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "FAILED router timed out", None
    try:
        data = json.loads(p.stdout)
    except ValueError:
        return "FAILED router output unreadable: %s" % (p.stdout or p.stderr)[-300:].strip(), None
    result = (data.get("result") or "").strip()
    lines = [l.strip() for l in result.splitlines() if l.strip()]
    return (lines[-1] if lines else "FAILED empty router output"), data.get("total_cost_usd")


def fmt_cost(cost):
    return "" if cost is None else " ($%.4f)" % cost


# ---------------------------------------------------------------- serve

def handle_request(msg):
    project = msg.get("project") or ""
    purpose = msg.get("purpose") or USER_REQUESTED
    if purpose != USER_REQUESTED and purpose not in allowed_purposes(project):
        reason = "Claude project %s does not accept self-initiated '%s' requests (allowed: %s)" % (
            project, purpose, ", ".join(allowed_purposes(project)) or "none")
        write_message("response", project, "ERROR: " + reason, reply_to=msg["id"])
        log("request %s rejected by policy: %s" % (msg["id"], reason))
        return
    text = REQUEST_TEMPLATE.format(id=msg["id"], project=project, body=msg.get("body", ""))
    result, cost = run_router(ROUTE_PROMPT.format(project=project, message=text))
    m = re.match(r"ROUTED\s+(\S+)", result)
    if m:
        pending = load_pending()
        pending[msg["id"]] = {"direction": "inbound", "project": project, "session": m.group(1),
                              "created_at": now_iso(), "created_ts": time.time()}
        save_pending(pending)
        log("request %s -> %s%s" % (msg["id"], m.group(1), fmt_cost(cost)))
    else:
        reason = ("no running Claude session for %s" % project) if result.startswith("NO_SESSION") \
            else "could not deliver: %s" % result
        write_message("response", project, "ERROR: " + reason, reply_to=msg["id"])
        log("request %s rejected: %s%s" % (msg["id"], reason, fmt_cost(cost)))


def handle_response(msg):
    pending = load_pending()
    rec = pending.pop(msg.get("reply_to"), None)
    if rec is None or not rec.get("origin_session"):
        log("response %s answers unknown request %s; archived, not delivered" % (msg["id"], msg.get("reply_to")))
        return
    save_pending(pending)
    text = "[codex-reply %s] %s" % (msg["reply_to"], msg.get("body", ""))
    result, cost = run_router(DELIVER_PROMPT.format(to=rec["origin_session"], message=text))
    log("response %s -> %s: %s%s" % (msg["id"], rec["origin_session"], result, fmt_cost(cost)))


def sweep_timeouts(limit):
    pending = load_pending()
    now = time.time()
    changed = False
    for k in list(pending):
        r = pending[k]
        age = now - float(r.get("created_ts", now))
        if r.get("direction") == "inbound" and limit > 0 and age > limit:
            pending.pop(k)
            write_message("response", r.get("project"),
                          "ERROR: %s did not answer within %d minutes" % (r.get("session"), limit // 60), reply_to=k)
            log("request %s timed out waiting for %s" % (k, r.get("session")))
            changed = True
        elif r.get("direction") == "outbound" and age > OUTBOUND_TTL:
            pending.pop(k)
            log("outbound request %s from %s got no answer from Codex within %d hours; forgotten"
                % (k, r.get("origin_session"), OUTBOUND_TTL // 3600))
            changed = True
    if changed:
        save_pending(pending)


class InboxWaiter(object):
    """Sleep until something changes in to-claude/ (kqueue on macOS), or a timeout passes.

    The watch is registered once, before the first scan. kqueue keeps any change that
    happens during a scan, so the next wait returns at once and nothing is missed.
    Falls back to fixed-interval polling where kqueue is unavailable.
    """

    def __init__(self, path, poll_interval):
        self.poll_interval = poll_interval
        self.kq = None
        if hasattr(select, "kqueue"):
            try:
                self.fd = os.open(path, getattr(os, "O_EVTONLY", 0x8000) if sys.platform == "darwin" else os.O_RDONLY)
                self.kq = select.kqueue()
                self.kq.control([select.kevent(
                    self.fd, filter=select.KQ_FILTER_VNODE,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME)], 0, 0)
            except OSError as e:
                log("kqueue unavailable (%s); polling every %ss instead" % (e, poll_interval))
                self.kq = None
        self.mode = "kqueue" if self.kq else "polling every %ss" % poll_interval

    def wait(self, timeout):
        if self.kq is None:
            time.sleep(min(timeout, self.poll_interval))
            return
        events = self.kq.control(None, 8, timeout)
        for ev in events:
            if ev.fflags & (select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME):
                sys.exit("inbox folder %s was deleted or moved; restart serve" % TO_CLAUDE)


def ensure_router_dir():
    if not os.path.isdir(ROUTER_CWD):
        os.makedirs(ROUTER_CWD)
    md = os.path.join(ROUTER_CWD, "CLAUDE.md")
    if not os.path.exists(md):
        with open(md, "w") as f:
            f.write(ROUTER_CLAUDE_MD)


def cmd_serve(args):
    ensure_router_dir()
    waiter = InboxWaiter(TO_CLAUDE, args.interval)
    log("dispatcher listening on %s (%s, router model %s)" % (TO_CLAUDE, waiter.mode, ROUTER_MODEL))
    last_sweep = 0
    while True:
        for name in inbox_files():
            try:
                msg, dup = claim(name)
            except (IOError, OSError, ValueError) as e:
                bad = os.path.join(ARCHIVE, name + ".bad")
                try:
                    os.rename(os.path.join(TO_CLAUDE, name), bad)
                except OSError:
                    pass
                log("unreadable message %s (%s); moved to %s" % (name, e, bad))
                continue
            if dup:
                log("duplicate %s ignored" % msg["id"])
                continue
            try:
                if msg.get("type") == "request":
                    handle_request(msg)
                elif msg.get("type") == "response":
                    handle_response(msg)
                else:
                    log("message %s has unknown type %r; archived" % (msg["id"], msg.get("type")))
            except Exception as e:  # keep serving no matter what one message does
                log("error handling %s: %r" % (msg.get("id"), e))
        if time.time() - last_sweep > 60:
            sweep_timeouts(args.reply_timeout)
            last_sweep = time.time()
        waiter.wait(60)  # wakes immediately on a new file; at least once a minute for timeouts


# ---------------------------------------------------------------- other commands

def cmd_reply(args):
    pending = load_pending()
    rec = pending.get(args.id)
    if rec is None or rec.get("direction") != "inbound":
        sys.exit("no open bridge request %s (already answered, timed out, or wrong id)" % args.id)
    rid = write_message("response", rec.get("project"), read_body(args), reply_to=args.id)
    pending.pop(args.id, None)
    save_pending(pending)
    print("Sent to Codex as %s." % rid)


def cmd_send(args):
    if args.type == "response" and not args.reply_to:
        sys.exit("a response needs --reply-to")
    purpose = args.purpose
    if args.type == "request" and purpose != USER_REQUESTED:
        if not args.origin:
            sys.exit("a self-initiated request needs --origin <your session name>")
        own = project_of_session(args.origin)
        allowed = allowed_purposes(own)
        if purpose not in allowed:
            sys.exit("not allowed: project %s may not send self-initiated '%s' requests (allowed: %s). "
                     "Ask the user first, then send with --purpose user-requested."
                     % (own, purpose, ", ".join(allowed) or "none"))
    msg_id = write_message(args.type, args.project, read_body(args), reply_to=args.reply_to,
                           purpose=purpose if args.type == "request" else None)
    if args.type == "request" and args.origin:
        pending = load_pending()
        pending[msg_id] = {"direction": "outbound", "origin_session": args.origin,
                           "project": args.project, "created_at": now_iso(), "created_ts": time.time()}
        save_pending(pending)
    print(msg_id)


def cmd_policy(args):
    pol = load_policy()
    if args.action == "show":
        projects = pol.get("projects", {})
        print("default: %s" % (", ".join(pol.get("default", {}).get("self_initiated", [])) or "none"))
        for name in sorted(projects):
            print("%s: %s" % (name, ", ".join(projects[name].get("self_initiated", [])) or "none"))
        print("\nknown purposes: %s" % ", ".join(KNOWN_PURPOSES))
        return
    # Changing the policy is for the human who runs this bridge, not for agents.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit("policy changes must be made by a person in an interactive terminal")
    target = args.project
    if not target:
        sys.exit("policy %s needs a project name (or 'default')" % args.action)
    if target == "default":
        rule = pol.setdefault("default", {"self_initiated": []})
    elif args.action == "reset":
        pol.setdefault("projects", {}).pop(target, None)
        atomic_write_json(POLICY, pol)
        print("%s now follows the default" % target)
        return
    else:
        projects = pol.setdefault("projects", {})
        if target not in projects:  # start from the default, then adjust
            projects[target] = {"self_initiated": list(pol.get("default", {}).get("self_initiated", []))}
        rule = projects[target]
    current = rule.setdefault("self_initiated", [])
    unknown = [p for p in args.purposes if p not in KNOWN_PURPOSES]
    if unknown:
        sys.exit("unknown purpose(s): %s (known: %s)" % (", ".join(unknown), ", ".join(KNOWN_PURPOSES)))
    if args.action == "allow":
        current.extend(p for p in args.purposes if p not in current)
    elif args.action == "deny":
        rule["self_initiated"] = [p for p in current if p not in args.purposes]
    atomic_write_json(POLICY, pol)
    print("%s: %s" % (target, ", ".join(rule["self_initiated"]) or "none"))


def cmd_claim(args):
    msg, dup = claim(args.file)
    print(json.dumps({"duplicate": True, "id": msg["id"]}) if dup else json.dumps(msg, indent=2))


def cmd_pending(args):
    pending = load_pending()
    if args.action == "list":
        print(json.dumps(pending, indent=2))
    elif not args.id:
        sys.exit("pending %s needs an id" % args.action)
    elif args.action == "get":
        if args.id not in pending:
            sys.exit("no pending record for %s" % args.id)
        print(json.dumps(pending[args.id]))
    elif args.action == "done":
        pending.pop(args.id, None)
        save_pending(pending)
        print("ok")


def main():
    for d in (TO_CODEX, TO_CLAUDE, ARCHIVE, TMP, STATE):
        if not os.path.isdir(d):
            os.makedirs(d)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    sv = sub.add_parser("serve")
    sv.add_argument("--interval", type=float, default=1.0, help="poll interval if kqueue is unavailable")
    sv.add_argument("--reply-timeout", type=int, default=7200,
                    help="seconds before an unanswered request gets an ERROR response (0 = never)")

    r = sub.add_parser("reply")
    r.add_argument("id")
    r.add_argument("--body")
    r.add_argument("--body-file")

    s = sub.add_parser("send")
    s.add_argument("--type", choices=["request", "response"], required=True)
    s.add_argument("--project", required=True)
    s.add_argument("--reply-to")
    s.add_argument("--origin", help="your Claude session name, so Codex's answer can be delivered to you")
    s.add_argument("--body")
    s.add_argument("--body-file")
    s.add_argument("--purpose", default=USER_REQUESTED,
                   help="'user-requested' (default) when the user asked for this; otherwise one of %s, "
                        "which must be allowed for your project (see: policy show)" % ", ".join(KNOWN_PURPOSES))

    po = sub.add_parser("policy", help="show/allow/deny/reset self-initiated request purposes per project")
    po.add_argument("action", choices=["show", "allow", "deny", "reset"])
    po.add_argument("project", nargs="?", help="project folder name, or 'default'")
    po.add_argument("purposes", nargs="*")

    c = sub.add_parser("claim")
    c.add_argument("file")

    pe = sub.add_parser("pending")
    pe.add_argument("action", choices=["get", "done", "list"])
    pe.add_argument("id", nargs="?")

    args = p.parse_args()
    handlers = {"serve": cmd_serve, "reply": cmd_reply, "send": cmd_send,
                "claim": cmd_claim, "pending": cmd_pending, "policy": cmd_policy}
    if args.cmd not in handlers:
        p.print_help()
        sys.exit(1)
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
