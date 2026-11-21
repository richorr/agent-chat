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
  register / unregister / chats
           named Claude chats: a session claims a name and so opts into incoming dispatch
  names / connect / connection / disconnect
           a session's saved instructions for when to ask a named Codex chat something
  claim    archive one inbox file by hand (marks it processed, prints it)
  pending  list/get/done routing records
"""
import argparse
import contextlib
import datetime
import fcntl
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
CHATS = os.path.join(STATE, "claude-chats.json")              # chat name -> registered Claude session
CONNECTIONS = os.path.join(STATE, "claude-connections.json")  # session -> Codex name -> saved instructions
CODEX_ROUTES = os.path.join(STATE, "codex-routes.json")       # Codex's names (read only here)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

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

REQUEST_TEMPLATE = """[bridge-request {id}] Request from Codex for chat {project}.

{body}

When done, send your answer back by running this command (write the answer to a file first):
  python3 ~/.agent-bridge/bin/bridge.py reply {id} --body-file <file>
For a short answer you can use --body "<answer>" instead. If you cannot do the task, start the answer with "ERROR:".
Do not answer with SendMessage: the router that delivered this has already exited."""

ROUTE_PROMPT = """You are a message router. Do exactly these steps and nothing else.
1. Call ListAgents.
2. Look for a peer session whose name is exactly "{session}".{ref_rule} Never pick a session with any other name.
3. If there is none, do nothing else; your final output is "OFFLINE".
4. Otherwise call SendMessage once with "to" set to {to} and "message" set to the text between the markers below, copied exactly.
5. Your final output must be one line and nothing else: "ROUTED {session}", "OFFLINE", or "FAILED <reason>".

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
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:  # private
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


def load_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (IOError, ValueError):
        return {}


def load_pending():
    return load_json(PENDING)


def save_pending(data):
    atomic_write_json(PENDING, data)


@contextlib.contextmanager
def json_txn(path):
    """Read-modify-write a state file under an exclusive lock (<file>.lock) shared by every bridge
    process, so concurrent writers cannot overwrite each other's changes. Yields the data; saves it
    on exit if it changed. Never hold one across a router run."""
    with open(os.path.splitext(path)[0] + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = load_json(path)
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                atomic_write_json(path, data)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def pending_txn():
    return json_txn(PENDING)


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


def new_message_id():
    return "claude-%s-%s" % (datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S"), uuid.uuid4().hex[:8])


def resolve_chat(target):
    """Which registered Claude chat should receive a request addressed to `target`?

    Codex puts the destination in the message's `project` field. A registered chat name matches
    first; otherwise `target` is treated as a project folder name (what Codex has always sent) and
    matches the one chat registered from a session of that project. Unregistered sessions never
    receive requests. Returns (chat name, entry) or (None, error text)."""
    chats = load_json(CHATS)
    if target in chats:
        return target, chats[target]
    in_project = sorted(n for n, e in chats.items() if e.get("project") == target)
    if len(in_project) == 1:
        return in_project[0], chats[in_project[0]]
    if in_project:
        return None, ("'%s' matches several registered Claude chats (%s); address one of them by name"
                      % (target, ", ".join(in_project)))
    return None, ("no Claude chat is registered as '%s'. Run /register-chat %s in the Claude session "
                  "that should receive it" % (target, target))


def owner(entry):
    """How a registration's owner is shown: 'name [ref]', or just 'name' for an old entry without a ref."""
    return "%s [%s]" % (entry["session"], entry["ref"]) if entry.get("ref") else entry["session"]


def same_owner(entry, session, ref):
    """A recorded ref pins the registration to that exact session: a later session that reuses the
    name (different or missing ref) is a different owner. An entry without a ref matches by name."""
    return entry["session"] == session and (not entry.get("ref") or entry["ref"] == ref)


def route_prompt(entry, message):
    session, ref = entry["session"], entry.get("ref")
    ref_rule, to = "", '"%s"' % session
    if ref:  # pinned: the row must match name AND ref, even when it is the only row with that name
        ref_rule = (' Its [ref] must also be exactly "%s". A row with that name but any other [ref] is a '
                    'different session: treat it as not listed.' % ref)
        to = '"%s [%s]"' % (session, ref)
    return ROUTE_PROMPT.format(session=session, ref_rule=ref_rule, to=to, message=message)


def write_message(msg_type, project, body, reply_to=None, purpose=None, msg_id=None):
    msg_id = msg_id or new_message_id()
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
    project = msg.get("project") or ""  # the name Codex addressed: a chat name or a project name
    chat, entry = resolve_chat(project)
    if chat is None:
        write_message("response", project, "ERROR: " + entry, reply_to=msg["id"])
        log("request %s rejected: %s" % (msg["id"], entry))
        return
    own_project = entry.get("project") or project_of_session(entry["session"])
    purpose = msg.get("purpose") or USER_REQUESTED
    if purpose != USER_REQUESTED and purpose not in allowed_purposes(own_project):
        reason = "Claude project %s does not accept self-initiated '%s' requests (allowed: %s)" % (
            own_project, purpose, ", ".join(allowed_purposes(own_project)) or "none")
        write_message("response", project, "ERROR: " + reason, reply_to=msg["id"])
        log("request %s rejected by policy: %s" % (msg["id"], reason))
        return
    # Record the request *before* delivery: the session can answer (cmd_reply) while the router
    # run is still finishing, and that answer needs an open record to go through.
    with pending_txn() as pending:
        pending[msg["id"]] = {"direction": "inbound", "project": project, "chat": chat, "session": None,
                              "created_at": now_iso(), "created_ts": time.time()}
    text = REQUEST_TEMPLATE.format(id=msg["id"], project=chat, body=msg.get("body", ""))
    result, cost = run_router(route_prompt(entry, text))
    m = re.match(r"ROUTED\s+(\S+)", result)
    if m:
        with pending_txn() as pending:
            rec = pending.get(msg["id"])
            if rec is not None:  # if it is gone, the session already answered: don't recreate it
                rec["session"] = m.group(1)
        log("request %s -> %s%s%s" % (msg["id"], m.group(1), fmt_cost(cost),
                                      "" if rec is not None else " (already answered)"))
    else:
        if result.startswith("OFFLINE"):
            reason = ("Claude chat '%s' is registered to session %s, which is offline. The request was not "
                      "sent to any other session" % (chat, owner(entry)))
        else:
            reason = "could not deliver to Claude chat '%s' (session %s): %s" % (chat, owner(entry), result)
        with pending_txn() as pending:
            rec = pending.pop(msg["id"], None)
            if rec is not None:  # a session that got it anyway and answered wins over the error
                write_message("response", project, "ERROR: " + reason, reply_to=msg["id"])
        log("request %s rejected: %s%s%s" % (msg["id"], reason, fmt_cost(cost),
                                             "" if rec is not None else " (but already answered; no error sent)"))


def handle_response(msg):
    with pending_txn() as pending:
        rec = pending.get(msg.get("reply_to"))
        if rec is not None and rec.get("origin_session"):
            pending.pop(msg["reply_to"])
    if rec is None or not rec.get("origin_session"):
        log("response %s answers unknown request %s; archived, not delivered" % (msg["id"], msg.get("reply_to")))
        return
    text = "[codex-reply %s] %s" % (msg["reply_to"], msg.get("body", ""))
    result, cost = run_router(DELIVER_PROMPT.format(to=rec["origin_session"], message=text))
    log("response %s -> %s: %s%s" % (msg["id"], rec["origin_session"], result, fmt_cost(cost)))


def sweep_timeouts(limit):
    now = time.time()
    with pending_txn() as pending:
        for k in list(pending):
            r = pending[k]
            age = now - float(r.get("created_ts", now))
            if r.get("direction") == "inbound" and limit > 0 and age > limit:
                pending.pop(k)
                who = r.get("session") or "the Claude session"
                write_message("response", r.get("project"),
                              "ERROR: %s did not answer within %d minutes" % (who, limit // 60), reply_to=k)
                log("request %s timed out waiting for %s" % (k, who))
            elif r.get("direction") == "outbound" and age > OUTBOUND_TTL:
                pending.pop(k)
                log("outbound request %s from %s got no answer from Codex within %d hours; forgotten"
                    % (k, r.get("origin_session"), OUTBOUND_TTL // 3600))


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
    body = read_body(args)
    with pending_txn() as pending:  # check, send and close under one lock: exactly one answer per request
        rec = pending.get(args.id)
        if rec is None or rec.get("direction") != "inbound":
            sys.exit("no open bridge request %s (already answered, timed out, or wrong id)" % args.id)
        rid = write_message("response", rec.get("project"), body, reply_to=args.id)
        pending.pop(args.id)
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
    if args.type == "request" and args.origin:
        conn = load_json(CONNECTIONS).get(args.origin, {}).get(args.project)
        if conn is not None:
            status = connection_status(conn, args.project, load_json(CODEX_ROUTES))
            if status != "ok":
                sys.exit("not sent: your saved connection to '%s' is %s. Ask the user to reconnect "
                         "(/chat-with %s) before using it." % (args.project, status, args.project))
    body = read_body(args)
    msg_id = new_message_id()
    if args.type == "request" and args.origin:
        # Record first, so a fast answer from Codex always finds the record.
        with pending_txn() as pending:
            pending[msg_id] = {"direction": "outbound", "origin_session": args.origin,
                               "project": args.project, "created_at": now_iso(), "created_ts": time.time()}
    write_message(args.type, args.project, body, reply_to=args.reply_to,
                  purpose=purpose if args.type == "request" else None, msg_id=msg_id)
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


# ---------------------------------------------------------------- named chats (receiving side)

def check_name(name, what):
    if not NAME_RE.match(name or ""):
        sys.exit("invalid %s %r: use letters, digits, '.', '_' or '-' (max 64 characters)" % (what, name))


def cmd_register(args):
    check_name(args.chat, "chat name")
    if not args.session:
        sys.exit("--session <this session's name> is required (first line of ListAgents)")
    with json_txn(CHATS) as chats:
        cur = chats.get(args.chat)
        mine = cur is not None and same_owner(cur, args.session, args.ref)
        if cur and not mine and args.replace != owner(cur):
            sys.exit("CLAIMED: '%s' is registered to session %s (since %s). Nothing changed. To move it here, "
                     "confirm with the user, then rerun with --replace '%s'"
                     % (args.chat, owner(cur), cur.get("registered_at", "?"), owner(cur)))
        new = {"session": args.session, "ref": args.ref or None, "project": project_of_session(args.session),
               "registered_at": cur["registered_at"] if mine else now_iso()}
        chats[args.chat] = new
    if cur and not mine:
        print("MOVED: '%s' now delivers to %s (was %s)." % (args.chat, owner(new), owner(cur)))
    elif cur:
        print("UNCHANGED: '%s' already delivers to %s." % (args.chat, owner(new)))
    else:
        print("REGISTERED: '%s' now delivers to %s." % (args.chat, owner(new)))
    others = sorted(n for n, e in load_json(CHATS).items() if e["session"] == args.session and n != args.chat)
    if others:
        print("This session also receives: %s" % ", ".join(others))


def cmd_unregister(args):
    with json_txn(CHATS) as chats:
        cur = chats.get(args.chat)
        if cur is None:
            sys.exit("'%s' is not registered" % args.chat)
        if not same_owner(cur, args.session, args.ref):
            me = owner({"session": args.session, "ref": args.ref})
            sys.exit("'%s' belongs to session %s, not %s; nothing changed" % (args.chat, owner(cur), me))
        chats.pop(args.chat)
    print("UNREGISTERED: '%s'. Codex requests for it now get an ERROR." % args.chat)


def cmd_chats(args):
    chats = load_json(CHATS)
    if args.json:
        print(json.dumps(chats, indent=2, sort_keys=True))
        return
    if not chats:
        print("no Claude chats registered")
    for n in sorted(chats):
        e = chats[n]
        print("%s -> %s%s (project %s, since %s)" % (n, e["session"], " [%s]" % e["ref"] if e.get("ref") else "",
                                                     e.get("project"), e.get("registered_at")))


# ---------------------------------------------------------------- connections (sending side)

def connection_status(conn, name, routes):
    if name not in routes:
        return "missing (Codex no longer has a chat named '%s')" % name
    return "ok" if routes[name] == conn.get("destination") else "reassigned (Codex moved '%s' to another chat)" % name


def cmd_names(args):
    routes = load_json(CODEX_ROUTES)
    mine = load_json(CONNECTIONS).get(args.session, {}) if args.session else {}
    print("Codex chats you can address: %s" % (", ".join(sorted(routes)) or "none"))
    if not args.session:
        return
    if not mine:
        print("No saved connections for %s." % args.session)
    for n in sorted(mine):
        first = (mine[n].get("instructions") or "").strip().splitlines() or [""]
        print("connection %s [%s]: %s" % (n, connection_status(mine[n], n, routes), first[0][:100]))


def cmd_connection(args):
    conn = load_json(CONNECTIONS).get(args.session, {}).get(args.name)
    if conn is None:
        print("NONE: no saved connection to '%s' for %s." % (args.name, args.session))
        return
    print("connection %s [%s], saved %s\n\n%s" % (args.name, connection_status(conn, args.name, load_json(CODEX_ROUTES)),
                                                 conn.get("saved_at"), conn.get("instructions", "").strip()))


def cmd_connect(args):
    """Save (or replace) this session's instructions for a Codex chat. Sends nothing, and does not
    touch incoming dispatch."""
    routes = load_json(CODEX_ROUTES)
    if args.name not in routes:
        sys.exit("unknown Codex chat '%s'. Known: %s" % (args.name, ", ".join(sorted(routes)) or "none"))
    given = args.body is not None or args.body_file
    text = read_body(args).strip() if (given or not args.reconnect) else None
    if text == "":
        sys.exit("no instructions given")
    with json_txn(CONNECTIONS) as conns:
        mine = conns.setdefault(args.session, {})
        old = mine.get(args.name)
        if args.reconnect:
            if old is None:
                sys.exit("no saved connection to '%s' to reconnect" % args.name)
            text = text or old["instructions"]
        elif old is not None and old.get("destination") != routes[args.name]:
            # Never let new text quietly re-point a connection at a different Codex chat.
            sys.exit("STALE: Codex moved '%s' to another chat since this connection was saved. Nothing changed. "
                     "Ask the user to confirm the new destination, then rerun with --reconnect "
                     "(add the new text to replace the instructions at the same time)." % args.name)
        mine[args.name] = {"instructions": text, "destination": routes[args.name], "saved_at": now_iso()}
    print("%s: connection %s for %s. Nothing was sent." % (
        "RECONNECTED" if args.reconnect else ("REPLACED" if old else "SAVED"), args.name, args.session))


def cmd_disconnect(args):
    with json_txn(CONNECTIONS) as conns:
        gone = conns.get(args.session, {}).pop(args.name, None)
        if args.session in conns and not conns[args.session]:
            conns.pop(args.session)
    print("REMOVED" if gone else "NONE: no saved connection to '%s'" % args.name)


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
        with pending_txn() as locked:
            locked.pop(args.id, None)
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

    rg = sub.add_parser("register", help="claim a chat name for a Claude session (opts it into incoming dispatch)")
    rg.add_argument("chat")
    rg.add_argument("--session", required=True, help="this session's name, from the first line of ListAgents")
    rg.add_argument("--ref", help="this session's [ref] from ListAgents, to tell apart sessions with the same name")
    rg.add_argument("--replace", metavar="OLD_OWNER",
                    help="move the name from OLD_OWNER, exactly as CLAIMED printed it (only after the user agrees)")

    ur = sub.add_parser("unregister")
    ur.add_argument("chat")
    ur.add_argument("--session", required=True)
    ur.add_argument("--ref", help="this session's [ref] from ListAgents")

    ch = sub.add_parser("chats", help="list registered Claude chats")
    ch.add_argument("--json", action="store_true")

    nm = sub.add_parser("names", help="Codex chat names, plus this session's saved connections")
    nm.add_argument("--session")

    co = sub.add_parser("connect", help="save/replace instructions for a Codex chat (sends nothing)")
    co.add_argument("name")
    co.add_argument("--session", required=True)
    co.add_argument("--body", help="the instructions")
    co.add_argument("--body-file")
    co.add_argument("--reconnect", action="store_true",
                    help="accept Codex's current destination for the name (keeps the instructions unless new text is given)")

    cs = sub.add_parser("connection", help="show this session's saved instructions for a Codex chat")
    cs.add_argument("name")
    cs.add_argument("--session", required=True)

    dc = sub.add_parser("disconnect")
    dc.add_argument("name")
    dc.add_argument("--session", required=True)

    args = p.parse_args()
    handlers = {"serve": cmd_serve, "reply": cmd_reply, "send": cmd_send,
                "claim": cmd_claim, "pending": cmd_pending, "policy": cmd_policy,
                "register": cmd_register, "unregister": cmd_unregister, "chats": cmd_chats,
                "names": cmd_names, "connect": cmd_connect, "connection": cmd_connection,
                "disconnect": cmd_disconnect}
    if args.cmd not in handlers:
        p.print_help()
        sys.exit(1)
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
