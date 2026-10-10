"""Checks that keep the tracker reliable: its instructions, its file format, its commands and its hooks, run end to end
in temporary git repos. Run: python3 -m unittest discover tests"""

from __future__ import annotations

import atexit
import contextlib
import html
import http.client
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = tempfile.mkdtemp()
atexit.register(shutil.rmtree, BASE, True)
os.environ["TRACKER_HOME"] = f"{BASE}/home"  # before the import: HOME is read once
os.environ["CLAUDE_CONFIG_DIR"] = f"{BASE}/claude"  # the running Claude sessions the viewer reads: not the user's
os.environ["TRACKER_VIEWER_PORT"] = "0"  # a free port, and no viewer started by a tracked session's hooks
# An agent session running the tests must not leak in: its tracker, its id, or that it is one.
for var in ("TRACKER", "TRACKER_SESSION", "CLAUDE_ENV_FILE", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID", "PLUGIN_ROOT"):
    os.environ.pop(var, None)
sys.path.insert(0, str(ROOT / "scripts"))
from tracker import cli, git, github, hooks, markdown, model, session, viewer, views, watcher  # noqa: E402


def slug() -> str:
    return f"t{time.monotonic_ns()}"


def repo(branch: str) -> Path:
    """A new git repo on `branch` with one commit."""
    path = Path(tempfile.mkdtemp(dir=BASE))
    for args in (["init", "-q", "-b", branch], ["config", "user.email", "me@x"], ["config", "user.name", "me"]):
        subprocess.run(["git", "-C", str(path), *args], check=True)
    commit(path, "start")
    return path


def commit(path: Path, msg: str) -> None:
    (path / f"{time.monotonic_ns()}.txt").write_text(msg)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", msg], check=True)


def run(*args: str, cwd: Path | None = None, code: int = 0) -> str:
    """One `tracker` command in this process; its output. `code` is the exit code it must give."""
    out, old = io.StringIO(), os.getcwd()
    os.chdir(cwd or old)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            cli.main(list(args))
        got = 0
    except SystemExit as exc:
        got = exc.code or 0
    finally:
        os.chdir(old)
        git.worktree.cache_clear()
    assert got == code, f"tracker {' '.join(args)} exited {got}:\n{out.getvalue()}"
    return out.getvalue()


SEQ_HEAD = [("step", "Step"), ("ticket", "Ticket"), ("status", "Status"), ("group", "Group"),
            ("priority", "Priority"), ("size", "Size"), ("waits", "← Waits on"),
            ("unblocks", "→ Unblocks")]  # the viewer's sequence heading buttons, as (sort key, label)


def seq_head(page: str) -> list[tuple[str, str]]:
    """The heading buttons of the viewer's sequence, as (sort key, label)."""
    head = re.search(r"<div class=seq-head>(.*?)</div>", page).group(1)
    return re.findall(r'<button type=button data-sort="(\w+)" data-said="[^"]+">([^<]+)</button>', head)


def piped(text: str):
    """stdin for a command given `-`, as a heredoc passes it."""
    return mock.patch.object(sys, "stdin", io.StringIO(text))


class InstructionsMatchTheCli(unittest.TestCase):
    """Every command the skills, the README, the templates, the hooks and the code give must exist, with its flags."""

    def test_commands_and_flags(self):
        sub = cli.build_parser()[1]
        texts = [*sorted((ROOT / "skills").glob("*/*.md")), ROOT / "README.md", ROOT / "hooks/hooks.json",
                 *sorted((ROOT / "scripts/tracker").glob("*.py")), *sorted((ROOT / "templates").glob("*.md"))]
        bad = []
        for path in texts:
            for span in re.findall(r"`([^`\n]+)`", path.read_text()):
                words = span.split()
                named = words[0] == "tracker"
                if named:
                    words = words[1:]
                if not words or words[0][0] in "<${-":  # a placeholder or a global flag: `tracker <command> --help`
                    continue
                cmd = sub.choices.get(words[0])
                if not cmd:
                    if named:
                        bad.append(f"{path.name}: `{span}`: no command {words[0]}")
                    continue
                known = {s for a in cmd._actions for s in a.option_strings} | {"--tracker"}
                bad += [f"{path.name}: `{span}`: {words[0]} has no {flag}"
                        for flag in re.findall(r"(?<![\w-])--[a-z][a-z-]*", span) if flag not in known]
        self.assertEqual(bad, [])


class Format(unittest.TestCase):
    def test_frontmatter_round_trip(self):
        for value in ["plain", "a: b", "[x]", '"q"', "# h", "x #y", " pad", "ünï", "", ["a", "b,c", 'd"e', "[f]"], []]:
            with self.subTest(value=value):
                line = markdown.render_frontmatter([], {"k": value})[0] if value != "" else "k: "
                meta, problems = markdown.parse_meta([line])
                self.assertEqual((meta["k"], problems), (value, []))

    def test_links_are_safe(self):
        html = viewer.md_inline('[a](javascript:alert(1)) [b](https://x.y/?q="1") <script> [c](evidence/r.txt)')
        self.assertNotIn("<script>", html)
        self.assertNotIn('href="javascript', html)
        self.assertIn('href="https://x.y/?q=&quot;1&quot;"', html)
        self.assertIn('href="evidence/r.txt"', html)
        # A bare URL inside a link's URL is not linked again: its quotes would end the href.
        html = viewer.md_inline("[t](https://a.b/-http://x/onmouseover=alert//) https://c.d")
        self.assertEqual(html.count('"'), 4)
        self.assertIn('<a href="https://c.d">https://c.d</a>', html)

    def test_html_escapes_text_and_keeps_markup(self):
        H = viewer.Html
        self.assertEqual(H("<b>{}</b>").format('<i>"&'), "<b>&lt;i&gt;&quot;&amp;</b>")
        self.assertEqual(H("<b>{}</b>").format(H("<i>")), "<b><i></b>")
        self.assertEqual(H(", ").join([H("<i>"), "<"]), "<i>, &lt;")
        self.assertEqual((H("<i>") + "<", "<" + H("<i>")), ("<i>&lt;", "&lt;<i>"))
        self.assertIsInstance(H("<i>").format() + "x" + H(""), H)
        self.assertEqual(viewer.attributes({"title": 'a"b', "open": True, "hidden": False, "x": None}),
                         ' title="a&quot;b" open')

    def test_the_page_escapes_every_field_once(self):
        s = slug()
        t = ("--tracker", s)
        bad = '<b x="1">&amp;'
        run("init", s, "--title", f"Work {bad}", "--owner", "me")
        run(*t, "new", "T-1", "--title", f"One {bad}", "--group", f"g{bad}")
        run(*t, "new", "T-2", "--title", "Two", "--depends", "T-1")
        run(*t, "set", "T-1", f"next=Do {bad}", "status=in-progress")
        run(*t, "add", "T-1", "link", f"Issue: [SC-1 {bad}](https://issues.example/1)")
        model.Tracker(model.HOME / s).lookup("T-1").save({"priority": bad})  # check refuses it; the page shows it
        page = viewer.main_html(model.Tracker(model.HOME / s))
        self.assertNotIn("<b x=", page)
        self.assertGreaterEqual(page.count("&lt;b x=&quot;1&quot;&gt;&amp;amp;"), 4)  # title, group, priority, next
        for markup in ("&lt;span", "&lt;a ", "&lt;/", "&lt;details", "&lt;p", "&lt;div", "&amp;lt;", "&amp;quot;"):
            self.assertNotIn(markup, page.replace("&lt;b x=&quot;1&quot;&gt;&amp;amp;", ""))

    def test_fork_prs_are_not_the_tickets(self):
        t = model.Record(Path("T-1.md"), "ticket", {"id": "T-1", "branch": "fix"}, "")
        fork = {"number": 9, "headRefName": "fix", "state": "OPEN", "isCrossRepository": True}
        own = {"number": 3, "headRefName": "fix", "state": "CLOSED", "isCrossRepository": False}
        self.assertIs(github.pick_pr([fork, own], t), own)
        self.assertIsNone(github.pick_pr([fork], t))

    def test_gh_budget(self):
        with mock.patch.object(github.subprocess, "run") as run_gh:
            github.budget(0)  # spent: no call starts, so a hook ends before its timeout
            self.assertIsNone(github.gh("pr", "list"))
            github.budget(None)
        run_gh.assert_not_called()


def ticket(root: Path, ident: str, **meta) -> None:
    meta = {"id": ident, "title": ident, "status": "todo", **meta}
    front = "\n".join(f"{k}: {markdown.format_value(v)}" for k, v in meta.items())
    (root / "tickets" / f"{ident}.md").write_text(f"---\n{front}\n---\n\n## Plan\n\n## Carry forward\n\n## Links\n")


class MachineState(unittest.TestCase):
    def setUp(self):
        self.root = model.HOME / slug()
        (self.root / "tickets").mkdir(parents=True)
        (self.root / "README.md").write_text("---\ntitle: T\nrepo: [a/x, b/y]\n---\n")
        ticket(self.root, "T-1", status="done", branch="f1", repo="a/x")
        ticket(self.root, "T-2", status="in-progress", branch="f2", repo="a/x")
        ticket(self.root, "T-3", status="in-progress", branch="f3", repo="a/x", pr_state="open", base="f2")

    def state(self, data: dict) -> dict:
        (self.root / ".state.json").write_text(json.dumps(data))
        return model.Tracker(self.root).state()

    def test_keys_per_repo(self):
        entries = {"a/x:main": 1, "b/y:main": 2, "legacy": 3}
        self.assertEqual(model.branch_entry(entries, "b/y", "main"), 2)
        self.assertEqual(model.branch_entry(entries, "A/X", "main"), 1)
        self.assertEqual(model.branch_entry(entries, "b/y", "legacy"), 3)  # a bare key stands for any repo
        self.assertIsNone(model.branch_entry(entries, "c/z", "main"))
        model.put_entry(entries, "b/y", "legacy", 4)
        self.assertEqual(entries, {"a/x:main": 1, "b/y:main": 2, "b/y:legacy": 4})

    def test_stale_entries_go(self):
        now, old = int(time.time()), int(time.time()) - 30 * 86400
        state = self.state({
            "handoff": {"a/x:f1": {"at": now}, "a/x:f2": {"at": old}, "b/y:f1": {"at": now},
                        "a/x:scratch": {"at": old}},
            "synced": {"a/x:f1": {"at": now}, "a/x:f2": {"at": old}, "a/x:main": {"at": old}, "a/x:gone": {"at": old}},
            "use": {"/w@main": ["T-1"], "/v@main": ["T-2", "T-1"]},
            "pr_match": {"a/x:f9": {"at": old}},
            "last_sync": now,
        })
        # f1's tickets are closed: its handoff goes; the same branch in another repo is not f1's ticket's.
        self.assertEqual(set(state["handoff"]), {"a/x:f2", "b/y:f1"})
        # A mark is a baseline: it goes by age alone, unless the branch has an open ticket (main: T-2, by `use`).
        self.assertEqual(set(state["synced"]), {"a/x:f1", "a/x:f2", "a/x:main"})
        self.assertEqual(state["use"], {"/v@main": ["T-2"]})
        self.assertNotIn("pr_match", state)
        self.assertEqual(state["last_sync"], now)

    def test_stacked_pr_waits_while_open(self):
        tr = model.Tracker(self.root)
        t3 = tr.lookup("T-3")
        self.assertEqual([d.ident for d in tr.deps(t3)], ["T-2"])
        self.assertEqual([t.id for t in tr.waiting_on("T-2")], ["T-3"])
        t3.save({"base": "main"})  # the PR was retargeted
        self.assertEqual(tr.deps(t3), [])


def pull(number: int, **facts) -> dict:
    """A PR as the review query answers it (github.REVIEW_FIELDS)."""
    return {"isDraft": facts.get("draft", False), "reviewDecision": facts.get("decision"),
            "mergeStateStatus": facts.get("merge", "CLEAN"),
            "reviewRequests": {"nodes": [{"requestedReviewer": {"login": x}} for x in facts.get("requested", [])]},
            "latestReviews": {"nodes": facts.get("reviews", [])},
            "reviewThreads": {"nodes": [{"isResolved": False}] * facts.get("threads", 0) + [{"isResolved": True}]},
            "commits": {"nodes": [{"commit": {"committedDate": facts.get("pushed", "2026-09-01T00:00:00Z"),
                                              "statusCheckRollup": {"state": facts.get("checks", "SUCCESS")}}}]}}


class Moves(unittest.TestCase):
    """Whose move a ticket under way waits on: read from its PR by `sync`, and from its blockers."""

    def test_review_facts(self):
        bot = {"author": {"__typename": "Bot", "login": "lint"}, "state": "CHANGES_REQUESTED",
               "submittedAt": "2026-09-02T00:00:00Z"}
        rev = {"author": {"__typename": "User", "login": "rev"}, "state": "CHANGES_REQUESTED",
               "submittedAt": "2026-09-02T00:00:00Z"}
        facts = github.review_facts(pull(1, decision="CHANGES_REQUESTED", reviews=[bot, rev, {"author": None}],
                                         threads=2, pushed="2026-09-03T00:00:00Z"))
        self.assertEqual(facts, {"decision": "changes_requested", "changes": ["rev"], "reviewed": 1788307200,
                                 "pushed": 1788393600, "checks": "success", "merge": "clean", "threads": 2})

    def test_rules(self):
        root = model.HOME / slug()
        for d in ("tickets", "decisions"):
            (root / d).mkdir(parents=True)
        (root / "README.md").write_text("---\ntitle: T\nrepo: a/x\n---\n")
        (root / "decisions" / "D-01.md").write_text("---\nid: D-01\ntitle: Store\nstatus: open\nowner: Ryan\n---\n")
        facts = {1: {"merge": "dirty", "checks": "failure"}, 2: {"checks": "failure"},
                 3: {"decision": "changes_requested", "changes": ["rev"], "reviewed": 100, "pushed": 50, "threads": 2},
                 4: {"decision": "changes_requested", "changes": ["rev"], "reviewed": 100, "pushed": 200},
                 5: {"decision": "changes_requested", "requested": ["rev"]},
                 7: {"decision": "approved", "approved": ["rev"], "threads": 1},
                 8: {"decision": "approved", "checks": "pending"}, 9: {"decision": "approved", "merge": "behind"},
                 10: {"draft": True}, 12: {"decision": "review_required"}}
        (root / ".state.json").write_text(json.dumps({"reviews": {f"a/x#{n}": f for n, f in facts.items()}}))
        for n in [*facts, 11]:
            ticket(root, f"M-{n}", status="in-progress", pr=str(n), pr_state="draft" if n == 10 else "open")
        ticket(root, "M-6", status="in-progress", depends_on=["D-01"])
        ticket(root, "M-13", status="in-progress", depends_on=["EXT-1"])
        with open(root / "tickets" / "M-13.md", "a") as f:
            f.write("\n- Blocker: [EXT-1 Legal](https://x.y) — legal must sign off\n")
        ticket(root, "M-14", status="in-progress")
        ticket(root, "M-15")
        tr = model.Tracker(root)
        moves = {t.id: (m.text() if (m := model.whose_move(tr, t)) else None) for t in tr.tickets}
        self.assertEqual(moves, {
            "M-1": "you: merge conflicts", "M-2": "you: checks failing",
            "M-3": "you: changes requested by rev (2 unresolved review threads)",
            "M-4": "you: pushed since rev requested changes; review not re-requested",
            "M-5": "rev: review requested", "M-6": "Ryan: D-01 open",
            "M-7": "you: approved by rev; 1 unresolved review thread", "M-8": "CI: checks running",
            "M-9": "you: approved; branch behind its base", "M-10": "you: draft", "M-11": None,
            "M-12": "you: no review requested", "M-13": "EXT-1: legal must sign off", "M-14": "you: no PR yet",
            "M-15": None})
        lines = views.move_lines(tr)
        self.assertEqual(lines[1:3], ["  you: merge conflicts — M-1", "  you: checks failing — M-2"])  # most urgent
        self.assertEqual(lines[-1], "  not read from GitHub yet (`tracker sync`) — M-11")

    def test_sync_reads_reviews(self):
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Reviewed", "--owner", "me", "--repo", "a/x")
        for i in (1, 2):
            run(*t, "new", f"S-{i}", "--title", f"Part {i}", "--branch", f"f{i}")
            run(*t, "set", f"S-{i}", "status=in-progress")
        lists = [{"number": 10 + i, "headRefName": f"f{i}", "state": "OPEN", "isDraft": i == 2, "mergedAt": None,
                  "baseRefName": "main", "updatedAt": ""} for i in (1, 2)]
        answers = {11: pull(11, requested=["rev"]), 12: pull(12, draft=True)}
        queries = []

        def fake_gh(*args, fields=None, partial=False):
            if args[:2] == ("pr", "list"):
                return lists
            queries.append(args[3])
            return {"data": {"r0": {f"p{n}": answers[n] for n in answers if f"p{n}:" in args[3]}}}

        with mock.patch.object(github, "gh", fake_gh):
            run(*t, "sync")
            self.assertEqual(len(queries), 1)  # the PRs were new: asked for once the lists named them
            tr = model.Tracker(model.HOME / s)
            self.assertEqual(set(tr.reviews), {"a/x#11", "a/x#12"})
            index = run(*t, "index")
            self.assertIn("  you: draft — S-2\n  rev: review requested — S-1", index)
            self.assertIn("move: rev — review requested", run(*t, "context", "S-1"))
            page = viewer.main_html(tr)
            self.assertIn('<span class="chip move s-you">you: draft</span>', page)
            self.assertIn("Now · 2 · 1 your move", page)

            seen = session.watch(tr, [tr.lookup("S-1")])
            answers[11] = pull(11, decision="APPROVED", reviews=[
                {"author": {"__typename": "User", "login": "rev"}, "state": "APPROVED", "submittedAt": ""}])
            run(*t, "sync")
            self.assertEqual(len(queries), 2)  # known PRs: asked for with the lists
            tr = model.Tracker(model.HOME / s)
            self.assertEqual(session.changes_since(tr, seen, session.watch(tr, [tr.lookup("S-1")])),
                             ["S-1 move now: you — approved by rev; ready to merge"])
        # Of the session's own tickets, only a move that becomes its own and a ticket's end are news.
        own = {"S-1": ["own", "in-progress", ""]}
        for now, said_ in ((["own", "in-review", ""], []), (["own", "merged", ""], ["S-1 is now merged"]),
                           (["own", "in-progress", "you — checks failing"], ["S-1 move now: you — checks failing"])):
            self.assertEqual(session.changes_since(tr, own, {"S-1": now}), said_)


    def test_stale_sync_refreshes_in_the_background(self):
        s, work = slug(), repo("feat/R-1")
        run("init", s, "--title", "Fresh", "--owner", "me", "--repo", "a/x")
        run("--tracker", s, "new", "R-1", "--title", "Fresh work", "--branch", "feat/R-1")
        m = session.match_cwd(work, tracker=model.Tracker(model.HOME / s))
        with mock.patch.object(hooks, "spawn") as spawn:
            hooks.refresh(m, {}, {})  # no ticket under way: nothing to learn
            run("--tracker", s, "set", "R-1", "status=in-progress", cwd=work)
            m = session.match_cwd(work, tracker=model.Tracker(model.HOME / s))
            fields = {}
            hooks.refresh(m, {}, fields)
            hooks.refresh(m, fields, {})  # asked already: not again within the interval
        spawn.assert_called_once_with("--tracker", s, "sync")


class Starts(unittest.TestCase):
    """Where a todo ticket's work can start: the default branch, or the branch of the work under way it waits on."""

    def test_start_points(self):
        s = slug()
        root = model.HOME / s
        for d in ("tickets", "decisions"):
            (root / d).mkdir(parents=True)
        (root / "README.md").write_text("---\ntitle: T\nrepo: a/x\n---\n")
        (root / "decisions" / "D-01.md").write_text("---\nid: D-01\ntitle: Store\nstatus: open\n---\n")
        ticket(root, "A-1", status="in-progress", branch="fa")
        ticket(root, "A-2", status="in-progress", branch="fb", pr="7", pr_state="open", base="fa")  # on A-1's branch
        ticket(root, "A-3", status="in-progress", branch="fa")
        ticket(root, "A-4", status="in-progress", branch="fc")
        ticket(root, "S-1", depends_on=["A-1"])
        ticket(root, "S-2", depends_on=["A-1", "A-2"])  # A-2's branch holds A-1's work: the top of the stack
        ticket(root, "S-3", depends_on=["A-1", "A-3"])  # one branch holds both
        ticket(root, "S-4", depends_on=["A-1", "A-4"])  # separate branches: someone chooses
        ticket(root, "R-1")
        ticket(root, "W-1", depends_on=["R-1"])  # waits on work not started
        ticket(root, "W-2", depends_on=["A-1", "D-01"])  # and on a decision
        tr = model.Tracker(root)
        starts = {t.id: (x.base.id if x.base else None, [b.id for b in x.apart]) for t, x in tr.startable()}
        self.assertEqual(starts, {"S-1": ("A-1", []), "S-2": ("A-2", []), "S-3": ("A-1", []),
                                  "S-4": (None, ["A-1", "A-4"]), "R-1": (None, [])})

        ready = run("--tracker", s, "ready")
        self.assertIn("S-1  —  S-1 — stack on fa (A-1 in-progress; you: no PR yet)\n", ready)
        self.assertIn("S-2  —  S-2 — stack on fb (A-2 in-review, #7)\n", ready)  # its PR's review not read yet
        self.assertIn("S-4  —  S-4 — A-1 (fa) and A-4 (fc) are on separate branches: stack on one, or wait", ready)
        self.assertIn("R-1  —  R-1 · unblocks W-1\n", ready)
        self.assertEqual([x.split()[0] for x in ready.splitlines()], ["R-1", "S-1", "S-2", "S-3", "S-4"])
        self.assertIn("Ready to start: R-1\nCan start stacked on work under way: S-1, S-3 on A-1; S-2 on A-2; S-4 on "
                      "A-1 or A-4", run("--tracker", s, "seq"))
        index = run("--tracker", s, "index")
        self.assertIn("[stacks on A-1] ", index)
        self.assertIn("[waits on R-1] ", index)
        self.assertIn("start: stack on fa (A-1 in-progress; you: no PR yet)", run("--tracker", s, "context", "S-1"))
        self.assertIn("blocked by: A-1, D-01", run("--tracker", s, "context", "W-2"))
        self.assertIn("<dt>start</dt><dd>stack on fa (A-1 in-progress; you: no PR yet)</dd>",
                      viewer.main_html(model.Tracker(root)))  # the opened ticket on the page

    def test_start_checks_the_branch(self):
        s, work = slug(), repo("main")
        t = ("--tracker", s)

        def branch(*args: str) -> None:
            subprocess.run(["git", "-C", str(work), "checkout", "-q", *args], check=True)

        run("init", s, "--title", "Stacked", "--owner", "me")
        run(*t, "new", "B-1", "--title", "Base")
        run(*t, "new", "B-2", "--title", "On main", "--depends", "B-1")
        run(*t, "new", "B-3", "--title", "On the base", "--depends", "B-1")
        branch("-b", "feat/B-1")
        commit(work, "base work")
        run(*t, "set", "B-1", "status=in-progress", cwd=work)
        self.assertIn("B-2  —  On main — stack on feat/B-1 (B-1 in-progress", run(*t, "ready"))
        branch("-b", "feat/B-2", "main")
        self.assertIn("B-2 waits on work under way that branch feat/B-2 does not contain: feat/B-1 (B-1)",
                      run(*t, "set", "B-2", "status=in-progress", cwd=work))
        branch("-b", "feat/B-3", "feat/B-1")
        self.assertNotIn("does not contain", run(*t, "set", "B-3", "status=in-progress", cwd=work))


class LiveSessions(unittest.TestCase):
    """The viewer shows the Claude sessions that run on this machine and that `tracker start` tied to the tracker."""

    def test_live_sessions(self):
        s, n = slug(), time.monotonic_ns()
        t = ("--tracker", s)
        work, spare = repo("feat/L-1"), repo("main")
        run("init", s, "--title", "Watched", "--owner", "me")
        run(*t, "new", "L-1", "--title", "Under way", "--branch", "feat/L-1")
        run(*t, "new", "L-2", "--title", "Chosen")
        run(*t, "set", "L-1", "status=in-progress", cwd=work)
        gone = subprocess.Popen(["true"])
        gone.wait()  # a process that no longer runs
        claude = session.CLAUDE_SESSIONS
        claude.mkdir(parents=True, exist_ok=True)

        since = time.mktime((2026, 9, 1, 9, 5, 0, 0, 0, -1))

        def running(name: str, pid: int, status: str = "busy", tracker: str = s, cwd: Path = work, **fields) -> None:
            sid = f"{name}{n}"
            (claude / f"{sid}.json").write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": "/", "name": name,
                                                            "status": status, "statusUpdatedAt": since * 1000}))
            if tracker:
                session.save_session(sid, tracker=tracker, cwd=str(cwd), **fields)

        running("agent-a", os.getpid())
        running("agent-b", os.getpid(), "idle", cwd=spare, focus=["L-2"])
        running("agent-c", gone.pid)  # stopped without removing its file
        running("agent-d", os.getpid(), tracker="other")
        running("agent-e", os.getpid(), tracker="")  # no `tracker start`
        (claude / f"bad{n}.json").write_text("{")
        tr = model.Tracker(model.HOME / s)
        self.assertEqual([x.name for x in session.live_sessions(s)], ["agent-a", "agent-b"])

        page = viewer.main_html(tr)
        self.assertRegex(page, r'data-id="_now-L-1"[^>]*><summary><span class="agent busy" role="img" '
                               r'aria-label="agent working" title="agent-a: working since Tue 09:05"></span>')
        self.assertRegex(page, rf'data-id="_now-agent-agent-b{n}"><summary><span class="agent" role="img" '
                               r'aria-label="agent idle" title="agent-b: idle since Tue 09:05"></span><b>agent-b</b>'
                               r'<span class=meta>main</span>'
                               r'<span class=nx>on <a class=id href="#L-2">L-2</a> todo</span>')
        self.assertIn(" · 2 agents</h2>", page)
        self.assertIn(f"<p class=meta>session agent-a: working since Tue 09:05, in {work}</p>", page)
        self.assertNotRegex(page, "agent-[cde]")

        before = viewer.version(tr).split(".")[0]
        running("agent-a", os.getpid(), "idle")  # the page swaps its content when a session's status changes
        self.assertNotEqual(viewer.version(tr).split(".")[0], before)


class Flow(unittest.TestCase):
    """The commands a session runs, from the plan to a paused branch, in a git repo on a ticket's branch."""

    def test_plan_decide_build_pause(self):
        s, work = slug(), repo("feat/T-2-api")
        t = ("--tracker", s)
        run("init", s, "--title", "Work", "--owner", "me")
        run(*t, "new", "T-1", "--title", "Schema")
        run(*t, "new", "T-2", "--title", "API", "--depends", "T-1")
        self.assertIn("dependency cycle", run(*t, "wait", "T-1", "on", "T-2", code=2))
        self.assertIn("Critical path (longest chain of unfinished tickets): T-1 → T-2", run(*t, "seq"))

        run(*t, "add", "T-1", "carry", "Table users(id) is the contract")
        run(*t, "add", "T-1", "carry", "Wrong fact")
        run(*t, "drop", "T-1", "carry", "Wrong")
        run(*t, "add", "T-1", "plan", "Make the users table")
        run(*t, "add", "T-1", "plan", "Make the users and roles tables", "--replace", "Make the users table")
        self.assertIn("Make the users and roles tables", run(*t, "context", "T-1"))
        self.assertIn("T-1: Table users(id) is the contract", run(*t, "context", "T-2"))
        self.assertNotIn("Wrong fact", run(*t, "context", "T-2"))

        self.assertIn("D-01 opened, blocks T-2", run(*t, "decide", "Auth scheme", "--blocks", "T-2"))
        done = run(*t, "step", "T-1", "--done", "users table", cwd=work)
        self.assertIn("T-1: done; feat/T-2-api not marked: T-1 is not on it", done)  # T-2's branch keeps its mark
        self.assertNotIn("nothing blocks", done)  # T-2 still waits on D-01
        self.assertIn("nothing blocks T-2 now", run(*t, "decide", "D-1", "--resolve", "JWT", "--by", "me"))
        context = run(*t, "context", "T-2")
        self.assertIn("D-01: Auth scheme → ", context)
        self.assertNotIn("Decided D-01", context)  # the log line says it again: left out

        run(*t, "set", "T-2", "status=in-progress", cwd=work)
        self.assertEqual(model.Tracker(model.HOME / s).lookup("T-2").get("branch"), "feat/T-2-api")
        run(*t, "synced", cwd=work)  # the first mark is the baseline: only later commits are new work
        commit(work, "api routes")
        step = run(*t, "step", "T-2", "Routes built", "--next", "Auth middleware", "--carry", "GET /users is public",
                   cwd=work)
        self.assertIn("T-2: next set, 1 carry forward, logged; feat/T-2-api up to date", step)
        self.assertIn("logged 1 commit(s)", step)
        run(*t, "step", "T-2", "--done", "api", "--pause", "half", cwd=work, code=2)  # --done leaves no handoff
        run(*t, "step", "T-2", "--pause", "routes done; auth half done", cwd=work)
        here = run(*t, "here", cwd=work)
        self.assertIn("Handoff", here)
        self.assertIn("routes done; auth half done", here)
        self.assertIn("T-1 done ✓", here)
        self.assertEqual(here.count("T-1: Table users(id) is the contract"), 1)  # what T-2 builds on, once
        self.assertIn("D-01: Auth scheme → ", here)
        self.assertNotIn("✗", run(*t, "check"))
        log = (model.HOME / s / "log.md").read_text()
        self.assertIn("[T-2] Routes built", log)
        self.assertIn("[T-2] Commits on feat/T-2-api:", log)
        t2 = model.Tracker(model.HOME / s).lookup("T-2")
        self.assertEqual((t2.get("next"), t2.carry_forward), ("Auth middleware", ["GET /users is public"]))
        tr = model.Tracker(model.HOME / s)  # the viewer's page, rendered without its server
        page = viewer.page_html(tr.title, viewer.main_html(tr), s, viewer.version(tr))
        self.assertIn('data-id="T-2"', page)
        self.assertFalse(viewer.code_ready())  # the code on disk is the code running: no restart
        self.assertIn("Decided D-01 Auth scheme (me): JWT", log)


class FileSafety(unittest.TestCase):
    """A tracker write changes only its intended file and keeps private files private."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=BASE))

    def link(self, path: Path, target: Path):
        try:
            path.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

    def test_atomic_write_ignores_the_old_temporary_name(self):
        outside, path = self.root / "outside", self.root / "record.md"
        outside.write_text("keep me")
        self.link(path.with_suffix(".md.tmp"), outside)
        model.atomic_write(path, "new ünicode\n")
        self.assertEqual(path.read_text(), "new ünicode\n")
        self.assertEqual(outside.read_text(), "keep me")
        self.assertTrue(path.with_suffix(".md.tmp").is_symlink())
        self.assertEqual(list(self.root.glob(".record.md.*.tmp")), [])

    def test_atomic_write_refuses_destination_symlinks(self):
        for exists in (False, True):
            with self.subTest(exists=exists):
                outside = self.root / f"outside-{exists}"
                if exists:
                    outside.write_text("keep me")
                path = self.root / f"record-{exists}.md"
                self.link(path, outside)
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    model.atomic_write(path, "replacement")
                self.assertTrue(path.is_symlink())
                self.assertEqual(outside.read_text() if exists else outside.exists(), "keep me" if exists else False)

    @unittest.skipIf(model.WINDOWS, "POSIX permission bits")
    def test_atomic_write_preserves_modes_and_creates_private_files(self):
        old_mask = os.umask(0o022)
        try:
            for mode in (None, 0o600, 0o640, 0o644):
                with self.subTest(mode=mode):
                    path = self.root / f"record-{mode}.md"
                    if mode is not None:
                        path.write_text("old")
                        path.chmod(mode)
                    with model.atomic_file(path) as f:
                        self.assertEqual(stat.S_IMODE(os.fstat(f.fileno()).st_mode), 0o600)
                        f.write(b"new")
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode if mode is not None else 0o600)
                    self.assertEqual(path.read_text(), "new")
        finally:
            os.umask(old_mask)

    def test_a_destination_swapped_for_a_symlink_does_not_redirect_the_write(self):
        outside, path = self.root / "outside", self.root / "record.md"
        outside.write_text("keep me")
        path.write_text("old")
        with model.atomic_file(path) as f:
            path.unlink()
            self.link(path, outside)
            f.write(b"replacement")
        self.assertFalse(path.is_symlink())
        self.assertEqual(path.read_text(), "replacement")
        self.assertEqual(outside.read_text(), "keep me")

    def test_failed_writes_keep_the_original_and_remove_temporary_files(self):
        path = self.root / "record.md"
        path.write_text("keep me")
        with self.assertRaises(ValueError):
            with model.atomic_file(path) as f:
                f.write(b"partial")
                raise ValueError("interrupted write")
        self.assertEqual(path.read_text(), "keep me")
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        with mock.patch.object(model.os, "replace", side_effect=PermissionError), \
                mock.patch.object(model.time, "sleep"), self.assertRaises(PermissionError):
            model.atomic_write(path, "replacement")
        self.assertEqual(path.read_text(), "keep me")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_attach_rejects_symlinks_even_with_force(self):
        s = slug()
        run("init", s, "--title", "Safe attachments", "--owner", "me")
        folder = model.HOME / s / "evidence"
        folder.mkdir()
        source = self.root / "source.txt"
        source.write_text("new")
        for exists in (False, True):
            outside = self.root / f"outside-{exists}"
            if exists:
                outside.write_text("keep me")
            name = f"report-{exists}.txt"
            self.link(folder / name, outside)
            for flags in ([], ["--force"]):
                with self.subTest(exists=exists, flags=flags):
                    out = run("--tracker", s, "attach", str(source), "--name", name, *flags, code=2)
                    self.assertIn("destination is a symlink", out)
                    self.assertEqual(outside.read_text() if exists else outside.exists(),
                                     "keep me" if exists else False)

    def test_attach_rejects_an_evidence_directory_outside_the_tracker(self):
        s = slug()
        run("init", s, "--title", "Safe directory", "--owner", "me")
        self.link(model.HOME / s / "evidence", self.root)
        source = self.root / "source.txt"
        source.write_text("keep me")
        out = run("--tracker", s, "attach", str(source), "--name", "copy.txt", code=2)
        self.assertIn("must stay inside the tracker", out)
        self.assertFalse((self.root / "copy.txt").exists())

    def test_attach_copies_binary_files_and_preserves_private_replacements(self):
        s = slug()
        run("init", s, "--title", "Copies", "--owner", "me")
        source = self.root / "source.bin"
        source.write_bytes(b"\x00\xff\r\n")
        run("--tracker", s, "attach", str(source))
        dest = model.HOME / s / "evidence/source.bin"
        self.assertEqual(dest.read_bytes(), source.read_bytes())
        if not model.WINDOWS:
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
        source.write_bytes(b"changed\x00")
        run("--tracker", s, "attach", str(source), "--force")
        self.assertEqual(dest.read_bytes(), source.read_bytes())
        if not model.WINDOWS:
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)


class Writes(unittest.TestCase):
    """What a write refuses, what it reports after itself, and the views that read records back."""

    def test_limits_check_and_show(self):
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Limits", "--owner", "me")
        run(*t, "new", "L-1", "--title", "One")
        long = "word " * 100
        self.assertIn("over 400: one concrete action", run(*t, "set", "L-1", f"next={long}", code=2))
        self.assertIn("the log line is", run(*t, "log", long, code=2))
        self.assertIn("over 600", run(*t, "add", "L-1", "carry", long * 2, code=2))
        log = (model.HOME / s / "log.md").read_text()
        run(*t, "step", "L-1", "Short", "--carry", "fits", "--carry", long * 2, code=2)
        self.assertEqual((model.HOME / s / "log.md").read_text(), log)  # refused before any write

        for i in range(6):
            run(*t, "add", "L-1", "carry", f"Fact {i}")
        done = run(*t, "step", "L-1", "--done", "all of it")
        self.assertIn("added `tracker check` problems:\n⚠ L-1: done but Carry forward has 6 bullets", done)
        self.assertEqual(done.count("Carry forward has 6"), 1)
        # The problem was there before. Each write ends by saying what `check` found, so no `check` follows it.
        self.assertTrue(run(*t, "set", "L-1", "title=One thing").endswith("\n`check`: no new problems\n"))

        run(*t, "new", "L-2", "--title", "Two")
        run(*t, "log", "about two", "--ref", "l-2")
        self.assertEqual(run(*t, "history", "--ref", "L-2").splitlines(),
                         [f"{model.today()} [L-2] Added L-2 Two", f"{model.today()} [L-2] about two"])
        self.assertEqual(run(*t, "history", "--last", "1"), f"{model.today()} [L-2] about two\n")
        self.assertEqual(run(*t, "history", "--since", "2999-01-01"), "no log lines\n")
        self.assertIn("about two", run(*t, "show", "log"))  # the log is no record, but `show log` reads it

        # The README takes sections after the required ones: `put` or `add` makes one, a part of its name finds it.
        with piped("- L-1 first: it fixes the gate."):
            self.assertIn("`check`: no new problems", run(*t, "put", "tracker", "why this order", "-"))  # a write
        run(*t, "add", "tracker", "order", "- L-2 next.")
        self.assertIn("## Why this order\n\n- L-1 first: it fixes the gate.\n- L-2 next.",
                      run(*t, "show", "tracker", "--section", "why"))
        self.assertIn("names none of L-1's sections", run(*t, "put", "L-1", "order", "x", code=2))

        # More text for an evidence file the tracker keeps: `attach --append`, not `cat >>`.
        made = Path(tempfile.mkdtemp(dir=BASE)) / "run.md"
        made.write_text("run 1\n")
        run(*t, "attach", str(made), "--ref", "L-1")
        made.write_text("other\n")
        self.assertIn("`--append -`", run(*t, "attach", str(made), code=2))
        with piped("## Run 2\n\nIt `works`."):
            self.assertIn("`check`: no new problems", run(*t, "attach", "run.md", "--ref", "L-1", "--append", "-"))
        self.assertEqual((model.HOME / s / "evidence/run.md").read_text(), "run 1\n\n## Run 2\n\nIt `works`.\n")
        self.assertEqual(run(*t, "show", "L-1", "--section", "links").count("evidence/run.md"), 1)
        self.assertIn("no file evidence/../README.md", run(*t, "attach", "../README.md", "--append", "x", code=2))

        # A subagent asks `context` for the tracker by name: it gets the open work, and how to read one record.
        whole = run("context", s)
        self.assertIn(f"{s} is a tracker; `tracker context <id>` takes one", whole)
        self.assertIn("L-2", whole)

        # The skill runs `tracker $ARGUMENTS`: a slash command's tracker name starts it, and `view` opens.
        commands = cli.build_parser()[1].choices
        self.assertEqual(cli.named_command([s], commands), ["start", s])
        self.assertEqual(cli.named_command(["--tracker", s, "view", "L-1"], commands), ["--tracker", s, "open", "L-1"])
        self.assertEqual(cli.named_command(["index", "--active"], commands), ["index", "--active"])
        self.assertEqual(cli.named_command(["frobnicate"], commands), ["frobnicate"])  # argparse refuses it
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": f"sid{time.monotonic_ns()}"}):
            self.assertIn(f"[work-tracker] {s} — Limits", run(s))

        shown = run(*t, "show", "L-1", "tracker", "--section", "carry,goal")
        self.assertIn("== L-1 · One thing · done\n## Carry forward\n\n- Fact 0", shown)
        self.assertIn("(no section 'goal')\n\n== README · Limits · planning\n(no section 'carry')\n\n## Goal", shown)
        whole = run(*t, "show", "l-01")
        self.assertIn("status: done", whole)
        self.assertNotIn("<!--", whole)

        run(*t, "decide", "Open one", "--refs", "L-1")
        run(*t, "decide", "Settled one", "--resolve", "yes", "--by", "me")
        listed = run(*t, "decisions")
        self.assertIn("Open one", listed)
        self.assertNotIn("Settled one", listed)
        self.assertIn("1 settled", listed)
        self.assertIn("Settled one", run(*t, "decisions", "--all"))

        path = model.HOME / s / "tickets" / "L-1.md"
        path.write_text(path.read_text().replace("## Carry forward", "## carry Forward"))  # a hand edit
        run(*t, "add", "L-1", "carry", "Fact 6")
        self.assertEqual(path.read_text().lower().count("## carry forward"), 1)  # found, not added again

        for name in ("..", f"../{Path(model.HOME).name}/{s}"):  # a slug is a folder in TRACKER_HOME, not a path
            self.assertIn("no tracker", run("--tracker", name, "index", code=2))
            self.assertIn("no tracker", run("context", f"{name}:L-1", code=2))



    def test_writes_log_what_history_needs(self):
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Logs", "--owner", "me")
        run(*t, "new", "G-1", "--title", "One")
        run(*t, "new", "G-2", "--title", "Two", "--depends", "G-1")
        run(*t, "new", "G-3", "--title", "Three", "--depends", "G-2")
        log = model.HOME / s / "log.md"
        def last() -> str:
            return log.read_text().splitlines()[-1].removeprefix(f"- {model.today()} ")

        # A todo ticket's Plan is a draft: it changes freely, and a change with no reason logs nothing.
        before = log.read_text()
        run(*t, "put", "G-1", "plan", "- build it")
        run(*t, "set", "G-1", "next=build it")
        self.assertEqual(log.read_text(), before)
        # A status change logs itself.
        run(*t, "set", "G-1", "status=in-progress")
        self.assertEqual(last(), "[G-1] G-1 in-progress: One")
        # A started ticket's Plan is the agreed plan: it changes only with a reason, which the log keeps.
        self.assertIn("--why", run(*t, "put", "G-1", "plan", "- build it twice", code=2))
        self.assertIn("--why", run(*t, "drop", "G-1", "plan", "build it", code=2))
        run(*t, "add", "G-1", "plan", "- build it twice", "--replace", "- build it", "--why", "the first build failed")
        self.assertEqual(last(), "[G-1] Plan changed: the first build failed")
        self.assertIn("- build it twice", run(*t, "show", "G-1", "--section", "plan"))
        run(*t, "add", "tracker", "context", "- Spec: [The spec](https://example.com/spec) — the rules",
            "--why", "the spec governs it")
        self.assertEqual(last(), "README Context changed: the spec governs it")

        # Any write that leaves a ticket unblocked says so.
        self.assertIn("nothing blocks G-3 now", run(*t, "wait", "G-3", "off", "G-2"))
        out = run(*t, "set", "G-1", "status=done", "summary=shipped")
        self.assertIn("nothing blocks G-2 now", out)
        self.assertEqual(last(), "[G-1] G-1 done: shipped")
        run(*t, "step", "G-2", "Tested by hand", "--done", "two delivered")
        self.assertEqual(log.read_text().splitlines()[-2:], [f"- {model.today()} [G-2] Tested by hand",
                                                             f"- {model.today()} [G-2] G-2 done: two delivered"])
        run(*t, "set", "tracker", "status=active")
        self.assertEqual(last(), "The work is active now")
    def test_put_stdin_and_the_tracker_folder(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/P-1")
        t = ("--tracker", s)
        run("init", s, "--title", "Put", "--owner", "me")
        run(*t, "new", "P-1", "--title", "One", "--branch", "feat/P-1")
        run(*t, "new", "P-2", "--title", "Two")
        run(*t, "new", "P-3", "--title", "Three", "--depends", "P-1,P-2")
        self.assertIn("ready to start", run(*t, "wait", "P-3", "off", "P-1,P-2"))  # a comma list, as elsewhere

        carry = '- `api/x.py` holds the "contract"\n  - $HOME stays as it is\n- A second fact\n'
        with piped(carry):
            run(*t, "put", "P-1", "carry")
        self.assertIn(carry.strip(), run(*t, "show", "P-1", "--section", "carry"))
        self.assertNotIn("P-1", run(*t, "check"))  # its sections keep their order
        with piped(""):
            self.assertIn("put takes the section's whole new text", run(*t, "put", "P-1", "plan", code=2))
        with piped("- Nonsense: [x](https://x.y)\n"):
            self.assertIn("label 'Nonsense'", run(*t, "put", "P-1", "links", "-", code=2))

        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run("start", s, cwd=work)  # keeps the session's project directory
            with piped('Fixed the "quoted" `thing`\n'):
                run("step", "-", "--next", "Test it", cwd=work)  # no id: the session's one ticket
            with piped("Ship it"):
                run("set", "next=-", cwd=work)
            self.assertIn("only one text", run("step", "P-1", "-", "--next", "-", cwd=work, code=2))
            commit(work, "more")
            folder = model.HOME / s
            self.assertIn("feat/P-1 up to date", run("synced", cwd=folder))  # from the tracker folder: the project's
        self.assertIn("run it from the project", run(*t, "synced", cwd=folder, code=2))  # no session to ask
        tr = model.Tracker(model.HOME / s)
        self.assertEqual(tr.lookup("P-1").get("next"), "Ship it")
        self.assertIn('[P-1] Fixed the "quoted" `thing`', (tr.root / "log.md").read_text())


class Upgrade(unittest.TestCase):
    def test_migrate_brings_an_older_format_up_to_date(self):
        s = slug()
        run("init", s, "--title", "Old", "--owner", "me")
        readme = model.HOME / s / "README.md"
        readme.write_text(readme.read_text().replace(f"schema: {model.SCHEMA}\n", ""))
        self.assertIn("run `tracker migrate`", run("--tracker", s, "check"))
        (model.HOME / s / ".DS_Store").write_text("finder")
        run("--tracker", s, "migrate")
        self.assertNotIn("migrate", run("--tracker", s, "check"))
        backup = next((model.HOME / ".backups").glob(f"{s}-*"))
        self.assertTrue((backup / "README.md").exists())
        self.assertFalse((backup / ".DS_Store").exists())  # the backup holds the tracker, not Finder's files

    def test_migrate_turns_priority_words_into_numbers(self):
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Old", "--owner", "me")
        tr = model.Tracker(model.HOME / s)
        tr.readme().save({"schema": 1})
        words = {"T-1": "High", "T-2": "p0", "T-3": "Lowest", "T-4": "Someday"}
        for ident, word in words.items():
            run(*t, "new", ident, "--title", ident)
            run(*t, "add", ident, "link", f"Issue: [SC-{ident[2]} Story](https://issues.example/{ident[2]})")
            run(*t, "issue", ident)
            model.Tracker(model.HOME / s).lookup(ident).save({"priority": word})
        self.assertIn("T-4: priority 'Someday' is not 0-4 — run `tracker migrate`", run(*t, "check", code=1))
        self.assertIn("no ticket's issue fields are to read", run(*t, "issue"))
        run(*t, "migrate")
        tr = model.Tracker(model.HOME / s)
        self.assertEqual([tr.lookup(x).meta.get("priority") for x in words], ["1", "0", "4", None])
        self.assertEqual(re.findall(r"^(T-\d+) ", run(*t, "issue"), re.M), ["T-4"])  # read again
        self.assertNotIn("priority", run(*t, "check"))


def hook(event: str, sid: str, cwd: Path, env: dict | None = None, **extra) -> dict | None:
    """Run scripts/hook.sh for one event; its JSON output, or None when it adds nothing."""
    data = json.dumps({"session_id": sid, "cwd": str(cwd), "hook_event_name": event, **extra})
    out = subprocess.run([str(ROOT / "scripts/hook.sh"), event], input=data, capture_output=True, text=True,
                         check=True, env={**os.environ, **(env or {})}).stdout
    return json.loads(out) if out.strip() else None


def said(out: dict | None) -> str:
    return json.dumps(out or {})


class Hooks(unittest.TestCase):
    """scripts/hook.sh end to end: its shell filters, and what each event tells the model."""

    def test_session(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/H-1")
        run("init", s, "--title", "Hooked", "--owner", "me")
        run("--tracker", s, "new", "H-1", "--title", "Hook work")
        run("--tracker", s, "set", "H-1", "status=in-progress", cwd=work)

        # No tracker in the session, on a default branch with an open ticket: the model offers the link; nothing else
        # runs.
        main = repo("main")
        run("--tracker", s, "new", "H-0", "--title", "Main work", "--branch", "main")
        env_file = Path(BASE) / f"{sid}.env"  # what the session's Bash commands get: its id, and the CLI on PATH
        path = f"{Path(sys.executable).parent}:/usr/bin:/bin"
        offer = hook("session-start", sid, main, env={"CLAUDE_ENV_FILE": str(env_file), "PATH": path}, source="startup")
        self.assertIn("host's question tool", said(offer))
        self.assertEqual(env_file.read_text(), f'export TRACKER_SESSION={sid}\nexport PATH="$PATH:{ROOT}/bin"\n')
        self.assertIsNone(hook("prompt", sid, main, prompt="hi"))
        self.assertIsNone(hook("stop", sid, main))
        run("start", "--decline", cwd=main)  # "Not now": no offer on this branch in the next sessions
        self.assertIsNone(hook("session-start", f"{sid}b", main, source="startup"))
        named = repo("feat/H-9-named")  # an id in the branch name only: not sure, so asked
        run("--tracker", s, "new", "H-9", "--title", "Named work")
        self.assertIn("host's question tool", said(hook("session-start", f"{sid}n", named, source="startup")))

        # A feature branch that a ticket's `branch` names: the session links at its start, no question.
        auto = said(hook("session-start", f"{sid}a", work, source="startup"))
        self.assertIn(f"Linked this session to {s}", auto)
        self.assertIn("Linked at session start", auto)
        self.assertIn("Tracker protocol", auto)
        self.assertEqual(session.load_session(f"{sid}a")["tracker"], s)
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": f"{sid}a"}):
            self.assertIn(f"this session is off {s} now", run("start", "--decline", cwd=work))
        self.assertNotIn("tracker", session.load_session(f"{sid}a"))
        self.assertIsNone(hook("session-start", f"{sid}c", work, source="startup"))  # declined: no link, no offer
        (model.HOME / ".declined.json").unlink()
        hook("session-start", f"{sid}w", work, source="startup")  # linked by itself; the user's watch wins
        self.assertIn(f"off {s} now: it watches", said(hook("prompt", f"{sid}w", work, prompt="/work-tracker:watch")))
        self.assertTrue(watcher.granted(f"{sid}w"))
        self.assertNotIn("tracker", session.load_session(f"{sid}w"))
        watcher.set_grant(f"{sid}w", False)

        env = {**os.environ, "TRACKER_SESSION": sid}
        linked = subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True,
                                text=True, check=True).stdout
        self.assertIn("H-1", linked)
        brief = said(hook("session-start", sid, work, source="startup"))
        self.assertIn(f"[work-tracker] {s}", brief)
        self.assertIn("Tracker protocol", brief)
        self.assertIn(model.ISOLATION_RULE, brief)  # stated once, carried into every session with a tracker
        self.assertIn(model.ISOLATION_RULE, run("rules"))
        self.assertIn("H-1 in-progress", said(hook("prompt", sid, work, prompt="go")))
        self.assertIsNone(hook("post-bash", sid, work, tool_input={"command": "ls"}))
        go = {"questions": [{"question": "Push?", "options": [{"label": "Yes, push"}, {"label": "Not now"}]}]}
        self.assertIsNone(hook("answered", sid, work, tool_input=go))  # approval, not direction
        pick = {"questions": [{"question": "Store?", "options": [{"label": "Postgres"}, {"label": "Yes, SQLite"}]}]}
        self.assertIn("direction decision", said(hook("answered", sid, work, tool_input=pick)))

        helper = said(hook("subagent-start", sid, work, agent_id="a1", agent_type="Explore"))
        self.assertIn("do not write it", helper)
        self.assertIn("`tracker index` for the whole work", helper)  # not `context <tracker name>`
        self.assertIn(model.ISOLATION_RULE, helper)  # a subagent writes code and commits too

        # The hooks log every commit; the model is asked only whether `next` still holds, once per `next`.
        committed = {"tool_input": {"command": "git commit -m x"}}
        commit(work, "hook work")
        self.assertIsNone(hook("post-bash", sid, work, agent_id="a1", **committed))  # a subagent's: logged, no ask
        ask = said(hook("prompt", sid, work, prompt="done?"))  # the session is asked at its next message
        self.assertIn("1 commit(s) on feat/H-1 since `next` last changed", ask)
        self.assertIsNone(hook("prompt", sid, work, prompt="again"))
        commit(work, "more work")
        self.assertIsNone(hook("post-bash", sid, work, **committed))  # logged; `next` is the one already asked about
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            self.assertIn("H-1: next set; feat/H-1 up to date", run("step", "--next", "Ship it", cwd=work))
        commit(work, "last work")  # as in the terminal: no post-bash hook sees it
        self.assertIsNone(hook("stop", sid, work))  # logs it, and never blocks
        self.assertIn("1 logged commit(s) since `next` last changed", run("--tracker", s, "here", cwd=work))
        self.assertIn("H-1 next: Ship it", said(hook("prompt", sid, work, prompt="and now?")))  # a new `next`
        log = (model.HOME / s / "log.md").read_text()
        self.assertEqual([x.split(": ", 1)[1].split(" ", 1)[1] for x in log.splitlines() if "Commits on" in x],
                         ["hook work", "more work", "last work"])  # each once

    def test_clear_keeps_the_tracker(self):
        s, sid, main = slug(), f"clr{time.monotonic_ns()}", repo("main")
        run("init", s, "--title", "Cleared", "--owner", "me")
        run("--tracker", s, "new", "K-1", "--title", "Kept work", "--branch", "main")
        run("--tracker", s, "new", "K-2", "--title", "Other work", "--branch", "main")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run("start", s, "--on", "K-2", cwd=main)
        hook("session-end", sid, main, reason="other")  # not a /clear: nothing handed over
        self.assertFalse(session.CLEARED_FILE.exists())
        hook("session-end", sid, main, reason="clear")
        elsewhere = said(hook("session-start", f"{sid}x", repo("main"), source="clear"))  # another directory: offered
        self.assertNotIn("stays on", elsewhere)
        self.assertEqual(session.load_session(f"{sid}x"), {})
        kept = said(hook("session-start", f"{sid}b", main, source="clear"))
        self.assertIn(f"This session stays on {s} after /clear", kept)
        self.assertIn("K-2", kept)
        self.assertEqual(session.load_session(f"{sid}b")["focus"], ["K-2"])  # its `--on` choice too
        self.assertFalse(session.CLEARED_FILE.exists())  # taken once
        hook("session-end", f"{sid}b", main, reason="clear")
        self.assertIn("host's question tool", said(hook("session-start", f"{sid}c", main, source="startup")))
        session.CLEARED_FILE.unlink()

    def test_codex_session(self):
        s, sid, work = slug(), f"codex{time.monotonic_ns()}", repo("feat/C-1")
        run("init", s, "--title", "Codex", "--owner", "me")
        run("--tracker", s, "new", "C-1", "--title", "Codex work", "--branch", "feat/C-1")
        env = {"CODEX_THREAD_ID": sid, "PLUGIN_ROOT": str(ROOT)}
        with mock.patch.dict(os.environ, env):
            self.assertIn("C-1", run("start", s, cwd=work))
            run("set", "C-1", "status=in-progress", cwd=work)
        started = said(hook("session-start", sid, work, env=env, source="resume"))
        self.assertIn(f"TRACKER_SESSION={sid} {ROOT}/bin/tracker", started)
        self.assertIn("C-1 in-progress", started)
        self.assertEqual(session.load_session(sid)["activity"]["status"], "idle")
        self.assertIn("C-1", said(hook("prompt", sid, work, env=env, prompt="work")))
        self.assertEqual(session.load_session(sid)["activity"]["status"], "busy")
        go = {"questions": [{"title": "Push?", "options": ["Yes, push", "Not now"]}]}
        self.assertIsNone(hook("answered", sid, work, tool_input=go))
        pick = {"questions": [{"title": "Store?", "options": ["Postgres", "SQLite"]}]}
        self.assertIn("direction decision", said(hook("answered", sid, work, tool_input=pick)))
        commit(work, "Codex commit")
        hook("post-bash", sid, work, env=env, tool_name="Bash", tool_input={"command": "git commit -m x"})
        self.assertIsNone(hook("stop", sid, work, env=env))
        self.assertEqual(session.load_session(sid)["activity"]["status"], "idle")
        self.assertEqual([x.sid for x in session.live_sessions(s)], [sid])
        self.assertEqual((model.HOME / s / "log.md").read_text().count("Codex commit"), 1)
        hook("session-end", sid, work, env=env)
        self.assertEqual(session.live_sessions(s), [])
        self.assertEqual(session.load_session(sid)["tracker"], s)  # a resume keeps its tracker

        # A fork's hook id can differ from its shell thread id: the command prefix carries the hook's id.
        other = {**env, "CODEX_THREAD_ID": f"fork{sid}"}
        self.assertIn(f"TRACKER_SESSION={sid}", said(hook("session-start", sid, work, env=other)))
        with mock.patch.dict(os.environ, {**other, "TRACKER_SESSION": sid}):
            self.assertEqual(session.session_id(), sid)
            self.assertIn("C-1", run("index", cwd=work))

    def test_codex_command_outside_a_repo(self):
        sid = f"codex{time.monotonic_ns()}"
        env = {"PLUGIN_ROOT": str(ROOT)}
        self.assertIn(f"TRACKER_SESSION={sid}", said(hook("session-start", sid, Path(BASE), env=env)))
        self.assertEqual(session.load_session(sid), {})  # command discovery does not link a tracker

    def test_edit_input_formats(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/E-1")
        run("init", s, "--title", "Edits", "--owner", "me")
        run("--tracker", s, "new", "E-1", "--title", "Edit work", "--branch", "feat/E-1")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run("start", s, cwd=work)
        root = model.HOME / s
        path = root / "tickets/E-1.md"
        cases = [(work, {"file_path": str(path)}),
                 (work, {"command": f"*** Begin Patch\n*** Update File: {path}\n*** End Patch"}),
                 (root, {"command": "*** Begin Patch\n*** Update File: tickets/E-1.md\n*** End Patch"})]
        for (cwd, tool), status in zip(cases, ("in-progress", "todo", "in-progress")):
            with self.subTest(tool=tool):
                before = session.load_session(sid)["seen"]
                path.write_text(re.sub(r"^status: .*", f"status: {status}", path.read_text(), flags=re.M))
                hook("edit", sid, cwd, tool_input=tool)
                self.assertNotEqual(session.load_session(sid)["seen"], before)
                self.assertNotIn("Changed since your brief", said(hook("prompt", sid, work, prompt="next")))

    def test_session_identity(self):
        cases = [({}, ""), ({"CODEX_THREAD_ID": "codex"}, "codex"),
                 ({"CLAUDE_CODE_SESSION_ID": "claude", "CODEX_THREAD_ID": "codex"}, "claude"),
                 ({"TRACKER_SESSION": "explicit", "CODEX_THREAD_ID": "codex"}, "explicit")]
        for env, expected in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(session.session_id(), expected)

    def test_recent_activity(self):
        sid, s, work = f"sid{time.monotonic_ns()}", slug(), repo("feat/A-1")
        session.session_activity(sid, "busy")
        self.assertEqual(session.load_session(sid), {})  # untracked sessions have no activity file
        session.save_session(sid, tracker=s, cwd=str(work))
        with mock.patch.object(session.time, "time", return_value=100):
            session.session_activity(sid, "busy")
        with mock.patch.object(session.time, "time", return_value=110):
            session.session_activity(sid, "busy")
            self.assertEqual(session.load_session(sid)["activity"], {"status": "busy", "since": 100, "at": 110})
            self.assertEqual(session.live_sessions(s)[0].since, 100)
        with mock.patch.object(session.time, "time", return_value=110 + session.ACTIVITY_MAX_AGE_S):
            self.assertEqual(session.live_sessions(s), [])
        session.session_activity(sid, "idle")
        native = session.CLAUDE_SESSIONS / f"{sid}.json"
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_text(json.dumps({"sessionId": sid, "pid": os.getpid(), "name": "native", "status": "busy"}))
        self.assertEqual([(x.name, x.status) for x in session.live_sessions(s)], [("native", "busy")])
        native.write_text(json.dumps({"sessionId": sid, "pid": -1}))
        self.assertEqual(session.live_sessions(s), [])  # a dead native process is not replaced by hook activity

    def test_project_modules_do_not_run(self):
        """Python starts in the user's project: its json.py must not replace the stdlib's in the hooks or the CLI."""
        work, ran = repo("feat/S-1"), Path(BASE) / f"ran{time.monotonic_ns()}"
        (work / "json.py").write_text(f"open({str(ran)!r}, 'w').close()\n")
        data = json.dumps({"session_id": f"sid{time.monotonic_ns()}", "cwd": str(work)})
        runs = [([str(ROOT / "scripts/hook.sh"), "session-start"], data), ([str(ROOT / "bin/tracker"), "rules"], ""),
                ([*model.CLI, "rules"], "")]  # the viewer and spawn() start the CLI so
        for cmd, stdin in runs:
            subprocess.run(cmd, input=stdin, cwd=work, capture_output=True, text=True)
            self.assertFalse(ran.exists(), cmd[0])

    def test_commits_wait_for_a_ticket(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/W-1")
        run("init", s, "--title", "Waits", "--owner", "me")
        run("--tracker", s, "new", "W-1", "--title", "Wait work", "--branch", "feat/W-1")
        env = {**os.environ, "TRACKER_SESSION": sid}
        subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True, check=True)
        commit(work, "early work")
        self.assertIsNone(hook("post-bash", sid, work, tool_input={"command": "git commit"}))  # no ticket under way
        self.assertIn("No ticket in progress on feat/W-1; 1 commit(s) not logged",
                      said(hook("prompt", sid, work, prompt="go")))
        run("--tracker", s, "set", "W-1", "status=in-progress", cwd=work)
        self.assertIsNone(hook("stop", sid, work))
        self.assertIn("[W-1] Commits on feat/W-1: ", (model.HOME / s / "log.md").read_text())  # logged for it now

    def test_commits_from_main_not_logged(self):
        """A merge from the default branch or a rebase onto it brings in work its own tickets logged: only the
        branch's own commits are logged."""
        for update in (["merge", "-q", "--no-edit", "main"], ["rebase", "-q", "main"]):
            s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("main")
            sh = lambda *a: subprocess.run(["git", "-C", str(work), *a], check=True, capture_output=True)  # noqa: E731
            sh("checkout", "-qb", "feat/M-1")
            run("init", s, "--title", "Merges", "--owner", "me")
            run("--tracker", s, "new", "M-1", "--title", "Merge work", "--branch", "feat/M-1")
            env = {**os.environ, "TRACKER_SESSION": sid}
            subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True, check=True)
            run("--tracker", s, "set", "M-1", "status=in-progress", cwd=work)
            self.assertIsNone(hook("stop", sid, work))  # the branch's mark
            commit(work, "own work")
            sh("checkout", "-q", "main")
            commit(work, "another ticket, merged on main")  # by this git user, after the mark
            sh("checkout", "-q", "feat/M-1")
            sh(*update)
            commit(work, "after the update")
            self.assertIsNone(hook("stop", sid, work))
            logged = [x.split(": ", 1)[1] for x in (model.HOME / s / "log.md").read_text().splitlines()
                      if "Commits on" in x]
            self.assertEqual([c.split(" ", 1)[1] for x in logged for c in x.split("; ")],
                             ["own work", "after the update"], update[0])

    def test_work_git_shows(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/G-1")
        run("init", s, "--title", "Git", "--owner", "me")
        run("--tracker", s, "new", "G-1", "--title", "Git work", "--branch", "feat/G-1")
        (work / "before.txt").write_text("uncommitted before the mark")
        env = {**os.environ, "TRACKER_SESSION": sid}
        subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True, check=True)
        self.assertIn("No ticket in progress", said(hook("prompt", sid, work, prompt="go")))  # the first message
        self.assertIsNone(hook("prompt", sid, work, prompt="more"))
        (work / "edited.txt").write_text("written by a Bash command: no hook saw it")
        told = said(hook("prompt", sid, work, prompt="again"))
        self.assertIn("No ticket in progress on feat/G-1; 1 uncommitted file(s) since", told)
        self.assertIsNone(hook("prompt", sid, work, prompt="and again"))  # once per mark
        self.assertIsNone(hook("edit", sid, work, tool_input={"file_path": str(work / "edited.txt")}))

        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):  # this session's own change: not reported
            run("set", "G-1", "status=in-progress", cwd=work)
        self.assertIsNone(hook("prompt", sid, work, prompt="4"))
        self.assertIn("G-1 in-progress; 1 uncommitted file(s)", said(hook("prompt", sid, work, prompt="5")))
        self.assertIn("The tracker may lag the work: 1 uncommitted file(s)", run("--tracker", s, "here", cwd=work))
        run("--tracker", s, "synced", cwd=work)
        self.assertNotIn("may lag", run("--tracker", s, "here", cwd=work))

    def test_one_ticket_of_a_busy_branch(self):
        s, sid, work = slug(), f"sid{time.monotonic_ns()}", repo("feat/busy")
        run("init", s, "--title", "Busy", "--owner", "me")
        for i in (1, 2, 3):
            run("--tracker", s, "new", f"B-{i}", "--title", f"Part {i}", "--branch", "feat/busy")
        for i in (1, 2):
            run("--tracker", s, "set", f"B-{i}", "status=in-progress", cwd=work)
        run("--tracker", s, "set", "B-3", "status=dropped", "summary=not needed")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            self.assertIn("More than one ticket is under way here", run("start", s, cwd=work))
            self.assertIn("this session works on B-1, B-2", run("step", "Built a part", cwd=work, code=2))
            self.assertIn("--on takes open tickets: B-3 is dropped", run("start", s, "--on", "B-3", cwd=work, code=2))
            chosen = run("start", s, "--on", "B-2", cwd=work)
            self.assertIn("B-2 in-progress; also B-1 in-progress", chosen)
            self.assertNotIn("More than one ticket", chosen)
            run("step", "Built part 2", "--next", "Test it", cwd=work)
            run("set", "next=Ship it", cwd=work)
        self.assertIn("B-2 in-progress (next: Ship it)", said(hook("prompt", sid, work, prompt="go")))
        tr = model.Tracker(model.HOME / s)
        self.assertEqual((tr.lookup("B-1").get("next"), tr.lookup("B-2").get("next")), ("", "Ship it"))
        self.assertIn("[B-2] Built part 2", (tr.root / "log.md").read_text())


class BriefSize(unittest.TestCase):
    """The brief goes into every session and comes back after each compaction: it says what this work needs, once and
    short, and points to the command that has the rest."""

    def test_the_brief_leaves_out_what_this_work_does_not_need(self):
        s, work = slug(), repo("feat/B-9")
        t = ("--tracker", s)
        run("init", s, "--title", "Brief", "--owner", "me")
        run(*t, "add", "tracker", "context", "Spec: [Build spec](https://docs.example/spec) — the rules it keeps")
        for i in range(1, 10):
            run(*t, "new", f"B-{i}", "--title", f"Work {i}", "--branch", "feat/B-9")
        for i in range(1, 8):
            run(*t, "set", f"B-{i}", "status=done", "summary=shipped")
        run(*t, "set", "B-9", "status=in-progress", cwd=work)
        tr = model.Tracker(model.HOME / s)
        tr.lookup("B-1").save({"colour": "red"})  # a warning on a ticket this session does not work on
        brief = run(*t, "here", cwd=work)
        self.assertIn("B-9 in-progress; also B-8 todo, 7 done", brief)  # closed ones as a count
        self.assertIn("Spec: Build spec — the rules it keeps", brief)
        self.assertIn("`tracker show tracker --section context` gives the links", brief)
        self.assertNotIn("https://docs.example/spec", brief)
        self.assertNotIn("Tracker check", brief)  # `check` has it, and the write that made it said so
        model.Tracker(model.HOME / s).lookup("B-9").save({"colour": "blue"})
        brief = run(*t, "here", cwd=work)
        self.assertIn("Tracker check, on this work:\n  ⚠ B-9: unknown frontmatter key 'colour'", brief)
        self.assertNotIn("B-1: unknown", brief)
        self.assertIn("https://docs.example/spec", run(*t, "show", "tracker", "--section", "context"))

        for i in (8, 9):  # the session's own ticket named first
            run(*t, "add", f"B-{i}", "link", f"Issue: [SC-{i} Story](https://issues.example/{i})")
        tr = model.Tracker(model.HOME / s)
        self.assertIn("Issue fields to read for 2 ticket(s): B-9, B-8.", views.issue_request(tr, [tr.lookup("B-9")]))
        due = run(*t, "issue")
        self.assertIn("B-8  https://issues.example/8", due)
        self.assertIn(views.ISSUE_HOW, due)


class SequenceSort(unittest.TestCase):
    """The viewer's sequence sorts by any column in the page (viewer/app.js); the server gives each row the values it
    sorts on and each column a sort button."""

    def test_rows_carry_their_sort_values(self):
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Work", "--owner", "me")
        run(*t, "new", "T-10", "--title", "Late", "--group", "ui")
        run(*t, "new", "T-2", "--title", "Base", "--group", "api")
        run(*t, "new", "T-3", "--title", "Uses base", "--depends", "T-2")
        run(*t, "new", "T-4", "--title", "Gone", "--group", "api")
        run(*t, "set", "T-4", "status=dropped", "summary=not needed")
        run(*t, "new", "T-5", "--title", "Uses that", "--depends", "T-3")
        page = viewer.main_html(model.Tracker(model.HOME / s))

        self.assertEqual(seq_head(page), SEQ_HEAD)

        def row(ident: str) -> dict[str, str]:
            attrs = re.search(rf'<details data-id="{ident}"([^>]*)>', page).group(1)
            return dict(re.findall(r'data-sort-(\w+)="([^"]*)"', attrs))

        # step: the depth-first dependency order the page starts in, a chain together and dropped last; status: its
        # place in todo → dropped.
        keys = ("step", "ticket", "group", "status", "waits", "unblocks")
        self.assertEqual([row("T-2")[k] for k in keys], ["0", "T-2", "api", "0", "0", "1"])
        self.assertEqual([row("T-3")[k] for k in keys], ["1", "T-3", "", "0", "1", "1"])
        self.assertEqual([row("T-5")[k] for k in keys], ["2", "T-5", "", "0", "1", "0"])
        self.assertEqual([row("T-10")[k] for k in keys], ["3", "T-10", "ui", "0", "0", "0"])
        self.assertEqual([row("T-4")[k] for k in keys], ["4", "T-4", "api", "5", "0", "0"])
        self.assertEqual(len(row("T-2")), len(SEQ_HEAD))  # a value per heading button

        # One Deps column: a line ← what the ticket waits on, a line → what it unblocks; the title names both lists.
        def deps(ident: str) -> str:
            return re.search(rf'<details data-id="{ident}".*?(<span class="deps lines hide-narrow" .*?)</summary>',
                             page).group(1)
        self.assertIn('title="waits on: T-2; unblocks: T-5"><span><span class=meta>←</span> ', deps("T-3"))
        self.assertIn('</span><span><span class=meta>→</span> <a class=id href="#T-5">T-5</a></span>', deps("T-3"))
        self.assertIn('title="unblocks: T-3"><span><span class=meta>→</span> <a class=id href="#T-3">', deps("T-2"))

    def test_rows_carry_the_graph(self):
        """viewer/app.js draws the dependency graph from each row's data: its step (its dot's column), its links and
        its state (its dot's colour, and its id's, by its status class)."""
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Work", "--owner", "me")
        run(*t, "new", "T-1", "--title", "Base")
        run(*t, "new", "T-2", "--title", "Uses base", "--depends", "T-1")
        run(*t, "new", "T-3", "--title", "Also uses base", "--depends", "T-1")
        run(*t, "new", "T-4", "--title", "Joins both", "--depends", "T-3,T-2")
        run(*t, "new", "T-5", "--title", "Alone")
        page = viewer.main_html(model.Tracker(model.HOME / s))

        def row(ident: str) -> dict[str, str]:
            attrs = re.search(rf'<details data-id="{ident}"([^>]*)>', page).group(1)
            return dict(re.findall(r'(class|data-step|data-links|data-dot)="([^"]*)"', attrs))
        self.assertEqual(row("T-1"), {"class": "t s-ready", "data-step": "1", "data-dot": "ready"})
        self.assertEqual(row("T-5"), {"class": "t s-ready", "data-step": "1", "data-dot": "ready"})
        for ident in ("T-2", "T-3"):
            self.assertEqual(row(ident),
                             {"class": "t s-blocked", "data-step": "2", "data-links": "T-1", "data-dot": "blocked"})
        self.assertEqual(row("T-4"),
                         {"class": "t s-blocked", "data-step": "3", "data-links": "T-3 T-2", "data-dot": "blocked"})
        self.assertRegex(page, r'<details data-id="T-4"[^>]*><summary><span class="graph" title="step 3"></span>')
        run(*t, "set", "T-1", "status=done")
        run(*t, "set", "T-2", "status=in-progress")
        run(*t, "set", "T-5", "status=dropped")
        page = viewer.main_html(model.Tracker(model.HOME / s))
        dots = dict(re.findall(r'<details data-id="(T-\d)"[^>]* data-dot="(\w+)"', page))
        self.assertEqual(dots, {"T-1": "closed", "T-2": "going", "T-3": "ready", "T-4": "blocked", "T-5": "dropped"})
        self.assertEqual(row("T-1")["class"], "t closed s-closed")

    def test_the_graph_draws_only_the_links_no_chain_implies(self):
        edges = {"A": [], "B": ["A"], "C": ["B", "A"], "D": ["C", "A", "B"], "E": ["E2"], "E2": ["E"]}  # E: a cycle
        self.assertEqual(viewer.drawn_links(edges),
                         {"A": [], "B": ["A"], "C": ["B"], "D": ["C"], "E": ["E2"], "E2": ["E"]})

    def test_an_unknown_status_still_renders(self):
        """A hand-edited status outside STAGES sorts after them, and the page shows the check's error."""
        s = slug()
        run("init", s, "--title", "Work", "--owner", "me")
        run("--tracker", s, "new", "T-1", "--title", "Odd")
        path = model.HOME / s / "tickets" / "T-1.md"
        path.write_text(re.sub(r"^status: .*", "status: wip", path.read_text(), flags=re.M))
        page = viewer.main_html(model.Tracker(model.HOME / s))
        self.assertRegex(page, rf'<details data-id="T-1"[^>]* data-sort-status="{len(model.STAGES)}"')
        self.assertIn("T-1: status &#x27;wip&#x27; not one of", page)


class IssueFields(unittest.TestCase):
    """A ticket's issue fields (priority, when its issue was created) come from its issue tracker through the model,
    which reads them with its own tool for that issue tracker and records them with `tracker issue`. The tracker says
    when they are due."""

    def tracker(self) -> tuple[str, tuple[str, str]]:
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Work", "--owner", "me")
        for ident, title in (("T-1", "Linked"), ("T-2", "Unlinked"), ("T-3", "Linked, closed")):
            run(*t, "new", ident, "--title", title)
        run(*t, "add", "T-1", "link", "Issue: [SC-1 Story one](https://issues.example/story/1)")
        run(*t, "add", "T-3", "link", "Issue: [SC-3 Story three](https://issues.example/story/3)")
        run(*t, "set", "T-3", "status=done", "summary=shipped")
        model.Tracker(model.HOME / s).lookup("T-3").save({"started_at": "2026-09-01T00:00:00Z"})  # it has a wait time
        return s, t

    def read_at(self, s: str, ident: str, at: float) -> None:
        tr = model.Tracker(model.HOME / s)
        state = tr.raw_state()
        state.setdefault("issues", {}).setdefault("read", {})[ident] = at
        tr.save_state(state)

    def test_records_fields_and_says_which_are_due(self):
        s, t = self.tracker()
        due = run(*t, "issue")
        self.assertIn("T-1  https://issues.example/story/1", due)  # never read
        self.assertIn("T-3  https://issues.example/story/3", due)  # closed, but never read: its creation time
        self.assertNotIn("T-2", due)  # no Issue link: nothing to read
        run(*t, "new", "T-9", "--title", "Closed before starts were recorded")
        run(*t, "add", "T-9", "link", "Issue: [SC-9 Story nine](https://issues.example/story/9)")
        run(*t, "set", "T-9", "status=done", "summary=shipped")
        self.assertNotIn("T-9", run(*t, "issue"))  # no start: its issue's creation time gives no span

        out = run(*t, "issue", "T-1", "--priority", "1", "--created", "2026-10-01T09:30:00.123+10:00")
        self.assertIn("T-1: priority=1, issue_created_at=2026-09-30T23:30:00Z", out)
        t1 = model.Tracker(model.HOME / s).lookup("T-1")
        self.assertEqual((t1.get("priority"), t1.get("issue_created_at")), ("1", "2026-09-30T23:30:00Z"))
        self.assertIn("T-3: read; nothing to record", run(*t, "issue", "T-3"))  # its issue has no fields to give
        self.assertIn("no ticket's issue fields are to read", run(*t, "issue"))

        self.read_at(s, "T-1", time.time() - 30 * 86400)  # read once is enough: no read again by age
        self.assertIn("no ticket's issue fields are to read", run(*t, "issue"))

        viewer.request_refresh(model.Tracker(model.HOME / s))  # the viewer's Refresh asks again for every open one
        self.assertEqual(re.findall(r"^(T-\d+) ", run(*t, "issue"), re.M), ["T-1"])

        run(*t, "issue", "T-1", "--priority", "")
        self.assertNotIn("priority", model.Tracker(model.HOME / s).lookup("T-1").meta)  # the issue lost its priority
        self.assertIn("has no Issue link", run(*t, "issue", "T-2", "--priority", "3", code=2))
        self.assertIn("needs a time zone", run(*t, "issue", "T-1", "--created", "2026-10-01T09:30:00", code=2))
        self.assertIn("not an ISO 8601 time", run(*t, "issue", "T-1", "--created", "last week", code=2))

    def test_priority_is_a_number_any_ticket_can_have(self):
        s, t = self.tracker()
        run(*t, "new", "T-5", "--title", "Urgent, no issue", "--priority", "0")
        run(*t, "set", "T-2", "priority=4")  # no Issue link: the model's own call
        tr = model.Tracker(model.HOME / s)
        self.assertEqual([model.level(tr.lookup(x), "priority") for x in ("T-5", "T-2", "T-1")], [0, 4, None])
        for bad in ("High", "5", "-1", "P1", "1.5"):
            self.assertIn("is not 0-4", run(*t, "set", "T-2", f"priority={bad}", code=2))
        self.assertIn("is not 0-4", run(*t, "new", "T-6", "--title", "Six", "--priority", "High", code=2))
        self.assertIn("is not 0-4", run(*t, "issue", "T-1", "--priority", "High", code=2))
        run(*t, "set", "T-2", "priority=")
        self.assertIsNone(model.level(model.Tracker(model.HOME / s).lookup("T-2"), "priority"))
        self.assertIn("Priority: 0 (most urgent) to 4 (least)", run(*t, "rules"))

    def test_size_is_a_number_any_ticket_can_have(self):
        s, t = self.tracker()
        run(*t, "new", "T-5", "--title", "Small, no issue", "--size", "2")
        run(*t, "set", "T-2", "size=5")  # no Issue link: the model's own estimate
        run(*t, "issue", "T-1", "--size", "3")
        tr = model.Tracker(model.HOME / s)
        self.assertEqual([model.level(tr.lookup(x), "size") for x in ("T-5", "T-2", "T-1", "T-3")], [2, 5, 3, None])
        for bad in ("M", "0", "6", "1.5"):
            self.assertIn("is not 1-5", run(*t, "set", "T-2", f"size={bad}", code=2))
        self.assertIn("is not 1-5", run(*t, "new", "T-6", "--title", "Six", "--size", "XL", code=2))
        self.assertIn("is not 1-5", run(*t, "issue", "T-1", "--size", "8", code=2))
        run(*t, "issue", "T-1", "--size", "")
        self.assertNotIn("size", model.Tracker(model.HOME / s).lookup("T-1").meta)  # the issue lost its estimate
        model.Tracker(model.HOME / s).lookup("T-2").save({"size": "Large"})
        self.assertIn("T-2: size 'Large' is not 1-5", run(*t, "check", code=1))
        rules = run(*t, "rules")
        self.assertIn("Size: 1 (XS) to 5 (XL)", rules)
        self.assertIn("leave it empty when you do not know it or cannot estimate it reliably", rules)

    def test_the_brief_and_a_refresh_ask_for_due_fields(self):
        s, t = self.tracker()
        sid, work = f"sid{time.monotonic_ns()}", repo("feat/T-1")
        run(*t, "set", "T-1", "status=in-progress", cwd=work)
        env = {**os.environ, "TRACKER_SESSION": sid}
        subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True, check=True)
        brief = said(hook("session-start", sid, work, source="startup"))
        self.assertIn("Issue fields to read for 2 ticket(s): T-1, T-3", brief)
        self.assertIn("never guess", brief)

        run(*t, "issue", "T-1", "--priority", "1")
        run(*t, "issue", "T-3")
        self.assertNotIn("Issue fields to read", said(hook("session-start", sid, work, source="startup")))
        self.assertNotIn("Issue fields to read", said(hook("prompt", sid, work, prompt="go")))

        viewer.request_refresh(model.Tracker(model.HOME / s))  # the viewer's Refresh, which cannot read issues itself
        self.assertIn("Issue fields to read for 1 ticket(s): T-1", said(hook("prompt", sid, work, prompt="next")))
        self.assertNotIn("Issue fields to read", said(hook("prompt", sid, work, prompt="again")))  # once per request

    def test_the_viewer_shows_priority_and_what_a_refresh_waits_on(self):
        s, t = self.tracker()
        run(*t, "new", "T-4", "--title", "Least", "--priority", "4")
        run(*t, "issue", "T-1", "--priority", "1")
        run(*t, "issue", "T-3")
        page = viewer.main_html(model.Tracker(model.HOME / s))
        self.assertEqual(seq_head(page), SEQ_HEAD)

        def p(ident: str) -> str:
            return re.search(rf'<details data-id="{ident}"[^>]* data-sort-priority="([^"]*)"', page).group(1)
        self.assertEqual((p("T-1"), p("T-2"), p("T-4")), ("1", "", "4"))
        self.assertRegex(page, r'data-id="T-1".*?<summary>.*?<span class="hide-narrow">P1</span>')
        run(*t, "set", "T-4", "size=5")
        page = viewer.main_html(model.Tracker(model.HOME / s))
        self.assertRegex(page, r'<details data-id="T-4"[^>]* data-sort-size="5"')
        self.assertRegex(page, r'data-id="T-4".*?<summary>.*?<span class="hide-narrow">XL</span>')

        self.assertRegex(page, r"<span hidden id=issue-state>issues read \d\d:\d\d</span>")
        viewer.request_refresh(model.Tracker(model.HOME / s))
        self.assertIn("<span hidden id=issue-state>issues: asked; no session on this tracker to read them</span>",
                      viewer.main_html(model.Tracker(model.HOME / s)))
        sid = f"live{time.monotonic_ns()}"  # a Claude session on this machine, tied to the tracker by `tracker start`
        session.CLAUDE_SESSIONS.mkdir(parents=True, exist_ok=True)
        (session.CLAUDE_SESSIONS / f"{sid}.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": sid, "cwd": "/", "name": "live", "status": "idle",
             "statusUpdatedAt": time.time() * 1000}))
        session.save_session(sid, tracker=s, cwd=BASE)
        self.assertIn("<span hidden id=issue-state>issues: asked; waiting for a session to read them</span>",
                      viewer.main_html(model.Tracker(model.HOME / s)))
        unlinked = slug()
        run("init", unlinked, "--title", "Plain", "--owner", "me")
        self.assertNotIn("issue-state", viewer.main_html(model.Tracker(model.HOME / unlinked)))

    def test_the_server_guards_every_request(self):
        s, _ = self.tracker()
        threading.Thread(target=viewer.serve, daemon=True).start()
        for _ in range(100):
            if viewer.VIEWER_FILE.exists():
                break
            time.sleep(0.05)
        port = json.loads(viewer.VIEWER_FILE.read_text())["port"]
        base = f"http://127.0.0.1:{port}/t/{s}"

        def post(headers: dict) -> int:
            req = urllib.request.Request(f"{base}/refresh", data=b"", method="POST", headers=headers)
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as err:
                return err.code

        page = urllib.request.urlopen(f"{base}/").read().decode()
        token = re.search(r'data-token="([^"]+)"', page).group(1)
        self.assertEqual(post({}), 403)
        self.assertEqual(post({"X-Tracker-Token": "wrong"}), 403)
        self.assertEqual(post({"X-Tracker-Token": token, "Sec-Fetch-Site": "cross-site"}), 403)
        self.assertIsNone(model.Tracker(model.HOME / s).raw_state().get("issues", {}).get("requested"))
        self.assertEqual(post({"X-Tracker-Token": token, "Sec-Fetch-Site": "same-origin"}), 204)
        self.assertGreater(model.Tracker(model.HOME / s).raw_state()["issues"]["requested"], time.time() - 60)

        # Every response is uncached; a GET has the same guard as a POST; a file from evidence/ runs no script.
        def get(path: str, headers: dict | None = None) -> tuple[int, dict[str, str], bytes]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                conn.request("GET", path, headers=headers or {})
                r = conn.getresponse()
                return r.status, dict(r.getheaders()), r.read()
            finally:
                conn.close()

        (model.HOME / s / "evidence").mkdir(exist_ok=True)
        (model.HOME / s / "evidence" / "r.txt").write_text("hi")
        code, headers, body = get(f"/t/{s}/evidence/r.txt")
        self.assertEqual((code, body, headers["Content-Security-Policy"], headers["Cache-Control"]),
                         (200, b"hi", "sandbox", "no-store"))
        code, headers, _ = get(f"/t/{s}")
        self.assertEqual((code, headers["Location"], headers["Content-Length"]), (301, f"/t/{s}/", "0"))
        self.assertEqual(get(f"/t/{s}/", {"Host": f"evil.example:{port}"})[0], 403)
        self.assertEqual(get(f"/t/{s}/", {"Host": f"localhost:{port}"})[0], 200)
        self.assertEqual(get(f"/t/{s}/", {"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(get("/t/no-such-tracker/")[0], 404)
        code, headers, _ = get(f"/t/{s}/")
        self.assertEqual((code, headers["Cache-Control"]), (200, "no-store"))
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

    def test_a_tracked_session_starts_the_viewer_when_none_runs(self):
        with mock.patch.object(viewer, "spawn") as spawn:
            viewer.keep_viewer()  # TRACKER_VIEWER_PORT=0: the hooks start none
            self.assertFalse(spawn.called)
            with mock.patch.object(viewer, "VIEWER_PORT", 7316):
                with mock.patch.object(viewer, "VIEWER_FILE", Path(BASE) / "no-viewer.json"):
                    viewer.keep_viewer()
                spawn.assert_called_once_with("serve", "--port", "7316")
                spawn.reset_mock()
                with socket.create_server(("127.0.0.1", 0)) as running:
                    file = Path(BASE) / "viewer.json"
                    file.write_text(json.dumps({"port": running.getsockname()[1], "pid": os.getpid()}))
                    with mock.patch.object(viewer, "VIEWER_FILE", file):
                        viewer.keep_viewer()
                self.assertFalse(spawn.called)


class Spans(unittest.TestCase):
    """A ticket's spans: wait (issue created → started) and cycle (started → PR merged). The tracker records the start
    itself, so cycle time needs no issue tracker. Each needs both times exact (UTC to the second) and in order."""

    def ticket(self, s: str, ident: str, created: str = "", started: str = "", merged: str = "",
               status: str = "done") -> None:
        t = ("--tracker", s)
        run(*t, "new", ident, "--title", f"Work {ident}", "--branch", f"f-{ident}")
        if created:
            run(*t, "add", ident, "link", f"Issue: [SC-{ident} Story](https://issues.example/{ident})")
            run(*t, "issue", ident, "--created", created)
        rec = model.Tracker(model.HOME / s).lookup(ident)
        rec.save({"status": status, **({"started_at": started} if started else {}),
                  **({"pr": "9", "pr_state": "merged", "merged_at": merged} if merged else {})})

    def test_set_records_the_first_start(self):
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Timed", "--owner", "me")
        run(*t, "new", "S-1", "--title", "Starts", "--branch", "f1")
        out = run(*t, "set", "S-1", "status=in-progress")
        self.assertNotIn("started_at", out)
        first = model.Tracker(model.HOME / s).lookup("S-1").get("started_at")
        self.assertLessEqual(abs(model.utc_seconds(first) - time.time()), 5)
        model.Tracker(model.HOME / s).lookup("S-1").save({"started_at": "2026-09-01T00:00:00Z"})
        run(*t, "set", "S-1", "status=todo")
        run(*t, "set", "S-1", "status=in-progress")  # started again: the first start stays
        self.assertEqual(model.Tracker(model.HOME / s).lookup("S-1").get("started_at"), "2026-09-01T00:00:00Z")
        self.assertIn("the tracker writes it", run(*t, "set", "S-1", "started_at=2026-09-02T00:00:00Z", code=2))
        # Started before 0.29, so in progress with no start: set in progress again, it gets none rather than now
        run(*t, "new", "S-2", "--title", "Started earlier", "--branch", "f2")
        path = model.HOME / s / "tickets" / "S-2.md"
        path.write_text(re.sub(r"^status: .*", "status: in-progress", path.read_text(), flags=re.M))
        run(*t, "set", "S-2", "status=in-progress")
        self.assertEqual(model.Tracker(model.HOME / s).lookup("S-2").get("started_at"), "")

    def test_sync_keeps_the_merge_time(self):
        s = slug()
        t = ("--tracker", s)
        run("init", s, "--title", "Timed", "--owner", "me", "--repo", "a/x")
        run(*t, "new", "S-1", "--title", "Merging now", "--branch", "f1")
        run(*t, "set", "S-1", "status=in-progress")
        lists = [{"number": 11, "headRefName": "f1", "state": "MERGED", "isDraft": False,
                  "mergedAt": "2026-10-01T06:30:00Z", "baseRefName": "main", "updatedAt": ""}]
        with mock.patch.object(github, "gh", lambda *args, fields=None, partial=False:
                               lists if args[:2] == ("pr", "list") else {"data": {}}):
            run(*t, "sync")
        self.assertEqual(model.Tracker(model.HOME / s).lookup("S-1").get("merged_at"), "2026-10-01T06:30:00Z")

    def test_the_span_lines(self):
        s = slug()
        run("init", s, "--title", "Timed", "--owner", "me")
        self.ticket(s, "S-1", "2026-09-01T00:00:00Z", "2026-09-01T02:00:00Z", "2026-09-01T08:00:00Z")
        self.ticket(s, "S-2", "2026-09-02T00:00:00Z", "2026-09-03T00:00:00Z", "2026-09-05T00:00:00Z")
        self.ticket(s, "S-3", "2026-10-01T00:00:00Z", "2026-10-01T00:10:00Z", "2026-10-01T12:10:00Z")  # recent
        self.ticket(s, "S-4", started="2026-10-02T00:00:00Z", merged="2026-10-02T00:30:00Z")  # no issue: cycle only
        # S-5 started exactly 7 days ago and merged by date only; S-7's issue was made after its start; S-9 merged
        # before its start
        self.ticket(s, "S-5", "2026-09-01T00:00:00Z", "2026-09-29T00:00:00Z", "2026-09-30")
        self.ticket(s, "S-6", "2026-09-20T00:00:00Z", "2026-09-21T00:00:00Z", status="in-progress")  # not merged
        self.ticket(s, "S-7", "2026-09-10T00:00:00Z", "2026-09-09T00:00:00Z", "2026-09-11T00:00:00Z")
        self.ticket(s, "S-8", "2026-09-01T00:00:00Z", "2026-09-01T01:00:00Z", "2026-09-01T02:00:00Z",
                    status="dropped")
        self.ticket(s, "S-9", "2026-09-01T00:00:00Z", "2026-09-10T00:00:00Z", "2026-09-09T00:00:00Z")
        tr = model.Tracker(model.HOME / s)
        h, d = 3600, 86400
        self.assertEqual({t.id: (model.span(t, "wait"), model.span(t, "cycle")) for t in tr.tickets},
                         {"S-1": (2 * h, 6 * h), "S-2": (d, 2 * d), "S-3": (600, 12 * h), "S-4": (None, 1800),
                          "S-5": (28 * d, None), "S-6": (d, None), "S-7": (None, 2 * d), "S-8": (None, None),
                          "S-9": (9 * d, None)})
        now = model.utc_seconds("2026-10-06T00:00:00Z")
        self.assertEqual(views.span_lines(tr, now), [
            "Wait time (issue created → started): median 24 h over 6 · fastest 10 min (S-3) · last 7 days: median "
            "14.0 d over 2",
            "Cycle time (started → PR merged): median 12 h over 5 · fastest 30 min (S-4) · last 7 days: median 6 h "
            "over 2"])
        self.assertEqual(views.span_lines(tr, now + 30 * d), [
            "Wait time (issue created → started): median 24 h over 6 · fastest 10 min (S-3)",
            "Cycle time (started → PR merged): median 12 h over 5 · fastest 30 min (S-4)"])
        empty = slug()
        run("init", empty, "--title", "Untimed", "--owner", "me")
        self.assertEqual(views.span_lines(model.Tracker(model.HOME / empty), now), [])
        self.assertEqual([views.duration(x) for x in (59, 60, 3599, 3600, 47 * 3600 + 3599, 48 * 3600, 100 * 3600)],
                         ["1 min", "1 min", "59 min", "1 h", "47 h", "2.0 d", "4.2 d"])

    def test_the_viewer_and_index_show_the_spans(self):
        s = slug()
        run("init", s, "--title", "Timed", "--owner", "me")
        self.ticket(s, "S-1", "2026-09-01T00:00:00Z", "2026-09-01T02:00:00Z", "2026-09-01T08:00:00Z")
        self.ticket(s, "S-2", "2026-09-01T00:00:00Z", status="todo")
        tr = model.Tracker(model.HOME / s)
        page = viewer.main_html(tr)
        self.assertEqual(seq_head(page), SEQ_HEAD)

        # The opened ticket's first line gives its times, those it has, then its summary.
        self.assertRegex(page, r'data-id="S-1".*?<dl class=props><dt>time</dt><dd>'
                               r'<span title="issue created → started">wait 2 h</span> · '
                               r'<span title="started → PR merged">cycle 6 h</span></dd>')
        self.assertNotIn("<dt>time</dt>", re.search(r'<details data-id="S-2".*?</details>', page, re.S).group())
        run("--tracker", s, "set", "S-1", "summary=Shipped the API")
        page = viewer.main_html(model.Tracker(model.HOME / s))
        self.assertRegex(page, r'data-id="S-1".*?<dl class=props><dt>time</dt><dd>.*?</dd>'
                               r'<dt>summary</dt><dd>Shipped the API</dd>')
        lines = views.span_lines(tr)
        self.assertEqual(len(lines), 2)
        self.assertIn("".join(f"<p class=lead>{html.escape(x)}</p>" for x in lines), page)
        self.assertEqual(run("--tracker", s, "index").splitlines()[1:3], lines)
        bare = slug()
        run("init", bare, "--title", "Untimed", "--owner", "me")
        self.assertNotIn("class=lead", viewer.main_html(model.Tracker(model.HOME / bare)))
        self.assertNotIn("Cycle time", run("--tracker", bare, "index"))


class Watch(unittest.TestCase):
    """`tracker watch`: what it reports, that a restart misses nothing and repeats nothing, and that only the user
    starts it."""

    def setUp(self):
        self.s = slug()
        self.t = ("--tracker", self.s)
        self.work = repo("feat/W-1")
        run("init", self.s, "--title", "Watched", "--owner", "me")
        for i in (1, 2, 3):
            run(*self.t, "new", f"W-{i}", "--title", f"Part {i}", *(["--depends", "W-1"] if i == 3 else []))
        self.root = model.HOME / self.s

    def watcher(self, sid: str = "") -> watcher.Watcher:
        w = watcher.Watcher(model.Tracker(self.root), sid)
        w.settle = 0  # a batch at once, not after the change settles
        return w

    def test_reports_what_changed(self):
        w, now = self.watcher(), time.time()
        first = w.claim(now)
        self.assertIn(f"watching {self.s}: 0 under way, 0 your move, 0 agent(s), 0 open decision(s)", first[0])
        self.assertIn("can start: W-1, W-2", first[-1])
        self.assertEqual(w.poll(now), [])

        run(*self.t, "set", "W-1", "status=in-progress", cwd=self.work)
        run(*self.t, "step", "W-1", "Built the parser", "--next", "Test it", cwd=self.work)
        model.append_log(model.Tracker(self.root), "Commits on feat/W-1: a1 one; b2 two; and 3 more", ["W-1"])
        run(*self.t, "decide", "Pick a store", "--refs", "W-2")
        lines = w.poll(now)
        w1 = next(x for x in lines if " W-1: " in x)
        for fact in ("todo → in-progress", "move: you: no PR yet", "Built the parser", "next: Test it", "+5 commits"):
            self.assertIn(fact, w1)
        self.assertNotIn("!", "".join(lines))  # the agent's own work: nothing needs the user
        self.assertIn("W-3: can start now: stack on feat/W-1", "".join(lines))
        self.assertIn("D-01: Opened D-01 Pick a store", "".join(lines))

        # A review comes back: the move is the user's.
        tr = model.Tracker(self.root)
        tr.lookup("W-1").save({"pr": "12", "pr_state": "open"})
        tr.save_state({**tr.raw_state(), "reviews": {"#12": {"decision": "changes_requested", "changes": ["rev"]}}})
        lines = w.poll(now)
        self.assertRegex(lines[0], r"^\d\d:\d\d ! W-1: in-progress → in-review · move: you: changes requested by rev$")
        self.assertEqual(w.poll(now), [])  # said once
        ticket(self.root, "W-9", depends_on=["W-99"])
        self.assertIn("! check: ", "".join(w.poll(now)))

    def test_restart(self):
        """A restart misses nothing and repeats nothing; one watcher per tracker."""
        w, now = self.watcher(), time.time()
        w.claim(now)
        w.release(now)
        run(*self.t, "set", "W-2", "next=Plan it")  # while no watcher runs
        again = self.watcher()
        caught = again.claim(now)
        self.assertEqual(len(caught), 1)
        self.assertIn("W-2: next: Plan it", caught[0])
        again.release(now)
        self.assertEqual(self.watcher().claim(now), [])  # nothing new
        self.assertIn("watching", self.watcher("other-session").claim(now)[0])  # another watcher starts afresh

        (self.root / "log.md").write_text("")  # rewritten by hand: nothing counts as new
        w = self.watcher("other-session")
        w.claim(now)
        self.assertEqual(w.poll(now), [])
        run(*self.t, "log", "A note", "--ref", "W-2")
        self.assertIn("W-2: A note", "".join(w.poll(now)))

        other = subprocess.Popen(["sleep", "30"])
        try:
            watcher.state_path(self.s).write_text(json.dumps({"pid": other.pid, "who": "terminal"}))
            with self.assertRaises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()):
                self.watcher().claim(now)
            self.assertEqual(refused.exception.code, watcher.REFUSED)  # one watcher per tracker
        finally:
            other.kill()
            other.wait()

    def test_agents(self):
        run(*self.t, "set", "W-1", "status=in-progress", cwd=self.work)
        n, now = time.monotonic_ns(), time.time()
        claude = session.CLAUDE_SESSIONS
        claude.mkdir(parents=True, exist_ok=True)

        def running(name: str, status: str, since: float) -> Path:
            sid = f"{name}{n}"
            f = claude / f"{sid}.json"
            f.write_text(json.dumps({"pid": os.getpid(), "sessionId": sid, "name": name, "status": status,
                                     "statusUpdatedAt": since * 1000}))
            session.save_session(sid, tracker=self.s, cwd=str(self.work))
            return f

        running("api", "busy", now - 10)
        running("ui", "idle", now - watcher.IDLE_S - 60)
        w = self.watcher()
        first = "\n".join(w.claim(now))
        self.assertIn("! W-1 in-progress · move: you: no PR yet · agent api busy 0 min · agent ui idle 11 min", first)
        self.assertEqual(w.poll(now), [])  # the summary said it: the agents on W-1, and that ui waits

        late = now + watcher.BUSY_S + 1
        self.assertIn("agent api: busy 45 min; nothing recorded for W-1 in 45 min", "".join(w.poll(late)))
        self.assertEqual(w.poll(late + 1), [])  # once per spell
        extra = running("web", "busy", late)
        told = "\n".join(w.poll(late))
        self.assertIn("agent web: started on W-1", told)
        self.assertIn("W-1: 3 agents on it: api, ui, web", told)
        extra.unlink()
        self.assertIn("agent web: ended on W-1", "".join(w.poll(late)))

    def test_github(self):
        s, now = slug(), time.time()
        run("init", s, "--title", "Synced", "--owner", "me", "--repo", "a/x")
        w = watcher.Watcher(model.Tracker(model.HOME / s))
        failed = ["sync skipped: gh failed or timed out for a/x"]
        with mock.patch.object(watcher, "sync", return_value=failed):
            w.claim(now)
            self.assertIn("! github: sync skipped: gh failed", "".join(w.poll(now)))
            self.assertEqual(w.poll(now + watcher.SYNC_S), [])  # said once
        with mock.patch.object(watcher, "sync", return_value=[]):
            self.assertEqual(w.poll(now + 2 * watcher.SYNC_S), [])  # skipped for its interval: not proof it works
            tr = model.Tracker(model.HOME / s)
            tr.save_state({"last_sync": now + 1})
            self.assertIn("github: sync works again", "".join(w.poll(now + 3 * watcher.SYNC_S)))

    def test_only_the_user_starts_it(self):
        sid, cwd = f"sid{time.monotonic_ns()}", self.work
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            self.assertIn("starts only when the user asks", run("watch", self.s, "--once", code=4))
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):  # a Claude session whose hooks did not run
            run("watch", self.s, "--once", code=4)
        self.assertIsNone(hook("prompt", sid, cwd, prompt="/watchlist"))
        self.assertFalse(watcher.granted(sid))

        self.assertIsNone(hook("prompt", sid, cwd, prompt=f"/work-tracker:watch {self.s}"))  # only the user types it
        self.assertTrue(watcher.granted(sid))
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            out = run("watch", self.s, "--once")
            self.assertIn(f"watching {self.s}: 0 under way", out)
            self.assertIn(f"Next: `tracker watch {self.s} --once` again", out)
            run(*self.t, "log", "a note", code=4)  # it watches only
            run("start", self.s, code=4)
        page = viewer.main_html(model.Tracker(self.root))
        self.assertIn("Watched by session sid", page)  # between two runs of `--once` it still watches

        self.assertIsNone(hook("prompt", sid, cwd, prompt="/work-tracker:watch stop"))
        self.assertFalse(watcher.granted(sid))
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run(*self.t, "log", "a note")
        st = watcher.load_state(self.s)
        watcher.state_path(self.s).write_text(json.dumps({**st, "ended": time.time() - watcher.REARM_S - 1}))
        self.assertRegex(viewer.main_html(model.Tracker(self.root)), r'class="watch ended">Watch by session sid')

        worker = f"sid{time.monotonic_ns()}"
        subprocess.run([str(ROOT / "bin/tracker"), "start", self.s], cwd=cwd, env={**os.environ,
                       "TRACKER_SESSION": worker}, capture_output=True, check=True)
        self.assertIn("works on tracker", said(hook("prompt", worker, cwd, prompt="/work-tracker:watch")))
        self.assertFalse(watcher.granted(worker))  # a session on a tracker does the work

    def test_codex_watch_grant(self):
        sid, cwd = f"codex{time.monotonic_ns()}", self.work
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": sid}):
            run("watch", self.s, "--once", code=4)
            hook("prompt", sid, cwd, prompt=f"$work-tracker:watch {self.s}")
            self.assertTrue(watcher.granted(sid))
            self.assertIn(f"watching {self.s}", run("watch", self.s, "--once"))
            run(*self.t, "log", "refused", code=4)
            hook("prompt", sid, cwd, prompt="$work-tracker:watch stop")
            self.assertFalse(watcher.granted(sid))
            run("watch", self.s, "--once", code=4)


class Delete(unittest.TestCase):
    """`tracker archive` and `delete`, and the viewer's tracker menu: an archived tracker leaves every list and lookup
    but the viewer's; a deleted one goes to the system's trash, only by the user; neither while a session or a watch
    is on it. The tests use the freedesktop trash in BASE, not the user's."""

    def setUp(self):
        self.s = slug()
        run("init", self.s, "--title", "Doomed", "--owner", "me")
        self.root = model.HOME / self.s
        self.trash = Path(BASE) / "xdg" / "Trash"
        patches = [mock.patch.object(watcher, "to_trash", model.xdg_trash),
                   mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.trash.parent)})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def live(self) -> str:
        """A running Claude session tied to the tracker."""
        sid = f"live{time.monotonic_ns()}"
        session.CLAUDE_SESSIONS.mkdir(parents=True, exist_ok=True)
        (session.CLAUDE_SESSIONS / f"{sid}.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": sid, "cwd": "/", "name": "busy-one", "status": "busy"}))
        session.save_session(sid, tracker=self.s, cwd=BASE)
        return sid

    def test_the_cli_moves_the_folder_to_the_trash(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            self.assertIn("only the user deletes a tracker", run("delete", self.s, "--yes", code=4))
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            self.assertIn("pass --yes", run("delete", self.s, code=2))
        self.assertIn("no tracker", run("delete", "no-such-tracker", "--yes", code=2))

        sid = self.live()
        self.assertIn("in use by agent session busy-one", run("delete", self.s, "--yes", code=4))
        self.assertTrue(self.root.exists())
        (session.CLAUDE_SESSIONS / f"{sid}.json").unlink()  # the session ended; its tie stays until the delete
        watcher.state_path(self.s).parent.mkdir(parents=True, exist_ok=True)
        watcher.state_path(self.s).write_text("{}")

        self.assertIn("to the trash", run("delete", self.s, "--yes"))
        self.assertFalse(self.root.exists())
        self.assertTrue((self.trash / "files" / self.s / "README.md").exists())
        info = (self.trash / "info" / f"{self.s}.trashinfo").read_text()
        self.assertIn(f"Path={self.root.absolute()}\n", info)
        self.assertFalse(watcher.state_path(self.s).exists())
        self.assertEqual(session.load_session(sid), {})

        run("init", self.s, "--title", "Doomed again", "--owner", "me")  # a second one of the name: its own entry
        run("delete", self.s, "--yes")
        self.assertTrue((self.trash / "files" / f"{self.s}.2" / "README.md").exists())
        self.assertTrue((self.trash / "info" / f"{self.s}.2.trashinfo").exists())

    def test_an_archived_tracker_leaves_every_list(self):
        sid = self.live()
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):  # its own session lets go of it
            self.assertIn(f"archived {self.s}", run("archive", self.s))
        self.assertEqual(session.load_session(sid), {})
        self.assertFalse(self.root.exists())
        self.assertTrue((model.ARCHIVE / self.s / "README.md").exists())
        self.assertNotIn(self.s, [t.slug for t in model.all_trackers()])
        self.assertIn("1 archived (`tracker list --archived`)", run("list"))
        self.assertIn(f"  {self.s}", run("list", "--archived"))
        hint = f"{self.s} is archived: `tracker unarchive {self.s}` brings it back"
        self.assertIn(hint, run("--tracker", self.s, "index", code=2))
        self.assertIn(hint, run("archive", self.s, code=2))
        self.assertIn(hint, run("init", self.s, "--title", "Again", "--owner", "me", code=2))

        self.assertIn("brought", run("unarchive", self.s))
        self.assertIn("Doomed", run("--tracker", self.s, "index"))
        self.assertIn("no archived tracker", run("unarchive", self.s, code=2))
        other = self.live()
        self.assertIn("in use by agent session busy-one", run("archive", self.s, code=4))
        (session.CLAUDE_SESSIONS / f"{other}.json").unlink()
        session.drop_session(other)

        run("archive", self.s)  # an archived tracker deletes too
        self.assertIn("to the trash", run("delete", self.s, "--yes"))
        self.assertFalse((model.ARCHIVE / self.s).exists())

    def test_the_viewer_lists_and_deletes(self):
        threading.Thread(target=viewer.serve, daemon=True).start()
        for _ in range(100):
            if viewer.VIEWER_FILE.exists():
                break
            time.sleep(0.05)
        port = json.loads(viewer.VIEWER_FILE.read_text())["port"]
        base = f"http://127.0.0.1:{port}"
        page = urllib.request.urlopen(f"{base}/t/{self.s}/").read().decode()
        token = re.search(r'data-token="([^"]+)"', page).group(1)

        def listed(group: str = "active") -> dict:
            got = json.loads(urllib.request.urlopen(f"{base}/trackers").read())
            return next((t for t in got[group] if t["slug"] == self.s), {})

        def delete(headers: dict, what: str = "delete") -> tuple[int, str]:
            req = urllib.request.Request(f"{base}/t/{self.s}/{what}", data=b"", method="POST", headers=headers)
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, r.read().decode()
            except urllib.error.HTTPError as err:
                return err.code, err.read().decode()

        self.assertEqual(listed(), {"slug": self.s, "title": "Doomed", "root": str(self.root), "in_use": ""})
        self.assertEqual(delete({})[0], 403)
        self.assertEqual(delete({"X-Tracker-Token": token, "Sec-Fetch-Site": "cross-site"})[0], 403)
        sid = self.live()
        self.assertEqual(listed()["in_use"], "agent session busy-one")
        code, said = delete({"X-Tracker-Token": token})
        self.assertEqual((code, said.split(":")[0]), (409, f"{self.s} is in use by agent session busy-one"))
        self.assertEqual(delete({"X-Tracker-Token": token}, "archive")[0], 409)
        (session.CLAUDE_SESSIONS / f"{sid}.json").unlink()

        # Archived: in its own list, its page read-only, then back; deleted from the archive.
        self.assertEqual(delete({"X-Tracker-Token": token}, "archive"), (204, ""))
        self.assertEqual((listed(), listed("archived")),
                         ({}, {"slug": self.s, "title": "Doomed", "root": str(model.ARCHIVE / self.s)}))
        page = urllib.request.urlopen(f"{base}/t/{self.s}/").read().decode()
        self.assertIn("<p id=archived>Archived:", page)
        self.assertEqual(delete({"X-Tracker-Token": token}, "refresh")[0], 404)
        self.assertEqual(delete({"X-Tracker-Token": token}, "archive"), (409, f"{self.s} is archived already"))
        self.assertEqual(delete({"X-Tracker-Token": token}, "unarchive"), (204, ""))
        self.assertTrue(listed())
        self.assertNotIn("id=archived", urllib.request.urlopen(f"{base}/t/{self.s}/").read().decode())
        self.assertEqual(delete({"X-Tracker-Token": token}, "archive"), (204, ""))
        self.assertEqual(delete({"X-Tracker-Token": token}), (204, ""))
        self.assertFalse((model.ARCHIVE / self.s).exists())
        self.assertEqual((listed(), listed("archived")), ({}, {}))
        self.assertEqual(delete({"X-Tracker-Token": token})[0], 404)



class Actions(unittest.TestCase):
    """A user's action: a task the agent cannot or should not do. The agent adds it with `tracker act`; the user's
    word closes it, from the CLI or the viewer's Done and Drop. Open ones show above Now and in the brief; closed
    ones go to Reference."""

    def setUp(self):
        self.s = slug()
        self.t = ("--tracker", self.s)
        run("init", self.s, "--title", "Work", "--owner", "me")
        run(*self.t, "new", "T-1", "--title", "Schema")
        self.root = model.HOME / self.s

    def tr(self) -> model.Tracker:
        return model.Tracker(self.root)

    def test_an_action_is_added_noted_and_closed(self):
        out = run(*self.t, "act", "--refs", "T-1", "Ask Sam whether the billing API keeps v1", "--note", "T-1 needs it")
        self.assertIn("A-01 added", out)
        a = self.tr().action("a-1")
        self.assertEqual((a.get("status"), a.list("refs")), ("open", ["T-1"]))
        self.assertIn("- T-1 needs it\n", a.body)
        run(*self.t, "act", "A-1", "--note", "Reply: v1 stays", "--note", "Reply: until March")
        self.assertIn("- T-1 needs it\n- Reply: v1 stays\n- Reply: until March", run(*self.t, "show", "A-01"))
        self.assertIn(f"- {model.today()} [A-01 T-1] Updated A-01: Reply: v1 stays; Reply: until March",
                      (self.root / "log.md").read_text())
        self.assertIn("over 400", run(*self.t, "act", "A-01", "--note", "x " * 210, code=2))
        out = run(*self.t, "act", "Ask Sam if the billing API keeps v1", code=3)
        self.assertIn("similar open action: A-01", out)
        run(*self.t, "act", "Get read access to the prod DB", "--refs", "T-9", code=2)
        run(*self.t, "act", "Get read access to the prod DB")
        listed = run(*self.t, "actions").splitlines()
        self.assertEqual([x.split()[0] for x in listed], ["A-01", "A-02", "`tracker"])
        self.assertEqual(listed[-1], "`tracker show A-<n>` prints one with its notes")

        out = run(*self.t, "act", "A-01", "--done", "--note", "Sam: v1 stays")
        self.assertIn("A-01 done", out)
        self.assertIn("[A-01 T-1] A-01 done: Ask Sam whether the billing API keeps v1 → Sam: v1 stays",
                      (self.root / "log.md").read_text())
        self.assertIn("A-01 is done already", run(*self.t, "act", "A-01", "--drop", code=2))
        run(*self.t, "act", "A-02", "--drop")
        listed = run(*self.t, "actions")
        self.assertIn("no open actions", listed)
        self.assertIn("2 closed", listed)
        self.assertIn("A-02  dropped", run(*self.t, "actions", "--all"))
        self.assertEqual(cli.check(self.tr()), ([], ["README.md: ## Context lists nothing"]))
        self.assertIn("Actions: An action is a task for the user", run("rules"))

    def test_the_brief_lists_open_actions_and_asks_about_old_ones(self):
        run(*self.t, "act", "Ask Sam whether the billing API keeps v1", "--refs", "T-1")
        run(*self.t, "act", "Get read access to the prod DB")
        run(*self.t, "act", "A-02", "--done")
        self.tr().action("A-01").save({"created_at": "2026-01-01T09:00:00Z"})
        index = run(*self.t, "index")
        self.assertIn("Open actions for the user", index)
        self.assertRegex(index, r"A-01 \(open \d+ days: ask the user whether it is done\): Ask Sam .* \(T-1\)")
        self.assertNotIn("A-02", index)
        self.assertIn('`act "<what, with whom>" --refs <ids>`', views.protocol(self.s))

    def test_the_viewer_shows_them_and_closes_one(self):
        run(*self.t, "act", "Ask Sam whether the billing API keeps v1", "--refs", "T-1")
        run(*self.t, "act", "Get read access to the prod DB")
        page = viewer.main_html(self.tr())
        self.assertLess(page.index('data-id="_sec-actions"'), page.index('data-id="_sec-seq"'))
        self.assertIn('data-close="done" data-ref="A-01"', page)
        self.assertNotIn("_closed-actions", page)

        threading.Thread(target=viewer.serve, daemon=True).start()
        for _ in range(100):
            if viewer.VIEWER_FILE.exists():
                break
            time.sleep(0.05)
        base = f"http://127.0.0.1:{json.loads(viewer.VIEWER_FILE.read_text())['port']}/t/{self.s}"
        token = re.search(r'data-token="([^"]+)"', urllib.request.urlopen(f"{base}/").read().decode()).group(1)

        def close(ident: str, what: str, headers: dict) -> int:
            req = urllib.request.Request(f"{base}/actions/{ident}/{what}", data=b"", method="POST", headers=headers)
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as err:
                return err.code

        self.assertEqual(close("A-01", "done", {}), 403)
        self.assertEqual(close("A-01", "done", {"X-Tracker-Token": token}), 204)
        self.assertEqual(close("A-01", "drop", {"X-Tracker-Token": token}), 409)
        self.assertEqual(close("A-09", "done", {"X-Tracker-Token": token}), 409)
        self.assertEqual(close("A-02", "finish", {"X-Tracker-Token": token}), 404)
        self.assertEqual(close("A-02", "drop", {"X-Tracker-Token": token}), 204)
        self.assertEqual([a.get("status") for a in self.tr().actions], ["done", "dropped"])
        page = viewer.main_html(self.tr())
        self.assertNotIn("_sec-actions", page)
        self.assertIn("<b>Closed actions</b><span class=meta>2, newest first</span>", page)

    def test_a_due_day_orders_them_and_shows_in_the_row(self):
        run(*self.t, "act", "Send the deck to Kim")
        run(*self.t, "act", "Ask Sam whether the billing API keeps v1", "--due", "2026-13-01", code=2)
        run(*self.t, "act", "Ask Sam whether the billing API keeps v1", "--due", "2099-01-15")
        run(*self.t, "act", "Book the review with the design team", "--due", "2020-01-02")
        self.assertEqual([a.id for a in self.tr().open_actions()], ["A-03", "A-02", "A-01"])
        self.assertIn("A-02  open     Ask Sam whether the billing API keeps v1 · due 2099-01-15",
                      run(*self.t, "actions"))
        self.assertIn("A-03 (due 2020-01-02, overdue: ask the user whether it is done): Book the review",
                      run(*self.t, "index"))
        page = viewer.main_html(self.tr())
        self.assertIn('<span class="meta" title="2099-01-15">Due: 15 Jan 2099</span>', page)
        self.assertIn('<span class="meta overdue" title="2020-01-02">Due: 2 Jan 2020</span>', page)
        self.assertEqual(page.count("Due: "), 2)  # A-01 has none, and shows no age instead
        run(*self.t, "act", "A-03", "--due", "none")
        self.assertEqual(self.tr().action("A-03").get("due"), "")
        self.tr().action("A-02").save({"due": "soon"})
        self.assertIn("A-02: due 'soon' is not a day (YYYY-MM-DD)", cli.check(self.tr())[0])

    def test_an_action_gets_a_new_title(self):
        run(*self.t, "act", "Reply to Sam on SC-1", "--refs", "T-1")
        run(*self.t, "act", "Reply to Kim", "--title", "Reply to Kim on SC-2", code=2)
        self.assertIn("A-01 updated", run(*self.t, "act", "A-01", "--title", "Reply to Sam on SC-1 (login copy)"))
        self.assertEqual(self.tr().action("A-01").get("title"), "Reply to Sam on SC-1 (login copy)")
        self.assertIn("A-01 renamed: Reply to Sam on SC-1 → Reply to Sam on SC-1 (login copy)",
                      (self.root / "log.md").read_text())

    def test_an_action_blocks_tickets_as_a_decision_does(self):
        run(*self.t, "new", "T-2", "--title", "Billing")
        self.assertIn("A-01 added, blocks T-1",
                      run(*self.t, "act", "Get read access to the prod DB", "--blocks", "T-1", "--refs", "T-1,T-2"))
        tr = self.tr()
        t1, a = tr.lookup("T-1"), tr.action("A-01")
        self.assertEqual((t1.list("depends_on"), a.list("refs")), (["A-01"], ["T-2"]))  # a block is not a ref
        self.assertEqual([d.ident for d in tr.blockers(t1)], ["A-01"])
        self.assertIn("waits on: A-01 open: Get read access to the prod DB", run(*self.t, "context", "T-1"))
        self.assertIn("A-01: Get read access to the prod DB (T-2); blocks T-1", "\n".join(views.action_lines(tr)))
        self.assertIn("blocks T-1", viewer.main_html(tr))
        t1.save({"status": "in-progress"})
        self.assertEqual(model.whose_move(self.tr(), self.tr().lookup("T-1")).text(), "you: A-01 open")

        run(*self.t, "wait", "T-2", "on", "a-1")  # the wait takes the ticket out of refs
        self.assertEqual((self.tr().lookup("T-2").list("depends_on"), self.tr().action("A-01").list("refs")),
                         (["A-01"], []))
        self.tr().action("A-01").save({"refs": ["T-2"]})
        self.assertIn("A-01: refs T-2, which also waits on it — drop it from refs (`tracker act A-01 --unref T-2`)",
                      "\n".join(cli.check(self.tr())[1]))
        run(*self.t, "act", "A-01", "--unref", "T-2")
        self.assertEqual(self.tr().action("A-01").list("refs"), [])
        self.assertIn("no action A-09", run(*self.t, "wait", "T-1", "on", "A-09", code=2))
        run(*self.t, "act", "A-01", "--done", "--blocks", "T-2", code=2)
        self.assertIn("nothing blocks T-1, T-2 now", run(*self.t, "act", "A-01", "--done"))
        self.assertEqual(self.tr().blockers(self.tr().lookup("T-1")), [])

    def test_an_actions_id_works_as_any_records(self):
        run(*self.t, "act", "Ask Sam whether v1 stays", "--refs", "T-1")
        self.assertIn(f"[A-01 T-1] {model.ACTION_ADDED} Ask Sam", run(*self.t, "history", "--ref", "a-1"))
        run(*self.t, "log", "Sam is away until Monday", "--ref", "A-01")
        self.assertIn("[A-01] Sam is away until Monday", run(*self.t, "history", "--ref", "A-01"))
        context = run(*self.t, "context", "A-01")
        self.assertIn("A-01 · open · Ask Sam whether v1 stays\ntouches: T-1", context)
        self.assertIn("== A-01 · Ask Sam whether v1 stays · open", run(*self.t, "show", "A-01"))
        self.assertIn("due: use `tracker act`", run(*self.t, "set", "A-01", "due=2099-01-01", code=2))
        self.assertIn("--ref takes ticket or decision ids; A-01 is an action",
                      run(*self.t, "attach", "x.txt", "--ref", "A-01", code=2))
        self.assertIn("A-<n> ids are actions", run(*self.t, "new", "A-2", "--title", "Two", code=2))
        ticket(self.root, "A-02")  # a ticket made before actions, whose id has an action's form
        self.assertIn("A-03 added", run(*self.t, "act", "Book the review room"))  # A-02 is taken
        self.assertEqual(cli.check(self.tr())[0], [])

    def test_the_text_and_the_page_show_the_same_links(self):
        run(*self.t, "new", "T-2", "--title", "Billing")
        run(*self.t, "act", "Get read access to the prod DB", "--blocks", "T-1", "--refs", "T-2")
        self.assertIn("blocks: T-1 todo; touches: T-2 todo",
                      "; ".join(run(*self.t, "context", "A-01").splitlines()))
        self.assertIn("[waits on A-01] ", run(*self.t, "index"))
        page = viewer.main_html(self.tr())
        self.assertRegex(page, r"<dt[^>]*>blocks</dt><dd>.*T-1.*</dd><dt[^>]*>touches</dt><dd>.*T-2")
        self.assertIn('<span class="chip s-blocked">blocks T-1</span>', page)  # Tracker.gate, as the index's

    def test_the_brief_prints_the_readmes_instructions(self):
        work = repo("feat/T-1")
        run(*self.t, "set", "T-1", "branch=feat/T-1")
        self.assertNotIn("This tracker's instructions", run(*self.t, "here", cwd=work))
        with piped("When a feedback ticket's PR opens, add one reply action per story."):
            run(*self.t, "put", "tracker", "instructions", "-")
        brief = run(*self.t, "here", cwd=work)
        self.assertIn("This tracker's instructions (README ## Instructions; follow them):\nWhen a feedback ticket's PR "
                      "opens, add one reply action per story.", brief)
        long = "\n".join(f"- rule {i}: " + "x" * 90 for i in range(40))
        self.tr().readme().rewrite(self.root.joinpath("README.md").read_text().replace(
            "When a feedback ticket's PR opens, add one reply action per story.", long))
        text = views.instructions(self.tr())
        self.assertLess(len(text), views.BRIEF_INSTRUCTIONS_CHARS + 200)
        self.assertTrue(text.endswith("… the rest: `tracker show tracker --section instructions`"))

    def test_the_watch_marks_a_new_action(self):
        events = watcher.log_events([f"- 2026-10-09 [A-01 T-1] {model.ACTION_ADDED} Ask Sam",
                                     "- 2026-10-09 [A-01 T-1] A-01 done: Ask Sam"])
        self.assertEqual([urgent for _, urgent, _ in events], [True, False])


class Values(unittest.TestCase):
    """Each value has one form whatever its source: a level on a scale, a UTC time in an `_at` key, a day in `due`. A
    status is one of its kind's own words; closing a record stamps `closed_at`."""

    def setUp(self):
        self.s = slug()
        self.t = ("--tracker", self.s)
        run("init", self.s, "--title", "Work", "--owner", "me")
        run(*self.t, "new", "T-1", "--title", "One")
        self.root = model.HOME / self.s

    def tr(self) -> model.Tracker:
        return model.Tracker(self.root)

    def test_a_scale_names_and_weighs_its_levels(self):
        for key, scale in model.SCALES.items():
            self.assertEqual(len(scale.names), len(scale.levels))
            self.assertEqual(len(scale.weights), len(scale.levels))
            self.assertEqual(model.KEYS["ticket"][key][0], "set")  # `tracker set` takes it, from the entry alone
            self.assertEqual(model.ISSUE_FIELDS[key], key)  # so does `tracker issue --<key>`
        run(*self.t, "set", "T-1", "priority=0", "size=4")
        t = self.tr().lookup("T-1")
        self.assertEqual([model.level_name(t, k) for k in ("priority", "size")], ["P0", "L"])
        self.assertEqual([model.weight(t, k) for k in ("priority", "size")], [8, 5])  # amounts that add up
        self.assertEqual((model.SCALES["size"].unit, model.SCALES["size"].weight(1)), ("days of work", 0.25))  # XS
        run(*self.t, "set", "T-1", "size=")
        self.assertIsNone(model.weight(self.tr().lookup("T-1"), "size"))

    def test_times_are_utc_and_closing_stamps_closed_at(self):
        utc = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        t = self.tr().lookup("T-1")
        for key in ("created_at", "updated_at"):
            self.assertRegex(t.get(key), utc)
        self.assertRegex(self.tr().meta["created_at"], utc)
        run(*self.t, "set", "T-1", "next=Write the schema", "status=in-progress")
        t = self.tr().lookup("T-1")
        self.assertRegex(t.get("started_at"), utc)
        self.assertNotIn("closed_at", t.meta)  # started, not closed
        run(*self.t, "set", "T-1", "status=done", "summary=shipped")
        t = self.tr().lookup("T-1")
        self.assertRegex(t.get("closed_at"), utc)
        self.assertEqual(t.get("next"), "")  # a closed ticket has no next action
        run(*self.t, "set", "T-1", "status=todo")  # open again: no close time
        self.assertNotIn("closed_at", self.tr().lookup("T-1").meta)
        run(*self.t, "decide", "Auth scheme", "--resolve", "OAuth", "--by", "me")
        run(*self.t, "act", "Ask Sam about v1")
        run(*self.t, "act", "A-01", "--done")
        self.assertRegex(self.tr().lookup("D-01").get("closed_at"), utc)
        self.assertTrue(self.tr().lookup("D-01").closed)
        self.assertRegex(self.tr().action("A-01").get("closed_at"), utc)
        self.tr().lookup("T-1").save({"started_at": "last week"})
        self.assertIn("T-1: started_at 'last week' is not a time", "\n".join(cli.check(self.tr())[0]))
        self.tr().lookup("T-1").save({"started_at": None, "status": "in-progress", "closed_at": "2026-09-01T00:00:00Z"})
        self.assertIn("T-1: closed_at, but its status in-progress is open", "\n".join(cli.check(self.tr())[1]))
        run(*self.t, "migrate")
        self.assertNotIn("closed_at", self.tr().lookup("T-1").meta)

    def test_any_record_can_have_a_due_day(self):
        run(*self.t, "new", "T-2", "--title", "Two", "--due", "2099-03-01")
        run(*self.t, "set", "T-1", "due=2099-02-01")
        run(*self.t, "decide", "Auth scheme")
        run(*self.t, "set", "D-01", "due=2099-01-15")
        self.assertIn("is not a day", run(*self.t, "set", "T-1", "due=soon", code=2))
        self.assertIn("is not a day", run(*self.t, "new", "T-3", "--title", "Three", "--due", "2099-13-01", code=2))
        tr = self.tr()
        self.assertEqual([model.due_date(tr.lookup(x)).isoformat() for x in ("T-2", "T-1", "D-01")],
                         ["2099-03-01", "2099-02-01", "2099-01-15"])
        self.assertIn("due: 2099-02-01", run(*self.t, "context", "T-1"))
        self.assertIn("due: 2099-01-15", run(*self.t, "context", "D-01"))
        run(*self.t, "add", "T-1", "link", "Issue: [SC-1 Story](https://issues.example/1)")
        run(*self.t, "issue", "T-1", "--due", "2099-04-01")
        self.assertEqual(self.tr().lookup("T-1").get("due"), "2099-04-01")
        run(*self.t, "issue", "T-1", "--due", "")  # the issue has none now
        self.assertNotIn("due", self.tr().lookup("T-1").meta)
        self.tr().lookup("T-2").save({"due": "soon"})
        self.assertIn("T-2: due 'soon' is not a day (YYYY-MM-DD)", cli.check(self.tr())[0])

    def test_migrate_names_keys_by_their_form(self):
        run(*self.t, "decide", "Auth scheme", "--resolve", "OAuth")
        run(*self.t, "act", "Ask Sam about v1")
        run(*self.t, "act", "A-01", "--done")
        tr = self.tr()
        for r in (tr.lookup("T-1"), tr.lookup("D-01"), tr.action("A-01")):
            old = {new: name for name, new in model.RENAMED_KEYS[r.kind].items()}
            keys = {k: v for k, v in r.meta.items() if k in old or k == "closed_at"}
            r.save({**{k: None for k in keys}, **{old[k]: "2026-09-01" for k in keys if k in old}})
        tr.lookup("T-1").save({"issue_created": "2026-08-30T10:00:00Z"})
        readme = tr.readme()
        readme.save({"created_at": None, "created": "2026-08-01", "schema": 2})
        warnings = "\n".join(cli.check(self.tr())[1])
        self.assertIn("frontmatter key 'updated' is retired — run `tracker migrate`", warnings)
        run(*self.t, "migrate")
        tr = self.tr()
        t, d, a = tr.lookup("T-1"), tr.lookup("D-01"), tr.action("A-01")
        self.assertEqual((t.get("updated_at"), t.get("issue_created_at")), ("2026-09-01", "2026-08-30T10:00:00Z"))
        self.assertEqual((d.get("created_at"), d.get("closed_at")), ("2026-09-01", model.today()))  # Resolution's day
        self.assertEqual(a.get("closed_at"), "2026-09-01")  # an action's `updated` was when it closed
        self.assertEqual((tr.meta.get("created_at"), tr.schema), ("2026-08-01", model.SCHEMA))
        self.assertEqual(cli.check(tr), ([], ["README.md: ## Context lists nothing"]))


if __name__ == "__main__":
    unittest.main()
