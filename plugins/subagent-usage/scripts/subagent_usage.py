#!/usr/bin/env python3
"""SubagentStop hook: record usage data per subagent in a CSV file and HTML pages.

Reads the hook JSON on stdin, parses the subagent transcript, adds or replaces one row
(key: agent_id) in the CSV file, and regenerates the HTML pages: index.html (all sessions)
and sessions/<session-id>.html (one page per session).

Paths (environment variables, all optional):
  SUBAGENT_USAGE_DIR  pages and roles.toml   default <claude dir>/subagent-usage
  SUBAGENT_USAGE_CSV  the data file          default <claude dir>/subagent-usage.csv
  CLAUDE_CONFIG_DIR   the claude dir         default ~/.claude
Roles come from <SUBAGENT_USAGE_DIR>/roles.toml (copied from defaults/roles.toml on the
first run) and the optional project file <project>/.claude/subagent-roles.toml.

Manual use:
  subagent_usage.py --html                    regenerate all HTML pages, print the index path
  subagent_usage.py --backfill <session>      add all subagents of a past session
                                              (<session> = session id or its .jsonl path)
  subagent_usage.py <agent-transcript.jsonl>  add one subagent

Token notes:
- input / cache_read / cache_creation are taken once per API request (requestId).
- output_tokens is NOT recorded: transcripts store it at stream start, so it is wrong.
"""
import csv
import fcntl
import glob
import html
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import defaultdict
from datetime import datetime, timezone

CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
CODEX_DIR = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
CSV_PATH = os.environ.get("SUBAGENT_USAGE_CSV") or os.path.join(CLAUDE_DIR, "subagent-usage.csv")
OUT_DIR = os.environ.get("SUBAGENT_USAGE_DIR") or os.path.join(CLAUDE_DIR, "subagent-usage")
SESSIONS_DIR = os.path.join(OUT_DIR, "sessions")
FIELDS = [
    "logged_at", "started_at", "tool", "project", "project_dir", "session_id", "agent_id",
    "agent_type", "model", "description", "wall_s", "api_calls", "tool_calls", "input_tokens",
    "cache_read_tokens", "cache_creation_tokens", "total_input_tokens",
    "peak_context", "longest_turn_s", "api_errors", "idle_gaps", "commits",
    "lines_added", "lines_deleted", "code_added", "code_deleted", "output_tokens", "transcript",
]


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- role rules
#
# Roles come from TOML files, never from the CSV, so a rule change applies to old agents too.
#   global:  <OUT_DIR>/roles.toml  (copied from defaults/roles.toml if missing)
#   project: <project>/.claude/subagent-roles.toml (optional; its rules are tried first)
ROLES_FILE = os.path.join(OUT_DIR, "roles.toml")
PROJECT_ROLES_FILE = os.path.join(".claude", "subagent-roles.toml")
KNOWN_EXTRA_METRICS = {"code_added"}
# [settings] keys and their defaults. A project file value replaces the global value.
SETTINGS_DEFAULTS = {"git_stats": True}
GIT_FIELDS = ("commits", "lines_added", "lines_deleted", "code_added", "code_deleted")

TEMPLATE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "defaults", "roles.toml")
MINIMAL_ROLES_TOML = '[default]\nname = "other"\n'


def default_roles_toml():
    """The template text; a minimal rule set if the template file is missing."""
    try:
        with open(TEMPLATE_FILE) as f:
            return f.read()
    except OSError:
        return MINIMAL_ROLES_TOML


def parse_roles(text, source):
    """Return (rules, default_name, settings, warnings). Raises ValueError on a TOML syntax error."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{source}: {e}") from e
    rules, warnings = [], []
    for i, r in enumerate(data.get("role", []), 1):
        where = f"{source}: [[role]] #{i}"
        name = r.get("name")
        if not isinstance(name, str) or not name:
            warnings.append(f"{where}: no name; rule skipped")
            continue
        typ, desc = r.get("type"), r.get("description")
        if typ is None and desc is None:
            warnings.append(f"{where} ({name}): no type or description; rule skipped")
            continue
        pattern = None
        if desc is not None:
            try:
                pattern = re.compile(str(desc))
            except re.error as e:
                warnings.append(f"{where} ({name}): bad regex: {e}; rule skipped")
                continue
        extra = [m for m in r.get("extra_metrics", []) if m in KNOWN_EXTRA_METRICS]
        for m in r.get("extra_metrics", []):
            if m not in KNOWN_EXTRA_METRICS:
                warnings.append(f"{where} ({name}): unknown extra metric {m!r}; ignored")
        rules.append({"name": name, "type": typ, "re": pattern, "extra": extra})
    default = (data.get("default") or {}).get("name")
    settings = {}
    for k, v in (data.get("settings") or {}).items():
        if k not in SETTINGS_DEFAULTS:
            warnings.append(f"{source}: [settings] unknown key {k!r}; ignored")
        elif type(v) is not type(SETTINGS_DEFAULTS[k]):
            warnings.append(f"{source}: [settings] {k} must be {type(SETTINGS_DEFAULTS[k]).__name__}; ignored")
        else:
            settings[k] = v
    return rules, default, settings, warnings


_rules_cache = {}


def rules_for(project_dir):
    """Merged rule set for one project: project rules first, then global rules."""
    key = project_dir or ""
    if key in _rules_cache:
        return _rules_cache[key]
    warnings, sources = [], []
    if not os.path.exists(ROLES_FILE):
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(ROLES_FILE, "w") as f:
            f.write(default_roles_toml())
    try:
        with open(ROLES_FILE) as f:
            g_rules, g_default, g_settings, w = parse_roles(f.read(), ROLES_FILE)
        warnings += w
        sources.append(ROLES_FILE)
    except (OSError, ValueError) as e:
        try:
            g_rules, g_default, g_settings, _ = parse_roles(default_roles_toml(), "built-in rules")
        except ValueError:
            g_rules, g_default, g_settings, _ = parse_roles(MINIMAL_ROLES_TOML, "built-in rules")
        warnings.append(f"{e} -- the built-in rules are used instead")
        sources.append("built-in rules")
    p_rules, p_default, p_settings = [], None, {}
    if project_dir:
        path = os.path.join(project_dir, PROJECT_ROLES_FILE)
        if os.path.exists(path):
            try:
                with open(path) as f:
                    p_rules, p_default, p_settings, w = parse_roles(f.read(), path)
                warnings += w
                sources.insert(0, path)
            except (OSError, ValueError) as e:
                warnings.append(f"{e} -- this project file is ignored")
    rules = p_rules + g_rules
    settings = {**SETTINGS_DEFAULTS, **g_settings, **p_settings}
    extra = {}
    for r in rules:
        metrics = [m for m in r["extra"] if settings["git_stats"] or m != "code_added"]
        if metrics and r["name"] not in extra:
            extra[r["name"]] = metrics
    rs = {"rules": rules, "default": p_default or g_default or "other", "extra": extra,
          "settings": settings, "warnings": warnings, "sources": sources}
    _rules_cache[key] = rs
    return rs


def classify(description, agent_type, ruleset):
    d = (description or "").lower()
    for r in ruleset["rules"]:
        if r["type"] is not None and r["type"] != agent_type:
            continue
        if r["re"] is not None and not r["re"].search(d):
            continue
        return r["name"]
    return ruleset["default"]


IDLE_GAP_S = 30 * 60  # a gap between records above this is idle time, not work

# Files that count as code for code_added / code_deleted. Generated files never count.
CODE_EXT = (".rs", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".sol", ".c", ".h", ".cc",
            ".cpp", ".hpp", ".java", ".kt", ".swift", ".sh", ".move", ".zig")
GENERATED = re.compile(r"(^|/)(Cargo\.lock|package-lock\.json|yarn\.lock|pnpm-lock\.yaml)$|(^|/)(idls?|target|node_modules)/")


def git_stats(repo, segments):
    """Commits whose commit time falls in one of the agent's active segments.

    Limit: a commit by the main agent or by a parallel agent in the same window also counts.
    """
    empty = {"commits": "", "lines_added": 0, "lines_deleted": 0, "code_added": 0, "code_deleted": 0}
    if not repo or not segments:
        return empty
    since = int(min(a for a, _ in segments)) - 1
    until = int(max(b for _, b in segments)) + 6
    try:
        p = subprocess.run(["git", "-C", repo, "log", "--all", "--no-renames", "--numstat",
                            f"--since=@{since}", f"--until=@{until}", "--format=@%h %ct"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return empty
    if p.returncode != 0:
        return empty
    out = dict(empty, commits=[])
    take = False
    for line in p.stdout.splitlines():
        if line.startswith("@"):
            h, ct = line[1:].split(" ")
            ct = int(ct)
            take = any(a - 1 <= ct <= b + 5 for a, b in segments)
            if take:
                out["commits"].append(h)
        elif take and line.strip():
            parts = line.split("\t")
            if len(parts) != 3 or not parts[0].isdigit():
                continue  # binary file
            a, d, path = int(parts[0]), int(parts[1]), parts[2]
            if GENERATED.search(path):
                continue
            out["lines_added"] += a
            out["lines_deleted"] += d
            if path.endswith(CODE_EXT):
                out["code_added"] += a
                out["code_deleted"] += d
    out["commits"] = " ".join(out["commits"])
    return out


def is_codex(path):
    """Codex rollout files start with a session_meta record; Claude Code files never do."""
    try:
        with open(path) as f:
            for line in f:
                if line.strip():
                    return json.loads(line).get("type") == "session_meta"
    except (OSError, ValueError):
        pass
    return False


def analyze(path):
    return analyze_codex(path) if is_codex(path) else analyze_claude(path)


def analyze_claude(path):
    reqs = {}
    tool_calls = api_errors = idle_gaps = 0
    active = 0.0
    first = last = None
    seg_start = None
    segments = []
    repo = None
    prev_user = None
    longest = 0.0
    model = None
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            t = r.get("timestamp")
            if t:
                t = ts(t)
                first = first or t
                if last:
                    gap = (t - last).total_seconds()
                    if gap > IDLE_GAP_S:
                        idle_gaps += 1
                        segments.append((seg_start.timestamp(), last.timestamp()))
                        seg_start = t
                    elif gap > 0:
                        active += gap
                seg_start = seg_start or t
                last = t
            repo = repo or r.get("cwd")
            msg = r.get("message") or {}
            if r.get("type") == "user" and t:
                prev_user = t
            if r.get("type") != "assistant":
                continue
            if msg.get("model") == "<synthetic>":
                api_errors += 1
                continue
            model = msg.get("model") or model
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tool_calls += 1
            rid = r.get("requestId")
            if rid and rid not in reqs:
                reqs[rid] = msg.get("usage") or {}
                if prev_user and t:
                    turn = (t - prev_user).total_seconds()
                    if turn <= IDLE_GAP_S:
                        longest = max(longest, turn)
    inp = sum(u.get("input_tokens", 0) for u in reqs.values())
    cr = sum(u.get("cache_read_input_tokens", 0) for u in reqs.values())
    cc = sum(u.get("cache_creation_input_tokens", 0) for u in reqs.values())
    peak = max((u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                + u.get("cache_creation_input_tokens", 0) for u in reqs.values()), default=0)
    return {
        "started_at": first.isoformat(timespec="seconds") if first else "",
        "model": model or "",
        "wall_s": round(active),
        "idle_gaps": idle_gaps,
        "api_calls": len(reqs),
        "tool_calls": tool_calls,
        "input_tokens": inp,
        "cache_read_tokens": cr,
        "cache_creation_tokens": cc,
        "total_input_tokens": inp + cr + cc,
        "peak_context": peak,
        "longest_turn_s": round(longest),
        "api_errors": api_errors,
        "project_dir": repo or "",
        "tool": "claude",
        "output_tokens": "",
        **git_fields(repo, segments + ([(seg_start.timestamp(), last.timestamp())] if last else [])),
    }


def git_fields(repo, segments):
    if rules_for(repo)["settings"]["git_stats"]:
        return git_stats(repo, segments)
    return dict.fromkeys(GIT_FIELDS, "")


# Codex rollout files (~/.codex/sessions/**/rollout-*.jsonl). Each line is
# {"timestamp", "type", "payload"}; types: session_meta, turn_context, response_item, event_msg.
# A subagent's session_meta has source.subagent.thread_spawn {parent_thread_id, depth,
# agent_path, agent_nickname, agent_role}. Token counts are event_msg/token_count records with
# running totals (info.total_token_usage); input_tokens includes cached_input_tokens.
#
# A forked subagent file starts with the parent's history, written with the creation time,
# and its first total already includes the parent's usage. Records up to CODEX_REPLAY_S after
# session_meta are taken as that copy: the agent's own tokens are the last total minus the
# last total in that window. NOT VERIFIED against real files yet (built from the Codex source
# and bug reports of other usage tools).
CODEX_REPLAY_S = 1.0
CODEX_TOOL_ITEMS = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}
CODEX_USAGE_KEYS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                    "output_tokens", "total_tokens")


def read_codex(path):
    recs = []
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and r.get("timestamp"):
                recs.append((ts(r["timestamp"]), r.get("type"), r.get("payload") or {}))
    return recs


def codex_spawn(meta):
    src = meta.get("source")
    if isinstance(src, dict):
        sub = src.get("subagent")
        if isinstance(sub, dict) and isinstance(sub.get("thread_spawn"), dict):
            return sub["thread_spawn"]
    return {}


def analyze_codex(path):
    recs = read_codex(path)
    meta = next((p for _, k, p in recs if k == "session_meta"), {})
    start = next((t for t, k, _ in recs if k == "session_meta"), recs[0][0] if recs else None)
    cut = start.timestamp() + CODEX_REPLAY_S if start else 0
    base = dict.fromkeys(CODEX_USAGE_KEYS, 0)
    final = dict(base)
    seen_totals = set()
    api_calls = tool_calls = api_errors = idle_gaps = 0
    peak = 0
    active = longest = 0.0
    last = seg_start = None
    segments = []
    model = None
    task = ""
    for t, kind, p in recs:
        own = t.timestamp() > cut
        if kind == "turn_context" and p.get("model"):
            model = p["model"]
        if kind == "event_msg" and p.get("type") == "user_message" and (not own or not task):
            task = str(p.get("message") or "")
        if kind == "event_msg" and p.get("type") == "token_count" and p.get("info"):
            total = {k: (p["info"].get("total_token_usage") or {}).get(k, 0) or 0 for k in CODEX_USAGE_KEYS}
            if not own:
                base = total
            elif total["total_tokens"] not in seen_totals:  # rollouts can repeat a record
                api_calls += 1
                lu = p["info"].get("last_token_usage") or {}
                peak = max(peak, (lu.get("input_tokens") or 0) + (lu.get("cache_write_input_tokens") or 0))
            seen_totals.add(total["total_tokens"])
            final = total
        if not own:
            continue
        if kind == "response_item" and p.get("type") in CODEX_TOOL_ITEMS:
            tool_calls += 1
        if kind == "event_msg" and p.get("type") in ("error", "stream_error"):
            api_errors += 1
        if last is None:
            last = seg_start = start or t
        gap = (t - last).total_seconds()
        if gap > IDLE_GAP_S:
            idle_gaps += 1
            segments.append((seg_start.timestamp(), last.timestamp()))
            seg_start = t
        elif gap > 0:
            active += gap
            longest = max(longest, gap)
        last = t
    use = {k: max(final[k] - base[k], 0) for k in CODEX_USAGE_KEYS}
    cached, write = use["cached_input_tokens"], use["cache_write_input_tokens"]
    spawn = codex_spawn(meta)
    repo = meta.get("cwd")
    if last is not None:
        segments.append((seg_start.timestamp(), last.timestamp()))
    first_line = next((l.strip() for l in task.splitlines() if l.strip()), "")
    return {
        "started_at": start.isoformat(timespec="seconds") if start else "",
        "model": model or "",
        "wall_s": round(active),
        "idle_gaps": idle_gaps,
        "api_calls": api_calls,
        "tool_calls": tool_calls,
        "input_tokens": max(use["input_tokens"] - cached - write, 0),
        "cache_read_tokens": cached,
        "cache_creation_tokens": write,
        "total_input_tokens": use["input_tokens"],
        "peak_context": peak,
        "longest_turn_s": round(longest),
        "api_errors": api_errors,
        "project_dir": repo or "",
        "tool": "codex",
        "output_tokens": use["output_tokens"],
        "_agent_id": meta.get("id", ""),
        "_agent_type": spawn.get("agent_role") or spawn.get("agent_type") or "",
        "_description": spawn.get("agent_path") or spawn.get("agent_nickname") or first_line[:120],
        **git_fields(repo, segments),
    }


def add_row(hook):
    path = hook.get("agent_transcript_path")
    agent_id = hook.get("agent_id", "")
    if not path and agent_id and hook.get("transcript_path"):
        base = hook["transcript_path"][: -len(".jsonl")]
        path = os.path.join(base, "subagents", f"agent-{agent_id}.jsonl")
    if not path or not os.path.exists(path):
        return False
    meta = {}
    meta_path = path[: -len(".jsonl")] + ".meta.json"
    if os.path.exists(meta_path):
        with open(meta_path) as f:
            meta = json.load(f)
    row = analyze(path)
    agent_id = agent_id or row.pop("_agent_id", "") or os.path.basename(path)[len("agent-"):-len(".jsonl")]
    agent_type = hook.get("agent_type") or meta.get("agentType") or row.get("_agent_type", "")
    description = meta.get("description") or row.get("_description", "")
    for k in ("_agent_id", "_agent_type", "_description"):
        row.pop(k, None)
    row.update({
        "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project": os.path.basename(hook.get("cwd") or row.get("project_dir") or ""),
        "project_dir": hook.get("cwd") or row.get("project_dir") or "",
        "session_id": hook.get("session_id", ""),
        "agent_id": agent_id,
        "agent_type": agent_type,
        "description": description,
        "transcript": path,
    })
    upsert_row(row)
    return True


def upsert_row(row):
    """Replace the row with the same agent_id (a resumed agent stops again), else append.

    A lock file serializes parallel hooks; the file is replaced in one step.
    """
    os.makedirs(os.path.dirname(os.path.abspath(CSV_PATH)), exist_ok=True)
    with open(CSV_PATH + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = []
        if os.path.exists(CSV_PATH):
            with open(CSV_PATH, newline="") as f:
                rows = list(csv.DictReader(f))
        rows = [r for r in rows if r.get("agent_id") != row["agent_id"]] + [row]
        tmp = f"{CSV_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        os.replace(tmp, CSV_PATH)


def codex_children(root_id):
    """Rollout files of all subagents below one Codex session (any depth)."""
    parent_of = {}
    for path in glob.glob(os.path.join(CODEX_DIR, "sessions", "**", "rollout-*.jsonl"), recursive=True):
        try:
            with open(path) as f:
                r = json.loads(f.readline())
        except (OSError, ValueError):
            continue
        p = r.get("payload") or {}
        if r.get("type") == "session_meta" and codex_spawn(p).get("parent_thread_id"):
            parent_of[path] = (p.get("id"), codex_spawn(p)["parent_thread_id"])
    ids, found, grew = {root_id}, [], True
    while grew:
        grew = False
        for path, (tid, parent) in list(parent_of.items()):
            if parent in ids:
                ids.add(tid)
                found.append(path)
                del parent_of[path]
                grew = True
    return sorted(found)


def find_session(arg):
    """(session_id, [agent transcript paths]) for a session id or a session .jsonl path."""
    if arg.endswith(".jsonl") and is_codex(arg):
        with open(arg) as f:
            sid = (json.loads(f.readline()).get("payload") or {}).get("id", "")
        return sid, codex_children(sid)
    if arg.endswith(".jsonl"):
        sid = os.path.basename(arg)[: -len(".jsonl")]
        dirs = [os.path.join(os.path.dirname(os.path.abspath(arg)), sid, "subagents")]
    else:
        sid = arg
        dirs = glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", glob.escape(sid), "subagents"))
    paths = sorted(p for d in dirs for p in glob.glob(os.path.join(d, "agent-*.jsonl")))
    return sid, paths or codex_children(sid)


def session_cwd(agent_path):
    """First cwd in the main session transcript: the project folder that the hook gets.

    An agent transcript can start in a subfolder, so its own cwd is only the fallback.
    """
    if "/subagents/" not in agent_path:
        return None  # Codex: the rollout's own session_meta cwd is used
    main = os.path.dirname(os.path.dirname(agent_path)) + ".jsonl"
    try:
        with open(main) as f:
            for line in f:
                try:
                    cwd = json.loads(line).get("cwd")
                except ValueError:
                    continue
                if cwd:
                    return cwd
    except OSError:
        pass
    return None


def backfill(arg):
    sid, paths = find_session(arg)
    if not paths:
        print(f"No subagent transcripts found for {arg}", file=sys.stderr)
        return 1
    added = sum(add_row({"agent_transcript_path": p, "session_id": sid, "cwd": session_cwd(p)})
                for p in paths)
    render_html(sid)
    print(f"{added} subagents of session {sid} recorded. Pages: {os.path.join(OUT_DIR, 'index.html')}")
    return 0


# ---------------------------------------------------------------- HTML pages

def num(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def short_model(m):
    return m.replace("claude-", "").split("-2025")[0].split("-2026")[0] or "?"


# Fixed model-family colors (validated all-pairs, light and dark). Color follows the
# family, never its rank. Everything else folds into "other" (neutral gray).
MODEL_FAMILIES = ["opus", "sonnet", "fable"]


def family(model):
    m = (model or "").lower()
    for f in MODEL_FAMILIES:
        if f in m:
            return f
    return "other"


def fmt_n(v):
    return f"{v:,}"


def fmt_min(s):
    return f"{s / 60:.1f}"


def esc(v):
    return html.escape(str(v), quote=True)


def session_title(row):
    """customTitle from the session folder next to the transcript, if any."""
    path = row.get("transcript", "")
    if "/subagents/" not in path:
        return ""
    title_path = os.path.join(os.path.dirname(os.path.dirname(path)), "custom-title.json")
    try:
        with open(title_path) as f:
            return json.load(f).get("customTitle", "")
    except (OSError, ValueError):
        return ""


PAGE_CSS = """
:root { --bg:#fafaf9; --fg:#1c1917; --muted:#78716c; --line:#e7e5e4; --card:#fcfcfb;
  --head:#f5f5f4; --accent:#9a3412; --bar:#fdba74; --link:#1c5cab;
  --m-opus:#2a78d6; --m-sonnet:#eb6834; --m-fable:#1baf7a; --m-other:#8f8d88;
  --trend:#b8b5ae; --grid:#e7e5e4; --axis:#78716c; --ink2:#3d3c39; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --fg:#f5f5f4; --muted:#a8a29e;
  --line:#3a3936; --card:#1a1a19; --head:#262624; --accent:#fdba74; --bar:#9a3412; --link:#86b6ef;
  --m-opus:#3987e5; --m-sonnet:#d95926; --m-fable:#199e70; --m-other:#8f8d88;
  --trend:#55534e; --grid:#2e2d2a; --axis:#a8a29e; --ink2:#d6d4cc; } }
* { box-sizing:border-box; }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
  font:14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:1400px; margin:0 auto; }
a { color:var(--link); }
h1 { font-size:20px; margin:0 0 4px; }
h2 { font-size:16px; margin:32px 0 4px; }
p.note, .meta { color:var(--muted); margin:0 0 12px; }
.crumb { margin:0 0 8px; font-size:13px; }
.controls { display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin:12px 0; }
select, input { font:inherit; padding:4px 8px; border:1px solid var(--line); border-radius:6px;
  background:var(--card); color:var(--fg); }
.wrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; background:var(--card); }
table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; }
th, td { padding:6px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }
th { background:var(--head); font-weight:600; }
table.sortable th { position:sticky; top:0; cursor:pointer; user-select:none; }
th.num, td.num { text-align:right; }
td.desc { white-space:normal; min-width:220px; }
tr:last-child td { border-bottom:none; }
.bar { position:relative; }
.bar span { position:absolute; left:0; top:4px; bottom:4px; background:var(--bar); opacity:.45; border-radius:2px; }
.bar b { position:relative; font-weight:normal; }
th.sorted::after { content:" \\2193"; color:var(--accent); }
th.sorted.asc::after { content:" \\2191"; }
.key { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:-1px; }
.legend { display:flex; gap:16px; flex-wrap:wrap; color:var(--fg); font-size:13px; }
#charts { display:grid; grid-template-columns:repeat(auto-fill, minmax(340px, 1fr)); gap:12px; }
.chart { position:relative; display:flex; flex-direction:column; border:1px solid var(--line); border-radius:8px;
  background:var(--card); padding:10px 12px 6px; }
.chart svg { margin-top:auto; } /* charts in one row share the bottom line */
.chart h3 { font-size:14px; margin:0; font-weight:600; }
.chart .sub { color:var(--muted); font-size:12px; margin:0 0 4px; }
.chart .sub.lg { color:var(--fg); line-height:1.6; }
.chart .trend2 { fill:none; stroke:var(--ink2); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }
.chart .mark2 { fill:var(--card); stroke:var(--ink2); stroke-width:2; }
.key.sq { border-radius:2px; background:var(--card); border:2px solid var(--ink2); width:9px; height:9px; }
.key.line1 { border-radius:0; height:2px; width:14px; vertical-align:3px; background:var(--trend); }
.tip .small { color:var(--muted); font-size:11px; margin-top:2px; }
.warn { border:1px solid var(--accent); border-radius:8px; padding:8px 12px; margin:0 0 12px; }
.warn ul { margin:4px 0 0; padding-left:20px; }
code { font-size:12px; }
.chart svg { display:block; width:100%; overflow:visible; }
.chart text { fill:var(--axis); font-size:11px; font-variant-numeric:tabular-nums; }
.chart .gridline { stroke:var(--grid); stroke-width:1; }
.chart .trend { fill:none; stroke:var(--trend); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }
.chart .dot { stroke:var(--card); stroke-width:2; }
.chart .cross { stroke:var(--axis); stroke-width:1; }
.tip { position:absolute; pointer-events:none; z-index:2; background:var(--card); color:var(--fg);
  border:1px solid var(--line); border-radius:6px; padding:6px 8px; font-size:12px; line-height:1.4;
  box-shadow:0 2px 8px rgba(0,0,0,.15); max-width:260px; }
.tip b { font-weight:600; }
"""

SORT_JS = """
document.querySelectorAll('table.sortable').forEach(table => {
  const tbody = table.tBodies[0];
  table.querySelectorAll('th').forEach((th, i) => th.addEventListener('click', () => {
    const asc = th.classList.contains('sorted') && !th.classList.contains('asc');
    table.querySelectorAll('th').forEach(h => h.classList.remove('sorted', 'asc'));
    th.classList.add('sorted'); if (asc) th.classList.add('asc');
    const key = r => { const c = r.cells[i]; return c.dataset.v !== undefined ? +c.dataset.v : c.textContent.toLowerCase(); };
    const rows = [...tbody.rows];
    rows.sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * (asc ? 1 : -1); });
    rows.forEach(r => tbody.appendChild(r));
  }));
});
"""

SESSION_JS = """
const q = document.querySelector('#q'), metricSel = document.querySelector('#metric');
const tableRows = [...document.querySelector('#agents').tBodies[0].rows];
const DATA = JSON.parse(document.getElementById('agent-data').textContent);
const css = getComputedStyle(document.documentElement);
const colorOf = f => `var(--m-${f})`;
const trim = (v, d) => String(+v.toFixed(d));
const compact = v => v >= 1e6 ? trim(v / 1e6, 2) + 'M' : v >= 1e3 ? trim(v / 1e3, 2) + 'K' : trim(v, 1);
const full = v => Math.round(v).toLocaleString('en-US');
// Durations are in seconds. Each value shows in s, min or h, by its size.
const fmtDur = v => v < 60 ? trim(v, 0) + ' s' : v < 3600 ? trim(v / 60, 1) + ' min' : trim(v / 3600, 2) + ' h';
// The axis of one chart uses 1 unit, chosen from the largest value in that chart.
function durScale(max) {
  const [div, unit] = max < 120 ? [1, 's'] : max < 7200 ? [60, 'min'] : [3600, 'h'];
  return { unit, niceMax: v => niceMax(v / div) * div, tick: v => trim(v / div, 1) };
}
const METRICS = {
  total: { label: 'Total input tokens', tick: compact, tip: full },
  wall: { label: 'Wall time', scale: durScale, tip: fmtDur },
  peak: { label: 'Peak context tokens', tick: compact, tip: full },
  turn: { label: 'Longest turn', scale: durScale, tip: fmtDur },
  calls: { label: 'API calls', tick: compact, tip: full },
};
const NS = 'http://www.w3.org/2000/svg';
function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v)), n = v / p;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p;
}
const fmtTime = ms => new Date(ms).toISOString().slice(5, 16).replace('T', ' ');

// Extra metrics drawn as a second line in the chart of these roles only.
const ROLE_EXTRA = JSON.parse(document.getElementById('role-extra').textContent);
const EXTRA_METRICS = {
  code_added: { label: 'Code lines added', tick: compact, tip: full,
    detail: d => `code +${full(d.code_added)} / -${full(d.code_deleted)} · all files +${full(d.lines_added)} / -${full(d.lines_deleted)} · commits: ${d.commits || 'none found'}` },
};

// One chart per role. With an extra metric (E), both series share 1 axis: each value is
// a percentage of that series' own maximum in this role (indexed; never a second y axis).
function plot(card, ds, mk, M, aria, ek, E) {
  const W = Math.max(card.clientWidth - 24, 240), H = 170, m = { l: 44, r: 10, t: 8, b: 24 };
  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, height: H, role: 'img', 'aria-label': aria }, card);
  const n = ds.length, pw = W - m.l - m.r;
  const x = i => m.l + (n === 1 ? 0.5 : i / (n - 1)) * pw;
  const maxA = Math.max(...ds.map(d => d[mk]));
  const maxB = ek ? Math.max(...ds.map(d => d[ek])) : 0;
  const va = d => ek ? (maxA ? 100 * d[mk] / maxA : 0) : d[mk];
  const vb = d => maxB ? 100 * d[ek] / maxB : 0;
  const ymax = ek ? 100 : (M.niceMax || niceMax)(maxA);
  const y = v => m.t + (1 - v / ymax) * (H - m.t - m.b);
  [0, ymax / 2, ymax].forEach(v => {
    el('line', { class: 'gridline', x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }, svg);
    el('text', { x: m.l - 6, y: y(v) + 4, 'text-anchor': 'end' }, svg).textContent = ek ? `${v}%` : M.tick(v);
  });
  const step = Math.max(1, Math.ceil(n / 8)), ticks = [];
  for (let i = 0; i < n; i += step) ticks.push(i);
  if (ticks[ticks.length - 1] !== n - 1 && n - 1 - ticks[ticks.length - 1] >= step / 2) ticks.push(n - 1);
  ticks.forEach(i => {
    el('text', { x: x(i), y: H - 6, 'text-anchor': 'middle' }, svg).textContent = String(i + 1);
  });
  const line = (f, cls) => n > 1 && el('path', { class: cls,
    d: ds.map((d, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(f(d)).toFixed(1)}`).join('') }, svg);
  line(va, 'trend');
  if (ek) {
    line(vb, 'trend2');
    ds.forEach((d, i) => el('rect', { class: 'mark2', x: x(i) - 3.5, y: y(vb(d)) - 3.5,
      width: 7, height: 7, rx: 1.5 }, svg));
  }
  const dots = ds.map((d, i) => el('circle', { class: 'dot', cx: x(i), cy: y(va(d)), r: 4,
    style: `fill:${colorOf(d.family)}` }, svg));
  const cross = el('line', { class: 'cross', y1: m.t, y2: H - m.b, visibility: 'hidden' }, svg);
  const tip = document.createElement('div'); tip.className = 'tip'; tip.hidden = true; card.appendChild(tip);
  const hit = el('rect', { x: m.l - 8, y: 0, width: pw + 16, height: H, fill: 'transparent' }, svg);
  let cur = -1;
  hit.addEventListener('mousemove', ev => {
    const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width;
    let i = 0;
    ds.forEach((d, j) => { if (Math.abs(x(j) - px) < Math.abs(x(i) - px)) i = j; });
    if (i === cur) return;
    if (cur >= 0) dots[cur].setAttribute('r', 4);
    cur = i; dots[i].setAttribute('r', 6);
    const d = ds[i], cx = x(i);
    cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('visibility', 'visible');
    tip.replaceChildren();
    const b = document.createElement('b'); b.textContent = `#${i + 1} · ${d.desc || '(no description)'}`;
    tip.appendChild(b);
    const mrow = document.createElement('div');
    const key = document.createElement('span'); key.className = 'key'; key.style.background = colorOf(d.family);
    mrow.appendChild(key); mrow.appendChild(document.createTextNode(`${d.model} · ${fmtTime(d.t)} UTC`));
    tip.appendChild(mrow);
    const v = document.createElement('div'); v.textContent = `${M.label}: ${M.tip(d[mk])}`; tip.appendChild(v);
    if (ek) {
      const v2 = document.createElement('div');
      const k2 = document.createElement('span'); k2.className = 'key sq'; v2.appendChild(k2);
      v2.appendChild(document.createTextNode(`${E.label}: ${E.tip(d[ek])}`)); tip.appendChild(v2);
      if (E.detail) { const e = document.createElement('div'); e.className = 'small'; e.textContent = E.detail(d); tip.appendChild(e); }
    }
    tip.hidden = false;
    const left = cx * r.width / W + 12;
    tip.style.left = Math.min(left, card.clientWidth - tip.offsetWidth - 4) + 'px';
    tip.style.top = (svg.offsetTop + 8) + 'px';
  });
  hit.addEventListener('mouseleave', () => {
    if (cur >= 0) dots[cur].setAttribute('r', 4);
    cur = -1; cross.setAttribute('visibility', 'hidden'); tip.hidden = true;
  });
}

function renderCharts() {
  const t = q.value.toLowerCase(), mk = metricSel.value, M0 = METRICS[mk];
  const pts = DATA.filter(d => !t || d.search.includes(t));
  const box = document.getElementById('charts');
  box.innerHTML = '';
  if (!pts.length) { box.textContent = 'No agents match the filter.'; return; }
  // Width rule: max 30 points per tile width. 31-60 points = 2 tiles wide, 61-90 = 3,
  // more = 4 (the max). Never wider than the columns the screen has.
  const cols = getComputedStyle(box).gridTemplateColumns.split(' ').length;
  const byRole = {};
  pts.forEach(d => (byRole[d.role] = byRole[d.role] || []).push(d));
  const roles = Object.keys(byRole).sort((a, b) =>
    byRole[b].reduce((x, d) => x + d.total, 0) - byRole[a].reduce((x, d) => x + d.total, 0));
  for (const role of roles) {
    const ds = byRole[role].slice().sort((a, b) => a.t - b.t);
    const ek = (ROLE_EXTRA[role] || [])[0], E = ek && EXTRA_METRICS[ek];
    const M = M0.scale ? { ...M0, ...M0.scale(Math.max(...ds.map(d => d[mk]))) } : M0;
    const card = document.createElement('div');
    card.className = 'chart';
    const span = Math.min(Math.ceil(ds.length / 30), 4, cols);
    if (span > 1) card.style.gridColumn = `span ${span}`;
    const h = document.createElement('h3'); h.textContent = role; card.appendChild(h);
    const sub = document.createElement('p'); sub.className = 'sub';
    const count = `${ds.length} agent${ds.length > 1 ? 's' : ''}`;
    if (E) {
      const maxA = Math.max(...ds.map(d => d[mk])), maxB = Math.max(...ds.map(d => d[ek]));
      sub.textContent = `${count} · each line as % of its own maximum in this role`;
      card.appendChild(sub);
      const lg = document.createElement('p'); lg.className = 'sub lg';
      const k1 = document.createElement('span'); k1.className = 'key line1';
      const k2 = document.createElement('span'); k2.className = 'key sq';
      lg.append(k1, `${M.label} (max ${M.tip(maxA)}; dot color = model)`, document.createElement('br'),
        k2, `${E.label} (max ${E.tip(maxB)})`);
      card.appendChild(lg);
    } else {
      sub.textContent = `${count} · ${M.label}${M.unit ? ` (${M.unit})` : ''}`;
      card.appendChild(sub);
    }
    box.appendChild(card);
    plot(card, ds, mk, M, E ? `${role}: ${M.label} and ${E.label} per agent, indexed to their maximum`
      : `${role}: ${M.label} per agent in run order`, ek, E);
  }
}
function applyFilter() {
  const t = q.value.toLowerCase();
  tableRows.forEach(r => { r.style.display = !t || r.textContent.toLowerCase().includes(t) ? '' : 'none'; });
  renderCharts();
}
q.addEventListener('input', applyFilter);
metricSel.addEventListener('change', renderCharts);
let resizeTimer;
window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderCharts, 150); });
renderCharts();
"""

COLS = [  # (header, field, numeric, formatter)
    ("Started (UTC)", "started_at", False, lambda v: v.replace("T", " ")[:16]),
    ("Model", "model", False, short_model),
    ("Type", "agent_type", False, str),
    ("Role", "role", False, str),
    ("Description", "description", False, str),
    ("Wall min", "wall_s", True, lambda v: fmt_min(num(v))),
    ("API calls", "api_calls", True, lambda v: fmt_n(num(v))),
    ("Tool calls", "tool_calls", True, lambda v: fmt_n(num(v))),
    ("Total input", "total_input_tokens", True, lambda v: fmt_n(num(v))),
    ("Cache read", "cache_read_tokens", True, lambda v: fmt_n(num(v))),
    ("Cache write", "cache_creation_tokens", True, lambda v: fmt_n(num(v))),
    ("Peak context", "peak_context", True, lambda v: fmt_n(num(v))),
    ("Longest turn s", "longest_turn_s", True, lambda v: fmt_n(num(v))),
    ("API errors", "api_errors", True, lambda v: fmt_n(num(v))),
    ("Idle gaps", "idle_gaps", True, lambda v: fmt_n(num(v))),
    ("Code +", "code_added", True, lambda v: fmt_n(num(v))),
    ("Code -", "code_deleted", True, lambda v: fmt_n(num(v))),
    ("Commits", "commits", False, str),
    ("Output", "output_tokens", True, lambda v: fmt_n(num(v)) if v not in ("", None) else ""),
]

NOTE = ("<p class='note'>Total input = input + cache read + cache write tokens. Output tokens: "
        "Claude Code transcripts do not record them correctly, so only Codex agents have them "
        "(Output column). For Codex, the cached input is part of the total input. Token counts are not costs; "
        "cache reads have a lower price per token. Wall time is active time: gaps over 30 min "
        "between transcript records (an agent resumed later) count as idle gaps, not work. "
        "Wall times in the summaries are sums, so parallel agents count separately. "
        "Code lines come from git (setting git_stats in roles.toml): commits made in the agent's active time, generated files "
        "excluded. A commit by the main agent or a parallel agent in that time also counts.</p>")


def role_of(r):
    return classify(r.get("description", ""), r.get("agent_type", ""), rules_for(r.get("project_dir")))


def rule_notes(rulesets):
    """HTML: where the role rules come from, plus any rule warnings (deduplicated)."""
    sources, warnings = [], []
    for rs in rulesets:
        sources += [x for x in rs["sources"] if x not in sources]
        warnings += [x for x in rs["warnings"] if x not in warnings]
    out = ("<p class='note'>Roles come from " + ", ".join(f"<code>{esc(x)}</code>" for x in sources)
           + ". The first matching rule wins; project rules come first.</p>")
    if warnings:
        out += ("<div class='warn'><b>Role rule warnings</b><ul>"
                + "".join(f"<li>{esc(w)}</li>" for w in warnings) + "</ul></div>")
    return out


def model_key(r):
    m = short_model(r.get("model", ""))
    f = family(m)
    return f"<span class='key' style='background:var(--m-{f})'></span>{esc(m)}"


def group_summary(rows, label, key, key_is_html=False):
    agg = defaultdict(lambda: [0, 0, 0, 0])
    for r in rows:
        a = agg[key(r)]
        a[0] += 1
        a[1] += num(r.get("total_input_tokens"))
        a[2] += num(r.get("wall_s"))
        a[3] = max(a[3], num(r.get("peak_context")))
    out = [f"<div class='wrap'><table><thead><tr><th>{label}</th><th class='num'>Agents</th>"
           "<th class='num'>Total input</th><th class='num'>Share</th><th class='num'>Wall min (sum)</th>"
           "<th class='num'>Max peak context</th></tr></thead><tbody>"]
    total = sum(a[1] for a in agg.values()) or 1
    for m, (n, t, w, p) in sorted(agg.items(), key=lambda kv: -kv[1][1]):
        share = 100 * t / total
        name = m if key_is_html else esc(m)
        out.append(f"<tr><td>{name}</td><td class='num'>{n}</td><td class='num'>{fmt_n(t)}</td>"
                   f"<td class='num bar'><span style='width:{share:.1f}%'></span><b>{share:.1f}%</b></td>"
                   f"<td class='num'>{fmt_min(w)}</td><td class='num'>{fmt_n(p)}</td></tr>")
    out.append("</tbody></table></div>")
    return "".join(out)


def summaries(rows, scope):
    rulesets = {id(rs): rs for rs in (rules_for(r.get("project_dir")) for r in rows)}.values()
    return (f"<h2>{scope} by model</h2>"
            + group_summary(rows, "Model", model_key, key_is_html=True)
            + f"<h2>{scope} by role</h2>" + rule_notes(rulesets)
            + group_summary(rows, "Role", role_of))


def write_page(path, title, body, script):
    page = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{PAGE_CSS}</style></head><body><main>"
            f"{body}</main><script>{SORT_JS}{script}</script></body></html>")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        f.write(page)
    os.replace(tmp, path)


def session_file(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown")
    return os.path.join(SESSIONS_DIR, f"{safe}.html")


def generated():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def render_session(session_id, rows):
    rows = sorted(rows, key=lambda r: r.get("started_at") or r.get("logged_at") or "", reverse=True)
    first = rows[0]
    title = session_title(first) or "(untitled session)"
    starts = sorted(r["started_at"] for r in rows if r.get("started_at"))
    span = f"{starts[0][:16].replace('T', ' ')} to {starts[-1][:16].replace('T', ' ')} UTC" if starts else ""
    body = [f"<p class='crumb'><a href='../index.html'>&larr; All sessions</a></p>",
            f"<h1>{esc(title)}</h1><p class='meta'>{esc(tool_name(first))} &middot; "
            f"project {esc(first.get('project', ''))} &middot; "
            f"session {esc(session_id)} &middot; {len(rows)} subagents &middot; {esc(span)}. "
            f"Generated {generated()} UTC.</p>", NOTE, summaries(rows, "This session")]

    metric_opts = "".join(f"<option value='{k}'>{v}</option>" for k, v in [
        ("total", "Total input tokens"), ("wall", "Wall time"), ("peak", "Peak context"),
        ("turn", "Longest turn"), ("calls", "API calls")])
    present = {family(short_model(r.get("model", ""))) for r in rows}
    legend = "".join(f"<span><span class='key' style='background:var(--m-{f})'></span>{f}</span>"
                     for f in MODEL_FAMILIES + ["other"] if f in present)
    body.append("<h2>Trends by role</h2><p class='note'>One chart per role. Each point is 1 agent; the "
                "x axis is the run order within the role (1 = first agent started). Each chart has "
                "its own y scale. The dot color is the model family. Hover a chart to see the agent.</p>"
                f"<div class='controls'><label>Metric <select id='metric'>{metric_opts}</select></label>"
                "<label>Filter <input id='q' type='search' placeholder='model, role, description...'></label>"
                f"<div class='legend'>{legend}</div></div><div id='charts'></div>")
    data = []
    for r in rows:
        if not r.get("started_at"):
            continue
        desc, model, role = r.get("description", ""), short_model(r.get("model", "")), role_of(r)
        data.append({
            "t": int(ts(r["started_at"]).timestamp() * 1000), "role": role, "desc": desc,
            "model": model, "family": family(model),
            "total": num(r.get("total_input_tokens")), "wall": num(r.get("wall_s")),
            "peak": num(r.get("peak_context")), "turn": num(r.get("longest_turn_s")),
            "calls": num(r.get("api_calls")), "search": f"{desc} {model} {role}".lower(),
            "code_added": num(r.get("code_added")), "code_deleted": num(r.get("code_deleted")),
            "lines_added": num(r.get("lines_added")), "lines_deleted": num(r.get("lines_deleted")),
            "commits": r.get("commits", ""),
        })
    data_json = json.dumps(data).replace("</", "<\\/")
    body.append(f"<script id='agent-data' type='application/json'>{data_json}</script>")
    role_extra = rules_for(first.get("project_dir"))["extra"]
    body.append(f"<script id='role-extra' type='application/json'>{json.dumps(role_extra)}</script>")

    body.append("<h2>Agents</h2><p class='meta'>Click a column header to sort. The filter above "
                "also applies to this table.</p>")
    git_on = rules_for(first.get("project_dir"))["settings"]["git_stats"]
    has_output = any(r.get("output_tokens") not in ("", None) for r in rows)
    cols = [c for c in COLS if (git_on or c[1] not in GIT_FIELDS)
            and (has_output or c[1] != "output_tokens")]
    head = "".join(f"<th class='{'num' if n else ''}'>{h}</th>" for h, _, n, _ in cols)
    trs = []
    for r in rows:
        tds = []
        for _, field, numeric, fmt in cols:
            v = (role_of(r) if field == "role" else r.get(field, "")) or ""
            cls = "num" if numeric else ("desc" if field == "description" else "")
            dv = f" data-v='{num(v)}'" if numeric else ""
            text = esc(fmt(v))
            if field == "model":
                text = f"<span class='key' style='background:var(--m-{family(fmt(v))})'></span>{text}"
            tds.append(f"<td class='{cls}'{dv}>{text}</td>")
        trs.append(f"<tr>{''.join(tds)}</tr>")
    body.append(f"<div class='wrap'><table id='agents' class='sortable'><thead><tr>{head}</tr></thead>"
                f"<tbody>{''.join(trs)}</tbody></table></div>")
    write_page(session_file(session_id), f"{title} usage", "".join(body), SESSION_JS)


def tool_name(row):
    return {"codex": "Codex"}.get(row.get("tool") or "claude", "Claude Code")


def render_index(by_session):
    all_rows = [r for rows in by_session.values() for r in rows]
    items = []
    for sid, rows in by_session.items():
        starts = sorted(r["started_at"] for r in rows if r.get("started_at"))
        fams = defaultdict(int)
        for r in rows:
            fams[family(short_model(r.get("model", "")))] += 1
        items.append((starts[-1] if starts else "", starts[0] if starts else "", sid, rows, fams))
    items.sort(reverse=True)
    trs = []
    for last, first, sid, rows, fams in items:
        title = session_title(rows[0]) or "(untitled session)"
        link = os.path.relpath(session_file(sid), OUT_DIR)
        total = sum(num(r.get("total_input_tokens")) for r in rows)
        mix = " ".join(f"<span><span class='key' style='background:var(--m-{f})'></span>{fams[f]}</span>"
                       for f in MODEL_FAMILIES + ["other"] if fams.get(f))
        trs.append(f"<tr><td><a href='{esc(link)}'>{esc(title)}</a></td><td>{esc(tool_name(rows[0]))}</td>"
                   f"<td>{esc(rows[0].get('project', ''))}</td>"
                   f"<td title='{esc(sid)}'>{esc(sid[:8])}</td><td>{esc(first[:16].replace('T', ' '))}</td>"
                   f"<td>{esc(last[:16].replace('T', ' '))}</td><td class='num' data-v='{len(rows)}'>{len(rows)}</td>"
                   f"<td class='num' data-v='{total}'>{fmt_n(total)}</td><td>{mix}</td></tr>")
    legend = "".join(f"<span><span class='key' style='background:var(--m-{f})'></span>{f}</span>"
                     for f in MODEL_FAMILIES + ["other"])
    body = (f"<h1>Subagent usage</h1><p class='meta'>{len(all_rows)} subagents in {len(items)} "
            f"session{'s' if len(items) != 1 else ''}. Generated {generated()} UTC from {esc(CSV_PATH)}.</p>"
            + NOTE
            + "<h2>Sessions</h2><p class='meta'>Newest activity first. Open a session for its charts "
            "and agent table.</p>"
            "<div class='wrap'><table class='sortable'><thead><tr><th>Session</th><th>Tool</th><th>Project</th>"
            "<th>Id</th><th>First agent (UTC)</th><th>Last agent (UTC)</th><th class='num'>Agents</th>"
            "<th class='num'>Total input</th><th>Agents by model</th></tr></thead>"
            f"<tbody>{''.join(trs)}</tbody></table></div>"
            f"<div class='controls'><div class='legend'>{legend}</div></div>"
            + summaries(all_rows, "All sessions"))
    write_page(os.path.join(OUT_DIR, "index.html"), "Subagent usage", body, "")


def render_html(session_id=None):
    """Regenerate the index and one session page (or all session pages)."""
    if not os.path.exists(CSV_PATH):
        return
    with open(CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))
    by_session = defaultdict(list)
    for r in rows:
        by_session[r.get("session_id", "")].append(r)
    for sid, srows in by_session.items():
        if session_id is None or sid == session_id:
            render_session(sid, srows)
    render_index(by_session)


def main():
    args = sys.argv[1:]
    if args in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    if args == ["--html"]:
        render_html()
        print(os.path.join(OUT_DIR, "index.html") if os.path.exists(CSV_PATH)
              else f"No data yet: {CSV_PATH} does not exist.")
        return 0
    if len(args) == 2 and args[0] == "--backfill":
        return backfill(args[1])
    if len(sys.argv) > 1:
        hook = {"agent_transcript_path": sys.argv[1]}
    else:
        hook = json.load(sys.stdin)
    if add_row(hook):
        render_html(hook.get("session_id", ""))
    return 0


HOOK_OK = '{"continue": true}'  # Codex needs JSON on stdout for SubagentStop; Claude Code accepts it


if __name__ == "__main__":
    hook_mode = len(sys.argv) == 1
    try:
        code = main()
    except Exception as e:  # never break the session
        print(f"subagent_usage: {e}", file=sys.stderr)
        code = 0
    if hook_mode:
        print(HOOK_OK)
    sys.exit(code or 0)
