"""Tests for scripts/subagent_usage.py. Run: python3 -m unittest discover -s tests"""
import csv
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "subagent_usage.py")


def load(tmp):
    """Import a fresh copy of the script with all paths inside tmp."""
    os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(tmp, "claude")
    os.environ.pop("SUBAGENT_USAGE_DIR", None)
    os.environ.pop("SUBAGENT_USAGE_CSV", None)
    spec = importlib.util.spec_from_file_location("subagent_usage", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rec(kind, t, **kw):
    r = {"type": kind, "timestamp": f"2026-01-01T{t}Z", "cwd": kw.pop("cwd", "/nonexistent/proj")}
    r.update(kw)
    return r


def assistant(t, rid, inp, cr, cc, tools=0, model="claude-opus-5-5"):
    content = [{"type": "tool_use", "name": "Bash", "input": {}} for _ in range(tools)]
    usage = {"input_tokens": inp, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}
    return rec("assistant", t, requestId=rid, message={"model": model, "content": content, "usage": usage})


def write_session(claude_dir, sid, agents):
    """agents: {agent_id: (description, agent_type, [records])}. Returns the subagents dir."""
    d = os.path.join(claude_dir, "projects", "-proj", sid, "subagents")
    os.makedirs(d)
    for aid, (desc, typ, records) in agents.items():
        with open(os.path.join(d, f"agent-{aid}.jsonl"), "w") as f:
            f.write("".join(json.dumps(r) + "\n" for r in records))
        with open(os.path.join(d, f"agent-{aid}.meta.json"), "w") as f:
            json.dump({"description": desc, "agentType": typ}, f)
    return d


BASIC = [
    rec("user", "10:00:00", message={"content": "do it"}),
    assistant("10:00:10", "r1", 10, 1000, 200, tools=1),
    assistant("10:00:10", "r1", 10, 1000, 200, tools=1),  # same request, second content block
    rec("user", "10:00:20", message={"content": []}),
    assistant("10:01:20", "r2", 5, 3000, 100),
    rec("assistant", "10:01:21", message={"model": "<synthetic>", "content": []}),
    # resumed 2 hours later: an idle gap, not active time
    rec("user", "12:01:21", message={"content": "again"}),
    assistant("12:01:31", "r3", 1, 4000, 0),
]


class AnalyzeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tokens_count_once_per_request(self):
        d = write_session(self.m.CLAUDE_DIR, "s1", {"a1": ("Implement Task 1: x", "general-purpose", BASIC)})
        r = self.m.analyze(os.path.join(d, "agent-a1.jsonl"))
        self.assertEqual(r["api_calls"], 3)
        self.assertEqual(r["input_tokens"], 16)
        self.assertEqual(r["cache_read_tokens"], 8000)
        self.assertEqual(r["cache_creation_tokens"], 300)
        self.assertEqual(r["total_input_tokens"], 8316)
        self.assertEqual(r["peak_context"], 4001)
        self.assertEqual(r["tool_calls"], 2)
        self.assertEqual(r["api_errors"], 1)

    def test_idle_gap_is_not_active_time(self):
        d = write_session(self.m.CLAUDE_DIR, "s1", {"a1": ("x", "general-purpose", BASIC)})
        r = self.m.analyze(os.path.join(d, "agent-a1.jsonl"))
        self.assertEqual(r["idle_gaps"], 1)
        self.assertEqual(r["wall_s"], 81 + 10)
        self.assertEqual(r["longest_turn_s"], 60)

    def test_no_git_repo_gives_zero_counts(self):
        d = write_session(self.m.CLAUDE_DIR, "s1", {"a1": ("x", "general-purpose", BASIC)})
        r = self.m.analyze(os.path.join(d, "agent-a1.jsonl"))
        self.assertEqual((r["commits"], r["code_added"]), ("", 0))


class GitStatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t")

        def git(*a, date=None):
            e = dict(env, GIT_AUTHOR_DATE=date or "", GIT_COMMITTER_DATE=date or "") if date else env
            subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True, env=e)

        git("init", "-q")
        with open(os.path.join(self.repo, "a.rs"), "w") as f:
            f.write("fn a() {}\nfn b() {}\n")
        with open(os.path.join(self.repo, "notes.md"), "w") as f:
            f.write("x\n")
        os.makedirs(os.path.join(self.repo, "target"))
        with open(os.path.join(self.repo, "target", "gen.rs"), "w") as f:
            f.write("generated\n" * 50)
        git("add", "-A")
        git("commit", "-q", "-m", "in window", date="2026-01-01T10:00:30Z")

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_code_and_skips_generated(self):
        from datetime import datetime
        t = lambda s: datetime.fromisoformat(f"2026-01-01T{s}+00:00").timestamp()
        out = self.m.git_stats(self.repo, [(t("10:00:00"), t("10:01:00"))])
        self.assertEqual((out["code_added"], out["lines_added"]), (2, 3))
        self.assertEqual(len(out["commits"].split()), 1)
        out = self.m.git_stats(self.repo, [(t("11:00:00"), t("11:01:00"))])
        self.assertEqual(out["commits"], "")


class RolesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def project(self, text):
        p = os.path.join(self.tmp.name, "proj")
        os.makedirs(os.path.join(p, ".claude"), exist_ok=True)
        with open(os.path.join(p, ".claude", "subagent-roles.toml"), "w") as f:
            f.write(text)
        return p

    def test_global_file_is_created_from_template(self):
        rs = self.m.rules_for(None)
        self.assertTrue(os.path.exists(self.m.ROLES_FILE))
        self.assertEqual(rs["warnings"], [])
        role = lambda d, t="general-purpose": self.m.classify(d, t, rs)
        self.assertEqual(role("Implement Task 3: parser"), "implementer")
        self.assertEqual(role("Review Task 3"), "task reviewer")
        self.assertEqual(role("Re-review Task 3"), "re-reviewer")
        self.assertEqual(role("Final review of the branch"), "final reviewer")
        self.assertEqual(role("Review the spec"), "spec reviewer")
        self.assertEqual(role("find files", "Explore"), "explorer")
        self.assertEqual(role("something else"), "other")
        self.assertEqual(rs["extra"]["implementer"], ["code_added"])

    def test_project_rules_come_first(self):
        p = self.project('[[role]]\nname = "analyst"\ndescription = \'\\blens\\b\'\n'
                         '[default]\nname = "misc"\n')
        rs = self.m.rules_for(p)
        self.assertEqual(self.m.classify("Tier 3 Lens: rounding", "", rs), "analyst")
        self.assertEqual(self.m.classify("Implement Task 1", "", rs), "implementer")
        self.assertEqual(self.m.classify("zzz", "", rs), "misc")

    def test_bad_rules_give_warnings(self):
        p = self.project('[[role]]\nname = "x"\ndescription = "("\n'
                         '[[role]]\ndescription = "a"\n'
                         '[[role]]\nname = "y"\ndescription = "y"\nextra_metrics = ["nope"]\n'
                         '[settings]\ngit_stats = "no"\ncolor = 1\n')
        rs = self.m.rules_for(p)
        text = "\n".join(rs["warnings"])
        for part in ("bad regex", "no name", "unknown extra metric", "must be bool", "unknown key"):
            self.assertIn(part, text)
        self.assertTrue(rs["settings"]["git_stats"])

    def test_broken_global_file_uses_template(self):
        os.makedirs(self.m.OUT_DIR, exist_ok=True)
        with open(self.m.ROLES_FILE, "w") as f:
            f.write("[[role]\n")
        rs = self.m.rules_for(None)
        self.assertIn("built-in rules", rs["sources"])
        self.assertEqual(self.m.classify("Implement Task 1", "", rs), "implementer")

    def test_git_stats_off_in_project(self):
        p = self.project("[settings]\ngit_stats = false\n")
        rs = self.m.rules_for(p)
        self.assertFalse(rs["settings"]["git_stats"])
        self.assertNotIn("implementer", rs["extra"])


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.m = load(self.tmp.name)
        self.d = write_session(self.m.CLAUDE_DIR, "sess-1", {
            "a1": ("Implement Task 1: x", "general-purpose", BASIC),
            "a2": ("Review Task 1", "general-purpose", BASIC),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        with open(self.m.CSV_PATH, newline="") as f:
            return list(csv.DictReader(f))

    def test_backfill_by_id_and_rerun_does_not_duplicate(self):
        self.assertEqual(self.m.backfill("sess-1"), 0)
        self.assertEqual(self.m.backfill("sess-1"), 0)
        rows = self.rows()
        self.assertEqual(sorted(r["agent_id"] for r in rows), ["a1", "a2"])
        self.assertEqual({r["session_id"] for r in rows}, {"sess-1"})
        self.assertTrue(os.path.exists(os.path.join(self.m.OUT_DIR, "index.html")))
        page = os.path.join(self.m.SESSIONS_DIR, "sess-1.html")
        with open(page) as f:
            html = f.read()
        self.assertIn("Implement Task 1: x", html)

    def test_backfill_by_session_file_path(self):
        path = os.path.dirname(self.d) + ".jsonl"
        self.assertEqual(self.m.backfill(path), 0)
        self.assertEqual(len(self.rows()), 2)

    def test_backfill_uses_the_main_session_cwd(self):
        with open(os.path.dirname(self.d) + ".jsonl", "w") as f:
            f.write(json.dumps({"type": "user", "cwd": "/main/project"}) + "\n")
        self.m.backfill("sess-1")
        self.assertEqual({r["project_dir"] for r in self.rows()}, {"/main/project"})

    def test_unknown_session(self):
        self.assertEqual(self.m.backfill("nope"), 1)

    def test_hook_payload_on_stdin(self):
        payload = {"session_id": "sess-1", "agent_id": "a1", "agent_type": "general-purpose",
                   "agent_transcript_path": os.path.join(self.d, "agent-a1.jsonl")}
        env = dict(os.environ)
        p = subprocess.run([sys.executable, "-I", SCRIPT], input=json.dumps(payload), text=True,
                           capture_output=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, "")
        self.assertEqual([r["agent_id"] for r in self.rows()], ["a1"])

    def test_hook_never_fails(self):
        p = subprocess.run([sys.executable, "-I", SCRIPT], input="not json", text=True,
                           capture_output=True)
        self.assertEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
