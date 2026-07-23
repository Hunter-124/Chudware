#!/usr/bin/env python3
"""notify_discord.py — push Chudware human-attention events to a Discord channel.

Two entry paths share one dedupe ledger (work/sync/notify-state.json) so the same
event is never pinged twice:
  --hook TASK_ID   fired inline from work.py _commit_task_change (low latency,
                   commit-based triggers on this machine; best-effort, never blocks)
  --poll           run every ~5 min by Task Scheduler; the safety net that catches
                   what a commit hook structurally can't see: uncommitted-on-master,
                   aging review (>Nh unmerged), inbox messages, and board commits
                   that arrived from another checkout.

Delivery is Discord REST (bot token in an Authorization header) — no Gateway, no
always-on process. Config lives in gitignored work/sync/:
  .discord-token       one line: the bot token
  notify_config.json   channel_id, user_id, review_aging_hours, poll_interval_minutes

Flood control: the first run ever seeds the ledger silently and posts ONE "online"
summary instead of a ping per pre-existing item; any later cycle that would send more
than BATCH_CAP messages collapses them into a single digest.

Stdlib only. `python work/tools/notify_discord.py --check` verifies credentials;
`--test-ping` posts sample messages so you can approve the format before it's wired in.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows consoles default to cp1252 and choke on non-ASCII; force UTF-8 like work.py.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parent.parent          # work/
REPO = ROOT.parent                                      # repo top (for git cwd)
SYNC = ROOT / "sync"
TOKEN_FILE = SYNC / ".discord-token"
CONFIG_FILE = SYNC / "notify_config.json"
STATE_FILE = SYNC / "notify-state.json"

_TOKEN_PLACEHOLDER = "PASTE_YOUR_DISCORD_BOT_TOKEN_HERE"
_CHANNEL_PLACEHOLDER = "PASTE_DISCORD_CHANNEL_ID_HERE"
_USER_PLACEHOLDER = "PASTE_YOUR_DISCORD_USER_ID_HERE"

API = "https://discord.com/api/v10"
BATCH_CAP = 5            # more new events than this in one cycle -> single digest

# Embed accent colours per event type (Discord decimal ints).
COLORS = {
    "review": 0xE8A317, "blocked": 0xC0504D, "human_owned": 0x8A63D2,
    "question": 0x4A90D9, "uncommitted": 0xE67E22, "aging": 0xB8860B,
    "inbox": 0x1ABC9C, "digest": 0x95A5A6,
}


class ConfigError(RuntimeError):
    """Raised when the token or config file is missing / unfilled / malformed."""


# --------------------------------------------------------------- config / state

def load_token():
    if not TOKEN_FILE.exists():
        raise ConfigError(f"missing token file: {TOKEN_FILE} — create it with your bot token")
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token or token == _TOKEN_PLACEHOLDER:
        raise ConfigError(f"{TOKEN_FILE} still holds the placeholder — paste the real bot token")
    return token


def load_config():
    if not CONFIG_FILE.exists():
        raise ConfigError(f"missing config file: {CONFIG_FILE}")
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{CONFIG_FILE} is not valid JSON: {e}")
    channel = str(cfg.get("channel_id", "")).strip()
    user = str(cfg.get("user_id", "")).strip()
    if not channel or channel == _CHANNEL_PLACEHOLDER:
        raise ConfigError(f"channel_id not filled in {CONFIG_FILE}")
    if not user or user == _USER_PLACEHOLDER:
        raise ConfigError(f"user_id not filled in {CONFIG_FILE}")
    cfg["channel_id"], cfg["user_id"] = channel, user
    cfg.setdefault("review_aging_hours", 4)
    cfg.setdefault("poll_interval_minutes", 5)
    cfg.setdefault("uncommitted_hours", cfg["review_aging_hours"])
    cfg.setdefault("inbox_agent", _identity_agent())
    return cfg


def _identity_agent():
    """Best-effort: whose inbox counts as 'mine' — from work/sync/identity.json."""
    idf = SYNC / "identity.json"
    if idf.exists():
        try:
            prof = json.loads(idf.read_text(encoding="utf-8"))
            return prof.get("agent_name") or prof.get("github_user")
        except Exception:
            pass
    return None


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"seeded": False, "events": {}}


def save_state(state):
    SYNC.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --------------------------------------------------------------------- git glue

def _git(args):
    """Run git at the repo top, capture output; never raises."""
    try:
        return subprocess.run(["git"] + args, cwd=str(REPO),
                              capture_output=True, text=True)
    except Exception:
        class _R:
            returncode, stdout, stderr = 1, "", ""
        return _R()


def _main_worktree():
    """The primary checkout path (where master + the real board live)."""
    r = _git(["rev-parse", "--git-common-dir"])
    if r.returncode != 0:
        return REPO
    common = Path(r.stdout.strip())
    if not common.is_absolute():
        common = (REPO / common).resolve()
    return common.parent


def _roadblocks(main):
    """work.py roadblocks --json, read from the MAIN checkout's board."""
    script = main / "work" / "tools" / "work.py"
    try:
        r = subprocess.run([sys.executable, str(script), "roadblocks", "--json"],
                           cwd=str(main), capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return {"review_queue": [], "blocked": [], "human_owned": [], "questions": []}


def _dirty_files(main):
    """Tracked, uncommitted files on the main checkout (the 'at-risk on master' case)."""
    r = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                       cwd=str(main), capture_output=True, text=True)
    files = [ln[3:].strip() for ln in r.stdout.splitlines() if ln.strip()]
    br = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                        cwd=str(main), capture_output=True, text=True)
    return files, br.stdout.strip()


def _review_entered_ts(main, tid):
    """Unix time the task last transitioned INTO review, from its board commit."""
    r = subprocess.run(
        ["git", "log", "-1", "--format=%ct", "--all-match",
         f"--grep=status {tid}:", "--grep=-> review"],
        cwd=str(main), capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _inbox(agent):
    """New messages addressed to me (or broadcast), via teamsync --peek (non-consuming)."""
    if not agent:
        return []
    script = ROOT / "tools" / "teamsync.py"
    if not script.exists():
        return []
    try:
        r = subprocess.run([sys.executable, str(script), "inbox", "--agent", agent, "--peek"],
                           cwd=str(REPO), capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    msgs = []
    for ln in r.stdout.splitlines():
        # format: [<ts>] <from> -> <to>: <text>
        if ln.startswith("[") and "] " in ln and ": " in ln:
            ts = ln[1:ln.index("]")]
            rest = ln[ln.index("] ") + 2:]
            frm = rest.split(" -> ", 1)[0]
            text = rest.split(": ", 1)[1] if ": " in rest else rest
            msgs.append({"ts": ts, "from": frm, "text": text})
    return msgs


# ------------------------------------------------------------ event evaluation

def _impact(row):
    n = len(row.get("blocks", []))
    if not n:
        return "nothing waits on this"
    mp = row.get("blocks_min_priority")
    return f"holds up {n} task(s)" + (f", min P{mp}" if mp else "")


def evaluate(cfg):
    """Current active events (excluding the derived 'aging' pass). Each: key + payload."""
    main = _main_worktree()
    rb = _roadblocks(main)
    events = {}

    for row in rb.get("review_queue", []):
        tid = row["id"]
        events[f"rv:{tid}"] = {
            "type": "review", "tid": tid, "title": row.get("title", ""),
            "owner": row.get("owner", "?"), "branch": row.get("branch", ""),
            "impact": _impact(row), "file": _task_path(main, tid),
        }
    for row in rb.get("blocked", []):
        tid = row["id"]
        events[f"bl:{tid}"] = {
            "type": "blocked", "tid": tid, "title": row.get("title", ""),
            "owner": row.get("owner", "?"), "branch": row.get("branch", ""),
            "impact": _impact(row), "file": _task_path(main, tid),
        }
    for row in rb.get("human_owned", []):
        tid = row["id"]
        events[f"ho:{tid}"] = {
            "type": "human_owned", "tid": tid, "title": row.get("title", ""),
            "owner": row.get("owner", "?"), "branch": row.get("branch", ""),
            "impact": _impact(row), "file": _task_path(main, tid),
        }
    for q in rb.get("questions", []):
        events[f"q:{q['file']}"] = {
            "type": "question", "file": q["file"], "open": q.get("open", 0),
        }

    files, branch = _dirty_files(main)
    if files:
        events["uncommitted"] = {"type": "uncommitted", "files": files, "branch": branch}

    for m in _inbox(cfg.get("inbox_agent")):
        events[f"msg:{m['ts']}:{m['from']}"] = {
            "type": "inbox", "from": m["from"], "text": m["text"], "ts": m["ts"],
        }
    return events, main


def _task_path(main, tid):
    for sub in ("tasks", "backlog"):
        for p in (main / "work" / sub).glob(f"{tid}*.md"):
            return f"work/{sub}/{p.name}"
    return f"work/tasks/{tid}.md"


# ---------------------------------------------------------------- message build

def _embed(ev, cfg, test=False):
    t = ev["type"]
    tag = "[TEST] " if test else ""
    if t in ("review", "blocked", "human_owned", "aging"):
        heads = {"review": "Waiting on your **merge** (status: review)",
                 "blocked": "**Blocked** — needs a decision",
                 "human_owned": "Assigned to **you** (main-checkout)",
                 "aging": f"Still in **review**, unmerged for >{cfg['review_aging_hours']}h"}
        fields = [
            {"name": "Owner", "value": ev.get("owner", "?"), "inline": True},
            {"name": "Impact", "value": ev.get("impact", "-"), "inline": True},
            {"name": "Branch", "value": f"`{ev.get('branch','-')}`", "inline": False},
            {"name": "Review", "value": ev.get("file", "-"), "inline": False},
        ]
        return {"title": f"{tag}{ev['tid']} — {ev.get('title','')}",
                "description": heads[t], "color": COLORS[t], "fields": fields}
    if t == "question":
        return {"title": f"{tag}Open question(s) need your answer",
                "description": f"**{ev['open']}** open in `{ev['file']}`",
                "color": COLORS["question"]}
    if t == "uncommitted":
        shown = "\n".join(f"• {f}" for f in ev["files"][:8])
        more = f"\n…+{len(ev['files'])-8} more" if len(ev["files"]) > 8 else ""
        return {"title": f"{tag}Uncommitted work on the main checkout",
                "description": f"{len(ev['files'])} tracked file(s) dirty on "
                               f"`{ev['branch']}` past the {cfg['uncommitted_hours']}h "
                               f"threshold:\n{shown}{more}",
                "color": COLORS["uncommitted"]}
    if t == "inbox":
        return {"title": f"{tag}Message from {ev['from']}",
                "description": ev["text"], "color": COLORS["inbox"]}
    return {"title": f"{tag}{ev.get('title','event')}", "color": COLORS["digest"]}


def _post(token, channel, user, embeds, mention=True):
    """POST one message (<=10 embeds) to the channel; @mention pings the user."""
    payload = {
        "content": f"<@{user}>" if mention else "",
        "embeds": embeds[:10],
        "allowed_mentions": {"users": [user] if mention else []},
    }
    req = urllib.request.Request(
        f"{API}/channels/{channel}/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bot {token}",
                 "Content-Type": "application/json",
                 "User-Agent": "chudware-notify/1.0"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code == 429:                       # rate limited — back off once, retry
            try:
                time.sleep(float(json.loads(body).get("retry_after", 1)) + 0.5)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    return resp.status, ""
            except Exception as e2:
                return getattr(e2, "code", 0), str(e2)
        return e.code, body
    except Exception as e:
        return 0, str(e)


def _send_events(token, cfg, evs):
    """Send events, collapsing >BATCH_CAP into one digest. Returns (sent, err)."""
    ch, user = cfg["channel_id"], cfg["user_id"]
    if len(evs) > BATCH_CAP:
        by = {}
        for e in evs:
            by[e["type"]] = by.get(e["type"], 0) + 1
        lines = "\n".join(f"• {n} × {k}" for k, n in sorted(by.items()))
        digest = {"title": f"{len(evs)} new items need your attention",
                  "description": lines, "color": COLORS["digest"]}
        return _post(token, ch, user, [digest])
    status, err = 200, ""
    for e in evs:
        status, err = _post(token, ch, user, [_embed(e, cfg)])
        if status not in (200, 204):
            break
    return status, err


# ------------------------------------------------------------------- reconcile

def _reconcile(cfg, token, state, active, main, notify=True):
    """Diff active events vs the ledger; ping the new ones; age & prune. Returns count."""
    now = int(time.time())
    hrs = cfg["review_aging_hours"]
    pre = dict(state["events"])            # snapshot before we mutate
    to_send = []

    # 1) genuinely new events (not yet in the ledger)
    for key, ev in active.items():
        if key == "uncommitted":
            continue                        # time-thresholded — handled below
        if key not in state["events"]:
            state["events"][key] = {"type": ev["type"], "ts": now}
            to_send.append(ev)

    # 2) uncommitted: only fires once it has sat past the threshold
    if "uncommitted" in active:
        rec = state["events"].setdefault("uncommitted", {"type": "uncommitted",
                                                         "ts": now, "notified": False})
        if now - rec["ts"] >= cfg["uncommitted_hours"] * 3600 and not rec.get("notified"):
            rec["notified"] = True
            to_send.append(active["uncommitted"])

    # 3) aging: reviews already known last cycle that have now crossed the threshold
    for key, ev in active.items():
        if not key.startswith("rv:") or key not in pre:
            continue                        # brand-new reviews aren't "aging" yet
        tid = ev["tid"]
        akey = f"age:{tid}"
        if akey in state["events"]:
            continue
        ts = _review_entered_ts(main, tid)
        if ts and now - ts >= hrs * 3600:
            state["events"][akey] = {"type": "aging", "ts": now}
            to_send.append({**ev, "type": "aging"})

    # 4) prune events that are no longer active (re-arms future re-entry / re-dirty)
    active_keys = set(active)
    for key in list(state["events"]):
        if key in active_keys:
            continue
        if key.startswith("age:") and f"rv:{key[4:]}" in active_keys:
            continue                        # keep aging flag while still in review
        del state["events"][key]

    sent = 0
    if notify and to_send:
        status, err = _send_events(token, cfg, to_send)
        if status in (200, 204):
            sent = len(to_send)
        else:
            print(f"  discord POST failed: {status} {err[:200]}", file=sys.stderr)
    return sent, to_send


# ----------------------------------------------------------------- entry points

def cmd_check(_args):
    ok = True
    try:
        token = load_token()
        print(f"  token:   present ({len(token)} chars) in {TOKEN_FILE.name}")
    except ConfigError as e:
        ok = False
        print(f"  token:   MISSING — {e}")
    try:
        cfg = load_config()
        print(f"  channel: {cfg['channel_id']}")
        print(f"  user:    {cfg['user_id']}  (you'll be @mentioned)")
        print(f"  aging:   review escalates after {cfg['review_aging_hours']}h")
        print(f"  poll:    every {cfg['poll_interval_minutes']} min")
        print(f"  inbox:   watching messages for {cfg.get('inbox_agent') or '(none set)'}")
    except ConfigError as e:
        ok = False
        print(f"  config:  ERROR — {e}")
    print("\nOK — script can read all credentials." if ok
          else "\nNOT READY — fill in the values above, then re-run --check.")
    return 0 if ok else 1


def cmd_poll(_args):
    token = load_token()
    cfg = load_config()
    state = load_state()
    active, main = evaluate(cfg)
    if not state.get("seeded"):
        # First run ever: record everything as already-known (no per-item flood),
        # pre-arm aging for reviews already stale, and post ONE online summary.
        now = int(time.time())
        for key, ev in active.items():
            state["events"][key] = {"type": ev["type"], "ts": now}
            if key.startswith("rv:"):
                ts = _review_entered_ts(main, ev["tid"])
                if ts and now - ts >= cfg["review_aging_hours"] * 3600:
                    state["events"][f"age:{ev['tid']}"] = {"type": "aging", "ts": now}
        state["seeded"] = True
        save_state(state)
        counts = {}
        for ev in active.values():
            counts[ev["type"]] = counts.get(ev["type"], 0) + 1
        summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "nothing pending"
        digest = {"title": "Discord notifier online",
                  "description": f"Now watching the board. Current human-attention "
                                 f"queue: {summary}. You'll be pinged on new changes.",
                  "color": COLORS["digest"]}
        status, err = _post(token, cfg["channel_id"], cfg["user_id"], [digest])
        print(f"seeded {len(active)} existing events; online summary POST -> {status} {err[:120]}")
        return 0
    sent, _ = _reconcile(cfg, token, state, active, main)
    save_state(state)
    print(f"poll: {len(active)} active, {sent} new ping(s) sent")
    return 0


def cmd_hook(args):
    """One-task low-latency path from work.py. Best-effort; silent if not seeded yet.

    Only pings THIS task's new review/blocked/human_owned event and records it in the
    shared ledger so the next --poll won't re-send. Pruning/aging stay with --poll.
    """
    try:
        token = load_token()
        cfg = load_config()
    except ConfigError:
        return 0
    state = load_state()
    if not state.get("seeded"):
        return 0                            # let --poll do the first-run seeding
    active, _main = evaluate(cfg)
    tid = args.task_id
    now = int(time.time())
    to_send = []
    for key in (f"rv:{tid}", f"bl:{tid}", f"ho:{tid}"):
        if key in active and key not in state["events"]:
            state["events"][key] = {"type": active[key]["type"], "ts": now}
            to_send.append(active[key])
    if to_send:
        status, err = _send_events(token, cfg, to_send)
        if status not in (200, 204):
            print(f"  hook POST failed: {status} {err[:160]}", file=sys.stderr)
            return 0                        # don't record as sent if it failed
        save_state(state)
    return 0


def cmd_test_ping(_args):
    """Post one sample of each core format so the human can approve before it's live."""
    token = load_token()
    cfg = load_config()
    main = _main_worktree()
    rb = _roadblocks(main)
    samples = []
    rq = rb.get("review_queue", [])
    if rq:
        r = rq[0]
        samples.append({"type": "review", "tid": r["id"], "title": r.get("title", ""),
                        "owner": r.get("owner", "?"), "branch": r.get("branch", ""),
                        "impact": _impact(r), "file": _task_path(main, r["id"])})
    samples.append({"type": "blocked", "tid": "T-XXX", "title": "example blocked task",
                    "owner": "opus-wynn", "branch": "task/T-XXX-example",
                    "impact": "holds up 2 task(s), min P2", "file": "work/tasks/T-XXX.md"})
    files, branch = _dirty_files(main)
    samples.append({"type": "uncommitted",
                    "files": files or ["tools/translator/groundedness.py"],
                    "branch": branch or "master"})
    embeds = [_embed(s, cfg, test=True) for s in samples]
    status, err = _post(token, cfg["channel_id"], cfg["user_id"], embeds)
    print(f"test ping -> HTTP {status} {err[:200]}")
    print(f"sent {len(embeds)} sample embed(s): review, blocked, uncommitted")
    return 0 if status in (200, 204) else 1


# ---------------------------------------------------- stats / leaderboard / digest
# Consumed by the Gateway bot (work/tools/discord_bot.py) slash commands
# /stats /update /leaderboard /screenshot. All git-author-derived (no stored
# scoreboard); `identity_aliases` (work/sync/notify_config.json) folds a
# contributor's commit names/emails to one canonical handle. Additive only —
# none of the notify/poll/hook logic above depends on any of this. See T-BL53.

def _canon(name, email, aliases):
    """Fold a commit author (name or email) to a canonical handle via identity_aliases."""
    name = (name or "").strip()
    email = (email or "").strip()
    return aliases.get(name) or aliases.get(email) or name or email or "unknown"


def _commit_counts(main, since_args, aliases):
    """Per-canonical-author commit counts across ALL refs (so unmerged-branch work
    counts immediately). `since_args` is [] for all-time or ['--since=...'] for a window."""
    r = subprocess.run(
        ["git", "log", "--all", "--no-merges", *since_args, "--pretty=format:%an%x00%ae"],
        cwd=str(main), capture_output=True, text=True)
    counts = {}
    for line in r.stdout.splitlines():
        if "\x00" not in line:
            continue
        name, email = line.split("\x00", 1)
        who = _canon(name, email, aliases)
        counts[who] = counts.get(who, 0) + 1
    return counts


def _board_rows(main):
    """The full task board as a list of dicts, via `work.py board --json`."""
    script = main / "work" / "tools" / "work.py"
    try:
        r = subprocess.run([sys.executable, str(script), "board", "--json"],
                           cwd=str(main), capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return json.loads(r.stdout)
    except Exception:
        pass
    return []


def _active_owner_counts(main):
    """Tasks currently held (status doing/review) per owner."""
    counts = {}
    for row in _board_rows(main):
        if row.get("status") in ("doing", "review"):
            o = (row.get("owner") or "").strip()
            if o and o != "unassigned":
                counts[o] = counts.get(o, 0) + 1
    return counts


def _ranked(counts, top=None):
    """(handle, n) pairs, most commits first, ties broken alphabetically."""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return ranked[:top] if top else ranked


def _recent_screenshots(main, n=3):
    """The n most recent git-tracked images under work/questions/ (newest mtime first).
    Shared helper for /screenshot and _digest_embed."""
    qdir = main / "work" / "questions"
    if not qdir.exists():
        return []
    r = subprocess.run(["git", "ls-files", "work/questions"],
                       cwd=str(main), capture_output=True, text=True)
    exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    paths = []
    for rel in r.stdout.splitlines():
        rel = rel.strip()
        if rel.lower().endswith(exts):
            p = main / rel
            if p.exists():
                paths.append(p)
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return paths[:n]


def _compute_stats(main, days, aliases=None):
    """(commits, owners): per-author commit counts over `days` + active task-ownership counts.
    Matches discord_bot.py's `commits, owners = nd._compute_stats(main, d, aliases)`."""
    aliases = aliases or {}
    since = [f"--since={days} days ago"] if days else []
    return _commit_counts(main, since, aliases), _active_owner_counts(main)


def _stats_embed(commits, owners, days):
    """Discord embed dict: top committers + active task owners over the last `days`."""
    total = sum(commits.values())
    top = _ranked(commits, 10)
    commit_lines = "\n".join(f"`{n:>3}`  {who}" for who, n in top) or "_no commits in window_"
    own = _ranked(owners, 10)
    owner_lines = "\n".join(f"`{n:>2}`  {who}" for who, n in own) or "_none active_"
    return {
        "title": f"Chudware stats — last {days} day(s)",
        "description": f"**{total}** commit(s) from **{len(commits)}** contributor(s).",
        "color": COLORS["digest"],
        "fields": [
            {"name": "Top committers", "value": commit_lines[:1024], "inline": True},
            {"name": "Active task owners", "value": owner_lines[:1024], "inline": True},
        ],
    }


def _leaderboard_embed(main, cfg):
    """Discord embed dict: all-time commit leaderboard (aliases folded, medals for top 3)."""
    aliases = (cfg or {}).get("identity_aliases", {}) or {}
    counts = _commit_counts(main, [], aliases)         # [] => all-time
    ranked = _ranked(counts, 15)
    medals = ["\U0001F947", "\U0001F948", "\U0001F949"]   # gold/silver/bronze
    lines = []
    for i, (who, n) in enumerate(ranked):
        badge = medals[i] if i < 3 else f"`#{i+1:>2}`"
        lines.append(f"{badge}  **{who}** — {n}")
    body = "\n".join(lines) if lines else "_no commits yet_"
    return {
        "title": "\U0001F3C6 Chudware all-time commit leaderboard",
        "description": body[:4096],
        "color": 0xF1C40F,                              # gold
        "footer": {"text": f"{sum(counts.values())} commits · {len(counts)} contributors"},
    }


def _digest_embed(main, cfg, period_days, label):
    """(embed_dict, shots): commits + board progress over the period, plus recent screenshots.
    Matches discord_bot.py's `embed_dict, shots = nd._digest_embed(main, cfg, days, label)`."""
    aliases = (cfg or {}).get("identity_aliases", {}) or {}
    since = [f"--since={period_days} days ago"] if period_days else []
    commits = _commit_counts(main, since, aliases)
    total_commits = sum(commits.values())
    commit_lines = "\n".join(f"`{n:>3}`  {who}" for who, n in _ranked(commits, 5)) or "_none_"

    rows = _board_rows(main)
    done = sum(1 for r in rows if r.get("status") == "done")
    active = sum(1 for r in rows if r.get("status") in ("doing", "review"))
    total_tasks = len(rows)

    tr = subprocess.run(["git", "log", "--all", *since, "--oneline", "--",
                         "work/tasks", "work/backlog"],
                        cwd=str(main), capture_output=True, text=True)
    board_updates = len([ln for ln in tr.stdout.splitlines() if ln.strip()])

    fields = [
        {"name": f"Commits ({label})",
         "value": (f"**{total_commits}** across {len(commits)} contributor(s)\n{commit_lines}")[:1024],
         "inline": False},
        {"name": "Board", "value": f"**{done}**/{total_tasks} done · {active} active", "inline": True},
        {"name": "Task activity", "value": f"{board_updates} board update(s)", "inline": True},
    ]
    embed = {
        "title": f"Chudware {label} digest",
        "description": (f"Progress over the last {period_days} day(s). "
                        f"Master Plan: `docs/AI_TOOLING_MASTER_PLAN.md`"),
        "color": COLORS["digest"],
        "fields": fields,
    }
    return embed, _recent_screenshots(main, 3)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="verify config is readable")
    g.add_argument("--poll", action="store_true", help="scheduled full sweep (safety net)")
    g.add_argument("--hook", dest="task_id", metavar="TASK_ID",
                   help="low-latency single-task ping from work.py")
    g.add_argument("--test-ping", action="store_true",
                   help="post sample messages to approve the format")
    args = ap.parse_args()
    if args.check:
        return cmd_check(args)
    if args.poll:
        return cmd_poll(args)
    if args.test_ping:
        return cmd_test_ping(args)
    if args.task_id:
        return cmd_hook(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
