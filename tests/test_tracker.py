"""Checks that keep the tracker reliable: its instructions, its file format, its commands and its hooks, run end to end
in temporary git repos. Run: python3 -m unittest discover tests"""

from __future__ import annotations

import atexit
import contextlib
import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = tempfile.mkdtemp()
atexit.register(shutil.rmtree, BASE, True)
os.environ["TRACKER_HOME"] = f"{BASE}/home"  # before the import: HOME is read once
os.environ["CLAUDE_CONFIG_DIR"] = f"{BASE}/claude"  # the running Claude sessions the view reads: not the user's
os.environ["HERDR_APP_HOME"] = f"{BASE}/herdr"  # `open` and the hooks update an installed mod: not the user's
# An agent session running the tests must not leak in: its tracker, its id, or that it is one.
for var in ("TRACKER", "TRACKER_SESSION", "CLAUDE_ENV_FILE", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
            "CODEX_THREAD_ID", "PLUGIN_ROOT", "TRACKER_VIA"):
    os.environ.pop(var, None)
sys.path.insert(0, str(ROOT / "scripts"))
from tracker import (cli, commands, contract, git, github, hooks, lifecycle, markdown, model, moves,  # noqa: E402
                     schema, session, store, system, ui, viewmodel, views, watcher)


def slug() -> str:
    return f"t{time.monotonic_ns()}"


def repo(branch: str) -> Path:
    """A new git repo on `branch` with one commit."""
    path = Path(tempfile.mkdtemp(dir=BASE))
    for args in (["init", "-q", "-b", branch], ["config", "user.email", "me@x"], ["config", "user.name", "me"],
                 ["config", "commit.gpgsign", "false"]):
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


def new_tracker(title: str, in_repo: str = "") -> tuple[str, tuple[str, str]]:
    """A new tracker: its slug, and the flag that names it."""
    s = slug()
    run("init", s, "--title", title, "--owner", "me", *(["--repo", in_repo] if in_repo else []))
    return s, ("--tracker", s)


def data(s: str) -> dict:
    """What a UI reads of a tracker, archived or not."""
    return json.loads(run("data", s))


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

    def test_the_skill_names_the_link_labels(self):
        self.assertIn(", ".join(schema.DEFAULT_LABELS), (ROOT / "skills/tracker/SKILL.md").read_text())


class Format(unittest.TestCase):
    def test_frontmatter_round_trip(self):
        for value in ["plain", "a: b", "[x]", '"q"', "# h", "x #y", " pad", "ünï", "", ["a", "b,c", 'd"e', "[f]"], []]:
            with self.subTest(value=value):
                line = markdown.render_frontmatter([], {"k": value})[0] if value != "" else "k: "
                meta, problems = markdown.parse_meta([line])
                self.assertEqual((meta["k"], problems), (value, []))

    def test_the_data_keeps_text_as_it_is(self):
        """A UI draws text as text: no field of `tracker data` is markup, and none is escaped."""
        bad = '<b x="1">&amp;'
        s, t = new_tracker(f"Work {bad}")
        run(*t, "new", "T-1", "--title", f"One {bad}", "--group", f"g{bad}")
        run(*t, "new", "T-2", "--title", "Two", "--depends", "T-1")
        run(*t, "set", "T-1", f"next=Do {bad}", "status=in-progress")
        run(*t, "add", "T-1", "link", f"Issue: [SC-1 {bad}](https://issues.example/1)")
        model.tracker_at(s).lookup("T-1").save({"priority": bad})  # check refuses it; the UI shows it
        got = data(s)
        row = next(x for x in got["tickets"] if x["id"] == "T-1")
        self.assertEqual((got["title"], row["title"], row["group"], row["levels"]["priority"]),
                         (f"Work {bad}", f"One {bad}", f"g{bad}", bad))
        self.assertEqual(got["records"]["T-1"]["line"], {"label": "next", "text": f"Do {bad}"})
        self.assertIn(bad, got["records"]["T-1"]["links"][0]["text"])
        self.assertTrue(got["check"]["errors"])


    def test_a_remote_names_its_repo(self):
        for remote, slug_ in [("git@github.com:Acme/API.git", "acme/api"), ("https://github.com/acme/api", "acme/api"),
                              ("https://user:token@github.com/acme/api.git/", "acme/api"),
                              ("ssh://git@gitlab.example:2222/group/sub/name.git", "group/sub/name"),
                              ("git@gitlab.com:group/sub/name.git", "group/sub/name"), ("/srv/git/api.git", "")]:
            self.assertEqual(git.repo_slug(remote), slug_, remote)

    def test_a_sessions_status_is_its_hooks_but_for_what_only_its_process_file_knows(self):
        hook = {"status": "waiting", "since": 50.0, "at": 60.0}
        self.assertEqual(session.hook_status(hook, "idle", 70.0), ("waiting", 50.0))  # a wait is the hooks' to end
        self.assertEqual(session.hook_status({**hook, "status": "idle"}, "busy", 70.0), ("idle", 50.0))
        self.assertEqual(session.hook_status({**hook, "status": "busy"}, "busy", 70.0), ("busy", 50.0))
        self.assertEqual(session.hook_status({**hook, "status": "busy"}, "idle", 55.0), ("busy", 50.0))  # an older idle
        self.assertEqual(session.hook_status({**hook, "status": "busy"}, "idle", 70.0), ("idle", 70.0))  # interrupted
        self.assertEqual(session.hook_status({}, "busy", 70.0), ("busy", 70.0))  # no hook has told it yet
        self.assertEqual(session.hook_status({**hook, "status": "ended"}, "idle", 70.0), ("idle", 70.0))

    def test_fork_prs_are_not_the_tickets(self):
        t = model.Record(Path("T-1.md"), "ticket", {"id": "T-1", "branch": "fix"}, "")
        fork = {"number": 9, "headRefName": "fix", "state": "OPEN", "isCrossRepository": True}
        own = {"number": 3, "headRefName": "fix", "state": "CLOSED", "isCrossRepository": False}
        self.assertEqual(github.ticket_prs([fork, own], t, "a/x"), [own])
        self.assertEqual(github.ticket_prs([fork], t, "a/x"), [])

    def test_gh_budget(self):
        with mock.patch.object(github.subprocess, "run") as run_gh:
            github.budget(0)  # spent: no call starts, so a hook ends before its timeout
            self.assertIsNone(github.gh("pr", "list"))
            github.budget(None)
        run_gh.assert_not_called()

    def test_a_uis_sync_asks_github_only_when_no_sync_ran_lately(self):
        s, t = new_tracker("Offline", "a/x")
        run(*t, "new", "S-1", "--title", "Started", "--branch", "f1")
        run(*t, "set", "S-1", "status=in-progress")
        calls = []
        with mock.patch.object(github, "gh_prs", lambda *args: calls.append(args) or {"a/x": []}):
            for _ in range(3):  # the UI's rounds
                run(*t, "sync", "--live")
            self.assertEqual(len(calls), 1)
            run(*t, "sync")  # the user's or the agent's: always
            self.assertEqual(len(calls), 2)
        with mock.patch.object(github, "gh", lambda *args, **kw: None):  # gh failed: nothing is written
            tr = model.tracker_at(s)
            tr.save_state({**tr.raw_state(), "last_sync": 0})
            self.assertIn("sync skipped", run(*t, "sync", "--live"))

def ticket(root: Path, ident: str, **meta) -> None:
    meta = {"id": ident, "title": ident, "status": "todo", **meta}
    front = "\n".join(f"{k}: {markdown.format_value(v)}" for k, v in meta.items())
    (root / "tickets" / f"{ident}.md").write_text(f"---\n{front}\n---\n\n## Plan\n\n## Carry forward\n\n## Links\n")


class MachineState(unittest.TestCase):
    def setUp(self):
        self.root = store.HOME / slug()
        (self.root / "tickets").mkdir(parents=True)
        (self.root / "README.md").write_text(f"---\ntitle: T\nschema: {schema.SCHEMA}\nrepo: [a/x, b/y]\n---\n")
        ticket(self.root, "T-1", status="done", branch="f1", repo="a/x")
        ticket(self.root, "T-2", status="in-progress", branch="f2", repo="a/x")

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


def pull(number: int, **facts) -> dict:
    """A PR as sync's query answers it (github.PR_FRAGMENT)."""
    return {"number": number, "headRefName": facts.get("branch", ""), "state": facts.get("state", "OPEN"),
            "mergedAt": facts.get("merged_at"), "baseRefName": "main", "isCrossRepository": False,
            "isDraft": facts.get("draft", False), "reviewDecision": facts.get("decision"),
            "title": facts.get("title", f"Part {number}"), "reviews": {"nodes": facts.get("rounds", [])},
            "mergeStateStatus": facts.get("merge", "CLEAN"),
            "reviewRequests": {"nodes": [{"requestedReviewer": {"login": x}} for x in facts.get("requested", [])]},
            "latestReviews": {"nodes": facts.get("reviews", [])},
            "reviewThreads": {"nodes": [{"isResolved": False}] * facts.get("threads", 0) + [{"isResolved": True}]},
            "commits": {"nodes": [{"commit": {"committedDate": facts.get("pushed", "2026-09-01T00:00:00Z"),
                                              "statusCheckRollup": {"state": facts.get("checks", "SUCCESS")}}}]}}


def review_round(rid: int, by: str, state: str, at: str, bot: bool = False) -> dict:
    """One review of a PR as sync's query answers it (`reviews` in github.PR_FRAGMENT)."""
    return {"databaseId": rid, "author": {"__typename": "Bot" if bot else "User", "login": by}, "state": state,
            "submittedAt": at}


def answers(prs: list[dict]):
    """A fake `github.gh` for sync's one GraphQL call, on one repo: alias `p<n>` gets PR n, `b<j>` the PRs from its
    branch, out of `prs` (each as `pull` makes it; change the list to change the answers). `.queries` keeps each
    query."""
    def gh(*args, fields=None, partial=False):
        query = args[3]
        gh.queries.append(query)
        repo = {f"p{n}": next((p for p in prs if p["number"] == int(n)), None)
                for n in re.findall(r"\bp(\d+): pullRequest\(", query)}
        repo |= {f"b{j}": {"nodes": [p for p in prs if p["headRefName"] == b]}
                 for j, b in re.findall(r'\bb(\d+): pullRequests\(headRefName: "([^"]*)"', query)}
        return {"data": {"r0": repo}}
    gh.queries = []
    return gh


class Moves(unittest.TestCase):
    """Whose move a ticket under way waits on: read from its PR by `sync`, and from its blockers."""

    def waiting(self, owner: str = "", prs: dict | None = None) -> tuple[model.Tracker, model.Record]:
        """A tracker whose T-1, under way, waits on the open decision D-01 of `owner`: T-2 under way, T-3 done; with
        `prs`, T-1's open PRs and the facts `sync` read of them."""
        s, t = new_tracker("Whose")
        for ident, status in (("T-1", "in-progress"), ("T-2", "in-progress"), ("T-3", "done")):
            run(*t, "new", ident, "--title", ident)
            run(*t, "set", ident, f"status={status}")
        run(*t, "decide", "Which store", "--blocks", "T-1", *(["--owner", owner] if owner else []))
        tr = model.tracker_at(s)
        if prs:
            tr.lookup("T-1").save({"prs": [f"{n}:open" for n in prs]})
            tr.save_state({**tr.raw_state(), "prs": {f"#{n}": r for n, r in prs.items()}})
            tr = model.tracker_at(s)
        return tr, tr.lookup("T-1")

    def move(self, coordinated: bool, owner: str = "", prs: dict | None = None) -> moves.Move:
        return moves.whose_move(*self.waiting(owner, prs), coordinated)

    def test_a_decision_with_no_owner_is_a_humans(self):
        m = self.move(False)
        self.assertEqual((m.who, m.what, m.human, m.mine), ("human", "D-01 open", True, False))

    def test_a_decision_with_no_owner_is_the_coordinators(self):
        for owner in ("", "coordinator"):
            with self.subTest(owner=owner):
                m = self.move(True, owner)
                self.assertEqual((m.who, m.what, m.human), ("coordinator", "D-01 open", False))
                self.assertEqual((self.move(False, owner).who, self.move(False, owner).human), ("human", True))

    def test_a_named_owner_is_that_human(self):
        for coordinated in (False, True):
            m = self.move(coordinated, "Sam")
            self.assertEqual((m.who, m.what, m.human), ("Sam", "D-01 open", True))

    def test_a_ticket_owner_is_its_session(self):
        for coordinated in (False, True):
            m = self.move(coordinated, "T-2")
            self.assertEqual((m.who, m.what, m.human), ("T-2's session", "D-01 open", False))

    def test_a_closed_or_missing_ticket_owner_is_a_humans(self):
        for owner, who in (("T-9", "T-9"), ("T-3", "human"), ("D-01", "human")):  # no such record: a name
            with self.subTest(owner=owner):
                m = self.move(True, owner)
                self.assertEqual((m.who, m.human), (who, True))

    def test_an_open_action_is_a_humans(self):
        s, t = new_tracker("Act")
        run(*t, "new", "T-1", "--title", "One")
        run(*t, "set", "T-1", "status=in-progress")
        run(*t, "act", "Ask Sam", "--blocks", "T-1")
        tr = model.tracker_at(s)
        m = moves.whose_move(tr, tr.lookup("T-1"))
        self.assertEqual((m.who, m.what, m.human, m.mine), (moves.YOU, "A-01 open", True, True))

    def test_several_open_prs_keep_human(self):
        m = self.move(False, prs={12: {}, 13: {}})
        self.assertEqual((m.who, m.what, m.human), ("human", "#12 D-01 open", True))

    def test_the_order_of_the_rule_is_unchanged(self):
        m = self.move(False, prs={12: {"merge": "conflict"}})
        self.assertEqual((m.who, m.what, m.human), (moves.YOU, "merge conflicts", False))

    def handed(self, *writes: list[str], prs: dict | None = None) -> tuple[model.Tracker, model.Record]:
        """A tracker whose T-1 is under way, after these commands; with `prs`, its open PRs and their facts."""
        s, t = new_tracker("Handed")
        run(*t, "new", "T-1", "--title", "One")
        run(*t, "set", "T-1", "status=in-progress")
        for args in writes:
            run(*t, *args)
        tr = model.tracker_at(s)
        if prs:
            tr.lookup("T-1").save({"prs": [f"{n}:open" for n in prs]})
            tr.save_state({**tr.raw_state(), "prs": {f"#{n}": r for n, r in prs.items()}})
            tr = model.tracker_at(s)
        return tr, tr.lookup("T-1")

    def handed_move(self, tr: model.Tracker, t: model.Record, coordinated: bool) -> moves.Move:
        items = moves.open_items(tr, model.read_events(tr), time.time(), coordinated, {"T-1"}, {})
        return moves.whose_move(tr, t, coordinated, items)

    def test_a_submitted_plan_is_the_reviewers_move(self):
        tr, t = self.handed(["submit", "T-1", "--what", "plan"])
        m = self.handed_move(tr, t, True)
        self.assertEqual((m.who, m.what, m.human, m.mine), ("coordinator", "plan review", False, False))
        m = self.handed_move(tr, t, False)
        self.assertEqual((m.who, m.what, m.human), ("human", "plan review", True))
        tr, t = self.handed(["submit", "T-1", "--what", "code"])
        self.assertEqual(self.handed_move(tr, t, True).text(), "coordinator: plan conformance check")
        self.assertEqual(session.move_of(tr, t).text(), "human: plan conformance check")  # no coordinator watches

    def test_open_findings_are_yours(self):
        tr, t = self.handed(["review", "T-1", "--what", "plan", "--verdict", "changes", "--finding", "a",
                             "--finding", "b"])
        m = self.handed_move(tr, t, True)
        self.assertEqual((m.who, m.what, m.human), (moves.YOU, "2 findings of the plan review open", False))
        self.assertEqual(session.move_of(tr, t).text(), "you: 2 findings of the plan review open")

    def test_a_requested_pr_review_still_comes_first(self):
        tr, t = self.handed(["submit", "T-1", "--what", "code"], prs={12: {"requested": ["rev"]}})
        self.assertEqual(self.handed_move(tr, t, True).text(), "rev: review requested")

    def test_a_failing_check_still_comes_first(self):
        tr, t = self.handed(["submit", "T-1", "--what", "code"], prs={12: {"checks": "failing"}})
        self.assertEqual(self.handed_move(tr, t, True).text(), "you: checks failing")

    def test_sync_follows_every_pr_of_a_ticket(self):
        s, t = new_tracker("Work", "a/x")
        run(*t, "new", "S-1", "--title", "One", "--branch", "f1")
        run(*t, "set", "S-1", "status=in-progress")
        requested = {"requested": ["rev"]}
        prs = [pull(11, branch="f1", checks="FAILURE"), pull(12, branch="f1", **requested)]

        def synced():
            with mock.patch.object(github, "gh", answers(prs)):
                run(*t, "sync")
            tr = model.tracker_at(s)
            return tr, tr.lookup("S-1")

        tr, one = synced()
        self.assertEqual((one.prs, one.stage), ({"11": "open", "12": "open"}, "in-review"))
        self.assertEqual(set(tr.prs), {"a/x#11", "a/x#12"})
        self.assertEqual(moves.whose_move(tr, one).text(), "you: #11 checks failing")  # the most urgent, and yours
        self.assertIs(tr.by_pr_or_branch("#11"), one)
        self.assertEqual(views.pr_label(one), "#11 open, #12 open")
        self.assertEqual([x["number"] for x in viewmodel.pr_views(tr, one)], ["11", "12"])

        # One merges: the ticket stays under way while the other is open
        prs[1] = pull(12, branch="f1", state="MERGED", merged_at="2026-10-01T06:30:00Z")
        tr, one = synced()
        self.assertEqual((one.prs, one.stage), ({"11": "open", "12": "merged"}, "in-review"))
        self.assertEqual(moves.whose_move(tr, one).text(), "you: checks failing")  # one open PR: nothing to tell apart
        self.assertIn("PR #12 merged", (tr.root / "log.md").read_text())

        # A PR from another branch, by a link; a closed one drops out while another is not closed
        run(*t, "add", "S-1", "link", "PR: [Part two](https://github.com/a/x/pull/20)")
        run(*t, "add", "S-1", "link", "PR: [Elsewhere](https://github.com/b/y/pull/30)")
        prs += [pull(20, branch="other"), pull(13, branch="f1", state="CLOSED")]
        tr, one = synced()
        self.assertEqual(one.prs, {"11": "open", "12": "merged", "20": "open"})
        self.assertEqual([x["number"] for x in viewmodel.pr_views(tr, one)], ["11", "12", "20", "30"])

        # All merged: merged, at the last one's time, and sync is done with it
        prs[:] = [pull(11, branch="f1", state="MERGED", merged_at="2026-10-03T06:30:00Z"),
                  pull(12, branch="f1", state="MERGED", merged_at="2026-10-01T06:30:00Z"),
                  pull(20, branch="other", state="MERGED", merged_at="2026-10-02T06:30:00Z")]
        tr, one = synced()
        self.assertEqual((one.stage, one.get("merged_at")), ("done", "2026-10-03T06:30:00Z"))
        fake = answers(prs)
        with mock.patch.object(github, "gh", fake):
            run(*t, "sync")
        self.assertEqual(fake.queries, [])

    def test_each_open_pr_can_stack_a_ticket(self):
        s, t = new_tracker("Work", "a/x")
        run(*t, "new", "S-0", "--title", "Below", "--branch", "f0")
        run(*t, "new", "S-1", "--title", "One", "--branch", "f1")
        run(*t, "set", "S-0", "status=in-progress")
        run(*t, "set", "S-1", "status=in-progress")
        prs = [pull(11, branch="f1"), {**pull(12, branch="f1"), "baseRefName": "f0"}]

        def waits():
            with mock.patch.object(github, "gh", answers(prs)):
                run(*t, "sync")
            tr = model.tracker_at(s)
            return [d.ident for d in tr.deps(tr.lookup("S-1"))]

        self.assertEqual(waits(), ["S-0"])
        self.assertEqual([x.id for x in model.tracker_at(s).waiting_on("S-0")], ["S-1"])
        prs[1]["baseRefName"] = "main"  # retargeted
        self.assertEqual(waits(), [])

    def test_a_tickets_one_pr_becomes_its_list_and_its_base_a_fact_of_the_pr(self):
        s, t = new_tracker("Work", "a/x")
        run(*t, "new", "S-0", "--title", "Base", "--branch", "main2")
        run(*t, "set", "S-0", "status=in-progress")
        run(*t, "new", "S-1", "--title", "One", "--branch", "f1")
        run(*t, "set", "S-1", "status=in-progress")
        tr = model.tracker_at(s)
        tr.lookup("S-1").save({"pr": "11", "pr_state": "open", "base": "main2"})
        tr.save_state({**tr.raw_state(), "prs": {"a/x#11": {"review": "changes", "changes": ["rev"]}},
                       "reviews": {"old": 1}, "synced": {"a/x:f9": "abc123"}})
        tr.readme.save({"schema": 3})

        run(*t, "list")
        tr = model.tracker_at(s)
        one = tr.lookup("S-1")
        self.assertEqual((one.list("prs"), one.prs, one.stage), (["11:open"], {"11": "open"}, "in-review"))
        self.assertFalse(one.meta.keys() & {"pr", "pr_state", "base"})
        self.assertEqual([d.ident for d in tr.deps(one)], ["S-0"])  # still stacked, before any sync
        self.assertNotIn("reviews", tr.raw_state())
        self.assertEqual(tr.raw_state()["synced"]["a/x:f9"], {"head": "abc123", "at": 0})

        merged = pull(11, branch="f1", state="MERGED", merged_at="2026-10-01T06:30:00Z")
        with mock.patch.object(github, "gh", answers([merged])):
            run(*t, "sync")
        one = model.tracker_at(s).lookup("S-1")
        self.assertEqual((one.prs, one.stage, one.get("merged_at")), ({"11": "merged"}, "done", "2026-10-01T06:30:00Z"))

    def test_a_closed_pr_that_a_link_names_stays_and_a_merged_ticket_is_read_no_more(self):
        s, t = new_tracker("Work", "a/x")
        run(*t, "new", "S-1", "--title", "One", "--branch", "f1")
        run(*t, "set", "S-1", "status=in-progress")
        run(*t, "add", "S-1", "link", "PR: [First try](https://github.com/a/x/pull/1)")
        prs = [pull(1, branch="other", state="CLOSED"), pull(2, branch="f1", state="MERGED",
                                                            merged_at="2026-10-01T06:30:00Z")]
        with mock.patch.object(github, "gh", answers(prs)):
            run(*t, "sync")
        one = model.tracker_at(s).lookup("S-1")
        self.assertEqual((one.prs, one.stage), ({"1": "closed", "2": "merged"}, "done"))
        before = model.tracker_at(s).raw_state()["last_sync"]
        with mock.patch.object(github, "gh") as gh:
            run(*t, "sync")
        gh.assert_not_called()
        self.assertGreater(model.tracker_at(s).raw_state()["last_sync"], before)  # nothing to ask: it still counts

    def test_sync_with_only_a_closed_pr_keeps_it(self):
        s, t = new_tracker("Work", "a/x")
        run(*t, "new", "S-1", "--title", "One", "--branch", "f1")
        run(*t, "set", "S-1", "status=in-progress")
        closed = [pull(1, branch="f1", state="CLOSED"), pull(2, branch="f1", state="CLOSED")]
        with mock.patch.object(github, "gh", answers(closed)):
            run(*t, "sync")
        tr = model.tracker_at(s)
        one = tr.lookup("S-1")
        self.assertEqual((one.prs, one.pr_state), ({"1": "closed", "2": "closed"}, "closed"))
        move = moves.whose_move(tr, one)
        self.assertEqual((move.text(), move.quiet), ("you: PR closed", False))

    def test_sync_asks_github_for_no_damaged_pr_number(self):
        s, t = new_tracker("Damaged", "a/x")
        run(*t, "new", "S-1", "--title", "One", "--branch", "f1")
        run(*t, "set", "S-1", "status=in-progress")
        path = store.HOME / s / "tickets" / "S-1.md"
        damaged = "prs: [abc:open, \u0661\u0662:open, 0012:open, 99999999999:open, 7:open]"  # hand edits gone wrong
        path.write_text(path.read_text().replace("depends_on: []", f"depends_on: []\n{damaged}"))
        fake = answers([pull(7, branch="f1"), pull(12, branch="other")])
        with mock.patch.object(github, "gh", fake):
            run(*t, "sync")
        self.assertEqual(sorted(re.findall(r"\bp(\S+): pullRequest\(number: (\S+)\)", fake.queries[0])),
                         [("12", "12"), ("7", "7")])

    def test_review_facts(self):
        bot = {"author": {"__typename": "Bot", "login": "lint"}, "state": "CHANGES_REQUESTED",
               "submittedAt": "2026-09-02T00:00:00Z"}
        rev = {"author": {"__typename": "User", "login": "rev"}, "state": "CHANGES_REQUESTED",
               "submittedAt": "2026-09-02T00:00:00Z"}
        facts = github.review_facts(pull(1, decision="CHANGES_REQUESTED", reviews=[bot, rev, {"author": None}],
                                         threads=2, pushed="2026-09-03T00:00:00Z"))
        self.assertEqual(facts, {"review": "changes", "changes": ["rev"], "reviewed": 1788307200,
                                 "pushed": 1788393600, "checks": "passing", "threads": 2})  # a clean merge: none
        self.assertLessEqual(set(facts), set(moves.PR_FACTS))  # the tracker's words, not GitHub's
        merge = github.review_facts(pull(2, merge="DIRTY", checks="ERROR", decision="REVIEW_REQUIRED"))
        self.assertEqual((merge["merge"], merge["checks"], merge.get("review")), ("conflict", "failing", None))

    def test_rules(self):
        root = store.HOME / slug()
        for d in ("tickets", "decisions"):
            (root / d).mkdir(parents=True)
        (root / "README.md").write_text(f"---\ntitle: T\nschema: {schema.SCHEMA}\nrepo: a/x\n---\n")
        (root / "decisions" / "D-01.md").write_text("---\nid: D-01\ntitle: Store\nstatus: open\nowner: Ryan\n---\n")
        facts = {1: {"merge": "conflict", "checks": "failing"}, 2: {"checks": "failing"},
                 3: {"review": "changes", "changes": ["rev"], "reviewed": 100, "pushed": 50, "threads": 2},
                 4: {"review": "changes", "changes": ["rev"], "reviewed": 100, "pushed": 200},
                 5: {"review": "changes", "requested": ["rev"]},
                 7: {"review": "approved", "approved": ["rev"], "threads": 1},
                 8: {"review": "approved", "checks": "running"}, 9: {"review": "approved", "merge": "behind"},
                 10: {"draft": True}, 12: {}}  # 12: read, and nothing stops it
        (root / ".state.json").write_text(json.dumps({"prs": {f"a/x#{n}": f for n, f in facts.items()}}))
        for n in [*facts, 11]:
            ticket(root, f"M-{n}", status="in-progress", prs=[f"{n}:{'draft' if n == 10 else 'open'}"])
        ticket(root, "M-6", status="in-progress", depends_on=["D-01"])
        ticket(root, "M-13", status="in-progress", depends_on=["EXT-1"])
        with open(root / "tickets" / "M-13.md", "a") as f:
            f.write("\n- Blocker: [EXT-1 Legal](https://x.y) — legal must sign off\n")
        ticket(root, "M-14", status="in-progress")
        ticket(root, "M-15")
        tr = model.Tracker(root)
        whose = {t.id: (m.text() if (m := moves.whose_move(tr, t)) else None) for t in tr.tickets}
        self.assertEqual(whose, {
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
        self.assertEqual(lines[-1], "  PR not read yet (`tracker sync`) — M-11")

    def test_sync_reads_reviews(self):
        s, t = new_tracker("Reviewed", "a/x")
        for i in (1, 2):
            run(*t, "new", f"S-{i}", "--title", f"Part {i}", "--branch", f"f{i}")
            run(*t, "set", f"S-{i}", "status=in-progress")
        run(*t, "new", "S-3", "--title", "Not started", "--branch", "f3")
        fork = {**pull(13, branch="f1"), "isCrossRepository": True}
        prs = [pull(11, branch="f1", requested=["rev"]), pull(12, branch="f2", draft=True), fork, pull(14, branch="f3")]
        fake = answers(prs)
        with mock.patch.object(github, "gh", fake):
            run(*t, "sync")
            # One call: the started tickets' PRs by their branches, with what their moves need
            self.assertEqual(len(fake.queries), 1)
            self.assertIn('b0: pullRequests(headRefName: "f1"', fake.queries[0])
            self.assertNotIn('"f3"', fake.queries[0])
            tr = model.tracker_at(s)
            self.assertEqual([list(t.prs) for t in tr.tickets], [["11"], ["12"], []])  # not the fork's PR from f1
            self.assertEqual(set(tr.prs), {"a/x#11", "a/x#12"})
            index = run(*t, "index")
            self.assertIn("  you: draft — S-2\n  rev: review requested — S-1", index)
            self.assertIn("move: rev — review requested", run(*t, "context", "S-1"))
            now = viewmodel.now_view(tr)
            self.assertEqual(now["items"][0]["move"], {"text": "you: draft", "mine": True, "human": False,
                                                             "quiet": True})

            seen = session.watch(tr, [tr.lookup("S-1")])
            prs[0] = pull(11, branch="f1", decision="APPROVED", reviews=[
                {"author": {"__typename": "User", "login": "rev"}, "state": "APPROVED", "submittedAt": ""}])
            run(*t, "sync")
            self.assertEqual(len(fake.queries), 2)
            self.assertIn("p11: pullRequest(number: 11)", fake.queries[1])  # a known PR: by its number
            self.assertIn('pullRequests(headRefName: "f1"', fake.queries[1])  # and its branch, for another PR from it
            tr = model.tracker_at(s)
            self.assertEqual(session.changes_since(tr, seen, session.watch(tr, [tr.lookup("S-1")])),
                             ["S-1 move now: you — approved by rev; ready to merge"])
        # A repo GitHub does not answer for: nothing is written
        with mock.patch.object(github, "gh", lambda *args, fields=None, partial=False: {"data": {"r0": None}}):
            self.assertEqual(run(*t, "sync"), "sync skipped: gh failed or timed out for a/x\n")
        self.assertEqual(set(model.tracker_at(s).prs), {"a/x#11", "a/x#12"})
        # Of the session's own tickets, only a move that becomes its own and a ticket's end are news.
        own = {"S-1": {"role": "own", "stage": "in-progress", "move": "rev — review requested", "mine": False}}
        for now, said_ in (({"stage": "in-review"}, []), ({"stage": "done"}, ["S-1 is now done"]),
                           ({"stage": "in-progress", "move": "you — checks failing", "mine": True},
                            ["S-1 move now: you — checks failing"])):
            self.assertEqual(session.changes_since(tr, own, {"S-1": {"role": "own", **now}}), said_)


    def reviewed(self, *tickets: str, repos: str = "a/x") -> tuple[str, tuple[str, str]]:
        """A tracker of `repos` whose tickets are under way: the first on branch f1, each other with a `PR:` link to
        PR 11 of the first repo."""
        s, t = new_tracker("Reviews", repos)
        first = repos.split(",")[0]
        for i, ident in enumerate(tickets or ("S-1",)):
            run(*t, "new", ident, "--title", ident, "--repo", first, *(["--branch", "f1"] if not i else []))
            run(*t, "set", ident, "status=in-progress")
            if i:
                run(*t, "add", ident, "link", f"PR: [Part one](https://github.com/{first}/pull/11)")
        return s, t

    def synced(self, t: tuple[str, str], prs: list[dict], code: int = 0) -> str:
        with mock.patch.object(github, "gh", answers(prs)):
            return run(*t, "sync", code=code)

    def review_events(self, s: str) -> list[dict]:
        return [e for e in model.read_events(model.tracker_at(s)) if e["kind"] == "pr.review"]

    def test_a_review_is_logged_once(self):
        s, t = self.reviewed()
        prs = [pull(11, branch="f1", decision="CHANGES_REQUESTED",
                    rounds=[review_round(101, "rev", "CHANGES_REQUESTED", "2026-09-02T01:02:03Z")])]
        self.synced(t, prs)
        self.synced(t, prs)
        model.EVENTS_READ.clear()  # as a new process reads it: the log says which reviews it holds
        self.synced(t, prs)
        [e] = self.review_events(s)
        self.assertEqual({k: e[k] for k in ("text", "refs", "pr", "review", "by", "result", "submitted_at", "bot")},
                         {"text": "PR #11 review by rev: changes requested", "refs": ["S-1"], "pr": "a/x#11",
                          "review": 101, "by": "rev", "result": "changes", "submitted_at": "2026-09-02T01:02:03Z",
                          "bot": False})
        self.assertIn("PR #11 review by rev: changes requested", (store.HOME / s / "log.md").read_text())
        # Another PR's review with the same id is another review
        run(*t, "new", "S-2", "--title", "Two", "--branch", "f2")
        run(*t, "set", "S-2", "status=in-progress")
        prs.append(pull(12, branch="f2", rounds=[review_round(101, "rev", "APPROVED", "2026-09-03T00:00:00Z")]))
        self.synced(t, prs)
        self.assertEqual([(e["pr"], e["review"]) for e in self.review_events(s)], [("a/x#11", 101), ("a/x#12", 101)])

    def test_a_later_review_is_logged_as_another(self):
        s, t = self.reviewed()
        first = review_round(101, "rev", "CHANGES_REQUESTED", "2026-09-02T00:00:00Z")
        self.synced(t, [pull(11, branch="f1", rounds=[first])])
        later = review_round(102, "rev", "APPROVED", "2026-09-03T00:00:00Z")
        self.synced(t, [pull(11, branch="f1", rounds=[first, later])])
        self.assertEqual([(e["review"], e["result"], e["text"]) for e in self.review_events(s)],
                         [(101, "changes", "PR #11 review by rev: changes requested"),
                          (102, "approved", "PR #11 review by rev: approved")])
        # The sync that sees the merge still logs the last review
        last = review_round(103, "lead", "APPROVED", "2026-09-04T00:00:00Z")
        self.synced(t, [pull(11, branch="f1", state="MERGED", merged_at="2026-09-04T01:00:00Z",
                             rounds=[first, later, last])])
        self.assertEqual([e["review"] for e in self.review_events(s)], [101, 102, 103])

    def test_a_comment_pending_or_dismissed_review_is_not_logged(self):
        s, t = self.reviewed()
        at = "2026-09-02T00:00:00Z"
        rounds = [review_round(101, "rev", "COMMENTED", at), review_round(102, "rev", "PENDING", at),
                  review_round(103, "rev", "DISMISSED", at), {**review_round(104, "x", "APPROVED", at), "author": None},
                  {**review_round(105, "rev", "APPROVED", at), "databaseId": None}]
        self.synced(t, [pull(11, branch="f1", rounds=rounds)])
        self.assertEqual(self.review_events(s), [])

    def test_a_bots_review_is_logged_and_marked(self):
        s, t = self.reviewed()
        self.synced(t, [pull(11, branch="f1")])
        tr = model.tracker_at(s)
        before = moves.whose_move(tr, tr.lookup("S-1")).text()
        bot = review_round(101, "lint", "CHANGES_REQUESTED", "2026-09-02T00:00:00Z", bot=True)
        self.synced(t, [pull(11, branch="f1", rounds=[bot], reviews=[bot])])
        [e] = self.review_events(s)
        self.assertEqual((e["by"], e["result"], e["bot"]), ("lint", "changes", True))
        tr = model.tracker_at(s)
        self.assertEqual(moves.whose_move(tr, tr.lookup("S-1")).text(), before)  # a bot's review moves nothing

    def test_a_review_on_a_pr_of_two_tickets_names_both(self):
        s, t = self.reviewed("S-1", "S-2")
        self.synced(t, [pull(11, branch="f1", rounds=[review_round(101, "rev", "APPROVED", "2026-09-02T00:00:00Z")])])
        [e] = self.review_events(s)
        self.assertEqual(e["refs"], ["S-1", "S-2"])

    def test_a_review_in_a_tracker_of_two_repos_names_its_repo(self):
        s, t = self.reviewed(repos="a/x,b/y")
        self.synced(t, [pull(11, branch="f1", rounds=[review_round(101, "rev", "APPROVED", "2026-09-02T00:00:00Z")])])
        self.assertEqual([e["text"] for e in self.review_events(s)], ["PR a/x#11 review by rev: approved"])

    def test_reviews_already_there_come_as_one_watch_line(self):
        s, t = self.reviewed()
        rounds = [review_round(100 + i, "rev", "CHANGES_REQUESTED" if i < 4 else "APPROVED",
                               f"2026-09-0{i + 1}T00:00:00Z") for i in range(5)]
        prs = [pull(11, branch="f1", rounds=rounds)]
        w, now = watcher.Watcher(model.tracker_at(s)), time.time()
        w.claim(now)
        w.synced = now  # the watch's own sync is not due: this test syncs
        self.synced(t, prs)
        self.assertEqual(w.poll(now), [])  # the change settles first
        lines = w.poll(now + watcher.SETTLE_S)
        self.assertEqual(len([x for x in lines if "review by" in x]), 1, lines)
        self.assertEqual("".join(lines).count("review by"), 1, lines)
        self.assertIn("S-1: ", lines[0])
        self.assertIn("PR #11 review by rev: approved (+4 earlier)", lines[0])
        self.assertEqual(len(self.review_events(s)), 5)

    def test_a_sync_that_gh_fails_logs_no_review(self):
        s, t = self.reviewed()
        with mock.patch.object(github, "gh", lambda *args, fields=None, partial=False: None):
            self.assertIn("sync skipped", run(*t, "sync"))
        self.assertEqual(self.review_events(s), [])

    def test_the_title_is_kept_with_the_pr(self):
        s, t = self.reviewed()
        self.synced(t, [pull(11, branch="f1", title="Add the parser")])
        tr = model.tracker_at(s)
        self.assertEqual(tr.raw_state()["prs"]["a/x#11"]["title"], "Add the parser")
        [e] = [e for e in model.read_events(tr) if e["kind"] == "pr.state"]
        self.assertEqual(e["title"], "Add the parser")
        self.synced(t, [pull(11, branch="f1", title="Add the parser", state="MERGED",
                             merged_at="2026-09-04T01:00:00Z")])
        merged = [e for e in model.read_events(model.tracker_at(s)) if e["kind"] == "pr.state"][-1]
        self.assertEqual((merged["state"], merged["title"]), ("merged", "Add the parser"))

    def test_stale_sync_refreshes_in_the_background(self):
        (s, _), (local, _) = new_tracker("Fresh", "a/x"), new_tracker("Local")
        with mock.patch.object(hooks, "spawn") as spawn:
            fields = {}
            hooks.sync_later(model.tracker_at(s), {}, fields)
            hooks.sync_later(model.tracker_at(s), fields, {})  # asked already: not again within the interval
            hooks.sync_later(model.tracker_at(local), {}, {})  # no repo: nothing to ask
        spawn.assert_called_once_with("--tracker", s, "sync")

    def test_session_start_does_not_wait_for_github(self):
        (s, _), sid, work = new_tracker("Starts fast", "a/x"), f"sid{time.monotonic_ns()}", repo("feat/F-1")
        run("--tracker", s, "new", "F-1", "--title", "Fast work", "--branch", "feat/F-1")
        session.save_session(sid, tracker=s, cwd=str(work))
        data = {"session_id": sid, "cwd": str(work), "source": "startup"}
        with mock.patch.object(github, "gh") as gh, mock.patch.object(hooks, "spawn") as spawn, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            hooks.hook_session_start(data)
            hooks.hook_session_start(data)  # asked already: not again within the interval
        gh.assert_not_called()
        spawn.assert_called_once_with("--tracker", s, "sync")  # the next message reports what it changes
        self.assertIn("Starts fast", out.getvalue())


class Starts(unittest.TestCase):
    """Where a todo ticket's work can start: the default branch, or the branch of the work under way it waits on."""

    def test_start_points(self):
        s = slug()
        root = store.HOME / s
        for d in ("tickets", "decisions"):
            (root / d).mkdir(parents=True)
        (root / "README.md").write_text(f"---\ntitle: T\nschema: {schema.SCHEMA}\nrepo: a/x\n---\n")
        (root / "decisions" / "D-01.md").write_text("---\nid: D-01\ntitle: Store\nstatus: open\n---\n")
        ticket(root, "A-1", status="in-progress", branch="fa")
        ticket(root, "A-2", status="in-progress", branch="fb", prs=["7:open"])  # on A-1's branch
        (root / ".state.json").write_text(json.dumps({"prs": {"a/x#7": {"base": "fa"}}}))
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
        self.assertIn("S-2  —  S-2 — stack on fb (A-2 in-review, #7; you: no review requested)\n", ready)
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
        self.assertIn({"label": "start", "text": "stack on fa (A-1 in-progress; you: no PR yet)"},
                      data(s)["records"]["S-1"]["deps"])  # the opened ticket in a UI

    def test_start_checks_the_branch(self):
        (_, t), work = new_tracker("Stacked"), repo("main")

        def branch(*args: str) -> None:
            subprocess.run(["git", "-C", str(work), "checkout", "-q", *args], check=True)

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
    """A UI shows the Claude sessions that run on this machine and that `tracker start` tied to the tracker."""

    def test_live_sessions(self):
        (s, t), n = new_tracker("Watched"), time.monotonic_ns()
        work, spare = repo("feat/L-1"), repo("main")
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
        tr = model.tracker_at(s)
        self.assertEqual([x.name for x in session.live_sessions(s)], ["agent-a", "agent-b"])

        now = viewmodel.now_view(tr)
        self.assertEqual(now["items"][0]["agents"],
                         [{"status": "busy", "word": "working since Tue 09:05", "cwd": str(work)}])
        self.assertNotIn("line", now["items"][0])  # an opened ticket has it from its record
        spare_item = now["items"][-1]
        self.assertEqual((spare_item["key"], spare_item["kind"]), (f"agent-agent-b{n}", "agent"))
        self.assertFalse(spare_item.keys() & {"title", "meta", "line"})
        self.assertEqual(spare_item["on"], [{"id": "L-2", "stage": "todo", "state": "ready"}])
        self.assertEqual(spare_item["agents"][0]["word"], "idle since Tue 09:05")
        self.assertNotRegex(json.dumps(now), "agent-[cde]")


class Flow(unittest.TestCase):
    """The commands a session runs, from the plan to a paused branch, in a git repo on a ticket's branch."""

    def test_plan_decide_build_pause(self):
        (s, t), work = new_tracker("Work"), repo("feat/T-2-api")
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
        self.assertEqual(model.tracker_at(s).lookup("T-2").get("branch"), "feat/T-2-api")
        run(*t, "step", cwd=work)  # the first mark is the baseline: only later commits are new work
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
        log = (store.HOME / s / "log.md").read_text()
        self.assertIn("[T-2] Routes built", log)
        self.assertIn("[T-2] Commits on feat/T-2-api:", log)
        t2 = model.tracker_at(s).lookup("T-2")
        self.assertEqual((t2.get("next"), t2.carry_forward), ("Auth middleware", ["GET /users is public"]))
        self.assertEqual(data(s)["records"]["T-2"]["line"], {"label": "next", "text": "Auth middleware"})
        self.assertIn("Decided D-01 Auth scheme (unattributed, said me): JWT", log)  # a terminal: never a human


class FileSafety(unittest.TestCase):
    """A tracker write changes only its intended file and keeps private files private."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=BASE))

    def link(self, path: Path, target: Path):
        try:
            path.symlink_to(target, target_is_directory=target.is_dir())
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

    def test_the_lock_keeps_out_another_thread_of_the_process(self):
        """A watch's sync runs in a thread while its look (`session.news`) holds the lock: the two take turns."""
        order = []

        def sync_thread():
            with store.locked():
                order.append("sync")

        with store.locked():
            other = threading.Thread(target=sync_thread)
            other.start()
            other.join(0.3)
            order.append("look")
        other.join()
        self.assertEqual(order, ["look", "sync"])

    def test_atomic_write_ignores_the_old_temporary_name(self):
        outside, path = self.root / "outside", self.root / "record.md"
        outside.write_text("keep me")
        self.link(path.with_suffix(".md.tmp"), outside)
        store.atomic_write(path, "new ünicode\n")
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
                    store.atomic_write(path, "replacement")
                self.assertTrue(path.is_symlink())
                self.assertEqual(outside.read_text() if exists else outside.exists(), "keep me" if exists else False)

    @unittest.skipIf(system.WINDOWS, "POSIX permission bits")
    def test_atomic_write_preserves_modes_and_creates_private_files(self):
        old_mask = os.umask(0o022)
        try:
            for mode in (None, 0o600, 0o640, 0o644):
                with self.subTest(mode=mode):
                    path = self.root / f"record-{mode}.md"
                    if mode is not None:
                        path.write_text("old")
                        path.chmod(mode)
                    with store.atomic_file(path) as f:
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
        with store.atomic_file(path) as f:
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
            with store.atomic_file(path) as f:
                f.write(b"partial")
                raise ValueError("interrupted write")
        self.assertEqual(path.read_text(), "keep me")
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        with mock.patch.object(store.os, "replace", side_effect=PermissionError), \
                mock.patch.object(store.time, "sleep"), self.assertRaises(PermissionError):
            store.atomic_write(path, "replacement")
        self.assertEqual(path.read_text(), "keep me")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_attach_rejects_symlinks_even_with_force(self):
        s, _ = new_tracker("Safe attachments")
        folder = store.HOME / s / "evidence"
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
        s, _ = new_tracker("Safe directory")
        self.link(store.HOME / s / "evidence", self.root)
        source = self.root / "source.txt"
        source.write_text("keep me")
        out = run("--tracker", s, "attach", str(source), "--name", "copy.txt", code=2)
        self.assertIn("must stay inside the tracker", out)
        self.assertFalse((self.root / "copy.txt").exists())

    def test_attach_copies_binary_files_and_preserves_private_replacements(self):
        s, _ = new_tracker("Copies")
        source = self.root / "source.bin"
        source.write_bytes(b"\x00\xff\r\n")
        run("--tracker", s, "attach", str(source))
        dest = store.HOME / s / "evidence/source.bin"
        self.assertEqual(dest.read_bytes(), source.read_bytes())
        if not system.WINDOWS:
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
        source.write_bytes(b"changed\x00")
        run("--tracker", s, "attach", str(source), "--force")
        self.assertEqual(dest.read_bytes(), source.read_bytes())
        if not system.WINDOWS:
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)


class Writes(unittest.TestCase):
    """What a write refuses, what it reports after itself, and the views that read records back."""

    def test_limits_check_and_show(self):
        s, t = new_tracker("Limits")
        run(*t, "new", "L-1", "--title", "One")
        long = "word " * 100
        self.assertIn("over 400: one concrete action", run(*t, "set", "L-1", f"next={long}", code=2))
        run(*t, "log", long)  # only `next` has a limit: every brief prints it whole
        self.assertIn(" ".join(long.split()), (store.HOME / s / "log.md").read_text())
        log = (store.HOME / s / "log.md").read_text()
        run(*t, "step", "L-1", "Short", "--carry", "fits", "--next", long, code=2)
        self.assertEqual((store.HOME / s / "log.md").read_text(), log)  # refused before any write
        self.assertNotIn("fits", run(*t, "show", "L-1"))

        for i in range(6):
            run(*t, "add", "L-1", "carry", f"Fact {i}")
        done = run(*t, "step", "L-1", "--done", "all of it")
        self.assertIn("added `tracker check` problems:\n⚠ L-1: done but Carry forward has 6 bullets", done)
        self.assertEqual(done.count("Carry forward has 6"), 1)
        # The problem was there before. Each write ends by saying what `check` found, so no `check` follows it.
        self.assertTrue(run(*t, "set", "L-1", "title=One thing").endswith("\n`check`: no new problems\n"))

        # A Carry forward bullet and a summary of any length are kept whole; a brief cuts the bullet to its own limit.
        run(*t, "new", "L-3", "--title", "Three", "--depends", "L-1")
        fact = "fact " * 2000
        run(*t, "set", "L-3", "status=in-progress")
        run(*t, "step", "L-3", "--carry", fact, "--done", long)
        three = model.tracker_at(s).lookup("L-3")
        self.assertEqual((three.carry_forward, three.get("summary").strip()), ([fact.strip()], long.strip()))
        run(*t, "new", "L-4", "--title", "Four", "--depends", "L-3")
        tr = model.tracker_at(s)
        shown = views.carry_lines(tr, [tr.lookup("L-4")], limit=views.BRIEF_CARRY_CHARS)
        self.assertEqual([len(x) <= views.BRIEF_CARRY_CHARS + 8 for x in shown if x.startswith("  L-3: ")], [True])
        self.assertIn(fact.strip(), "\n".join(views.carry_lines(tr, [tr.lookup("L-4")])))  # `context` has it whole

        run(*t, "new", "L-2", "--title", "Two")
        run(*t, "log", "about two", "--ref", "l-2")
        self.assertEqual(run(*t, "history", "--ref", "L-2").splitlines(),
                         [f"{schema.today()} [L-2] Added L-2 Two", f"{schema.today()} [L-2] about two"])
        self.assertEqual(run(*t, "history", "--last", "1"), f"{schema.today()} [L-2] about two\n")
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
        self.assertEqual((store.HOME / s / "evidence/run.md").read_text(), "run 1\n\n## Run 2\n\nIt `works`.\n")
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

        path = store.HOME / s / "tickets" / "L-1.md"
        path.write_text(path.read_text().replace("## Carry forward", "## carry Forward"))  # a hand edit
        run(*t, "add", "L-1", "carry", "Fact 6")
        self.assertEqual(path.read_text().lower().count("## carry forward"), 1)  # found, not added again

        for name in ("..", f"../{Path(store.HOME).name}/{s}"):  # a slug is a folder in TRACKER_HOME, not a path
            self.assertIn("no tracker", run("--tracker", name, "index", code=2))
            self.assertIn("no tracker", run("context", f"{name}:L-1", code=2))

    def test_tickets_with_no_issue(self):
        s, t = new_tracker("Issues")
        run(*t, "new", "I-1", "--title", "One")
        self.assertIn("`check`: no new problems", run(*t, "new", "I-2", "--title", "Two"))  # no issue tracker in use
        self.assertNotIn("no issue", run(*t, "check"))
        run(*t, "new", "I-3", "--title", "Three")
        run(*t, "set", "I-3", "status=done")
        said_ = run(*t, "add", "I-1", "link", "Issue: [PROJ-1 One](https://x.y/PROJ-1)")
        self.assertIn("added `tracker check` problems:\n⚠ I-2: no issue\n`check`: fix the problems above", said_)
        report = run(*t, "check")
        self.assertIn("⚠ I-2: no issue", report.splitlines())
        self.assertNotIn("I-1: no issue", report)
        self.assertNotIn("I-3: no issue", report)  # closed
        said_ = run(*t, "new", "I-4", "--title", "Four")
        self.assertIn("added `tracker check` problems:\n⚠ I-4: no issue\n", said_)
        self.assertNotIn("I-2", said_)  # said once, by the write that added it
        self.assertIn("issues 'sometimes' is not one of optional|required",
                      run(*t, "set", "tracker", "issues=sometimes", code=2))
        run(*t, "set", "tracker", "issues=required")
        report = run(*t, "check", code=1)
        self.assertEqual([x for x in report.splitlines() if "no issue" in x],
                         ["✗ I-2: no issue (README issues: required)", "✗ I-4: no issue (README issues: required)"])
        run(*t, "drop", "I-1", "link", "PROJ-1")  # no ticket has an issue: still required
        self.assertIn("✗ I-1: no issue (README issues: required)", run(*t, "check", code=1).splitlines())
        run(*t, "set", "tracker", "issues=optional")
        self.assertNotIn("no issue", run(*t, "check"))

    def test_templates_take_the_contract_limits(self):
        s, _ = new_tracker("Limits")
        run("--tracker", s, "new", "M-1", "--title", "One")
        readme, ticket = (store.HOME / s / "README.md").read_text(), (store.HOME / s / "tickets/M-1.md").read_text()
        self.assertIn(f"~{schema.README_TOKEN_BUDGET:,} tokens", readme)
        self.assertIn(f"Max {schema.MERGED_CARRY_FORWARD_MAX} bullets", ticket)
        self.assertNotIn("{{", readme + ticket)
        self.assertIn("\nstatus: todo\n", ticket)

    def test_a_write_reports_on_the_tracker_it_wrote(self):
        """`slug:ID` writes another tracker than the one the command is on: what it unblocks and adds to `check` is
        that tracker's."""
        here, _ = new_tracker("Here")
        there, _ = new_tracker("There")
        run("--tracker", there, "new", "W-1", "--title", "One")
        run("--tracker", there, "new", "W-2", "--title", "Two", "--depends", "W-1")
        out = run("--tracker", here, "step", f"{there}:W-1", "--done", "all of it")
        self.assertIn("nothing blocks W-2 now", out)
        self.assertEqual(out.count("`check`:"), 1)

    def test_writes_log_what_history_needs(self):
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(BASE)  # on no git branch: the checkout's own branch would be recorded on the ticket it starts
        s, t = new_tracker("Logs")
        run(*t, "new", "G-1", "--title", "One")
        run(*t, "new", "G-2", "--title", "Two", "--depends", "G-1")
        run(*t, "new", "G-3", "--title", "Three", "--depends", "G-2")
        log = store.HOME / s / "log.md"
        def last() -> str:
            return log.read_text().splitlines()[-1].removeprefix(f"- {schema.today()} ")

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
        self.assertEqual(log.read_text().splitlines()[-2:], [f"- {schema.today()} [G-2] Tested by hand",
                                                             f"- {schema.today()} [G-2] G-2 done: two delivered"])
        run(*t, "set", "tracker", "status=active")
        self.assertEqual(last(), "The work is active now")
    def test_put_stdin_and_the_tracker_folder(self):
        (s, t), sid, work = new_tracker("Put"), f"sid{time.monotonic_ns()}", repo("feat/P-1")
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
        plan = '- `api/y.py` answers "ready"\n- Done when $HOME is not read\n'
        with piped(plan):  # a ticket with its Plan in one command
            self.assertIn("no new problems", run(*t, "new", "P-4", "--title", "Four", "--plan", "-"))
        self.assertEqual(markdown.section(run(*t, "show", "P-4"), "Plan").strip(), plan.strip())
        self.assertIn("no new problems", run(*t, "add", "P-4", "link", "Code: [api/y.py](https://x.y/api/y.py) — it"))
        self.assertIn(", Code, ", run(*t, "add", "--help"))  # the labels, where the link line is written
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
            folder = store.HOME / s
            self.assertIn("feat/P-1 up to date", run("step", cwd=folder))  # from the tracker folder: the project's
        self.assertIn("run it from the project", run(*t, "step", cwd=folder, code=2))  # no session to ask
        tr = model.tracker_at(s)
        self.assertEqual(tr.lookup("P-1").get("next"), "Ship it")
        self.assertIn('[P-1] Fixed the "quoted" `thing`', (tr.root / "log.md").read_text())


class Upgrade(unittest.TestCase):
    def old_tracker(self, schema=3) -> tuple[str, tuple[str, str]]:
        s, t = new_tracker("Old")
        model.tracker_at(s).readme.save({"schema": schema})
        return s, t

    def test_any_command_migrates_every_older_tracker_once(self):
        s, t = self.old_tracker(schema=schema.SCHEMA)  # any command migrates: the older formats go on last
        other, _ = self.old_tracker(schema=schema.SCHEMA)
        model.tracker_at(s).readme.save({"schema": 3})
        model.tracker_at(other).readme.save({"schema": 2})
        (store.HOME / s / ".DS_Store").write_text("finder")
        (store.HOME / s / "evidence").mkdir()
        (store.HOME / s / "evidence" / "run.log").write_text("no migration changes it")
        run("list")
        for name in (s, other):
            self.assertEqual(model.tracker_at(name).schema, schema.SCHEMA)
        backup = store.HOME / ".backups" / f"{s}-schema3"
        self.assertEqual(re.search(r"schema: (\d+)", (backup / "README.md").read_text())[1], "3")
        self.assertFalse((backup / ".DS_Store").exists())  # the backup holds the tracker, not Finder's files
        self.assertFalse((backup / "evidence").exists())  # or its evidence, which stays where it is
        self.assertTrue((store.HOME / s / "evidence" / "run.log").exists())
        log = (store.HOME / s / "log.md").read_text()
        self.assertEqual(log.count(f"Migrated to tracker format {schema.SCHEMA}"), 1)
        self.assertIn(str(backup), log)
        run("list")
        self.assertEqual((store.HOME / s / "log.md").read_text(), log)  # once
        self.assertNotIn("older", run(*t, "check"))

    def test_a_new_plugin_version_removes_only_the_old_migrate_commands_backups(self):
        backups = store.HOME / ".backups"
        old, kept, own = (backups / x for x in ("gone-20261106-143702", "kept-schema3", "kept-before-reduce"))
        for d in (old, kept, own):
            (d / "evidence").mkdir(parents=True)
        store.write_json(store.CLI_FILE, {"package": "/an/older/version"})
        hooks.record_version()
        self.assertEqual([d.exists() for d in (old, kept, own)], [False, True, True])

    def test_a_command_stops_the_old_browser_viewer_and_no_other_program(self):
        server = ("import http.server\n"
                  "class Ping(http.server.BaseHTTPRequestHandler):\n"
                  "    def do_GET(self):\n"
                  "        self.send_response(200); self.end_headers(); self.wfile.write(b'work-tracker 0.31.1')\n"
                  "    def log_message(self, *args): pass\n"
                  "s = http.server.HTTPServer(('127.0.0.1', 0), Ping)\n"
                  "print(s.server_address[1], flush=True)\n"
                  "s.serve_forever()\n")
        def started(code: str, **kw) -> subprocess.Popen:
            process = subprocess.Popen([sys.executable, "-c", code], **kw)
            self.addCleanup(lambda: (process.kill(), process.wait()))
            return process
        viewer = started(server, stdout=subprocess.PIPE, text=True)
        port = int(viewer.stdout.readline())
        viewer.stdout.close()
        other = started("import time; time.sleep(60)")
        file = store.HOME / ".viewer.json"
        store.HOME.mkdir(parents=True, exist_ok=True)
        store.write_json(file, {"port": port, "pid": viewer.pid})
        run("list")
        viewer.wait(10)
        self.assertFalse(file.exists())
        store.write_json(file, {"port": port, "pid": other.pid})  # nothing answers: the pid may be another program's
        run("list")
        self.assertIsNone(other.poll())
        self.assertFalse(file.exists())

    def test_a_hook_migrates_too_and_an_archived_tracker(self):
        s, _ = self.old_tracker(schema=schema.SCHEMA)
        archived, _ = self.old_tracker(schema=schema.SCHEMA)
        run("archive", archived)
        sid, work = f"sid{time.monotonic_ns()}", repo("feat/U-1")
        session.save_session(sid, tracker=s, cwd=str(work))
        model.tracker_at(s).readme.save({"schema": 3})
        model.archived_at(archived).readme.save({"schema": 3})
        hook("stop", sid, work)
        self.assertEqual(model.tracker_at(s).schema, schema.SCHEMA)
        self.assertEqual(model.archived_at(archived).schema, schema.SCHEMA)

    def test_a_tracker_that_fails_stays_says_why_and_stops_nothing(self):
        s, t = self.old_tracker(schema=schema.SCHEMA)
        healthy, _ = self.old_tracker(schema=schema.SCHEMA)
        model.tracker_at(s).readme.save({"schema": 3})
        model.tracker_at(healthy).readme.save({"schema": 3})
        real = contract.migrate
        calls = []

        def fails_on_one(tr):
            calls.append(tr.slug)
            if tr.slug == s:
                raise OSError("disk full")
            real(tr)

        with mock.patch.object(contract, "migrate", fails_on_one):
            said = run("list")
            report = run(*t, "check")
        # the refusal's sentence
        self.assertIn("fix the error each `tracker` command prints when it tries to migrate it", report)
        self.assertIn(f"could not migrate {s} to format {schema.SCHEMA}: disk full", report)  # what it reports
        self.assertIn("Old", said)
        self.assertIn(f"could not migrate {s} to format {schema.SCHEMA}: disk full", said)
        self.assertEqual(model.tracker_at(healthy).schema, schema.SCHEMA)
        self.assertEqual(model.tracker_at(s).schema, 3)
        self.assertIn(s, calls)
        run("list")  # it tries again, on the first backup
        self.assertEqual(model.tracker_at(s).schema, schema.SCHEMA)
        self.assertEqual(len(list((store.HOME / ".backups").glob(f"{s}-*"))), 1)

    def test_a_readme_that_cannot_be_read_is_left_alone(self):
        s, _ = self.old_tracker()
        readme = store.HOME / s / "README.md"
        readme.write_text("no frontmatter at all\n")
        run("list")
        self.assertEqual(readme.read_text(), "no frontmatter at all\n")
        self.assertFalse((store.HOME / ".backups" / f"{s}-schema0").exists())

    def test_migrate_turns_priority_words_into_numbers(self):
        s, t = self.old_tracker(schema=schema.SCHEMA)
        words = {"T-1": "High", "T-2": "p0", "T-3": "Lowest", "T-4": "Someday"}
        for ident, word in words.items():
            run(*t, "new", ident, "--title", ident)
            run(*t, "add", ident, "link", f"Issue: [SC-{ident[2]} Story](https://issues.example/{ident[2]})")
            run(*t, "issue", ident)
            model.tracker_at(s).lookup(ident).save({"priority": word})
        self.assertIn("T-4: priority 'Someday' is not 0-4", run(*t, "check", code=1))
        self.assertIn("no ticket's issue fields are to read", run(*t, "issue"))
        model.tracker_at(s).readme.save({"schema": 1})
        run("list")
        tr = model.tracker_at(s)
        self.assertEqual([tr.lookup(x).meta.get("priority") for x in words], ["1", "0", "4", None])
        # Read again: T-4 for its priority, and every open ticket for the size and due day format 3 reads
        self.assertEqual(re.findall(r"^(T-\d+) ", run(*t, "issue"), re.M), ["T-1", "T-2", "T-3", "T-4"])
        self.assertNotIn("priority", run(*t, "check"))

    def test_migrate_to_format_5_keeps_only_choices_in_options(self):
        s, t = self.old_tracker(schema=schema.SCHEMA)
        run(*t, "decide", "Which store")
        run(*t, "decide", "Which queue", "--resolve", "SQS", "--by", "me")
        old = ("- Option A: the first\n- 2026-01-02: a fact\n  - its detail\n"
               "- 2026-01-03: Evidence: [f.txt](evidence/f.txt)\nSome prose\n* Option B\n")
        tr = model.tracker_at(s)
        for ident in ("D-01", "D-02"):
            model.put_section(tr.lookup(ident), "Options", old)
        model.tracker_at(s).readme.save({"schema": schema.SCHEMA - 1})
        run("list")
        tr = model.tracker_at(s)
        for ident in ("D-01", "D-02"):
            d = tr.lookup(ident)
            self.assertEqual(markdown.section(d.body, "Options"), "- Option A: the first\n- Option B")
            self.assertEqual(markdown.section(d.body, "Notes"), "- 2026-01-02: a fact\n  - its detail\n"
                             "- 2026-01-03: Evidence: [f.txt](evidence/f.txt)\n\nSome prose")
        self.assertEqual(markdown.headings(tr.lookup("D-02").body), ["Question", "Options", "Notes", "Resolution"])
        found = [x for x in run(*t, "check").splitlines() if "a decision for a human" not in x]  # none recommended
        self.assertNotIn("D-0", "\n".join(found))

    OLD_LOG = ("# Log — Old\n\nAppend-only. Newest at the bottom.\n\n"
               "- 2026-01-02 [T-1 T-2] A plain line\n"
               "- 2026-01-03 [D-1 T-1] Opened D-1 Which store\n"
               "- 2026-01-04 [D-1] Decided D-1 Which store (me): S3\n"
               "- 2026-01-05 [A-01] Action for the user: x\n"
               "- 2026-01-06 [T-1] Commits on b: a s; and 3 more\n"
               "- no date here\n")

    def format_5_log(self) -> tuple[str, tuple[str, str], Path]:
        """A tracker as format 5 left it: log.md and no event file."""
        s, t = self.old_tracker(schema=5)
        root = store.HOME / s
        (root / schema.EVENTS_FILE).unlink()
        (root / "log.md").write_text(self.OLD_LOG)
        return s, t, root

    def test_format_5_log_becomes_legacy_events(self):
        s, t, root = self.format_5_log()
        run(*t, "index")
        events = model.read_events(model.tracker_at(s))
        self.assertEqual([e["seq"] for e in events], list(range(1, 8)))
        legacy = [e for e in events if e["kind"] == "legacy"]
        self.assertEqual([(e["day"], e["refs"], e.get("decision")) for e in legacy],
                         [("2026-01-02", ["T-1", "T-2"], None), ("2026-01-03", ["D-1", "T-1"], "D-1"),
                          ("2026-01-04", ["D-1"], "D-1"), ("2026-01-05", ["A-01"], None),
                          ("2026-01-06", ["T-1"], None), ("", [], None)])
        self.assertEqual(legacy[-1]["text"], "- no date here")  # kept whole
        self.assertEqual({(e["session"], e["via"], e["version"], e["repo"], "at" in e) for e in legacy},
                         {("", "", "", "", False)})
        self.assertEqual([*legacy[0]][:6], ["seq", "kind", "session", "via", "version", "repo"])
        migrated = events[-1]
        self.assertEqual((migrated["kind"], migrated["schema"]), ("migrated", schema.SCHEMA))
        self.assertRegex(migrated["at"], r"Z$")
        self.assertEqual((root / "log.md").read_text(), self.OLD_LOG + model.log_line(migrated) + "\n")
        self.assertEqual([model.log_line(e) for e in legacy],
                         [ln for ln in self.OLD_LOG.splitlines() if ln.startswith("- ")])

    def test_migration_runs_once(self):
        s, t, root = self.format_5_log()
        run(*t, "index")
        before = (root / schema.EVENTS_FILE).read_bytes()
        model.tracker_at(s).readme.save({"schema": 5})  # a run that stopped after the events, before the format
        run(*t, "index")
        events = model.read_events(model.tracker_at(s))
        self.assertEqual([e["kind"] for e in events].count("legacy"), 6)
        self.assertTrue((root / schema.EVENTS_FILE).read_bytes().startswith(before))

    def test_history_is_the_same_before_and_after(self):
        _, t, _ = self.format_5_log()
        before = [ln[2:] for ln in self.OLD_LOG.splitlines() if re.match(r"- \d{4}-\d\d-\d\d ", ln)]  # as 0.32
        after = run(*t, "history", "--last", "0").splitlines()
        self.assertEqual(after[:-1], before)
        self.assertTrue(after[-1].startswith(f"{schema.today()} Migrated to tracker format {schema.SCHEMA}"))

    def test_a_shown_decision_leaves_out_its_migrated_lines(self):
        s, t = self.old_tracker(schema=5)
        root = store.HOME / s
        (root / schema.EVENTS_FILE).unlink()
        (root / "log.md").write_text("# Log\n\n- 2026-01-03 [D-01] Opened D-01 Which store\n"
                                     "- 2026-01-04 [D-01] Decided D-01 Which store (me): S3\n"
                                     "- 2026-01-05 [D-01] A note on D-01\n")
        run(*t, "index")  # migrates
        lines = views.history_lines(model.tracker_at(s), [], "", 0, {"D-01"})
        self.assertEqual(lines[0], "2026-01-05 [D-01] A note on D-01")  # only the lines the view does not show
        self.assertTrue(lines[1].startswith(f"{schema.today()} Migrated to tracker format"))
        self.assertEqual(len(lines), 2)

    def unmigratable(self) -> tuple[str, tuple[str, str], Path, str]:
        """A format 5 tracker with two log lines whose README cannot migrate (a key twice); its README as it was."""
        s, t = self.old_tracker(schema=5)
        root = store.HOME / s
        (root / schema.EVENTS_FILE).unlink()
        (root / "log.md").write_text("# Log\n\n- 2026-01-02 [T-1] First old line\n- 2026-01-03 Second old line\n")
        readme = (root / "README.md").read_text()
        (root / "README.md").write_text(readme.replace("\nschema: 5\n", "\nschema: 5\nowner: you\n"))
        return s, t, root, readme

    def test_a_tracker_that_cannot_migrate_takes_no_write_and_keeps_its_log(self):
        s, t, root, readme = self.unmigratable()
        before = tree(root)
        said = run(*t, "log", "A note", code=watcher.REFUSED)
        self.assertRegex(said, rf"\Atracker: {s} {OLDER}[^\n]*`tracker check --tracker {s}`[^\n]*\n\Z")
        self.assertEqual(tree(root), before)  # no event
        report = run(*t, "check", code=1)  # what the refusal says to run reports it
        self.assertIn("appears twice", report)
        self.assertIn(f"README.md: {contract.other_format(root)}\n", report)  # as the refusal says it
        self.assertNotIn("could not migrate", report)  # a README that cannot be read stops the migration silently
        (root / "README.md").write_text(readme)  # fixed
        run(*t, "index")  # migrates
        self.assertEqual(run(*t, "history", "--last", "0").splitlines()[:2],
                         ["2026-01-02 [T-1] First old line", "2026-01-03 Second old line"])

    def test_a_migration_that_raises_leaves_its_tracker_refused(self):
        s, t = self.old_tracker(schema=5)
        with mock.patch.object(contract, "migrate", mock.Mock(side_effect=OSError("disk full"))):
            said = run(*t, "log", "A note", code=watcher.REFUSED)  # the same run: the guard is back on
        self.assertIn(f"could not migrate {s}", said)
        self.assertIn(OLDER, said)
        self.assertNotIn(FIX, said)  # its README is fine
        self.assertIn("fix the error each `tracker` command prints when it tries to migrate it", said)

    def test_start_on_a_tracker_that_cannot_migrate_changes_nothing(self):
        s, _, root, _ = self.unmigratable()
        sid, work = f"old{time.monotonic_ns()}", repo("main")
        before = tree(root)
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            self.assertIn(OLDER, run("start", s, cwd=work, code=watcher.REFUSED))
        self.assertNotIn("tracker", session.load_session(sid))  # the session was not moved onto it
        self.assertEqual(tree(root), before)

    def test_a_hook_on_a_tracker_that_cannot_migrate_writes_nothing_and_tells_once(self):
        s, _ = self.old_tracker(schema=schema.SCHEMA)
        sid, work = f"old{time.monotonic_ns()}", repo("feat/U-1")
        session.save_session(sid, tracker=s, cwd=str(work))
        hook("session-start", sid, work, source="startup")  # the branch's mark
        root = store.HOME / s
        (root / "README.md").write_text((root / "README.md").read_text().replace(
            f"\nschema: {schema.SCHEMA}\n", "\nschema: 5\nowner: you\n"))
        before = tree(root)
        commit(work, "on a tracker that cannot migrate")
        told = tool("Bash", sid, work, tool_input={"command": "git commit -m x"})  # exits 0: `hook` checks
        self.assertEqual(told["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(f"[work-tracker] {s} {OLDER}", told["hookSpecificOutput"]["additionalContext"])
        self.assertIsNone(hook("prompt", sid, work, prompt="go"))  # once per context
        self.assertIsNone(hook("stop", sid, work))
        self.assertEqual(tree(root), before)

    def test_migrate_to_format_3_reads_open_issues_again(self):
        s, t = self.old_tracker(schema=schema.SCHEMA)
        for ident in ("T-1", "T-2"):
            run(*t, "new", ident, "--title", ident)
            run(*t, "add", ident, "link", f"Issue: [SC-{ident[2]} Story](https://issues.example/{ident[2]})")
            run(*t, "issue", ident)
        run(*t, "set", "T-2", "status=done")
        model.tracker_at(s).readme.save({"schema": 2})
        self.assertEqual(re.findall(r"^(T-\d+) ", run(*t, "issue"), re.M), ["T-1"])  # not the closed T-2


def tree(root: Path) -> dict[str, bytes | None]:
    """Every file under `root` with its bytes, and every folder."""
    return {str(p.relative_to(root)): p.read_bytes() if p.is_file() else None for p in sorted(root.rglob("*"))}


OLDER = "could not be brought to this version's tracker format"
FIX = "which the user, not an agent, fixes by hand"  # an agent never edits frontmatter


class NewerFormat(unittest.TestCase):
    """A tracker in a newer format than this code's: a newer plugin version wrote it, and this one, still running in
    a session, writes nothing to it."""

    WRITES = (["set", "N-1", "next=More"], ["set", "tracker", "owner=you"], ["log", "A note"],
              ["new", "N-2", "--title", "Two"], ["decide", "Which store"], ["act", "Ask Sam"],
              ["add", "N-1", "carry", "A fact"], ["put", "N-1", "plan", "Do it", "--why", "A reason"],
              ["wait", "N-1", "on", "N-0"])

    def tracker(self, in_repo: str = "", branch: str = "feat/N-1") -> tuple[str, tuple[str, str]]:
        s, t = new_tracker("Newer", in_repo)
        run(*t, "new", "N-0", "--title", "Zero")
        run(*t, "new", "N-1", "--title", "One", "--branch", branch)
        run(*t, "set", "N-1", "status=in-progress")
        return s, t

    def newer(self, s: str) -> None:
        model.tracker_at(s).readme.save({"schema": schema.SCHEMA + 1})

    def unmigrated(self, s: str) -> None:
        """Make `s` an older format that cannot migrate: its README has a key twice."""
        readme = store.HOME / s / "README.md"
        readme.write_text(readme.read_text().replace(f"\nschema: {schema.SCHEMA}\n",
                                                     f"\nschema: {schema.SCHEMA - 1}\nowner: you\n"))
        self.assertIn(OLDER, contract.other_format(readme.parent))

    def refusals(self) -> tuple:
        """Each way a tracker takes no write from this code, how to make it so, and what the model is asked to do: a
        newer format, and an older one that could not migrate."""
        return (self.newer, hooks.RELOAD_REQUEST), (self.unmigrated, hooks.REPAIR_REQUEST)

    def why(self, s: str) -> str:
        """The sentence every reader gets for tracker `s`: a command, a hook, a UI, `check`."""
        return contract.other_format(store.HOME / s)

    def sid(self) -> str:
        """A new session id. Its file goes at the test's end: a later test would see a live session on a tracker."""
        x = f"new{time.monotonic_ns()}"
        self.addCleanup(session.drop_session, x)
        return x

    def test_a_write_names_its_tracker_through_home_or_what_it_resolves_to(self):
        # The guard and the first_writes report see a path's tracker by `tracker_root`; HOME can be a link.
        real = Path(tempfile.mkdtemp(dir=BASE))
        home = Path(tempfile.mkdtemp(dir=BASE)) / "home"
        home.symlink_to(real, target_is_directory=True)
        elsewhere = Path(tempfile.mkdtemp(dir=BASE)) / "linked"
        elsewhere.mkdir()
        (real / "linked").symlink_to(elsewhere, target_is_directory=True)  # a tracker folder that is a link
        with mock.patch.object(store, "HOME", home):
            for path, root in ((home / "a/README.md", home / "a"), (real / "a/tickets/A-1.md", home / "a"),
                               (home / ".archive/a/README.md", None), (real / ".archive/a/README.md", None),
                               (home / "a", None),  # the folder itself is one of HOME's own entries
                               (home / "linked/log.md", home / "linked"), (elsewhere / "log.md", None)):
                with self.subTest(path=path):
                    self.assertEqual(store.tracker_root(path), root)

    def test_a_write_refuses_and_changes_nothing(self):
        s, t = self.tracker()
        self.newer(s)
        evidence = Path(tempfile.mkdtemp(dir=BASE)) / "run.log"
        evidence.write_text("a run")
        before = tree(store.HOME / s)
        for args in (*self.WRITES, ["attach", str(evidence), "--ref", "N-1"], ["refresh"]):  # a UI's write too
            with self.subTest(args=args):
                said = run(*t, *args, code=watcher.REFUSED)
                self.assertEqual(said, f"tracker: {self.why(s)}\n")  # one sentence, nothing else
        self.assertEqual(tree(store.HOME / s), before)

    def test_a_linked_tracker_folder_counts_as_any_other(self):
        s, t = self.tracker()
        target = Path(tempfile.mkdtemp(dir=BASE)) / s
        (store.HOME / s).rename(target)
        (store.HOME / s).symlink_to(target, target_is_directory=True)
        self.assertIn("`check`: no new problems", run(*t, "set", "N-1", "next=Linked"))  # a write reports on it
        self.newer(s)
        before = tree(target)
        for args in (["set", "N-1", "next=More"], ["log", "A note"], ["act", "Ask Sam"]):
            with self.subTest(args=args):
                self.assertIn(self.why(s), run(*t, *args, code=watcher.REFUSED))
        self.assertEqual(tree(target), before)

    def test_attach_to_a_linked_tracker_folder_writes_nothing(self):
        s, t = self.tracker()
        target = Path(tempfile.mkdtemp(dir=BASE)) / s  # outside HOME: the link's target is no tracker's path
        (store.HOME / s).rename(target)
        (store.HOME / s).symlink_to(target, target_is_directory=True)
        src = Path(tempfile.mkdtemp(dir=BASE))
        (src / "kept.log").write_text("kept")
        (src / "run.log").write_text("a run")
        run(*t, "attach", str(src / "kept.log"), "--ref", "N-1")
        self.newer(s)
        before = tree(target)
        for args in ([str(src / "run.log"), "--ref", "N-1"], ["kept.log", "--append", "more"],
                     [str(target / schema.EVIDENCE_DIR / "kept.log"), "--append", "more"]):  # named by its target
            with self.subTest(args=args):
                self.assertIn(self.why(s), run(*t, "attach", *args, code=watcher.REFUSED))
        self.assertEqual(tree(target), before)

    def test_use_clear_refused_in_one_tracker_clears_none(self):
        for newer_first in (True, False):  # `use --clear` reaches the trackers in folder-name order: either may refuse
            with self.subTest(newer_first=newer_first):
                work = repo("main")
                a, b = sorted((self.tracker()[0], self.tracker()[0]))
                for x in (a, b):
                    run("--tracker", x, "use", "N-1", cwd=work)
                newer = a if newer_first else b
                self.newer(newer)
                before = {x: tree(store.HOME / x) for x in (a, b)}
                said = run("use", "--clear", cwd=work, code=watcher.REFUSED)
                self.assertEqual(said, f"tracker: {self.why(newer)}\n")  # and no "cleared the choice"
                self.assertEqual({x: tree(store.HOME / x) for x in (a, b)}, before)

    def test_start_on_a_newer_tracker_changes_nothing(self):
        (old, _), (s, _) = self.tracker(), self.tracker()
        sid, work = self.sid(), repo("feat/N-1")  # a branch s has not seen: its first mark
        own = session.session_path(sid)
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run("start", old, cwd=work)
            self.newer(s)
            before, held = tree(store.HOME / s), own.read_bytes()
            said = run("start", s, cwd=work, code=watcher.REFUSED)
        self.assertEqual(said, f"tracker: {self.why(s)}\n")
        self.assertEqual(own.read_bytes(), held)  # still on its tracker: it was not moved
        self.assertEqual(tree(store.HOME / s), before)

    def test_a_refusal_at_stop_is_told_at_the_next_prompt_which_asks_for_no_write(self):
        for refuse, request in self.refusals():
            with self.subTest(refuse.__name__):
                s, _ = self.tracker()
                sid, work = self.sid(), repo("feat/N-1")
                session.save_session(sid, tracker=s, cwd=str(work))
                hook("session-start", sid, work, source="startup")  # the branch's mark
                refuse(s)
                before = tree(store.HOME / s)
                commit(work, "on a refused tracker")
                self.assertIsNone(hook("stop", sid, work))  # refused: a Stop hook cannot tell the model
                told = hook("prompt", sid, work, prompt="go")
                self.assertEqual(told["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
                notice = f"[work-tracker] {self.why(s)}."
                self.assertEqual(told["systemMessage"], notice)  # the user, who acts
                self.assertEqual(told["hookSpecificOutput"]["additionalContext"], f"{notice} {request}")  # no step
                self.assertIsNone(hook("prompt", sid, work, prompt="again"))  # once per context
                self.assertEqual(tree(store.HOME / s), before)

    def test_a_session_that_takes_a_refused_tracker_over_after_clear_is_told_and_keeps_its_folder(self):
        for refuse, _ in self.refusals():
            with self.subTest(refuse.__name__):
                s, _ = self.tracker()
                sid, work = self.sid(), repo("feat/N-1")
                session.save_session(sid, tracker=s, cwd=str(work))
                hook("session-start", sid, work, source="startup")  # the branch's mark: no commit waits to be logged
                refuse(s)
                before = tree(store.HOME / s)
                hook("session-end", sid, work, reason="clear")  # /clear: the next session takes the tracker over
                new = self.sid()
                cleared = hook("session-start", new, work, source="clear")
                self.assertIn(self.why(s), cleared["hookSpecificOutput"]["additionalContext"])
                self.assertNotIn("Tracker protocol", said(cleared))  # no brief, which asks for writes
                self.assertEqual(session.load_session(new)["tracker"], s)
                self.assertEqual(session.load_session(new)["cwd"], str(work))  # `tracker agents` and the UI show it
                self.assertEqual(tree(store.HOME / s), before)

    def test_an_equal_or_older_format_writes_and_an_older_one_migrates_first(self):
        s, t = self.tracker()
        for args in self.WRITES:
            with self.subTest(args=args):
                run(*t, *args)
        model.tracker_at(s).readme.save({"schema": schema.SCHEMA - 1})
        self.assertIn("no new problems", run(*t, "set", "N-1", "next=Again"))
        self.assertEqual(model.tracker_at(s).schema, schema.SCHEMA)
        self.assertIn(f"Migrated to tracker format {schema.SCHEMA}", (store.HOME / s / "log.md").read_text())

    def test_a_read_works_and_check_keeps_its_error(self):
        for refuse, _ in self.refusals():
            with self.subTest(refuse.__name__):
                s, t = self.tracker()
                refuse(s)
                before = tree(store.HOME / s)
                self.assertIn("N-1", run(*t, "index"))
                self.assertIn("One", run(*t, "show", "N-1"))
                self.assertIn("One", run(*t, "context", "N-1"))
                self.assertEqual(data(s)["slug"], s)
                report = run(*t, "check", code=1)
                self.assertIn(f"README.md: {self.why(s)}\n", report)  # as a refused write says it
                self.assertEqual(report.count("tracker format"), 1)  # no other line on why it takes no write
                self.assertEqual(tree(store.HOME / s), before)
                self.assertEqual(list((store.HOME / ".backups").glob(f"{s}-*")), [])  # and no migration

    def test_sync_and_the_watchs_sync_write_nothing(self):
        s, t = self.tracker("a/x")
        self.newer(s)
        before = tree(store.HOME / s)
        fake = answers([pull(11, branch="feat/N-1", state="MERGED", merged_at="2026-10-01T06:30:00Z")])
        with mock.patch.object(github, "gh", fake):
            said = run(*t, "sync", code=watcher.REFUSED)
            job = watcher.SyncJob(model.tracker_at(s))
            job.run()
        self.assertEqual(len(fake.queries), 2)  # each read GitHub: the write is what refused
        self.assertEqual(said, f"tracker: {self.why(s)}\n")
        self.assertIn(self.why(s), job.changes[0])
        self.assertEqual(tree(store.HOME / s), before)

    def test_a_hook_writes_nothing_and_tells_each_context_once(self):
        s, t = self.tracker()
        sid, work = self.sid(), repo("feat/N-1")
        second = self.sid()
        for x in (sid, second):
            session.save_session(x, tracker=s, cwd=str(work))
        self.assertIn("Tracker protocol", said(hook("session-start", sid, work, source="startup")))  # the mark
        self.newer(s)
        before = tree(store.HOME / s)
        committed = {"tool_input": {"command": "git commit -m x"}}
        commit(work, "a subagent's")
        self.assertIsNone(tool("Bash", sid, work, agent_id="a1", **committed))  # the session reloads, not a subagent
        commit(work, "on a newer tracker")
        told = tool("Bash", sid, work, **committed)  # exits 0: `hook` checks
        self.assertEqual(told["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(f"[work-tracker] {self.why(s)}.", told["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(told["systemMessage"], f"[work-tracker] {self.why(s)}.")  # the user, who reloads, sees it too
        commit(work, "again")
        self.assertIsNone(tool("Bash", sid, work, **committed))  # once per context
        hook("prompt", sid, work, prompt="go")
        self.assertIsNone(hook("stop", sid, work))
        other = hook("session-start", second, work, source="startup")  # another session is told too
        self.assertEqual(other["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn(self.why(s), other["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(tree(store.HOME / s), before)
        self.assertEqual(session.load_session(sid)["activity"]["status"], "idle")  # its own files go on

    def test_a_compacted_context_is_told_again_with_the_command_prefix(self):
        for refuse, _ in self.refusals():
            with self.subTest(refuse.__name__):
                s, _ = self.tracker()
                sid, work, env = self.sid(), repo("feat/N-1"), {"PLUGIN_ROOT": str(ROOT)}  # no env file
                session.save_session(sid, tracker=s, cwd=str(work))
                hook("session-start", sid, work, env=env, source="startup")
                refuse(s)
                self.assertIn(self.why(s), hook("prompt", sid, work, env=env, prompt="go")["systemMessage"])
                compacted = hook("session-start", sid, work, env=env, source="compact")["hookSpecificOutput"]
                self.assertIn(f"[work-tracker] {self.why(s)}.", compacted["additionalContext"])
                self.assertIn(f"TRACKER_SESSION={sid} {ROOT}/bin/tracker", compacted["additionalContext"])  # reads
                self.assertIsNone(hook("prompt", sid, work, env=env, prompt="again"))  # once per context

    def test_a_subagent_start_leaves_the_telling_to_the_session(self):
        s, _ = self.tracker()
        sid, work = self.sid(), repo("feat/N-1")
        session.save_session(sid, tracker=s, cwd=str(work))
        self.newer(s)
        for extra in ({"agent_id": "a1"}, {}):  # a host that names no agent_id too
            with self.subTest(**extra):
                helper = said(hook("subagent-start", sid, work, **extra))
                self.assertIn("do not write it", helper)  # it reads the tracker, as on any other
                self.assertNotIn(self.why(s), helper)
        self.assertEqual(hook("prompt", sid, work, prompt="go")["systemMessage"], f"[work-tracker] {self.why(s)}.")

    def test_watch_stop_ends_the_watch_at_once(self):
        for refuse, _ in self.refusals():
            with self.subTest(refuse.__name__):
                s, _ = self.tracker()
                sid, work = self.sid(), repo("feat/N-1")
                session.save_session(sid, tracker=s, cwd=str(work))
                refuse(s)
                watcher.set_grant(sid, True)
                hook("prompt", sid, work, prompt="/work-tracker:watch stop")
                self.assertFalse(watcher.granted(sid))

    def test_a_session_on_no_tracker_is_told_of_a_refused_match_not_offered_it(self):
        for refuse, _ in self.refusals():
            with self.subTest(refuse.__name__):
                branch = f"feat/only-{time.monotonic_ns()}"  # no other test's tracker matches it
                (old, _), (s, _) = self.tracker(branch=branch), self.tracker(branch=branch)  # not sure, so asked
                sid, work = self.sid(), repo(branch)
                refuse(s)
                offered = hook("session-start", sid, work, source="startup")["hookSpecificOutput"]["additionalContext"]
                self.assertIn(f"`tracker start {old}`", offered)
                self.assertNotIn(f"`tracker start {s}`", offered)  # it would refuse
                self.assertIn(f"[work-tracker] {self.why(s)}.", offered)
                refuse(old)
                told = hook("session-start", self.sid(), work, source="startup")
                self.assertNotIn("host's question tool", said(told))  # nothing to link
                for x in (old, s):
                    self.assertIn(f"[work-tracker] {self.why(x)}.", told["hookSpecificOutput"]["additionalContext"])
                    self.assertIn(f"[work-tracker] {self.why(x)}.", told["systemMessage"])

    def test_a_sure_match_on_a_refused_tracker_is_told_not_linked(self):
        for refuse, _ in self.refusals():
            with self.subTest(refuse.__name__):
                s, t = new_tracker("Newer sure", "acme/sure")
                branch, nogh = f"feat/{s}", {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"}
                work = repo(branch)  # in the repo the tracker names, on the branch its one ticket names: a sure match
                subprocess.run(["git", "-C", str(work), "remote", "add", "origin", "git@github.com:acme/sure.git"],
                               check=True)
                with mock.patch.object(github, "gh", lambda *args, **kw: None):
                    run(*t, "new", "N-1", "--title", "One", "--branch", branch)
                    run(*t, "set", "N-1", "status=in-progress", cwd=work)
                refuse(s)
                sid, before = self.sid(), tree(store.HOME / s)
                told = hook("session-start", sid, work, nogh, source="startup")
                self.assertEqual(told["systemMessage"], f"[work-tracker] {self.why(s)}.")
                self.assertNotIn("Linked", said(told))
                self.assertNotIn("Tracker protocol", said(told))
                self.assertNotIn("tracker", session.load_session(sid))  # not linked: on no tracker, as before
                self.assertEqual(tree(store.HOME / s), before)

    def test_a_bare_start_and_here_on_a_refused_tracker_brief_and_say_so(self):
        for refuse, _ in self.refusals():
            for cmd in ("start", "here"):
                with self.subTest(refuse.__name__, cmd=cmd):
                    s, _ = self.tracker()
                    sid, work = self.sid(), repo("feat/N-1")
                    with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
                        run("start", s, cwd=work)
                        refuse(s)
                        before = tree(store.HOME / s)
                        out = run(cmd, cwd=work)  # a read: exits 0
                    self.assertIn("N-1", out)
                    self.assertTrue(out.endswith(f"\n{self.why(s)}.\n"), out)
                    self.assertEqual(out.count(self.why(s)[:60]), 1, out)  # once: not in the brief's check too
                    self.assertEqual(tree(store.HOME / s), before)

    def test_no_event_and_no_log_line(self):
        s, _ = self.tracker()
        self.newer(s)
        store.guard_writes(contract.refuse_other_format)  # as each command and hook does first
        before = tree(store.HOME / s)
        with store.locked(), self.assertRaises(contract.OtherFormat):
            model.append_event(model.tracker_at(s), "note", "A note", [])
        self.assertEqual(tree(store.HOME / s), before)

    def test_a_bare_start_after_the_reload_brings_the_protocol_once(self):
        s, _ = self.tracker()
        sid, work = self.sid(), repo("feat/N-1")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run("start", s, cwd=work)
            self.newer(s)
            hook("session-start", sid, work, source="resume")  # told in place of its brief: no protocol in this context
            self.assertNotIn("Tracker protocol", run("start", cwd=work))  # still newer: the protocol asks for writes
            readme = store.HOME / s / "README.md"  # by hand: the guard refuses this code's own write
            readme.write_text(readme.read_text().replace(f"schema: {schema.SCHEMA + 1}", f"schema: {schema.SCHEMA}"))
            self.assertFalse(self.why(s))  # as the reloaded version sees it
            self.assertIn("Tracker protocol", run("start", cwd=work))  # what RELOAD_REQUEST asks for
            self.assertNotIn("Tracker protocol", run("start", cwd=work))  # once: the session has it now
        self.assertNotIn("told_refusal", session.load_session(sid))

    def test_a_restart_brings_the_protocol_with_its_brief_and_the_start_after_adds_none(self):
        s, _ = self.tracker()
        sid, work = self.sid(), repo("feat/N-1")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run("start", s, cwd=work)
            self.newer(s)
            hook("session-start", sid, work, source="resume")  # told in place of its brief
            readme = store.HOME / s / "README.md"  # by hand: the guard refuses this code's own write
            readme.write_text(readme.read_text().replace(f"schema: {schema.SCHEMA + 1}", f"schema: {schema.SCHEMA}"))
            self.assertIn("Tracker protocol", said(hook("session-start", sid, work, source="resume")))  # the restart
            self.assertNotIn("told_refusal", session.load_session(sid))  # that brief had the protocol
            self.assertNotIn("Tracker protocol", run("start", cwd=work))  # so the start it was asked for adds none


class Events(unittest.TestCase):
    """The event file: each change the tool writes, as one typed event per line."""

    def setUp(self):
        self.s, self.t = new_tracker("Events")
        self.tr = model.tracker_at(self.s)
        self.file = store.HOME / self.s / schema.EVENTS_FILE

    def append(self, kind: str = "note", text: str = "x", refs: tuple = (), tr: model.Tracker | None = None,
               **data) -> dict:
        with store.locked():
            return model.append_event(tr or self.tr, kind, text, list(refs), **data)

    def test_an_event_has_the_fields_of_R12(self):
        e = self.append("note", "a  b\nc", ["T-1"])
        self.assertEqual([*e][:9], ["seq", "at", "kind", "session", "via", "version", "repo", "refs", "text"])
        self.assertEqual((e["seq"], e["kind"], e["refs"], e["text"]), (1, "note", ["T-1"], "a b c"))
        self.assertRegex(e["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(e["version"], json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"])
        self.assertEqual(model.read_events(self.tr), [e])

    def test_sequence_numbers_have_no_gaps(self):
        events = [self.append(text=f"n{i}") for i in range(3)]
        self.assertEqual([e["seq"] for e in events], [1, 2, 3])
        self.assertEqual(model.read_events(self.tr, after=2), [events[2]])
        self.assertEqual(model.last_seq(self.tr), 3)

    def test_the_session_is_the_environments(self):
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": "s1"}):
            self.assertEqual(self.append()["session"], "s1")
        with mock.patch.dict(os.environ):
            for var in ("TRACKER_SESSION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
                os.environ.pop(var, None)
            self.assertEqual(self.append()["session"], "")  # a human at a terminal: empty, never missing

    def test_an_event_says_how_it_was_entered(self):
        with mock.patch.dict(os.environ):
            for var in ("TRACKER_SESSION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "TRACKER_VIA"):
                os.environ.pop(var, None)
            self.assertEqual(self.append()["via"], "terminal")
            with mock.patch.dict(os.environ, {"TRACKER_SESSION": "s1"}):
                self.assertEqual(self.append()["via"], "agent")
            with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
                self.assertEqual(self.append()["via"], "agent")  # a Claude session whose hooks gave no id
            with mock.patch.dict(os.environ, {"TRACKER_VIA": "view", "TRACKER_SESSION": "s1"}):
                e = self.append()  # a view opened from an agent's shell has that session's environment
                self.assertEqual((e["via"], e["session"]), ("view", "s1"))

    def test_a_recorded_command_line_gains_the_mark(self):
        file = Path(BASE) / f"cli{time.monotonic_ns()}.json"
        with mock.patch.object(store, "CLI_FILE", file):
            store.record_cli()
            saved = json.loads(file.read_text())
            self.assertIn("TRACKER_VIA", saved["argv"][-2])
            # As the previous code wrote it: the same package, Python and mod, a start string with no mark
            older = {k: saved[k] for k in ("package", "python", "mod")}
            store.write_json(file, {**older, "argv": [*saved["argv"][:-2], "import os; old start", saved["argv"][-1]]})
            self.assertTrue(store.record_cli())
            self.assertEqual(json.loads(file.read_text())["argv"], saved["argv"])

    def test_the_view_marks_its_commands(self):
        for env in ({}, {"TRACKER_VIA": "terminal"}):  # forced, not only a default
            with self.subTest(env=env):
                subprocess.run([*system.cli_for_ui(), "--tracker", self.s, "log", "x"], env=env, check=True,
                               capture_output=True)
                e = model.read_events(model.tracker_at(self.s))[-1]
                self.assertEqual((e["text"], e["via"], e["session"]), ("x", "view", ""))

    def test_the_repo_is_the_tickets(self):
        s, t = new_tracker("Two repos", "a/x,b/y")
        run(*t, "new", "T-1", "--title", "One", "--repo", "b/y")
        two = model.tracker_at(s)
        self.assertEqual(self.append(refs=["X-9", "T-1"], tr=two)["repo"], "b/y")  # the first ticket's
        self.assertEqual(self.append(tr=two)["repo"], "")  # no ticket, and no one repo
        one, _ = new_tracker("One repo", "a/x")
        self.assertEqual(self.append(tr=model.tracker_at(one))["repo"], "a/x")

    def test_an_unknown_kind_is_refused(self):
        self.append()
        before = self.file.read_bytes()
        with self.assertRaises(ValueError):
            self.append("noted")
        self.assertEqual(self.file.read_bytes(), before)

    def test_data_cannot_shadow_a_fixed_field(self):
        for field in ("seq", "via"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.append(**{field: "x"})
        self.assertEqual(model.read_events(self.tr), [])

    def test_a_half_written_line_is_skipped(self):
        first = self.append()
        with open(self.file, "a") as f:
            f.write('{"seq": 2, "at')  # a process killed part way
        self.assertEqual(model.read_events(self.tr), [first])
        second = self.append()
        self.assertEqual(second["seq"], 2)
        self.assertEqual(json.loads(self.file.read_text().splitlines()[-1]), second)  # on its own line
        self.assertEqual(model.read_events(self.tr), [first, second])

    def test_the_number_is_found_past_a_long_damaged_tail(self):
        for _ in range(3):
            self.append(text="y" * 3000)
        with open(self.file, "a") as f:
            f.write("not an event " * 1000 + "\n")  # longer than the first block read back
        self.assertEqual(self.append()["seq"], 4)

    def test_a_failed_write_leaves_no_log_line_without_its_event(self):
        self.append(text="first")
        real, calls = model.append, []

        def second_fails(path, text):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            real(path, text)

        with mock.patch.object(model, "append", second_fails), self.assertRaises(OSError):
            self.append(text="second")
        rendered = [model.log_line(e) for e in model.read_events(self.tr)]
        log = [ln for ln in (store.HOME / self.s / "log.md").read_text().splitlines() if ln.startswith("- ")]
        self.assertEqual([ln for ln in log if ln not in rendered], [])

    def test_one_event_is_one_line(self):
        self.append()
        before = len(self.file.read_bytes().splitlines())
        e = self.append(text="é\nx")
        self.assertEqual(len(self.file.read_bytes().splitlines()), before + 1)
        self.assertIn("é x".encode(), self.file.read_bytes())  # as written, not escaped
        self.assertEqual((e["text"], model.read_events(self.tr)[-1]["text"]), ("é x", "é x"))

    def test_log_line_matches_todays_form(self):
        e = self.append(text="a  b c", refs=["T-1", "D-2"])
        self.assertEqual(model.log_line(e), f"- {schema.today()} [T-1 D-2] a b c")
        self.assertEqual(model.log_line(self.append(text="a b c")), f"- {schema.today()} a b c")
        self.assertEqual(model.log_line({**e, "day": "2025-01-02"}), "- 2025-01-02 [T-1 D-2] a b c")

    # Each write, the kind of its event and the event's own fields.
    KINDS_WRITTEN = [
        (["new", "T-1", "--title", "One"], "ticket.added", {}),
        (["new", "T-2", "--title", "Two"], "ticket.added", {}),
        (["set", "T-1", "status=in-progress"], "status", {"status": "in-progress"}),
        (["step", "T-1", "Built it"], "step", {}),
        (["log", "A note", "--ref", "T-1"], "note", {}),
        (["wait", "T-2", "on", "T-1"], "wait.added", {"on": ["T-1"]}),
        (["wait", "T-2", "off", "t-1"], "wait.removed", {"on": ["T-1"]}),
        (["put", "T-1", "plan", "Another way", "--why", "The first failed"], "body.changed", {"heading": "Plan"}),
        (["decide", "Which store", "--refs", "T-2"], "decision.opened", {"decision": "D-01"}),
        (["decide", "D-01", "--note", "S3 is cheap"], "decision.updated", {"decision": "D-01"}),
        (["decide", "D-01", "--blocks", "T-2"], "wait.added", {"on": ["D-01"]}),
        (["decide", "D-01", "--resolve", "S3", "--by", "me"], "decision.decided", {"decision": "D-01"}),
        (["act", "Ask Sam"], "action.added", {"action": "A-01"}),
        (["act", "A-01", "--title", "Ask Sam again"], "action.updated", {"action": "A-01"}),
        (["act", "A-01", "--due", "2099-01-01"], "action.updated", {"action": "A-01"}),
        (["act", "A-01", "--note", "Asked"], "action.updated", {"action": "A-01"}),
        (["act", "A-01", "--done"], "action.closed", {"action": "A-01", "status": "done"}),
        (["step", "T-1", "--done", "Delivered"], "status", {"status": "done"}),
        (["set", "tracker", "status=active"], "status", {"status": "active"}),
    ]

    def run_writes(self) -> list[dict]:
        """Run KINDS_WRITTEN, each write's events after the one before."""
        written, seen = [], 0
        for args, kind, fields in self.KINDS_WRITTEN:
            run(*self.t, *args)
            events = model.read_events(self.tr, seen)
            seen = events[-1]["seq"] if events else seen
            written.append((args, [(e["kind"], {k: e[k] for k in fields if k in e}) for e in events]))
        return written

    def test_each_command_writes_its_kind(self):
        self.assertEqual(self.run_writes(), [(args, [(kind, fields)]) for args, kind, fields in self.KINDS_WRITTEN])

    def test_log_md_is_the_events_rendered(self):
        self.run_writes()
        log = [ln for ln in (store.HOME / self.s / "log.md").read_text().splitlines() if ln.startswith("- ")]
        self.assertEqual(log, [model.log_line(e) for e in model.read_events(self.tr)])
        self.assertEqual(len(log), len(self.KINDS_WRITTEN))

    def test_the_branch_commits_and_pause_of_a_step(self):
        work = repo("feat/T-1")
        run(*self.t, "new", "T-1", "--title", "One")
        run(*self.t, "set", "T-1", "status=in-progress", cwd=work)
        run(*self.t, "step", cwd=work)  # the baseline mark
        commit(work, "one")
        run(*self.t, "step", "T-1", "--pause", "Half done", cwd=work)
        run(*self.t, "use", "T-1", cwd=repo("feat/other"))
        got = [(e["kind"], e.get("branch"), e.get("count")) for e in model.read_events(self.tr)][-3:]
        self.assertEqual(got, [("commits", "feat/T-1", 1), ("paused", "feat/T-1", None),
                               ("branch.set", "feat/other", None)])

    def test_attach_names_its_file(self):
        run(*self.t, "new", "T-1", "--title", "One")
        evidence = Path(tempfile.mkdtemp(dir=BASE)) / "run.log"
        evidence.write_text("a run")
        run(*self.t, "attach", str(evidence), "--ref", "T-1")
        e = model.read_events(self.tr)[-1]
        self.assertEqual((e["kind"], e["file"], e["refs"]), ("evidence.attached", "evidence/run.log", ["T-1"]))

    def test_sync_writes_a_prs_new_state(self):
        s, t = new_tracker("Synced", "a/x")
        run(*t, "new", "K-1", "--title", "One", "--branch", "feat/K-1")
        run(*t, "set", "K-1", "status=in-progress")
        with mock.patch.object(github, "gh", answers([pull(11, branch="feat/K-1", state="MERGED",
                                                           merged_at="2026-10-01T06:30:00Z")])):
            run(*t, "sync")
        e = model.read_events(model.tracker_at(s))[-1]
        self.assertEqual((e["kind"], e["pr"], e["state"], e["refs"], e["repo"], e["text"]),
                         ("pr.state", "a/x#11", "merged", ["K-1"], "a/x", "PR #11 merged"))

    def test_init_creates_the_event_file(self):
        self.assertEqual(self.file.read_bytes(), b"")
        self.assertIn(self.file, self.tr.data_files())

    def test_check_reports_a_damaged_event_file(self):
        for _ in range(2):
            self.append()
        jumped = json.dumps({"seq": 4, "kind": "note", "refs": [], "text": "x", "at": "2026-01-01T00:00:00Z"}) + "\n"
        with open(self.file, "a") as f:
            f.write("not an event\n" + jumped + jumped)  # a garbage line, a jump, and a number again
        errors = [x for x in contract.check(self.tr)[0] if x.startswith(schema.EVENTS_FILE)]
        self.assertEqual(errors, [f"{schema.EVENTS_FILE}:3: not an event",
                                  f"{schema.EVENTS_FILE}:4: sequence breaks at 4",
                                  f"{schema.EVENTS_FILE}:5: sequence breaks at 4"])
        self.assertEqual(self.append()["seq"], 5)  # the next follows the last
        self.assertEqual([x for x in contract.check(self.tr)[0] if x.startswith(schema.EVENTS_FILE)], errors)

    def test_nothing_reads_log_md(self):
        import ast
        writers = {("commands.py", "cmd_init"), ("model.py", "append_event"), ("contract.py", "legacy_events"),
                   ("commands.py", "cmd_find"), ("model.py", "data_files")}
        found = set()
        for path in sorted((ROOT / "scripts/tracker").glob("*.py")):
            for fn in ast.walk(ast.parse(path.read_text())):
                if isinstance(fn, ast.FunctionDef) and any(
                        isinstance(x, ast.Constant) and x.value == "log.md" for x in ast.walk(fn)):
                    found.add((path.name, fn.name))
        self.assertLessEqual(found, writers)

    def test_history_ignores_a_hand_edit_of_log_md(self):
        run(*self.t, "log", "Written by the tool")
        with open(store.HOME / self.s / "log.md", "a") as f:
            f.write(f"- {schema.today()} Written by hand\n")
        said = run(*self.t, "history")
        self.assertIn("Written by the tool", said)
        self.assertNotIn("Written by hand", said)
        self.assertNotIn("Written by hand", json.dumps(data(self.s)))

    def test_history_hides_a_shown_decisions_lines(self):
        run(*self.t, "decide", "Which store")
        run(*self.t, "decide", "D-01", "--note", "S3 is cheap")
        run(*self.t, "decide", "D-01", "--resolve", "S3")
        run(*self.t, "decide", "Which queue")
        self.assertEqual([x.split(" ", 2)[2] for x in views.history_lines(self.tr, [], "", 0, {"D-01"})],
                         ["Updated D-01: S3 is cheap", "Opened D-02 Which queue"])

    def test_history_filters_by_ref_and_since(self):
        run(*self.t, "new", "T-1", "--title", "One")
        with mock.patch.object(model, "utc_now", lambda: "2025-01-02T12:00:00Z"):
            run(*self.t, "log", "Long ago", "--ref", "T-1")
        run(*self.t, "log", "Today, on T-1", "--ref", "T-1")
        run(*self.t, "log", "Today, on nothing")
        today = schema.today()
        self.assertEqual(run(*self.t, "history", "--ref", "T-1").splitlines(),
                         [f"{today} [T-1] Added T-1 One", "2025-01-02 [T-1] Long ago", f"{today} [T-1] Today, on T-1"])
        self.assertEqual(run(*self.t, "history", "--since", "2025-06-01").splitlines(),
                         [f"{today} [T-1] Added T-1 One", f"{today} [T-1] Today, on T-1", f"{today} Today, on nothing"])

    def test_commits_event_counts_every_commit(self):
        commits = [(f"c{i}", f"commit {i}") for i in range(11)]
        with store.locked():
            session.log_commits(self.tr, "feat/x", commits, [])
        e = model.read_events(self.tr)[-1]
        self.assertEqual((e["kind"], e["branch"], e["count"]), ("commits", "feat/x", 11))
        self.assertTrue(e["text"].endswith("and 3 more"), e["text"])


def hook(event: str, sid: str, cwd: Path, env: dict | None = None, **extra) -> dict | None:
    """Run scripts/hook.sh for one event; its JSON output, or None when it adds nothing."""
    data = json.dumps({"session_id": sid, "cwd": str(cwd), "hook_event_name": event, **extra})
    out = subprocess.run([str(ROOT / "scripts/hook.sh"), event], input=data, capture_output=True, text=True,
                         check=True, env={**os.environ, **(env or {})}).stdout
    return json.loads(out) if out.strip() else None


def tool(name: str, sid: str, cwd: Path, **extra) -> dict | None:
    """The hook after the tool `name` (PostToolUse)."""
    return hook("post-tool", sid, cwd, tool_name=name, **extra)


def said(out: dict | None) -> str:
    return json.dumps(out or {})


def no_gh() -> dict:
    """A PATH with git and the shell's tools but no `gh`, which a hook's sync then cannot run: no hook in a test asks
    GitHub (`gh` may sit in /usr/bin)."""
    path = Path(BASE) / "nogh"
    if not path.exists():
        path.mkdir()
        for name in ("git", "cat", "sh"):
            (path / name).symlink_to(shutil.which(name))
    return {"PATH": f"{path}:{Path(sys.executable).parent}"}


def opened_prs(s: str) -> list[dict]:
    return [e for e in model.read_events(model.tracker_at(s)) if e["kind"] == "pr.opened"]


class PrsOpened(unittest.TestCase):
    """A PR a session on a tracker opens with `gh pr create` is logged (`pr.opened`), and `tracker check` warns of one
    that no ticket claims (R11)."""

    CREATE = {"command": "gh pr create --fill --title 'Parser'"}
    CREATED = {"stdout": "Creating pull request for f1 into main in a/x\n\nhttps://github.com/a/x/pull/12\n",
               "stderr": "", "interrupted": False, "isImage": False}

    def setUp(self):
        self.s, self.t = new_tracker("Opened", "a/x")
        run(*self.t, "new", "S-1", "--title", "One", "--branch", "f1")
        run(*self.t, "set", "S-1", "status=in-progress")
        self.work = repo("f1")
        self.sid = f"po{time.monotonic_ns()}"
        self.addCleanup(session.drop_session, self.sid)
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": self.sid}):
            run("start", self.s, cwd=self.work)

    def bash(self, tool_input: dict, response: dict | None = None, **extra) -> dict | None:
        return tool("Bash", self.sid, self.work, env=no_gh(), tool_input=tool_input,
                    **({"tool_response": response} if response is not None else {}), **extra)

    def test_a_pr_a_session_opens_is_logged_once(self):
        self.bash(self.CREATE, self.CREATED)
        self.bash(self.CREATE, self.CREATED)  # gh says the PR is there already: the same one
        [e] = opened_prs(self.s)
        self.assertEqual((e["text"], e["refs"], e["pr"], e["session"]), ("PR #12 opened by a session", [], "a/x#12",
                                                                          self.sid))
        self.assertNotIn("url", e)

    def test_the_last_pr_address_is_the_one_opened(self):
        out = {"stdout": "Warning: see https://github.com/a/x/pull/3\nhttps://github.com/a/x/pull/14\n"}
        self.bash(self.CREATE, out)
        self.assertEqual([e["pr"] for e in opened_prs(self.s)], ["a/x#14"])

    def fake_gh(self, answer: dict) -> dict:
        """A PATH whose `gh` prints `answer`, as the hook's sync reads it."""
        folder = Path(tempfile.mkdtemp(dir=BASE))
        (folder / "answer.json").write_text(json.dumps(answer))
        (folder / "gh").write_text(f"#!/bin/sh\ncat '{folder / 'answer.json'}'\n")
        (folder / "gh").chmod(0o755)
        return {"PATH": f"{folder}:{no_gh()['PATH']}"}

    def test_the_opening_is_logged_before_the_sync_and_whatever_it_does(self):
        env = self.fake_gh({"data": {"r0": {"b0": {"nodes": [pull(12, branch="f1")]}}}})
        tool("Bash", self.sid, self.work, env=env, tool_input=self.CREATE, tool_response=self.CREATED)
        kinds = [e["kind"] for e in model.read_events(model.tracker_at(self.s)) if e["kind"].startswith("pr.")]
        self.assertEqual(kinds, ["pr.opened", "pr.state"])  # opened, then open for review
        self.setUp()
        broken = self.fake_gh({"data": {"r0": {"b0": {"nodes": [{"title": "no number"}]}}}})  # the sync raises
        tool("Bash", self.sid, self.work, env=broken, tool_input=self.CREATE, tool_response=self.CREATED)
        self.assertEqual([e["pr"] for e in opened_prs(self.s)], ["a/x#12"])

    def test_a_silent_pr_create_logs_nothing(self):
        before = tree(store.HOME / self.s)
        dry = {"stdout": "Would have created a Pull Request with:\nBody: closes https://github.com/a/x/pull/5\n"}
        for response in ({"stdout": "", "stderr": "aborted"}, None, {"stdout": "see github.com/a/x"}, dry):
            with self.subTest(response=response):
                self.bash(self.CREATE, response)  # `hook` checks that it exits 0
        failed = hook("resumed", self.sid, self.work, env=no_gh(), hook_event_name="PostToolUseFailure",
                      tool_name="Bash", tool_input=self.CREATE,
                      error="Exit code 1\nhttps://github.com/a/x/pull/12 already exists")
        self.assertIsNone(failed)
        self.assertEqual(opened_prs(self.s), [])
        self.assertEqual(tree(store.HOME / self.s), before)

    def test_a_subagents_pr_is_logged_too(self):
        self.assertIsNone(self.bash(self.CREATE, self.CREATED, agent_id="a1"))
        self.assertEqual([e["pr"] for e in opened_prs(self.s)], ["a/x#12"])

    def test_gh_pr_new_opens_one_too(self):
        self.bash({"command": "gh pr new --fill"}, self.CREATED)
        self.assertEqual([e["pr"] for e in opened_prs(self.s)], ["a/x#12"])

    def test_another_gh_pr_command_logs_no_opening(self):
        for command in ("gh pr merge 12 --squash", "gh pr edit 12 --title x", "git push -u origin f1",
                        "gh pr view 12"):
            with self.subTest(command=command):
                self.bash({"command": command}, self.CREATED)
        self.assertEqual(opened_prs(self.s), [])

    def test_a_pr_in_another_repo_is_named_by_its_repo(self):
        self.bash(self.CREATE, {"stdout": "https://github.com/b/y/pull/7\n"})
        self.assertEqual([(e["pr"], e["text"]) for e in opened_prs(self.s)],
                         [("b/y#7", "PR b/y#7 opened by a session")])

    def test_a_refused_tracker_gets_no_pr_event(self):
        for refuse in (NewerFormat.newer, NewerFormat.unmigrated):
            for agent in ({}, {"agent_id": "a1"}):
                with self.subTest(refuse.__name__, **agent):
                    self.setUp()
                    refuse(self, self.s)
                    before = tree(store.HOME / self.s)
                    self.bash(self.CREATE, self.CREATED, **agent)  # exits 0: `hook` checks
                    self.assertEqual(tree(store.HOME / self.s), before)

    def test_check_warns_of_a_pr_no_ticket_claims(self):
        self.bash(self.CREATE, self.CREATED)
        before = tree(store.HOME / self.s)
        warning = ("⚠ PR a/x#12 was opened by a session on this tracker and no ticket claims it: add a PR: link to "
                   'the ticket it is for, `tracker add <id> link "PR: [title](url) — why"`')
        self.assertIn(warning, run(*self.t, "check").splitlines())
        self.assertEqual(tree(store.HOME / self.s), before)  # `check` writes nothing
        run(*self.t, "new", "S-2", "--title", "Two")
        run(*self.t, "add", "S-2", "link", "PR: [Parser](https://github.com/A/X/pull/012)")
        before = tree(store.HOME / self.s)
        self.assertNotIn("a/x#12", run(*self.t, "check"))
        self.assertEqual(tree(store.HOME / self.s), before)

    def test_a_pr_from_a_tickets_branch_is_claimed_by_sync(self):
        self.bash(self.CREATE, self.CREATED)
        self.assertIn("no ticket claims it", run(*self.t, "check"))
        with mock.patch.object(github, "gh", answers([pull(12, branch="f1")])):
            run(*self.t, "sync")
        self.assertNotIn("no ticket claims it", run(*self.t, "check"))
        self.assertEqual(model.tracker_at(self.s).lookup("S-1").prs, {"12": "open"})

    def test_a_pr_a_tickets_branch_held_stays_claimed_once_another_replaces_it(self):
        self.bash(self.CREATE, self.CREATED)
        with mock.patch.object(github, "gh", answers([pull(12, branch="f1")])):
            run(*self.t, "sync")
        with mock.patch.object(github, "gh", answers([pull(12, branch="f1", state="CLOSED"), pull(13, branch="f1")])):
            run(*self.t, "sync")
        self.assertEqual(model.tracker_at(self.s).lookup("S-1").prs, {"13": "open"})  # #12 dropped from its list
        self.assertNotIn("no ticket claims it", run(*self.t, "check"))

    def test_a_pr_an_older_format_held_is_claimed(self):
        self.bash(self.CREATE, self.CREATED)
        tr = model.tracker_at(self.s)
        tr.lookup("S-1").save({"pr": "12", "pr_state": "open"})  # an older format's one PR, which logged no pr.state
        tr.readme.save({"schema": 3})
        run(*self.t, "list")  # migrates it
        self.assertEqual(model.tracker_at(self.s).lookup("S-1").prs, {"12": "open"})
        self.assertEqual([e["kind"] for e in model.read_events(model.tracker_at(self.s)) if e.get("pr")], ["pr.opened"])
        self.assertNotIn("no ticket claims it", run(*self.t, "check"))

    def test_a_write_reports_the_new_warning_once(self):
        self.bash(self.CREATE, self.CREATED)
        said_ = run(*self.t, "set", "S-1", "next=Ship it")  # the PR came from a hook: this write did not add it
        self.assertNotIn("no ticket claims it", said_)
        self.assertIn("`check`: no new problems", said_)


class Hooks(unittest.TestCase):
    """scripts/hook.sh end to end: its shell filters, and what each event tells the model."""

    def test_a_wait_on_the_user_shows_until_the_session_moves_on(self):
        (s, _), sid, work = new_tracker("Hooked"), f"sid{time.monotonic_ns()}", repo("main")
        session.save_session(sid, tracker=s, cwd=str(work))

        def status():
            return session.load_session(sid)["activity"]["status"]

        self.assertIsNone(tool("Read", sid, work))  # no wait: the shell filter ends it
        self.assertNotIn("activity", session.load_session(sid))
        self.assertIsNone(hook("prompt", sid, work, prompt="go") and None)
        self.assertEqual(status(), "busy")
        self.assertIsNone(hook("waiting", sid, work, tool_name="Bash"))  # a hook that decides nothing and says nothing
        self.assertEqual(status(), "waiting")
        live = session.live_sessions(s)
        self.assertEqual([x.status for x in live], ["waiting"])
        self.assertEqual(viewmodel.agent_word(live[0]).split(" since")[0], "needs you")
        tool("Bash", sid, work)
        self.assertEqual(status(), "busy")
        hook("waiting", sid, work, tool_name="AskUserQuestion", agent_id="sub")  # a subagent's wait is the session's
        self.assertEqual(status(), "waiting")
        tool("Read", sid, work, agent_id="other")  # another agent's tool: "sub" still asks
        self.assertEqual(status(), "waiting")
        hook("waiting", sid, work, tool_name="Bash")
        tool("Bash", sid, work, agent_id="sub")
        self.assertEqual(status(), "waiting")  # the session's own wait holds
        hook("resumed", sid, work, tool_name="Bash")  # PostToolUseFailure and SubagentStop
        self.assertEqual(status(), "busy")
        self.assertNotIn("waits", session.load_session(sid))
        hook("waiting", sid, work, tool_name="Bash", agent_id="sub")
        hook("stop", sid, work)
        self.assertEqual(status(), "idle")  # the turn ended, and with it every wait
        self.assertNotIn("waits", session.load_session(sid))
        hook("prompt", sid, work, prompt="go")
        hook("stop-failure", sid, work)
        self.assertEqual(status(), "idle")

    def test_session(self):
        (s, _), sid, work = new_tracker("Hooked"), f"sid{time.monotonic_ns()}", repo("feat/H-1")
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

        # A feature branch that a ticket's `branch` names, in a tracker that names no repo: branch names repeat
        # across repos, so not sure, and asked.
        self.assertIn("host's question tool", said(hook("session-start", f"{sid}r", work, source="startup")))

        # The same in a repo the tracker names: the session links at its start, no question. No gh on PATH: the
        # hooks' PR sync reads nothing.
        (a, _), nogh = new_tracker("Auto", "acme/auto"), {"PATH": path}
        mine = repo(f"feat/{a}")
        subprocess.run(["git", "-C", str(mine), "remote", "add", "origin", "git@github.com:acme/auto.git"], check=True)
        with mock.patch.object(github, "gh", lambda *args, **kw: None):
            run("--tracker", a, "new", "U-1", "--title", "Auto work", "--branch", f"feat/{a}")
            run("--tracker", a, "set", "U-1", "status=in-progress", cwd=mine)
        auto = said(hook("session-start", f"{sid}a", mine, nogh, source="startup"))
        self.assertIn(f"Linked this session to {a}", auto)
        self.assertIn("Linked at session start", auto)
        self.assertIn("Tracker protocol", auto)
        self.assertEqual(session.load_session(f"{sid}a")["tracker"], a)
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": f"{sid}a"}):
            self.assertIn(f"this session is off {a} now", run("start", "--decline", cwd=mine))
        self.assertNotIn("tracker", session.load_session(f"{sid}a"))
        self.assertIsNone(hook("session-start", f"{sid}c", mine, nogh, source="startup"))  # declined: no link or offer
        (store.HOME / ".declined.json").unlink()
        hook("session-start", f"{sid}w", mine, nogh, source="startup")  # linked by itself; it stays on it to watch
        self.assertIsNone(hook("prompt", f"{sid}w", mine, nogh, prompt="/work-tracker:watch"))
        self.assertTrue(watcher.granted(f"{sid}w"))
        self.assertEqual(session.load_session(f"{sid}w")["tracker"], a)
        watcher.set_grant(f"{sid}w", False)
        session.drop_session(f"{sid}w")
        run("archive", a)  # off every later lookup

        env = {**os.environ, "TRACKER_SESSION": sid}
        linked = subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True,
                                text=True, check=True).stdout
        self.assertIn("H-1", linked)
        brief = said(hook("session-start", sid, work, source="startup"))
        self.assertIn(f"[work-tracker] {s}", brief)
        self.assertIn("Tracker protocol", brief)
        self.assertIn(schema.ISOLATION_RULE, brief)  # stated once, carried into every session with a tracker
        self.assertIn(schema.ISOLATION_RULE, run("rules"))
        self.assertIn("H-1 in-progress", said(hook("prompt", sid, work, prompt="go")))
        self.assertIsNone(tool("Bash", sid, work, tool_input={"command": "ls"}))
        self.assertIsNone(tool("Bash", sid, work, tool_input={"command": "git log"},
                               tool_response={"stdout": "commit abc: gh pr"}))  # its output names no command
        go = {"questions": [{"question": "Push?", "options": [{"label": "Yes, push"}, {"label": "Not now"}]}]}
        self.assertIsNone(tool("AskUserQuestion", sid, work, tool_input=go))  # approval, not direction
        pick = {"questions": [{"question": "Store?", "options": [{"label": "Postgres"}, {"label": "Yes, SQLite"}]}]}
        self.assertIn("direction decision", said(tool("AskUserQuestion", sid, work, tool_input=pick)))

        helper = said(hook("subagent-start", sid, work, agent_id="a1", agent_type="Explore"))
        self.assertIn("do not write it", helper)
        self.assertIn("`tracker index` for the whole work", helper)  # not `context <tracker name>`
        self.assertIn(schema.ISOLATION_RULE, helper)  # a subagent writes code and commits too

        # The hooks log every commit; the model is asked only whether `next` still holds, once per `next`.
        committed = {"tool_input": {"command": "git commit -m x"}}
        commit(work, "hook work")
        self.assertIsNone(tool("Bash", sid, work, agent_id="a1", **committed))  # a subagent's: logged, no ask
        ask = said(hook("prompt", sid, work, prompt="done?"))  # the session is asked at its next message
        self.assertIn("1 commit(s) on feat/H-1 since `next` last changed", ask)
        self.assertIsNone(hook("prompt", sid, work, prompt="again"))
        commit(work, "more work")
        self.assertIsNone(tool("Bash", sid, work, **committed))  # logged; `next` is the one already asked about
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            self.assertIn("H-1: next set; feat/H-1 up to date", run("step", "--next", "Ship it", cwd=work))
        commit(work, "last work")  # as in the terminal: no post-bash hook sees it
        self.assertIsNone(hook("stop", sid, work))  # logs it, and holds nothing back: its move is its own
        self.assertIn("1 logged commit(s) since `next` last changed", run("--tracker", s, "here", cwd=work))
        self.assertIn("H-1 next: Ship it", said(hook("prompt", sid, work, prompt="and now?")))  # a new `next`
        log = (store.HOME / s / "log.md").read_text()
        self.assertEqual([x.split(": ", 1)[1].split(" ", 1)[1] for x in log.splitlines() if "Commits on" in x],
                         ["hook work", "more work", "last work"])  # each once

    def test_clear_keeps_the_tracker(self):
        (s, _), sid, main = new_tracker("Cleared"), f"clr{time.monotonic_ns()}", repo("main")
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
        (s, _), sid, work = new_tracker("Codex"), f"codex{time.monotonic_ns()}", repo("feat/C-1")
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
        self.assertIsNone(tool("AskUserQuestion", sid, work, tool_input=go))
        pick = {"questions": [{"title": "Store?", "options": ["Postgres", "SQLite"]}]}
        self.assertIn("direction decision", said(tool("AskUserQuestion", sid, work, tool_input=pick)))
        commit(work, "Codex commit")
        tool("Bash", sid, work, env=env, tool_input={"command": "git commit -m x"})
        self.assertIsNone(hook("stop", sid, work, env=env))
        self.assertEqual(session.load_session(sid)["activity"]["status"], "idle")
        self.assertEqual([x.sid for x in session.live_sessions(s)], [sid])
        self.assertEqual((store.HOME / s / "log.md").read_text().count("Codex commit"), 1)
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
        (s, _), sid, work = new_tracker("Edits"), f"sid{time.monotonic_ns()}", repo("feat/E-1")
        run("--tracker", s, "new", "E-1", "--title", "Edit work", "--branch", "feat/E-1")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run("start", s, cwd=work)
        root = store.HOME / s
        path = root / "tickets/E-1.md"
        cases = [(work, {"file_path": str(path)}),
                 (work, {"command": f"*** Begin Patch\n*** Update File: {path}\n*** End Patch"}),
                 (root, {"command": "*** Begin Patch\n*** Update File: tickets/E-1.md\n*** End Patch"})]
        for (cwd, used), status in zip(cases, ("in-progress", "todo", "in-progress")):
            with self.subTest(tool=used):
                before = session.load_session(sid)["seen"]
                path.write_text(re.sub(r"^status: .*", f"status: {status}", path.read_text(), flags=re.M))
                tool("apply_patch" if "command" in used else "Edit", sid, cwd, tool_input=used)
                self.assertNotEqual(session.load_session(sid)["seen"], before)
                self.assertNotIn("by another session or a PR sync", said(hook("prompt", sid, work, prompt="next")))

    def test_session_identity(self):
        cases = [({}, ""), ({"CODEX_THREAD_ID": "codex"}, "codex"),
                 ({"CLAUDE_CODE_SESSION_ID": "claude", "CODEX_THREAD_ID": "codex"}, "claude"),
                 ({"TRACKER_SESSION": "explicit", "CODEX_THREAD_ID": "codex"}, "explicit")]
        for env, expected in cases:
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(session.session_id(), expected)

    def test_recent_activity(self):
        sid, s, work = f"sid{time.monotonic_ns()}", slug(), repo("feat/L-1")
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
        # Its hooks say, not the file.
        self.assertEqual([(x.name, x.status) for x in session.live_sessions(s)], [("native", "idle")])
        native.write_text(json.dumps({"sessionId": sid, "pid": -1}))
        self.assertEqual(session.live_sessions(s), [])  # a dead native process is not replaced by hook activity
        session.session_activity(sid, "idle")
        self.assertTrue(session.load_session(sid)["native"])
        native.unlink()  # its process ended with no SessionEnd hook: it does not live on in its hook activity
        self.assertEqual(session.live_sessions(s), [])

        # A SessionEnd hook marks the session ended before its other work, which the host may cut short.
        ended = f"sid{time.monotonic_ns()}"
        session.save_session(ended, tracker=s, cwd=str(work))
        session.session_activity(ended, "idle")
        self.assertEqual([x.sid for x in session.live_sessions(s)], [ended])

        def cut_short(data):
            raise KeyboardInterrupt

        with mock.patch.dict(hooks.HOOKS, {"session-end": cut_short}), mock.patch.dict(os.environ), \
                mock.patch.object(hooks, "hook_input", return_value={"session_id": ended, "cwd": str(work)}):
            with self.assertRaises(KeyboardInterrupt):
                hooks.run_hook("session-end")
        self.assertEqual(session.load_session(ended)["activity"]["status"], "ended")
        self.assertEqual(session.live_sessions(s), [])

    def test_project_modules_do_not_run(self):
        """Python starts in the user's project: its json.py must not replace the stdlib's in the hooks or the CLI."""
        work, ran = repo("feat/S-1"), Path(BASE) / f"ran{time.monotonic_ns()}"
        (work / "json.py").write_text(f"open({str(ran)!r}, 'w').close()\n")
        data = json.dumps({"session_id": f"sid{time.monotonic_ns()}", "cwd": str(work)})
        runs = [([str(ROOT / "scripts/hook.sh"), "session-start"], data), ([str(ROOT / "bin/tracker"), "rules"], ""),
                ([*system.CLI, "rules"], "")]  # spawn() and a UI start the CLI so
        for cmd, stdin in runs:
            subprocess.run(cmd, input=stdin, cwd=work, capture_output=True, text=True)
            self.assertFalse(ran.exists(), cmd[0])

    @unittest.skipIf(system.WINDOWS, "Python there keeps its bytecode beside the source")
    def test_python_keeps_its_bytecode(self):
        """Python keeps its bytecode in the folder scripts/python.sh names (it says why), and spawn() and a UI start the
        CLI with the same one."""
        home = Path(tempfile.mkdtemp(dir=BASE))
        env = {**os.environ, "TRACKER_HOME": str(home)}
        kept = f"{str(ROOT).lstrip('/')}/scripts/tracker"
        for cmd, stdin, module in (([str(ROOT / "bin/tracker"), "list"], "", "cli"),
                                   ([str(ROOT / "scripts/hook.sh"), "prompt"], '{"prompt": "/work-tracker:watch"}',
                                    "hooks")):
            subprocess.run(cmd, input=stdin, env=env, capture_output=True, text=True, check=True)
            self.assertTrue(list((home / ".pycache" / kept).glob(f"{module}.*.pyc")), cmd)
        code = "import sys; sys.path.insert(0, sys.argv[1]); from tracker import system; print(system.PYTHON[-1])"
        flags = ["-I", "-X", f"pycache_prefix={home / '.pycache'}"]
        out = subprocess.run([sys.executable, *flags, "-c", code, str(ROOT / "scripts")], capture_output=True,
                             text=True, check=True).stdout
        self.assertEqual(out.strip(), f"pycache_prefix={home / '.pycache'}")
        # A plugin version that is gone leaves its bytecode: the next version removes it, and no other folder.
        gone, other = home / ".pycache/old/1.0/scripts/tracker", home / ".pycache/old/1.0/lib"
        for d in (gone, other):
            d.mkdir(parents=True)
        with mock.patch.object(store, "PYCACHE", home / ".pycache"):
            store.prune_bytecode()  # this process keeps its bytecode elsewhere: not the tracker's to remove
            self.assertTrue(gone.exists())
            with mock.patch.object(sys, "pycache_prefix", str(home / ".pycache")):
                store.prune_bytecode()
        self.assertEqual((gone.exists(), other.exists(), (home / ".pycache" / kept).exists()), (False, True, True))

    def test_commits_wait_for_a_ticket(self):
        (s, _), sid, work = new_tracker("Waits"), f"sid{time.monotonic_ns()}", repo("feat/W-1")
        run("--tracker", s, "new", "W-1", "--title", "Wait work", "--branch", "feat/W-1")
        env = {**os.environ, "TRACKER_SESSION": sid}
        subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True, check=True)
        commit(work, "early work")
        self.assertIsNone(tool("Bash", sid, work, tool_input={"command": "git commit"}))  # no ticket under way
        marks = model.tracker_at(s).raw_state()["synced"]
        self.assertEqual([x.get("waiting") for x in marks.values()], [git.head_of(work)])  # the HEAD it waits at
        with mock.patch.object(session, "new_commits") as scan:  # the next turn's end: git is not asked again
            self.assertEqual(session.record_commits(session.match_cwd(work, sid), work), [])
        scan.assert_not_called()
        self.assertIn("No ticket in progress on feat/W-1; 1 commit(s) not logged",
                      said(hook("prompt", sid, work, prompt="go")))
        with mock.patch.object(hooks, "lag") as look, contextlib.redirect_stdout(io.StringIO()) as out:
            hooks.hook_prompt({"session_id": sid, "cwd": str(work), "prompt": "more"})
        look.assert_not_called()  # said for this mark: no git until the next state line
        self.assertEqual(out.getvalue(), "")
        run("--tracker", s, "set", "W-1", "status=in-progress", cwd=work)
        self.assertIsNone(hook("stop", sid, work))
        self.assertIn("[W-1] Commits on feat/W-1: ", (store.HOME / s / "log.md").read_text())  # logged for it now

    def test_commits_from_main_not_logged(self):
        """A merge from the default branch or a rebase onto it brings in work its own tickets logged: only the
        branch's own commits are logged."""
        for update in (["merge", "-q", "--no-edit", "main"], ["rebase", "-q", "main"]):
            (s, _), sid, work = new_tracker("Merges"), f"sid{time.monotonic_ns()}", repo("main")
            sh = lambda *a: subprocess.run(["git", "-C", str(work), *a], check=True, capture_output=True)  # noqa: E731
            sh("checkout", "-qb", "feat/M-1")
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
            logged = [x.split(": ", 1)[1] for x in (store.HOME / s / "log.md").read_text().splitlines()
                      if "Commits on" in x]
            self.assertEqual([c.split(" ", 1)[1] for x in logged for c in x.split("; ")],
                             ["own work", "after the update"], update[0])

    def test_work_git_shows(self):
        (s, _), sid, work = new_tracker("Git"), f"sid{time.monotonic_ns()}", repo("feat/G-1")
        run("--tracker", s, "new", "G-1", "--title", "Git work", "--branch", "feat/G-1")
        (work / "before.txt").write_text("uncommitted before the mark")
        env = {**os.environ, "TRACKER_SESSION": sid}
        subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True, check=True)
        self.assertIsNone(hook("prompt", sid, work, prompt="go"))  # no ticket and no work since the mark: the brief
        self.assertIsNone(hook("prompt", sid, work, prompt="more"))
        (work / "edited.txt").write_text("written by a Bash command: no hook saw it")
        told = said(hook("prompt", sid, work, prompt="again"))
        self.assertIn("No ticket in progress on feat/G-1; 1 uncommitted file(s) since", told)
        self.assertIsNone(hook("prompt", sid, work, prompt="and again"))  # once per mark
        self.assertIsNone(tool("Edit", sid, work, tool_input={"file_path": str(work / "edited.txt")}))

        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):  # this session's own change: not reported
            run("set", "G-1", "status=in-progress", cwd=work)
        self.assertIsNone(hook("prompt", sid, work, prompt="4"))
        self.assertIn("G-1 in-progress; 1 uncommitted file(s)", said(hook("prompt", sid, work, prompt="5")))
        self.assertIn("The tracker may lag the work: 1 uncommitted file(s)", run("--tracker", s, "here", cwd=work))
        run("--tracker", s, "step", cwd=work)
        self.assertNotIn("may lag", run("--tracker", s, "here", cwd=work))

    def test_one_ticket_of_a_busy_branch(self):
        (s, _), sid, work = new_tracker("Busy"), f"sid{time.monotonic_ns()}", repo("feat/busy")
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
        tr = model.tracker_at(s)
        self.assertEqual((tr.lookup("B-1").get("next"), tr.lookup("B-2").get("next")), ("", "Ship it"))
        self.assertIn("[B-2] Built part 2", (tr.root / "log.md").read_text())

    def test_one_process_reads_the_event_file_once(self):
        (s, t), work = new_tracker("Once"), repo("feat/once")
        for i in (1, 2, 3):
            run(*t, "new", f"O-{i}", "--title", f"Part {i}", "--branch", "feat/once")
            run(*t, "set", f"O-{i}", "status=in-progress", cwd=work)
            run(*t, "submit", f"O-{i}", "--what", "plan")
        m, reads = session.match_cwd(work, tracker=model.tracker_at(s)), []
        self.assertEqual(len(m.focus), 3)
        real = model.event_lines
        model.EVENTS_READ.clear()  # this process's writes read it already
        counted = mock.patch.object(model, "event_lines", lambda tr: reads.append(tr) or real(tr))
        with counted:
            views.brief(m, work)
            views.brief(m, work)
        self.assertEqual(len(reads), 1)
        with store.locked():
            model.append_event(m.tracker, "note", "A note", [])
        with counted:
            views.brief(m, work)
        self.assertEqual(len(reads), 2)  # the cache follows the file


class TwoSessions(unittest.TestCase):
    """A tracker with T-1 under way on branch f1 and T-2 not started; session A on T-1 (`tracker start`), and B,
    another session. The commands and hooks they run are the real ones."""

    def setUp(self, repos: str = ""):
        self.s, self.t = new_tracker("Two", repos)
        run(*self.t, "new", "T-1", "--title", "One", "--branch", "f1")
        run(*self.t, "new", "T-2", "--title", "Two")
        run(*self.t, "set", "T-1", "status=in-progress")
        self.work = repo("f1")
        n = time.monotonic_ns()
        self.a, self.b = f"sa{n}", f"sb{n}"
        for sid in (self.a, self.b):
            self.addCleanup(session.drop_session, sid)
            self.addCleanup(watcher.set_grant, sid, False)
        self.as_a("start", self.s)

    def as_a(self, *args: str, code: int = 0, cwd: Path | None = None) -> str:
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": self.a}):
            return run(*args, cwd=cwd or self.work, code=code)

    def as_b(self, *args: str, code: int = 0) -> str:
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": self.b}):
            return run(*args, cwd=self.work, code=code)

    def message(self) -> str:
        """A's next message: what the prompt hook tells it."""
        return said(hook("prompt", self.a, self.work, env=no_gh(), prompt="go on"))

    def run_watch(self, w: watcher.Watcher, once: bool = False, during=None) -> tuple[str, int]:
        """`Watcher.run` in this process: its output, and how many looks it slept before; `during` runs at the first
        sleep. Each sleep is short, so a change still takes SETTLE_S to settle."""
        sleeps, real = [], time.sleep

        def sleep(_):
            if not sleeps and during:
                during()
            sleeps.append(1)
            if len(sleeps) > 300:
                raise KeyboardInterrupt  # it never returned: the assertions below say so
            real(0.05)

        out = io.StringIO()
        with mock.patch.object(watcher.time, "sleep", sleep), contextlib.redirect_stdout(out):
            w.run(once)
        return out.getvalue(), len(sleeps)


class SessionNews(TwoSessions):
    """What a session is told at its next message about its tickets: what other sessions, a terminal or GitHub wrote
    on them since it last looked, once, and never its own writes (R5)."""

    def test_a_note_another_session_writes_on_my_ticket_is_in_my_next_message(self):
        self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved")
        self.assertIn("T-1: base branch moved", self.message())
        self.assertNotIn("base branch moved", self.message())

    def test_a_review_of_my_submission_is_in_my_next_message(self):
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.message()
        self.as_b(*self.t, "review", "T-1", "--what", "plan", "--verdict", "changes", "--finding", "Split the parser")
        told = self.message()
        self.assertIn("T-1 plan review: changes, 1 finding: 1. Split the parser", told)
        self.assertIn("T-1 move now: ", told)

    def test_my_own_writes_are_no_news(self):
        self.message()
        self.as_a(*self.t, "log", "--ref", "T-1", "my own note")
        self.as_a(*self.t, "step", "T-1", "Built the parser", "--next", "Test it")
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.assertNotIn("Changed on", self.message())

    def test_a_pr_review_is_news_whoever_synced(self):
        self.setUp("a/x")
        self.message()
        rounds = [review_round(101, "rev", "CHANGES_REQUESTED", "2026-09-02T01:02:03Z")]
        with mock.patch.object(github, "gh", answers([pull(11, branch="f1", decision="CHANGES_REQUESTED",
                                                           rounds=rounds)])):
            self.as_a(*self.t, "sync")  # A's own sync, as its hooks' background sync runs it
        told = self.message()
        self.assertIn("PR #11 review by rev: changes requested", told)
        self.assertIn("T-1 move now: you", told)  # the review made it A's

    def test_a_settled_decision_is_said_once(self):
        self.as_b(*self.t, "decide", "Pick a store", "--blocks", "T-1")
        self.assertIn("D-01 open now touches this work", self.message())
        self.as_b(*self.t, "decide", "D-01", "--resolve", "Postgres", "--source", "the spec")
        told = self.message()
        self.assertIn("D-01 settled: ", told)
        self.assertEqual(told.count("Postgres"), 1, told)  # not its event's line too

    def test_an_event_on_another_ticket_is_no_news(self):
        self.message()
        self.as_b(*self.t, "log", "--ref", "T-2", "only about two")
        self.as_b(*self.t, "log", "about nothing")
        self.assertNotIn("Changed on", self.message())

    def test_a_wait_on_a_human_and_commits_are_no_news(self):
        self.as_b("start", self.s)  # B on T-1 too: its waits and commits name T-1
        self.message()
        hook("waiting", self.b, self.work, env=no_gh(), tool_name="Bash")
        commit(self.work, "B's work")
        tool("Bash", self.b, self.work, env=no_gh(), tool_input={"command": "git commit -m x"})
        kinds = {e["kind"] for e in model.read_events(model.tracker_at(self.s)) if e.get("session") == self.b}
        self.assertTrue({"human.wait", "commits"} <= kinds, kinds)
        self.assertNotIn("Changed on", self.message())

    def test_more_than_five_events_are_cut_to_the_newest_five_and_a_count(self):
        self.message()
        for i in range(1, 8):
            self.as_b(*self.t, "log", "--ref", "T-1", f"note {i}")
        told = self.message()
        self.assertIn("(+2 earlier: `tracker history --ref T-1`); T-1: note 3; T-1: note 4; T-1: note 5; "
                      "T-1: note 6; T-1: note 7.", told)
        self.assertNotIn("note 2", told)

    def test_events_from_before_the_brief_are_no_news(self):
        self.as_b(*self.t, "log", "--ref", "T-1", "written before the brief")
        self.as_a("start", self.s)
        self.assertNotIn("written before the brief", self.message())

    def test_a_terminals_note_is_news(self):
        self.message()
        run(*self.t, "log", "--ref", "T-1", "from a terminal")
        self.assertIn("T-1: from a terminal", self.message())

    def test_news_survives_a_ticket_of_mine_closing(self):
        run(*self.t, "new", "T-3", "--title", "Three", "--branch", "f1")
        run(*self.t, "set", "T-3", "status=in-progress")
        self.as_a("start", self.s)  # on T-1 and T-3
        self.message()
        self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved")
        self.as_b(*self.t, "set", "T-3", "status=done", "summary=Shipped")  # A's focus is now T-1 alone
        told = self.message()
        self.assertIn("Changed on T-1, T-3 since", told)  # the tickets its lines are about
        self.assertIn("T-3 is now done", told)
        self.assertIn("T-1: base branch moved", told)

    def test_my_own_writes_after_my_tickets_changed_are_no_news(self):
        run(*self.t, "new", "T-3", "--title", "Three", "--branch", "f1")
        run(*self.t, "set", "T-3", "status=in-progress")
        self.as_a("start", self.s)  # on T-1 and T-3
        self.message()
        self.as_a(*self.t, "set", "T-3", "status=done", "summary=Shipped")  # A's focus is now T-1 alone
        self.as_a(*self.t, "set", "T-1", "status=done", "summary=Shipped")
        self.assertNotIn("Changed on", self.message())

    def test_news_survives_another_ticket_starting_on_my_branch(self):
        run(*self.t, "new", "T-3", "--title", "Three", "--branch", "f1")
        self.message()
        self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved")
        run(*self.t, "set", "T-3", "status=in-progress")  # A's focus is now T-1 and T-3
        self.as_b(*self.t, "log", "--ref", "T-3", "the API changed under you")  # before A looks again
        told = self.message()
        self.assertIn("Changed on T-1, T-3 since", told)
        self.assertIn("T-1: base branch moved", told)
        self.assertIn("T-3: the API changed under you", told)
        self.as_b(*self.t, "log", "--ref", "T-3", "after it started")
        self.assertIn("T-3: after it started", self.message())

    def test_a_note_on_a_ticket_i_started_myself_is_news(self):
        run(*self.t, "new", "T-3", "--title", "Three", "--branch", "f1")
        self.message()
        self.as_a(*self.t, "set", "T-3", "status=in-progress")  # its own start: no brief, no new baseline
        self.as_b(*self.t, "log", "--ref", "T-3", "the API changed under you")
        self.assertIn("T-3: the API changed under you", self.message())

    def test_my_own_hand_edit_of_my_last_ticket_is_no_news(self):
        self.message()
        path = store.HOME / self.s / "tickets/T-1.md"
        path.write_text(re.sub(r"^status: .*", "status: done", path.read_text(), flags=re.M))
        tool("Edit", self.a, self.work, env=no_gh(), tool_input={"file_path": str(path)})
        self.assertNotIn("Changed on", self.message())

    def test_a_message_from_the_tracker_folder_keeps_the_baseline(self):
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.message()
        hook("prompt", self.a, store.HOME / self.s, env=no_gh(), prompt="go on")  # after a `cd` there
        self.as_b(*self.t, "review", "T-1", "--what", "plan", "--verdict", "changes", "--finding", "Split it")
        self.assertIn("T-1 move now: you", self.message())

    def test_news_on_a_restored_shorter_log_does_not_raise(self):
        self.message()
        file = store.HOME / self.s / schema.EVENTS_FILE
        backup = file.read_bytes()
        for text in ("one", "two"):
            self.as_b(*self.t, "log", "--ref", "T-1", text)
        self.assertIn("T-1: two", self.message())
        file.write_bytes(backup)  # restored: A has seen past its end
        self.as_b(*self.t, "log", "--ref", "T-1", "after the restore")  # numbered as one A has seen
        self.assertNotIn("Changed on", self.message())
        self.as_b(*self.t, "log", "--ref", "T-1", "later")
        self.assertIn("T-1: later", self.message())


class BriefSize(unittest.TestCase):
    """The brief goes into every session and comes back after each compaction: it says what this work needs, once and
    short, and points to the command that has the rest."""

    def test_the_brief_leaves_out_what_this_work_does_not_need(self):
        (s, t), work = new_tracker("Brief"), repo("feat/B-9")
        run(*t, "add", "tracker", "context", "Spec: [Build spec](https://docs.example/spec) — the rules it keeps")
        for i in range(1, 10):
            run(*t, "new", f"B-{i}", "--title", f"Work {i}", "--branch", "feat/B-9")
        for i in range(1, 8):
            run(*t, "set", f"B-{i}", "status=done", "summary=shipped")
        run(*t, "set", "B-9", "status=in-progress", cwd=work)
        tr = model.tracker_at(s)
        tr.lookup("B-1").save({"colour": "red"})  # a warning on a ticket this session does not work on
        brief = run(*t, "here", cwd=work)
        self.assertIn("B-9 in-progress; also B-8 todo, 7 done", brief)  # closed ones as a count
        self.assertIn("Spec: Build spec — the rules it keeps", brief)
        self.assertIn("`tracker show tracker --section context` gives the links", brief)
        self.assertNotIn("https://docs.example/spec", brief)
        self.assertNotIn("Tracker check", brief)  # `check` has it, and the write that made it said so
        model.tracker_at(s).lookup("B-9").save({"colour": "blue"})
        brief = run(*t, "here", cwd=work)
        self.assertIn("Tracker check, on this work:\n  ⚠ B-9: unknown frontmatter key 'colour'", brief)
        self.assertNotIn("B-1: unknown", brief)
        self.assertIn("https://docs.example/spec", run(*t, "show", "tracker", "--section", "context"))

        for i in (8, 9):  # the session's own ticket named first
            run(*t, "add", f"B-{i}", "link", f"Issue: [SC-{i} Story](https://issues.example/{i})")
        tr = model.tracker_at(s)
        self.assertIn("Issue fields to read for 2 ticket(s): B-9, B-8.", views.issue_request(tr, [tr.lookup("B-9")]))
        due = run(*t, "issue")
        self.assertIn("B-8  https://issues.example/8", due)
        self.assertIn(views.ISSUE_HOW, due)


class SequenceSort(unittest.TestCase):
    """A UI's sequence sorts by any column (ui/model.js); `tracker data` gives each row the values it sorts on."""

    def test_rows_carry_their_sort_values(self):
        s, t = new_tracker("Work")
        run(*t, "new", "T-10", "--title", "Late", "--group", "ui")
        run(*t, "new", "T-2", "--title", "Base", "--group", "api")
        run(*t, "new", "T-3", "--title", "Uses base", "--depends", "T-2")
        run(*t, "new", "T-4", "--title", "Gone", "--group", "api")
        run(*t, "set", "T-4", "status=dropped", "summary=not needed")
        run(*t, "new", "T-5", "--title", "Uses that", "--depends", "T-3")
        rows = {x["id"]: x for x in data(s)["tickets"]}

        # `order`, the Step column's sort: the depth-first dependency order the sequence starts in, a chain together and
        # dropped last. `sort.status`: its place in todo → dropped.
        self.assertEqual([[rows[i][k] for k in ("order", "group")] + [rows[i]["sort"]["status"]]
                          for i in ("T-2", "T-3", "T-5", "T-10", "T-4")],
                         [[0, "api", 0], [1, "", 0], [2, "", 0], [3, "ui", 0], [4, "api", 4]])
        self.assertEqual(set(rows["T-2"]["sort"]), {"status", *schema.SCALES})  # what a row does not show as it sorts

        # What the ticket waits on, and what it unblocks.
        self.assertEqual((rows["T-3"]["waits"], rows["T-3"]["unblocks"]), ([{"id": "T-2", "done": False}], ["T-5"]))
        self.assertEqual(rows["T-3"]["gate"], {"state": "blocked"})
        self.assertEqual((rows["T-2"]["waits"], rows["T-2"]["unblocks"]), ([], ["T-3"]))

    def test_rows_carry_the_graph(self):
        """ui/model.js draws the dependency graph from each row's data: its step (its dot's column), its links and its
        state (its dot's colour)."""
        s, t = new_tracker("Work")
        run(*t, "new", "T-1", "--title", "Base")
        run(*t, "new", "T-2", "--title", "Uses base", "--depends", "T-1")
        run(*t, "new", "T-3", "--title", "Also uses base", "--depends", "T-1")
        run(*t, "new", "T-4", "--title", "Joins both", "--depends", "T-3,T-2")
        run(*t, "new", "T-5", "--title", "Alone")

        def rows() -> dict[str, tuple]:
            return {x["id"]: (x["step"], x["links"], x["state"]) for x in data(s)["tickets"]}
        self.assertEqual(rows(), {"T-1": (1, [], "ready"), "T-5": (1, [], "ready"),
                                  "T-2": (2, ["T-1"], "blocked"), "T-3": (2, ["T-1"], "blocked"),
                                  "T-4": (3, ["T-3", "T-2"], "blocked")})
        run(*t, "set", "T-1", "status=done")
        run(*t, "set", "T-2", "status=in-progress")
        run(*t, "set", "T-5", "status=dropped")
        self.assertEqual({i: x[2] for i, x in rows().items()},
                         {"T-1": "closed", "T-2": "going", "T-3": "ready", "T-4": "blocked", "T-5": "dropped"})

    def test_the_graph_draws_only_the_links_no_chain_implies(self):
        edges = {"A": [], "B": ["A"], "C": ["B", "A"], "D": ["C", "A", "B"], "E": ["E2"], "E2": ["E"]}  # E: a cycle
        self.assertEqual(viewmodel.drawn_links(edges),
                         {"A": [], "B": ["A"], "C": ["B"], "D": ["C"], "E": ["E2"], "E2": ["E"]})

    def test_an_unknown_status_still_renders(self):
        """A hand-edited status outside STAGES sorts after them, and a UI shows the check's error."""
        s, _ = new_tracker("Work")
        run("--tracker", s, "new", "T-1", "--title", "Odd")
        path = store.HOME / s / "tickets" / "T-1.md"
        path.write_text(re.sub(r"^status: .*", "status: wip", path.read_text(), flags=re.M))
        got = data(s)
        self.assertEqual(got["tickets"][0]["sort"]["status"], len(schema.STAGES))
        self.assertTrue(any(x.startswith("T-1: status 'wip' not one of") for x in got["check"]["errors"]))


class IssueFields(unittest.TestCase):
    """A ticket's issue fields (priority, size, due day, when its issue was created) come from its issue tracker
    through the model, which reads them with its own tool for that issue tracker and records them with
    `tracker issue`. The tracker says when they are due."""

    def tracker(self) -> tuple[str, tuple[str, str]]:
        s, t = new_tracker("Work")
        for ident, title in (("T-1", "Linked"), ("T-2", "Unlinked"), ("T-3", "Linked, closed")):
            run(*t, "new", ident, "--title", title)
        run(*t, "add", "T-1", "link", "Issue: [SC-1 Story one](https://issues.example/story/1)")
        run(*t, "add", "T-3", "link", "Issue: [SC-3 Story three](https://issues.example/story/3)")
        run(*t, "set", "T-3", "status=done", "summary=shipped")
        model.tracker_at(s).lookup("T-3").save({"started_at": "2026-09-01T00:00:00Z"})  # it has a wait time
        return s, t

    def read_at(self, s: str, ident: str, at: float) -> None:
        tr = model.tracker_at(s)
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
        t1 = model.tracker_at(s).lookup("T-1")
        self.assertEqual((t1.get("priority"), t1.get("issue_created_at")), ("1", "2026-09-30T23:30:00Z"))
        self.assertEqual(schema.read_value("issue_created_at", "2026-10-01T09:30:00Z"),
                         "2026-10-01T09:30:00Z")  # Z: UTC
        self.assertIn("T-3: read; nothing to record", run(*t, "issue", "T-3"))  # its issue has no fields to give
        self.assertIn("no ticket's issue fields are to read", run(*t, "issue"))

        self.read_at(s, "T-1", time.time() - 30 * 86400)  # read once is enough: no read again by age
        self.assertIn("no ticket's issue fields are to read", run(*t, "issue"))

        run(*t, "refresh")  # a UI's Refresh asks again for every open one
        self.assertEqual(re.findall(r"^(T-\d+) ", run(*t, "issue"), re.M), ["T-1"])

        run(*t, "issue", "T-1", "--priority", "")
        self.assertNotIn("priority", model.tracker_at(s).lookup("T-1").meta)  # the issue lost its priority
        self.assertIn("has no Issue link", run(*t, "issue", "T-2", "--priority", "3", code=2))
        self.assertIn("needs a time zone", run(*t, "issue", "T-1", "--created", "2026-10-01T09:30:00", code=2))
        self.assertIn("not an ISO 8601 time", run(*t, "issue", "T-1", "--created", "last week", code=2))

    def test_a_level_is_a_number_any_ticket_can_have(self):
        scales = {"priority": (0, 4, "0-4", ("High", "5", "-1", "P1", "1.5"), "High", "High",
                               "Priority: 0 (most urgent) to 4 (least)"),
                  "size": (2, 5, "1-5", ("M", "0", "6", "1.5"), "XL", "8", "Size: 1 (XS) to 5 (XL)")}
        for key, (low, high, span, bad, bad_new, bad_issue, rule) in scales.items():
            with self.subTest(key=key):
                s, t = self.tracker()
                run(*t, "new", "T-5", "--title", "No issue", f"--{key}", str(low))
                run(*t, "set", "T-2", f"{key}={high}")  # no Issue link: the model's own
                run(*t, "issue", "T-1", f"--{key}", "3")
                tr = model.tracker_at(s)
                self.assertEqual([model.level(tr.lookup(x), key) for x in ("T-5", "T-2", "T-1", "T-3")],
                                 [low, high, 3, None])
                for value in bad:
                    self.assertIn(f"is not {span}", run(*t, "set", "T-2", f"{key}={value}", code=2))
                self.assertIn(f"is not {span}", run(*t, "new", "T-6", "--title", "Six", f"--{key}", bad_new, code=2))
                self.assertIn(f"is not {span}", run(*t, "issue", "T-1", f"--{key}", bad_issue, code=2))
                run(*t, "set", "T-2", f"{key}=")
                self.assertIsNone(model.level(model.tracker_at(s).lookup("T-2"), key))
                run(*t, "issue", "T-1", f"--{key}", "")
                self.assertNotIn(key, model.tracker_at(s).lookup("T-1").meta)  # the issue lost its value
                model.tracker_at(s).lookup("T-2").save({key: "Large"})
                self.assertIn(f"T-2: {key} 'Large' is not {span}", run(*t, "check", code=1))
                rules = run(*t, "rules")
                self.assertIn(rule, rules)
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

        run(*t, "refresh")  # a UI's Refresh, which cannot read issues itself
        self.assertIn("Issue fields to read for 1 ticket(s): T-1", said(hook("prompt", sid, work, prompt="next")))
        self.assertNotIn("Issue fields to read", said(hook("prompt", sid, work, prompt="again")))  # once per request

    def updates(self, s: str) -> list[str]:
        return [f"{t.id}: {'; '.join(items)}" for t, items in model.tracker_at(s).issue_updates_due()]

    def write(self, t: tuple[str, str], *ids: str) -> None:
        """The model's write: list the updates, then record each ticket's as written."""
        run(*t, "issue")
        for ident in ids:
            run(*t, "issue", ident, "--written")

    def test_issue_updates_are_off_until_the_tracker_turns_them_on(self):
        s, t = self.tracker()
        run(*t, "set", "T-1", "status=in-progress")
        run(*t, "decide", "Which store", "--refs", "T-1", "--resolve", "Postgres", "--by", "me")
        self.assertEqual(self.updates(s), [])  # off: the tracker never asks to write to an issue
        self.assertNotIn("Issue updates", run(*t, "issue"))
        self.assertIn("issue_updates 'yes' is not one of on|off", run(*t, "set", "tracker", "issue_updates=yes",
                                                                       code=2))
        self.assertIn("issue_updates", run(*t, "rules"))

    def test_status_summary_and_settled_decisions_are_due_until_written(self):
        s, t = self.tracker()
        run(*t, "decide", "Settled before", "--refs", "T-1", "--resolve", "Old answer", "--by", "me")
        run(*t, "set", "tracker", "issue_updates=on")
        # T-1 has not started and was never written: its issue keeps whatever it shows. T-3 closed with a summary.
        # A decision settled before the tracker turned updates on is history, not news.
        self.assertEqual(self.updates(s), ["T-3: status done; summary: shipped"])
        run(*t, "decide", "Which store", "--refs", "T-1", "--resolve", "Postgres, for the joins", "--by", "me")
        run(*t, "decide", "Which queue", "--blocks", "T-1", "--question", "SQS or Kafka?")
        self.assertEqual(self.updates(s)[0], "T-1: decision settled: Which store: Postgres, for the joins")
        run(*t, "decide", "D-3", "--resolve", "SQS", "--by", "me")  # a decision the ticket waited on counts too
        self.assertIn("decision settled: Which queue: SQS", self.updates(s)[0])

        run(*t, "issue", "T-1")  # its fields read: the listing holds only updates
        run(*t, "issue", "T-3")
        listing = run(*t, "issue")
        self.assertTrue(listing.startswith("no ticket's issue fields are to read"))
        self.assertIn("Issue updates to write for 2 ticket(s)", listing)
        self.assertIn("T-1  https://issues.example/story/1", listing)
        self.assertIn(schema.ISOLATION_RULE, listing)  # the isolation rule, where the writing happens
        self.assertNotIn("T-2", listing)  # no Issue link: nowhere to write

        self.assertIn("T-1: written as listed", run(*t, "issue", "T-1", "--written"))
        self.assertEqual(self.updates(s), ["T-3: status done; summary: shipped"])
        run(*t, "set", "T-1", "status=in-progress")
        self.assertEqual(self.updates(s)[0], "T-1: status in-progress")  # a later change is due again
        self.write(t, "T-1", "T-3")
        self.assertEqual(self.updates(s), [])
        self.assertIn("has no Issue link", run(*t, "issue", "T-2", "--written", code=2))
        self.assertIn("listed no update", run(*t, "issue", "T-1", "--written", code=2))  # nothing listed to record

        run(*t, "set", "tracker", "issue_updates=off")
        run(*t, "set", "T-1", "status=dropped", "summary=not needed")
        run(*t, "decide", "Which cache", "--refs", "T-1", "--resolve", "None", "--by", "me")
        self.assertEqual(self.updates(s), [])
        run(*t, "set", "tracker", "issue_updates=on")  # on again: what changed while off is due
        self.assertEqual(self.updates(s), ["T-1: status dropped; summary: not needed; "
                                           "decision settled: Which cache: None"])

    def test_a_changed_answer_summary_or_stage_is_due_again(self):
        s, t = self.tracker()
        run(*t, "decide", "Which queue", "--refs", "T-1", "--resolve", "SQS", "--by", "me")
        run(*t, "set", "tracker", "issue_updates=on")
        run(*t, "set", "T-1", "status=in-progress")
        self.write(t, "T-1", "T-3")
        run(*t, "decide", "D-1", "--resolve", "Kafka", "--by", "me")  # settled again: history holds the old answer
        run(*t, "set", "T-3", "summary=shipped, with the retry")  # set after the close
        self.assertEqual(self.updates(s), ["T-1: decision settled: Which queue: Kafka",
                                           "T-3: status done; summary: shipped, with the retry"])
        self.write(t, "T-1", "T-3")
        run(*t, "set", "T-3", "status=in-progress")  # reopened: its old summary is not news
        tr = model.tracker_at(s)
        tr.lookup("T-1").save({"prs": ["9:open"]})  # what `sync` writes: its PR is in review
        self.assertEqual(self.updates(s), ["T-1: status in-review", "T-3: status in-progress"])
        self.write(t, "T-1")
        tr.lookup("T-1").save({"prs": ["9:merged"]})
        self.assertEqual(self.updates(s), ["T-1: status done", "T-3: status in-progress"])

    def test_written_records_what_was_listed_not_what_changed_since(self):
        s, t = self.tracker()
        run(*t, "set", "tracker", "issue_updates=on")
        run(*t, "issue")  # lists T-3's status
        run(*t, "decide", "Which store", "--refs", "T-3", "--resolve", "Postgres", "--by", "me")  # another session
        out = run(*t, "issue", "T-3", "--written")
        self.assertIn("T-3: written as listed; due since the listing: decision settled: Which store: Postgres", out)
        self.assertEqual(self.updates(s), ["T-3: decision settled: Which store: Postgres"])

    def test_written_with_fields_records_both_and_check_reads_the_setting(self):
        s, t = self.tracker()
        run(*t, "set", "tracker", "issue_updates=on")
        run(*t, "issue")
        out = run(*t, "issue", "T-3", "--size", "2", "--written")
        self.assertIn("T-3: size=2", out)
        self.assertIn("T-3: written", out)
        self.assertEqual(self.updates(s), [])
        self.assertEqual(model.tracker_at(s).lookup("T-3").get("size"), "2")
        tr = model.tracker_at(s)
        tr.readme.save({"issue_updates": "yes"})  # a hand edit
        self.assertIn("README.md: issue_updates 'yes' is not one of on|off", run(*t, "check", code=1))

    def test_on_with_no_record_of_what_was_written_asks_for_nothing(self):
        s, t = self.tracker()
        run(*t, "decide", "Which store", "--refs", "T-1", "--resolve", "Postgres", "--by", "me")
        model.tracker_at(s).readme.save({"issue_updates": "on"})  # a hand edit, or a lost .state.json
        self.assertEqual(self.updates(s), [])  # not every issue written again
        self.assertIn("no record of what its issues show", run(*t, "check"))
        run(*t, "set", "tracker", "issue_updates=on")  # already on: starts the record
        self.assertEqual(self.updates(s), ["T-3: status done; summary: shipped"])
        self.assertNotIn("no record", run(*t, "check"))
        run(*t, "decide", "Which queue", "--refs", "T-1", "--resolve", "SQS", "--by", "me")
        run(*t, "set", "tracker", "issue_updates=on")  # on again: the history stays, and so does what is due
        self.assertEqual(self.updates(s), ["T-1: decision settled: Which queue: SQS",
                                           "T-3: status done; summary: shipped"])

    def test_a_write_asks_for_the_update_it_makes_due(self):
        s, t = self.tracker()
        run(*t, "set", "tracker", "issue_updates=on")
        self.write(t, "T-3")
        out = run(*t, "decide", "Which store", "--refs", "T-1", "--resolve", "Postgres", "--by", "me")
        self.assertIn("[work-tracker] Issue updates to write for 1 ticket(s): T-1", out)
        self.assertIn("issue_updates=off` stops these requests", out)  # the way out with no tool for it
        self.assertNotIn("Issue updates to write", run(*t, "log", "a note", "--ref", "T-1"))  # nothing new: no repeat
        self.assertIn("Issue updates to write", run(*t, "set", "T-1", "status=in-progress"))

    def test_the_request_names_the_sessions_tickets_first_and_counts_the_rest(self):
        s, t = self.tracker()
        run(*t, "set", "tracker", "issue_updates=on")
        for n in range(4, 10):
            run(*t, "new", f"T-{n}", "--title", f"Linked {n}")
            run(*t, "add", f"T-{n}", "link", f"Issue: [SC-{n} Story](https://issues.example/story/{n})")
            run(*t, "set", f"T-{n}", "status=in-progress")
        tr = model.tracker_at(s)
        request = views.issue_update_request(tr, [tr.lookup("T-9")])
        self.assertIn("for 7 ticket(s): T-9, T-3, T-4, T-5, T-6 +2.", request)

    def test_the_brief_and_the_prompt_hook_ask_for_due_updates_once_per_change(self):
        s, t = self.tracker()
        sid, work = f"sid{time.monotonic_ns()}", repo("feat/T-1")
        run(*t, "issue", "T-1")
        run(*t, "issue", "T-3")  # no issue fields due: only updates are asked for
        run(*t, "set", "tracker", "issue_updates=on")
        run(*t, "set", "T-1", "status=in-progress", cwd=work)
        env = {**os.environ, "TRACKER_SESSION": sid}
        started = subprocess.run([str(ROOT / "bin/tracker"), "start", s], cwd=work, env=env, capture_output=True,
                                 text=True, check=True).stdout
        self.assertIn("Issue updates to write for 2 ticket(s): T-1, T-3", started)
        self.assertNotIn("Issue updates", said(hook("prompt", sid, work, prompt="go")))  # `start`'s brief told them
        run(*t, "decide", "Which store", "--refs", "T-1", "--resolve", "Postgres", "--by", "me")
        self.assertIn("Issue updates to write", said(hook("prompt", sid, work, prompt="next")))  # a new one
        self.assertNotIn("Issue updates", said(hook("prompt", sid, work, prompt="again")))  # told once
        self.write(t, "T-1")
        self.assertNotIn("Issue updates", said(hook("prompt", sid, work, prompt="later")))  # T-3 left: not asked again
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):  # this session's write asks, and has told it
            self.assertIn("Issue updates to write", run(*t, "set", "T-1", "status=dropped", "summary=not needed"))
        self.assertNotIn("Issue updates", said(hook("prompt", sid, work, prompt="then")))
        self.assertIn("Issue updates to write", said(hook("session-start", sid, work, source="compact")))
        self.assertNotIn("Issue updates", said(hook("prompt", sid, work, prompt="after")))  # the brief told them
        self.write(t, "T-1", "T-3")
        self.assertNotIn("Issue updates", said(hook("session-start", sid, work, source="startup")))

    def test_a_ui_shows_priority_and_what_a_refresh_waits_on(self):
        s, t = self.tracker()
        run(*t, "new", "T-4", "--title", "Least", "--priority", "4")
        run(*t, "issue", "T-1", "--priority", "1")
        run(*t, "issue", "T-3")

        def row(ident: str) -> dict:
            return next(x for x in data(s)["tickets"] if x["id"] == ident)
        self.assertEqual([row(i)["sort"]["priority"] for i in ("T-1", "T-2", "T-4")], [1, None, 4])
        self.assertEqual(row("T-1")["levels"]["priority"], "P1")
        run(*t, "set", "T-4", "size=5")
        self.assertEqual((row("T-4")["sort"]["size"], row("T-4")["levels"]["size"]), (5, "XL"))

        self.assertRegex(data(s)["issues"], r"^issues read \d\d:\d\d$")
        run(*t, "refresh")
        self.assertEqual(data(s)["issues"], "issues: asked; no session on this tracker to read them")
        sid = f"live{time.monotonic_ns()}"  # a Claude session on this machine, tied to the tracker by `tracker start`
        session.CLAUDE_SESSIONS.mkdir(parents=True, exist_ok=True)
        (session.CLAUDE_SESSIONS / f"{sid}.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": sid, "cwd": "/", "name": "live", "status": "idle",
             "statusUpdatedAt": time.time() * 1000}))
        session.save_session(sid, tracker=s, cwd=BASE)
        self.assertEqual(data(s)["issues"], "issues: asked; waiting for a session to read them")
        unlinked, _ = new_tracker("Plain")
        self.assertEqual(data(unlinked)["issues"], "")

    def test_a_tracked_session_records_how_a_ui_starts_the_cli(self):
        file = Path(BASE) / f"cli{time.monotonic_ns()}.json"
        with mock.patch.object(store, "CLI_FILE", file), \
                mock.patch.dict(os.environ, {"GH_TOKEN": "secret", "PATH": "/my/bin", "HOME": "/me",
                                             "CLAUDE_CONFIG_DIR": "/c", "TRACKER_SYNC": "60"}):
            hooks.record_cli()
            saved = json.loads(file.read_text())
            self.assertEqual((saved["package"], saved["python"]), (str(system.PACKAGE), system.PYTHON))
            self.assertNotIn("secret", file.read_text())
            self.assertIn("/my/bin", saved["argv"][-2])
            self.assertIn("'CLAUDE_CONFIG_DIR': '/c'", saved["argv"][-2])
            self.assertIn("'TRACKER_SYNC': '60'", saved["argv"][-2])
            before = file.stat().st_mtime_ns
            with mock.patch.dict(os.environ, {"PATH": "/another/session"}):
                hooks.record_cli()  # a UI watches this folder: a session's own PATH changes nothing
            self.assertEqual(file.stat().st_mtime_ns, before)
        # A host can start programs with no environment: the CLI still has its home, and what `gh` needs.
        code = saved["argv"][-2].replace("from tracker.cli import main; main()",
                                         "import os; print(os.environ['HOME'], os.environ['PATH'])")
        out = subprocess.run([saved["argv"][0], *saved["argv"][1:-2], code, saved["argv"][-1]], env={},
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.split(), ["/me", "/my/bin"])
        out = subprocess.run([saved["argv"][0], *saved["argv"][1:-2], code, saved["argv"][-1]], env={"HOME": "/other"},
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.split()[0], "/other")  # the real environment wins
        # A hook loads no module only a rare command or a UI needs
        code = (f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); import tracker.hooks; "
                "print(*sys.modules)")
        loaded = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                                check=True).stdout.split()
        self.assertEqual([x for x in ("tracker.viewmodel", "tracker.ui", "tracker.cli", "statistics", "tempfile",
                                      "shutil", "concurrent.futures") if x in loaded], [])


class Ui(unittest.TestCase):
    """`tracker open` shows a tracker in Herdr when Herdr is open, else in its own window; `tracker evidence` is the
    UI's link to a file."""

    def tracker(self) -> str:
        s, _ = new_tracker("Shown")
        run("--tracker", s, "new", "T-1", "--title", "One")
        return s

    def pr_tracker(self, repos: str = "a/x") -> tuple[str, tuple[str, str]]:
        """A tracker of `repos` whose S-1 is under way on branch f1."""
        s, t = new_tracker("Pulls", repos)
        run(*t, "new", "S-1", "--title", "One", "--branch", "f1", "--repo", repos.split(",")[0])
        run(*t, "set", "S-1", "status=in-progress")
        return s, t

    def synced(self, t: tuple[str, str], prs: list[dict]) -> None:
        with mock.patch.object(github, "gh", answers(prs)):
            run(*t, "sync")

    def test_the_view_data_lists_a_pr_once_with_each_ticket_that_names_it(self):
        s, t = self.pr_tracker()
        run(*t, "new", "S-2", "--title", "Two")
        run(*t, "set", "S-2", "status=in-progress")
        run(*t, "add", "S-2", "link", "PR: [Part one](https://github.com/A/X/pull/11)")
        self.synced(t, [pull(11, branch="f1", requested=["rev"])])
        [row] = data(s)["prs"]
        self.assertEqual({k: row[k] for k in ("key", "repo", "number", "url", "state", "tickets")},
                         {"key": "a/x#11", "repo": "a/x", "number": "11", "url": "https://github.com/a/x/pull/11",
                          "state": "open", "tickets": ["S-1", "S-2"]})

    def test_the_same_number_in_two_repos_is_two_prs(self):
        s, t = self.pr_tracker("a/x,b/y")
        run(*t, "new", "S-2", "--title", "Two", "--repo", "b/y")
        run(*t, "add", "S-2", "link", "PR: [Other](https://github.com/b/y/pull/5)")
        run(*t, "add", "S-1", "link", "PR: [Mine](https://github.com/a/x/pull/5)")
        rows = data(s)["prs"]
        self.assertEqual([(x["key"], x["tickets"], x["state"], x["move"]) for x in rows],
                         [("a/x#5", ["S-1"], "", None), ("b/y#5", ["S-2"], "", None)])  # linked, never read

    def test_an_open_prs_move_is_its_tickets(self):
        s, t = self.pr_tracker()
        self.synced(t, [pull(11, branch="f1", requested=["rev"])])
        got = data(s)
        self.assertEqual(got["prs"][0]["move"], got["records"]["S-1"]["move"])
        self.assertEqual(got["prs"][0]["move"]["text"], "rev: review requested")

    def test_a_merged_or_closed_pr_has_no_move_and_open_ones_come_first(self):
        s, t = self.pr_tracker()
        run(*t, "add", "S-1", "link", "PR: [Two](https://github.com/a/x/pull/12)")
        run(*t, "add", "S-1", "link", "PR: [Three](https://github.com/a/x/pull/13)")
        self.synced(t, [pull(11, branch="f1", state="MERGED", merged_at="2026-09-04T01:00:00Z"),
                        pull(12, branch="other"), pull(13, branch="other", state="CLOSED")])
        rows = data(s)["prs"]
        self.assertEqual([(x["number"], x["state"]) for x in rows], [("12", "open"), ("11", "merged"),
                                                                      ("13", "closed")])
        self.assertIsNotNone(rows[0]["move"])
        self.assertEqual([x["move"] for x in rows[1:]], [None, None])

    def test_an_open_pr_not_read_yet_has_no_move(self):
        s, t = self.pr_tracker()
        tr = model.tracker_at(s)
        tr.lookup("S-1").save({"pr": "11", "pr_state": "open"})  # an older format's one PR: its facts never read
        tr.readme.save({"schema": 3})
        run(*t, "list")  # migrates it
        self.assertEqual((model.tracker_at(s).lookup("S-1").prs, model.tracker_at(s).prs), ({"11": "open"}, {}))
        [row] = data(s)["prs"]
        self.assertEqual((row["state"], row["move"]), ("open", None))

    def test_a_pr_whose_tickets_are_not_under_way_has_no_move(self):
        s, t = self.pr_tracker()
        self.synced(t, [pull(11, branch="f1", requested=["rev"])])
        run(*t, "set", "S-1", "status=done")
        [row] = data(s)["prs"]
        self.assertEqual((row["state"], row["move"]), ("open", None))

    def test_a_prs_history_is_its_events_and_its_tickets_reviews(self):
        s, t = self.pr_tracker()
        run(*t, "new", "S-2", "--title", "Two")
        run(*t, "submit", "S-2", "--what", "plan")  # another ticket's: not this PR's
        run(*t, "submit", "S-1", "--what", "plan")
        rounds = [review_round(101, "rev", "APPROVED", "2026-09-02T00:00:00Z")]  # older on GitHub than the submission
        self.synced(t, [pull(11, branch="f1", rounds=rounds)])
        run(*t, "review", "S-1", "--what", "plan", "--verdict", "changes", "--finding", "Split it")
        run(*t, "review", "S-1", "--what", "plan", "--fixed", "1")
        run(*t, "step", "S-1", "Built it")  # the ticket's, not the PR's
        [row] = data(s)["prs"]
        self.assertEqual([x["kind"] for x in row["history"]],
                         ["submission", "pr.state", "pr.review", "review", "finding.fixed"])  # as logged
        self.assertEqual(row["history"][2]["text"], "PR #11 review by rev: approved")
        self.assertEqual(set(row["history"][0]), {"at", "kind", "text"})

    def test_a_prs_title_is_githubs_then_its_last_logged_then_its_links(self):
        s, t = self.pr_tracker()
        run(*t, "add", "S-1", "link", "PR: [Link text](https://github.com/a/x/pull/11)")
        run(*t, "add", "S-1", "link", "PR: [Elsewhere](https://github.com/b/y/pull/7) — the other half")
        self.synced(t, [pull(11, branch="f1", title="Old title")])
        self.synced(t, [pull(11, branch="f1", title="New title")])  # no new state: no new event
        titles = {x["key"]: x["title"] for x in data(s)["prs"]}
        self.assertEqual(titles, {"a/x#11": "New title", "b/y#7": "Elsewhere"})
        self.synced(t, [pull(11, branch="f1", title="Merged title", state="MERGED",
                             merged_at="2026-09-04T01:00:00Z")])
        self.assertEqual({x["key"]: x["title"] for x in data(s)["prs"]}["a/x#11"], "Merged title")

    def test_a_damaged_pr_number_is_no_pr(self):
        s, t = self.pr_tracker()
        self.synced(t, [pull(11, branch="f1")])
        path = store.HOME / s / "tickets" / "S-1.md"
        damaged = f"x12:open, ²:open, {'9' * 5000}:open, 011:open, 11:open"  # hand edits gone wrong
        path.write_text(path.read_text().replace("11:open", damaged))
        self.assertEqual([x["key"] for x in data(s)["prs"]], ["a/x#11"])

    def test_a_link_to_no_pr_number_is_no_pr(self):
        s, t = self.pr_tracker()
        for url in (f"https://github.com/a/x/pull/{'9' * 5000}", "https://github.com/a/x/pull/\u0663"):
            self.assertIn("`check`:", run(*t, "add", "S-1", "link", f"PR: {url}"))
        self.assertEqual(data(s)["prs"], [])
        run(*t, "check")

    def test_a_tracker_with_no_pr_lists_none(self):
        self.assertEqual(data(self.tracker())["prs"], [])

    def test_the_view_data_lists_open_items(self):
        s = self.tracker()
        run("--tracker", s, "set", "T-1", "status=in-progress")
        before = data(s)["now"]["items"]
        run("--tracker", s, "submit", "T-1", "--what", "plan")
        now = data(s)["now"]
        self.assertEqual(now["items"][0]["move"]["text"], "human: plan review")
        self.assertEqual([{**x, "move": None} for x in now["items"]], [{**x, "move": None} for x in before])
        (item,) = now["open"]
        self.assertEqual({k: item[k] for k in ("kind", "refs", "what", "stuck")},
                         {"kind": "submission", "refs": ["T-1"], "what": "T-1 plan ready for review", "stuck": False})
        self.assertEqual(item["move"], {"text": "human: plan review", "mine": False, "human": True, "quiet": False})
        self.assertGreaterEqual(item["age_s"], 0)

    def test_a_command_loads_no_download_code(self):
        code = (f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r}); import tracker.cli; "
                "print(sorted({'http.client', 'urllib.request', 'tarfile', 'zipfile', 'tempfile'} & set(sys.modules)))")
        got = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(got.stdout.strip(), "[]")

    def test_await_change_ends_on_a_change_or_at_its_limit(self):
        s = self.tracker()
        tr = model.tracker_at(s)
        session.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        watcher.state_path(s).parent.mkdir(parents=True, exist_ok=True)
        on = json.dumps({"tracker": s})
        for path, text in ((tr.root / "tickets/T-1.md", None), (session.SESSIONS_DIR / f"{slug()}.json", on),
                           (watcher.state_path(s), None)):
            with self.subTest(path=path.name), mock.patch.object(watcher, "CHANGE_WAIT_S", 30), \
                    mock.patch.object(watcher, "CHANGE_POLL_S", 0.01):
                old = path.read_text() if path.exists() else ""
                later = threading.Timer(0.1, lambda: path.write_text(text or f"{old}\n"))
                later.start()
                began = time.monotonic()
                watcher.await_change(tr)
                later.join()
                self.assertLess(time.monotonic() - began, 10)
        with mock.patch.object(watcher, "CHANGE_WAIT_S", 0.3), mock.patch.object(watcher, "CHANGE_POLL_S", 0.01):
            other = session.SESSIONS_DIR / f"{slug()}.json"
            later = threading.Timer(0.05, lambda: other.write_text(json.dumps({"tracker": f"{s}x"})))
            later.start()
            began = time.monotonic()
            run("await-change", s)  # another tracker's session changes nothing here: it ends at its limit
            later.join()
            self.assertGreaterEqual(time.monotonic() - began, 0.3)
        self.assertIn(f"no tracker '{s}x'", run("await-change", f"{s}x", code=2))

    def test_install_mod_gives_the_mod_all_it_loads_and_only_its_own_files(self):
        home = Path(tempfile.mkdtemp(dir=BASE))
        with mock.patch.dict(os.environ, {"HERDR_APP_HOME": str(home / "none")}):
            self.assertIn("no Herdr", run("install-mod", code=2))
        target = home / "mods/work-tracker"
        target.mkdir(parents=True)
        (target / "gone.js").write_text("an older version's")
        (target / "jsconfig.json").write_text("{}")  # Herdr's
        with mock.patch.dict(os.environ, {"HERDR_APP_HOME": str(home)}):
            self.assertIn(str(target), run("install-mod"))
        names = {x.name for x in target.iterdir()}
        loads, todo = {"mod.json", "icon.svg"}, ["main.js"]  # each module main.js imports, and those they import
        while todo:
            name = todo.pop()
            loads.add(name)
            self.assertTrue((target / name).is_file(), name)
            todo += [x for x in re.findall(r'from "\./([^"]+)"', (target / name).read_text()) if x not in loads]
        self.assertLessEqual(loads, names)
        for name in loads:
            self.assertEqual((target / name).read_bytes(), (ROOT / "ui" / name).read_bytes())
        # Not the window's (app.js; gpui-shell.json, which Herdr refuses), the tests, or what this version no longer has
        self.assertEqual(names - loads, {"jsconfig.json"})

    def test_a_new_plugin_version_updates_an_installed_mod_and_installs_none(self):
        home = Path(tempfile.mkdtemp(dir=BASE))
        target = home / "mods/work-tracker"
        view = target / "view.js"

        def older():
            store.write_json(store.CLI_FILE, {"package": "/an/older/version"})
        with mock.patch.dict(os.environ, {"HERDR_APP_HOME": str(home)}):
            older()
            hooks.record_version()
            self.assertFalse(target.exists())  # the user installs it
            run("install-mod")
            view.write_text("an older version's")
            hooks.record_version()  # the same version: the mod is not read
            self.assertEqual(view.read_text(), "an older version's")
            older()
            hooks.record_version()
            self.assertEqual(view.read_bytes(), (ROOT / "ui/view.js").read_bytes())

            edited = home / "edited.js"  # a file of the mod in a checkout, whose path stays from edit to edit
            edited.write_text("1")
            with mock.patch.object(store, "mod_files", lambda: [*system.mod_files(), edited]):
                hooks.record_version()
                view.write_text("an older edit's")
                hooks.record_version()  # no edit since: the mod is not read
                self.assertEqual(view.read_text(), "an older edit's")
                edited.write_text("22")
                hooks.record_version()
                self.assertEqual(view.read_bytes(), (ROOT / "ui/view.js").read_bytes())
            view.unlink()
            try:
                view.symlink_to(home / "mine.js")  # a link is the user's own
            except OSError:  # Windows, without the right to make one
                return
            older()
            hooks.record_version()
            self.assertTrue(view.is_symlink())

    def test_open_updates_an_installed_mod_and_offers_one(self):
        s = self.tracker()
        home = Path(tempfile.mkdtemp(dir=BASE))
        with mock.patch.dict(os.environ, {"HERDR_APP_HOME": str(home)}), \
                mock.patch.object(commands, "open_ui", lambda *a, **k: "gpui-shell"):
            self.assertIn("tracker install-mod", run("--tracker", s, "open"))
            self.assertNotIn("install-mod", run("--tracker", s, "open", "--window"))
            run("install-mod")
            (home / "mods/work-tracker/view.js").write_text("an older version's")
            self.assertEqual(run("--tracker", s, "open"), f"opened {s} in gpui-shell\n")
            self.assertEqual((home / "mods/work-tracker/view.js").read_bytes(), (ROOT / "ui/view.js").read_bytes())

    def test_a_pr_link_that_sync_has_not_read_is_not_no_pr(self):
        s, t = new_tracker("Shown", "acme/app")
        run(*t, "new", "T-1", "--title", "One", "--branch", "b")
        run(*t, "set", "T-1", "status=in-progress")

        def move():
            tracker = model.tracker_at(s)
            return moves.whose_move(tracker, tracker.lookup("T-1"))
        self.assertEqual((move().text(), move().quiet), ("you: no PR yet", True))
        run(*t, "add", "T-1", "link", "PR: [Not a PR page](https://example.com/7)")
        self.assertEqual(move().text(), "you: no PR yet")
        run(*t, "add", "T-1", "link", "PR: [Elsewhere](https://github.com/b/y/pull/30)")
        self.assertEqual((move().text(), move().quiet), ("you: PR in b/y", True))
        run(*t, "add", "T-1", "link", "PR: [Part two](https://github.com/acme/app/pull/8)")
        self.assertEqual((move().text(), move().quiet), ("you: PR not synced", True))
        path = model.tracker_at(s).lookup("T-1").path
        path.write_text(path.read_text().replace("status: in-progress", "status: in-progress\nprs: [8:closed]", 1))
        self.assertEqual(move().text(), "you: PR in b/y")  # the link here is read now

    def test_a_current_ticket_lists_each_of_its_prs_once(self):
        s, t = new_tracker("Shown", "acme/app")
        run(*t, "new", "T-1", "--title", "One", "--branch", "b")
        run(*t, "set", "T-1", "status=in-progress")
        tracker = model.tracker_at(s)
        record = tracker.lookup("T-1")
        self.assertEqual(viewmodel.pr_views(tracker, record), [])
        run(*t, "add", "T-1", "link", "PR: [Part two](https://github.com/acme/app/pull/8) — the rest")
        run(*t, "add", "T-1", "link", "PR: [Same as synced](https://github.com/acme/app/pull/7)")
        run(*t, "add", "T-1", "link", "PR: [Not a PR page](https://example.com/7)")
        path = model.tracker_at(s).lookup("T-1").path
        path.write_text(path.read_text().replace("status: in-progress", "status: in-progress\nprs: [7:open]", 1))
        tracker = model.tracker_at(s)
        self.assertEqual(viewmodel.pr_views(tracker, tracker.lookup("T-1")), [
            {"number": "7", "url": "https://github.com/acme/app/pull/7", "state": "open",
             "text": "[Same as synced](https://github.com/acme/app/pull/7)"},
            {"number": "8", "url": "https://github.com/acme/app/pull/8", "state": "",
             "text": "[Part two](https://github.com/acme/app/pull/8) — the rest"}])
        run(*t, "add", "T-1", "link", "PR: [Other case](https://github.com/Acme/App/pull/7)")
        tracker = model.tracker_at(s)
        self.assertEqual([x["number"] for x in viewmodel.pr_views(tracker, tracker.lookup("T-1"))], ["7", "8"])
        # In an opened ticket each PR has one row: not its link as well. A link to anything else stays a link.
        opened = viewmodel.record_view(tracker, tracker.lookup("T-1"))
        self.assertEqual([x["number"] for x in opened["prs"]], ["7", "8"])
        self.assertEqual([x["text"] for x in opened["links"]], ["[Not a PR page](https://example.com/7)"])

    def test_attached_finds_the_tracker_of_an_agents_process(self):
        s = self.tracker()
        session.CLAUDE_SESSIONS.mkdir(parents=True, exist_ok=True)

        def process(sid):
            pid = time.monotonic_ns()
            (session.CLAUDE_SESSIONS / f"{pid}.json").write_text(json.dumps(
                {"pid": pid, "sessionId": sid, "cwd": "/", "status": "idle"}))
            return pid

        def attached(pid, cwd=""):
            return json.loads(run("attached", "--pid", str(pid), "--cwd", cwd))["slug"]

        sid = f"linked{time.monotonic_ns()}"
        pid = process(sid)
        session.save_session(sid, tracker=s, cwd=BASE)
        self.assertEqual(attached(pid), s)
        self.assertIsNone(attached(process(f"unlinked{time.monotonic_ns()}")))

        # A host with no process file: its hooks' live sessions in that folder, while they agree on one tracker.
        now, folder = time.time(), f"/nowhere/{time.monotonic_ns()}"
        activity = {"status": "busy", "since": now, "at": now}
        none = 2 ** 40 + time.monotonic_ns() % 1000
        self.assertIsNone(attached(none, folder))
        session.save_session(f"other{time.monotonic_ns()}", tracker=s, cwd=folder, activity=activity)
        self.assertEqual(attached(none, folder), s)
        session.save_session(f"other{time.monotonic_ns()}", tracker=self.tracker(), cwd=folder, activity=activity)
        self.assertIsNone(attached(none, folder))

    @unittest.skipIf(system.WINDOWS, "no ps")
    def test_attached_finds_the_agent_started_from_a_shell(self):
        # A tab opened as a shell, with `claude` run in it: the host names the shell, which has no process file.
        s = self.tracker()
        shell = subprocess.Popen(["sh", "-c", "sleep 60 & echo $!; sleep 60 & echo $!; wait"], stdout=subprocess.PIPE,
                                 text=True)
        under = [int(shell.stdout.readline()), int(shell.stdout.readline())]
        self.addCleanup(shell.stdout.close)
        self.addCleanup(shell.wait)
        for pid in (*under, shell.pid):
            self.addCleanup(os.kill, pid, 9)
        session.CLAUDE_SESSIONS.mkdir(parents=True, exist_ok=True)

        def process(pid, sid):
            path = session.CLAUDE_SESSIONS / f"{pid}.json"
            path.write_text(json.dumps({"pid": pid, "sessionId": sid, "cwd": "/", "status": "idle"}))
            self.addCleanup(path.unlink, missing_ok=True)
            return path

        def attached(pid):
            return json.loads(run("attached", "--pid", str(pid)))["slug"]

        sid = f"linked{time.monotonic_ns()}"
        session.save_session(sid, tracker=s, cwd=BASE)
        process(under[0], sid)
        self.assertEqual(attached(shell.pid), s)
        self.assertEqual(attached(under[0]), s)
        # A process with its own file is that session, linked or not
        own = process(shell.pid, f"unlinked{time.monotonic_ns()}")
        self.assertIsNone(attached(shell.pid))
        own.unlink()
        # Two agents under the shell: no telling which one is in front
        process(under[1], f"unlinked{time.monotonic_ns()}")
        self.assertIsNone(attached(shell.pid))

    def test_install_window_unpacks_the_pinned_archive_once(self):
        tar = io.BytesIO()
        with tarfile.open(fileobj=tar, mode="w:gz") as files:
            entry = tarfile.TarInfo("gpui-shell-t/gpui-shell")
            entry.size = len(b"built")
            files.addfile(entry, io.BytesIO(b"built"))
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w") as files:
            files.writestr("gpui-shell-t/gpui-shell.exe", b"built")
        cases = (("Linux", "x86_64", "gpui-shell-t.tar.gz", tar.getvalue(), False, "gpui-shell"),
                 ("Windows", "AMD64", "gpui-shell-t.zip", packed.getvalue(), True, "gpui-shell.exe"))
        for system_name, machine, name, archive, windows, binary in cases:
            with self.subTest(name=name), mock.patch.object(ui, "HOME", Path(tempfile.mkdtemp(dir=BASE))), \
                    mock.patch.object(ui, "WINDOWS", windows), \
                    mock.patch("platform.system", lambda: system_name), \
                    mock.patch("platform.machine", lambda: machine), \
                    mock.patch.object(ui, "SHELL_ARCHIVES", {(system_name, machine.lower()): (
                        name, hashlib.sha256(archive).hexdigest())}), \
                    mock.patch.object(ui, "fetch", mock.Mock(return_value=archive)) as fetch:
                self.assertIn(binary, run("install-window"))
                self.assertEqual(ui.own_shell().name, binary)
                self.assertEqual(ui.own_shell().read_bytes(), b"built")
                run("install-window")
                fetch.assert_called_once()
                self.assertIn(f"/v{ui.GPUI_SHELL_VERSION}/{name}", fetch.call_args[0][0])

    def test_install_window_installs_nothing_it_cannot_trust(self):
        key = ("Linux", "x86_64")
        with mock.patch.object(ui, "HOME", Path(tempfile.mkdtemp(dir=BASE))), mock.patch.object(ui, "WINDOWS", False), \
                mock.patch("platform.system", lambda: key[0]), \
                mock.patch("platform.machine", lambda: key[1]):
            with mock.patch.object(ui, "fetch", mock.Mock(return_value=b"changed")):
                self.assertIn("not the file", run("install-window", code=2))
            with mock.patch.object(ui, "fetch", mock.Mock(side_effect=OSError("offline"))):
                self.assertIn("offline", run("install-window", code=2))
            with mock.patch.object(ui, "SHELL_ARCHIVES", {}), mock.patch.object(ui, "fetch") as fetch:
                self.assertIn("no build", run("install-window", code=2))
                fetch.assert_not_called()
            self.assertFalse(ui.own_shell().parent.exists())

    def test_open_window_skips_herdr_and_prefers_the_installed_shell(self):
        s = self.tracker()
        herdr = mock.Mock(return_value=True)
        with mock.patch.object(ui, "HOME", Path(tempfile.mkdtemp(dir=BASE))), mock.patch.object(ui, "WINDOWS", False), \
                mock.patch.object(ui, "open_in_herdr", herdr), mock.patch.object(ui, "WINDOW_START_S", 30), \
                mock.patch.object(ui.shutil, "which", lambda name: "/bin/false"):
            shell = ui.own_shell()
            shell.parent.mkdir(parents=True)
            shell.write_text("#!/bin/sh\necho 'installed shell' >&2\nexit 3\n")
            shell.chmod(0o755)
            self.assertIn("installed shell", run("--tracker", s, "open", "--window", code=2))
            herdr.assert_not_called()
            self.assertIn("in Herdr", run("--tracker", s, "open"))

    def test_open_says_why_the_window_refused(self):
        s = self.tracker()
        folder = Path(tempfile.mkdtemp(dir=BASE))
        shell = folder / "gpui-shell"
        shell.write_text("#!/bin/sh\necho 'needs shell 9' >&2\nexit 3\n")
        shell.chmod(0o755)
        with mock.patch.object(ui, "open_in_herdr", lambda *a: False), mock.patch.object(ui, "WINDOW_START_S", 30), \
                mock.patch.object(ui.shutil, "which", lambda name: str(shell)):
            said = run("--tracker", s, "open", code=2)  # it ends the wait: only a machine under load nears the limit
            self.assertIn("exit 3", said)
            self.assertIn("needs shell 9", said)
            shell.write_text("#!/bin/sh\nexec sleep 2\n")  # still up when `open` stops waiting: the window runs
            with mock.patch.object(ui, "WINDOW_START_S", 0.2):
                self.assertIn("in gpui-shell", run("--tracker", s, "open"))

    def test_commands_only_a_ui_runs_are_not_listed(self):
        parser = cli.build_parser()[0]
        listed = parser.format_help().split("positional arguments")[-1].split("options:")[0]
        for name in ("data", "attached", "evidence", "await-change", "refresh"):
            self.assertNotRegex(listed, rf"(?m)^\s+{name}\s")
        self.assertRegex(listed, r"(?m)^\s+open\s")

    def test_every_status_has_a_meaning_in_the_ui(self):
        table = re.search(r"export const MEANINGS = \{(.*?)\n\};", (ROOT / "ui/model.js").read_text(), re.S).group(1)
        for kind, stages in schema.LIFECYCLES.items():
            for status in stages:
                self.assertIn(f'"{status}"', table, kind)

    def test_the_ui_reads_this_schema(self):
        self.assertIn(f"const SCHEMA = {viewmodel.VIEW_SCHEMA};", (ROOT / "ui/view.js").read_text())
        manifest = json.loads((ROOT / "ui/gpui-shell.json").read_text())
        self.assertEqual((manifest["shell-version"], manifest["entry"]), (ui.GPUI_SHELL_VERSION, "app.js"))
        self.assertEqual(manifest["capabilities"]["process"]["env"], ["TRACKER_OPEN", "TRACKER_CLI"])
        self.assertEqual(manifest["capabilities"]["fs"], {"execute": "*"})  # it reads a tracker only through the CLI

    def test_open_uses_the_tab_it_has_else_a_new_one(self):
        s = self.tracker()
        calls = []

        def herdr(*args):
            calls.append(args)
            if args == ("ls",):
                return {"spaces": [{"tabs": [{"id": "tb1", "name": f"tracker: {s}"}] * have}]}
            return {"ok": True}
        home = Path(tempfile.mkdtemp(dir=BASE))
        (home / "mods/work-tracker").mkdir(parents=True)
        with mock.patch.object(ui, "herdr", herdr), mock.patch.object(ui, "herdr_home", lambda: home):
            have = 0
            self.assertEqual(run("--tracker", s, "open", "T-1"), f"opened {s} T-1 in Herdr\n")
            self.assertEqual(calls[-1][:2], ("do", "tab.new"))
            self.assertEqual(json.loads(calls[-1][2]),
                             {"name": f"tracker: {s}", "mod": "work-tracker",
                              "props": {"slug": s, "open": "T-1", "trackers": str(store.HOME)}})
            have = 1
            calls.clear()
            run("--tracker", s, "open", "T-1")
            self.assertEqual(calls[1:], [("do", "tab.select", json.dumps({"id": "tb1"})),
                                         ("do", f"work-tracker.open:{s}/T-1")])

    def test_open_falls_back_to_the_window_and_says_when_it_has_neither(self):
        s = self.tracker()
        with mock.patch.object(ui, "herdr", lambda *args: None):
            with mock.patch.object(ui.shutil, "which", lambda name: None):
                self.assertIn("needs Herdr", run("--tracker", s, "open", code=2))
            none, home = mock.Mock(), Path(tempfile.mkdtemp(dir=BASE))  # no mod to show it: Herdr is not asked
            with mock.patch.object(ui, "herdr", none), mock.patch.object(ui, "herdr_home", lambda: home), \
                    mock.patch.object(ui.shutil, "which", lambda name: None):
                self.assertIn("tracker install-mod", run("--tracker", s, "open", code=2))
            none.assert_not_called()
            with mock.patch.object(ui.shutil, "which", lambda name: "/bin/gpui-shell"), \
                    mock.patch.object(ui.subprocess, "Popen") as popen:
                popen.return_value.wait.return_value = 0
                self.assertEqual(run("--tracker", s, "open", "T-1"), f"opened {s} T-1 in gpui-shell\n")
            self.assertEqual(popen.call_args.args[0], ["/bin/gpui-shell", "run", "--dir", str(ROOT / "ui")])
            self.assertEqual(popen.call_args.kwargs["env"]["TRACKER_OPEN"], f"{s}/T-1")

    def test_refresh_and_sync_are_for_trackers_at_work(self):
        s = self.tracker()
        run("archive", s)
        run("--tracker", s, "refresh", code=2)
        self.assertTrue(data(s)["archived"])  # its page still shows

    def test_evidence_opens_only_the_trackers_files_and_runs_no_code(self):
        s = self.tracker()
        t = ("--tracker", s)
        folder = store.HOME / s / "evidence"
        folder.mkdir()
        (folder / "run.txt").write_text("hi")
        (folder / "page.html").write_text("<script>")
        (store.HOME / s / "secret.txt").write_text("no")
        with mock.patch.object(commands, "open_with_system") as opened:
            run(*t, "evidence", "run.txt")
            self.assertEqual(opened.call_args.kwargs, {"reveal": False})
            run(*t, "evidence", "page.html")
            self.assertEqual(opened.call_args.kwargs, {"reveal": True})  # only shown in its folder
            opened.reset_mock()
            run(*t, "evidence", "../secret.txt", code=2)
            run(*t, "evidence", "none.txt", code=2)
            with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
                run(*t, "evidence", "run.txt", code=4)  # the user's click, not the agent's
            opened.assert_not_called()
            run("archive", s)
            run(*t, "evidence", "run.txt")  # an archived tracker still opens in a UI, its evidence too
            opened.assert_called_once()
            run(*t, "index", code=2)  # all else stays away from it


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
        rec = model.tracker_at(s).lookup(ident)
        rec.save({"status": status, **({"started_at": started} if started else {}),
                  **({"prs": ["9:merged"], "merged_at": merged} if merged else {})})

    def test_set_records_the_first_start(self):
        s, t = new_tracker("Timed")
        run(*t, "new", "S-1", "--title", "Starts", "--branch", "f1")
        out = run(*t, "set", "S-1", "status=in-progress")
        self.assertNotIn("started_at", out)
        first = model.tracker_at(s).lookup("S-1").get("started_at")
        self.assertLessEqual(abs(schema.utc_seconds(first) - time.time()), 5)
        model.tracker_at(s).lookup("S-1").save({"started_at": "2026-09-01T00:00:00Z"})
        run(*t, "set", "S-1", "status=todo")
        run(*t, "set", "S-1", "status=in-progress")  # started again: the first start stays
        self.assertEqual(model.tracker_at(s).lookup("S-1").get("started_at"), "2026-09-01T00:00:00Z")
        self.assertIn("the tracker writes it", run(*t, "set", "S-1", "started_at=2026-09-02T00:00:00Z", code=2))
        # In progress with no start, as a ticket started before the tracker recorded starts: set in progress again, it
        # gets none rather than now
        run(*t, "new", "S-2", "--title", "Started earlier", "--branch", "f2")
        path = store.HOME / s / "tickets" / "S-2.md"
        path.write_text(re.sub(r"^status: .*", "status: in-progress", path.read_text(), flags=re.M))
        run(*t, "set", "S-2", "status=in-progress")
        self.assertEqual(model.tracker_at(s).lookup("S-2").get("started_at"), "")

    def test_the_span_lines(self):
        s, _ = new_tracker("Timed")
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
        tr = model.tracker_at(s)
        h, d = 3600, 86400
        self.assertEqual({t.id: (model.span(t, "wait"), model.span(t, "cycle")) for t in tr.tickets},
                         {"S-1": (2 * h, 6 * h), "S-2": (d, 2 * d), "S-3": (600, 12 * h), "S-4": (None, 1800),
                          "S-5": (28 * d, None), "S-6": (d, None), "S-7": (None, 2 * d), "S-8": (None, None),
                          "S-9": (9 * d, None)})
        now = schema.utc_seconds("2026-10-06T00:00:00Z")
        self.assertEqual(views.span_lines(tr, now), [
            "Wait time (issue created → started): median 24 h over 6 · fastest 10 min (S-3) · last 7 days: median "
            "14.0 d over 2",
            "Cycle time (started → PR merged): median 12 h over 5 · fastest 30 min (S-4) · last 7 days: median 6 h "
            "over 2"])
        self.assertEqual(views.span_lines(tr, now + 30 * d), [
            "Wait time (issue created → started): median 24 h over 6 · fastest 10 min (S-3)",
            "Cycle time (started → PR merged): median 12 h over 5 · fastest 30 min (S-4)"])
        empty, _ = new_tracker("Untimed")
        self.assertEqual(views.span_lines(model.tracker_at(empty), now), [])
        self.assertEqual([views.duration(x) for x in (59, 60, 3599, 3600, 47 * 3600 + 3599, 48 * 3600, 100 * 3600)],
                         ["1 min", "1 min", "59 min", "1 h", "47 h", "2.0 d", "4.2 d"])

    def test_a_ui_and_the_index_show_the_spans(self):
        s, _ = new_tracker("Timed")
        self.ticket(s, "S-1", "2026-09-01T00:00:00Z", "2026-09-01T02:00:00Z", "2026-09-01T08:00:00Z")
        self.ticket(s, "S-2", "2026-09-01T00:00:00Z", status="todo")
        tr = model.tracker_at(s)
        got = data(s)["records"]

        # The opened ticket's first line gives its times, those it has, then its summary.
        self.assertEqual(got["S-1"]["time"], [{"name": "wait", "text": "2 h"},
                                              {"name": "cycle", "text": "6 h"}])
        self.assertEqual(got["S-2"]["time"], [])
        run("--tracker", s, "set", "S-1", "summary=Shipped the API")
        self.assertEqual(data(s)["records"]["S-1"]["line"], {"label": "summary", "text": "Shipped the API"})
        lines = views.span_lines(tr)
        self.assertEqual(len(lines), 2)
        self.assertEqual(data(s)["spans"], lines)
        self.assertEqual(run("--tracker", s, "index").splitlines()[1:3], lines)
        bare, _ = new_tracker("Untimed")
        self.assertEqual(data(bare)["spans"], [])
        self.assertNotIn("Cycle time", run("--tracker", bare, "index"))


class Watch(unittest.TestCase):
    """`tracker watch`: what it reports, that a restart misses nothing and repeats nothing, and that only the user
    starts it."""

    def setUp(self):
        self.s, self.t = new_tracker("Watched")
        self.work = repo("feat/W-1")
        for i in (1, 2, 3):
            run(*self.t, "new", f"W-{i}", "--title", f"Part {i}", *(["--depends", "W-1"] if i == 3 else []))
        self.root = store.HOME / self.s
        at_once = mock.patch.object(watcher, "SETTLE_S", 0)  # a batch at once, not after the change settles
        at_once.start()
        self.addCleanup(at_once.stop)

    def watcher(self, sid: str = "") -> watcher.Watcher:
        return watcher.Watcher(model.Tracker(self.root), sid)

    def test_reports_what_changed(self):
        w, now = self.watcher(), time.time()
        first = w.claim(now)
        self.assertIn(f"watching {self.s}: 0 under way, 0 your move, 0 agent(s), 0 open decision(s)", first[0])
        self.assertIn("can start: W-1, W-2", first[-1])
        self.assertEqual(w.poll(now), [])

        run(*self.t, "set", "W-1", "status=in-progress", cwd=self.work)
        run(*self.t, "step", "W-1", "Built the parser", "--next", "Test it", cwd=self.work)
        with store.locked():
            model.append_event(model.Tracker(self.root), "commits", "Commits on feat/W-1: a1 one; b2 two; and 3 more",
                               ["W-1"], branch="feat/W-1", count=5)
        run(*self.t, "decide", "Pick a store", "--refs", "W-2")
        lines = w.poll(now)
        w1 = next(x for x in lines if " W-1: " in x)
        for fact in ("todo → in-progress", "move: you — no PR yet", "Built the parser", "next: Test it", "+5 commits"):
            self.assertIn(fact, w1)
        self.assertNotIn("!", "".join(x for x in lines if " D-01: " not in x))  # the agent's own work
        self.assertIn("W-3: can start now: stack on feat/W-1", "".join(lines))
        self.assertIn("! D-01: Opened D-01 Pick a store", "".join(lines))  # no owner, no coordinator: a human's

        # A review comes back: the move is the user's.
        tr = model.Tracker(self.root)
        tr.lookup("W-1").save({"prs": ["12:open"]})
        tr.save_state({**tr.raw_state(), "prs": {"#12": {"review": "changes", "changes": ["rev"]}}})
        lines = w.poll(now)
        self.assertRegex(lines[0], r"^\d\d:\d\d ! W-1: in-progress → in-review · move: you — changes requested by rev$")
        self.assertEqual(w.poll(now), [])  # said once
        ticket(self.root, "W-9", depends_on=["W-99"])
        self.assertIn("! check: ", "".join(w.poll(now)))

    def test_reads_git_again_at_each_look(self):
        """`tracker watch` is one long command: an agent's new branch shows at the next look."""
        w, now = self.watcher(), time.time()
        w.claim(now)
        self.assertEqual(git.branch_of(self.work), "feat/W-1")
        subprocess.run(["git", "-C", str(self.work), "checkout", "-qb", "feat/W-2"], check=True)
        self.assertEqual(git.branch_of(self.work), "feat/W-1")  # one look keeps what it read
        w.poll(now)
        self.assertEqual(git.branch_of(self.work), "feat/W-2")

    def test_a_watch_from_before_the_upgrade_reports_nothing_old(self):
        w, now = self.watcher(), time.time()
        w.claim(now)
        w.release(now)
        path = watcher.state_path(self.s)
        st = json.loads(path.read_text())
        st.pop("seq", None)
        path.write_text(json.dumps({**st, "offset": 0}))  # a byte offset into log.md, as an older version kept
        self.assertEqual(len(model.read_events(model.Tracker(self.root))), 3)
        again = self.watcher()
        self.assertEqual(again.claim(now), [])
        self.assertNotIn("offset", again.st)
        with store.locked():
            model.append_event(model.Tracker(self.root), "note", "A note", ["W-2"])
        lines = again.poll(now)
        self.assertEqual(len(lines), 1)
        self.assertIn("W-2: A note", lines[0])

    def test_lines_that_are_no_event_are_skipped_by_every_reader_and_reported_by_check(self):
        w, now = self.watcher(), time.time()
        w.claim(now)
        file = self.root / schema.EVENTS_FILE
        n = len(file.read_bytes().splitlines())
        odd = [{"seq": n + 1}, {"seq": n + 2, "kind": "nope", "refs": [], "text": "x", "at": "2026-01-01T00:00:00Z"},
               {"seq": n + 3, "kind": "note"}, {"seq": n + 4, "kind": "note", "refs": [], "text": "x", "at": "x"}]
        with file.open("a") as f:
            f.write("".join(json.dumps(e) + "\n" for e in odd))
        with store.locked():
            model.append_event(model.Tracker(self.root), "note", "After the odd lines", ["W-2"])
        self.assertIn("After the odd lines", run(*self.t, "history"))
        self.assertIn("After the odd lines", json.dumps(data(self.s)))
        self.assertIn("W-2: After the odd lines", "".join(w.poll(now)))
        said = run(*self.t, "check", code=1)
        for k in range(1, 5):
            self.assertIn(f"{schema.EVENTS_FILE}:{n + k}: not an event", said)

    def test_a_watch_sees_new_events_after_the_event_file_got_shorter(self):
        w, now = self.watcher(), time.time()
        w.claim(now)
        file = self.root / schema.EVENTS_FILE
        backup = file.read_bytes()
        for text in ("One", "Two"):
            with store.locked():
                model.append_event(model.Tracker(self.root), "note", text, ["W-2"])
        w.poll(now)
        file.write_bytes(backup)  # restored from a backup: the watch has seen past its end
        self.assertEqual(w.poll(now), [])
        with store.locked():
            model.append_event(model.Tracker(self.root), "note", "After the restore", ["W-2"])
        self.assertIn("W-2: After the restore", "".join(w.poll(now)))

    def test_the_watch_counts_commits_from_the_event(self):
        commits = {"kind": "commits", "refs": ["W-1"], "text": "Commits on feat/W-1: a1 one", "count": 11}
        self.assertEqual(watcher.log_events([commits]), [watcher.Event("log", "W-1", False, "+11 commits")])

    def stuck_lines(self, lines: list[str]) -> list[str]:
        return [x for x in lines if "no session is on" in x or "has not reviewed" in x or "has waited" in x]

    def submitted(self, ident: str = "W-1") -> None:
        """A coordinator watches, and `ident`'s plan is submitted: with no session on W-1, under way, it is stuck at
        once."""
        self.coordinator()
        run(*self.t, "set", "W-1", "status=in-progress")
        run(*self.t, "submit", ident, "--what", "plan")

    def test_a_stuck_item_is_said_once(self):
        w, now = self.watcher(), time.time()
        w.claim(now)
        self.submitted()
        first = self.stuck_lines(w.poll(now))
        self.assertEqual(len(first), 1)
        self.assertRegex(first[0], r"^\d\d:\d\d ! stuck W-1: W-1 plan ready for review, and no session is on W-1$")
        self.assertEqual(self.stuck_lines(w.poll(now + 1)), [])
        self.assertEqual(self.stuck_lines(w.poll(now + 7200)), [])  # later, past its deadline too: said once

    def test_an_item_that_comes_back_is_said_again(self):
        w, now = self.watcher(), time.time()
        w.claim(now)
        self.submitted()
        self.assertEqual(len(self.stuck_lines(w.poll(now))), 1)
        run(*self.t, "review", "W-1", "--what", "plan", "--verdict", "pass")
        self.assertEqual(self.stuck_lines(w.poll(now)), [])
        run(*self.t, "submit", "W-1", "--what", "plan")
        self.assertEqual(len(self.stuck_lines(w.poll(now))), 1)

    def test_the_watch_writes_nothing_for_stuck(self):
        w, now = self.watcher(), time.time()
        w.claim(now)
        self.submitted()
        before = tree(self.root)
        self.assertTrue(self.stuck_lines(w.poll(now)))
        w.poll(now + 7200)
        self.assertEqual(tree(self.root), before)
        self.assertIn("stuck", watcher.load_state(self.s))  # what it said: in the watch's own state

    def test_a_wait_the_agent_line_covers_is_not_said_twice(self):
        sid, now = f"asker{time.monotonic_ns()}", time.time()
        self.addCleanup(session.drop_session, sid)
        session.save_session(sid, tracker=self.s, cwd=str(self.work),
                             activity={"status": "waiting", "since": now - 10, "at": now})
        w = self.watcher()
        w.claim(now)
        with store.locked(), mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            model.append_event(model.Tracker(self.root), "human.wait", "Waiting on a human: Bash", [], asker="",
                               why="Bash")
        self.assertTrue(any(i.stuck for i in session.items_of(model.Tracker(self.root), now + 40)))
        lines = w.poll(now + 40)
        self.assertIn("asks for a permission or an answer", "".join(lines))
        self.assertEqual(self.stuck_lines(lines), [])
        self.assertEqual(self.stuck_lines(w.poll(now + 41)), [])

    def test_a_watch_that_leaves_agent_lines_out_says_a_stuck_wait(self):
        sid, now = f"asker{time.monotonic_ns()}", time.time()
        self.addCleanup(session.drop_session, sid)
        session.save_session(sid, tracker=self.s, cwd=str(self.work),
                             activity={"status": "waiting", "since": now - 10, "at": now})
        w = watcher.Watcher(model.Tracker(self.root), only=frozenset({"stuck"}))  # `--only stuck`: no agent line
        w.claim(now)
        with store.locked(), mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            model.append_event(model.Tracker(self.root), "human.wait", "Waiting on a human: Bash", [], asker="",
                               why="Bash")
        lines = w.poll(now + 40)
        self.assertEqual(len(self.stuck_lines(lines)), 1, lines)
        self.assertEqual(w.poll(now + 41), [])  # said once

    def test_a_damaged_time_in_its_own_state_stops_no_watch(self):
        w, now = self.watcher(), time.time()
        w.claim(now)
        w.release(now)
        path = watcher.state_path(self.s, "")
        store.write_json(path, {**json.loads(path.read_text()), "at": "x"})
        self.assertTrue(self.watcher().claim(now + 1))  # as a new watch: its first lines

    def test_a_deadline_passing_is_seen_without_a_read(self):
        w, now = self.watcher(), time.time()
        w.claim(now)
        self.coordinator()
        run(*self.t, "submit", "W-2", "--what", "plan")  # W-2 has not started: not stuck at once
        self.assertEqual(self.stuck_lines(w.poll(now)), [])
        late = now + schema.DEADLINE_S["submission"] + 1
        with mock.patch.object(model, "event_lines", side_effect=AssertionError("the event file was read")):
            lines = self.stuck_lines(w.poll(late))
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^\d\d:\d\d ! stuck W-2: coordinator has not reviewed W-2's plan for 30 min$")

    def coordinator_claims(self, prior: dict | None = None) -> tuple[list[str], str]:
        """A coordinator's watch claims the tracker, with `prior` as its saved state, when W-1 (under way, a worker
        session on it) has had its plan submitted for two hours. Its first lines, and its session id."""
        sid, now = f"co{time.monotonic_ns()}", time.time()
        worker = f"wk{time.monotonic_ns()}"
        session.save_session(worker, tracker=self.s, focus=["W-1"], cwd=BASE,
                             activity={"status": "busy", "since": now, "at": now})
        self.addCleanup(session.drop_session, worker)
        self.addCleanup(watcher.set_grant, sid, False)
        run(*self.t, "set", "W-1", "status=in-progress")
        two_hours = time.strftime(schema.TIME_FORMAT, time.gmtime(now - 7200))
        with store.locked(), mock.patch.object(model, "utc_now", return_value=two_hours):
            model.append_event(model.Tracker(self.root), "submission", "W-1 plan ready for review", ["W-1"],
                               what="plan")
        if prior is not None:
            watcher.set_grant(sid, True)
            watcher.state_path(self.s, sid).parent.mkdir(parents=True, exist_ok=True)
            store.write_json(watcher.state_path(self.s, sid), {"who": "session co", "sid": sid, "pid": 0, **prior})
        lines = watcher.Watcher(model.Tracker(self.root), sid, role="coordinator").claim(now)
        return lines, sid

    def test_a_coordinators_first_lines_count_from_its_start(self):
        lines, _ = self.coordinator_claims()
        w1 = next(x for x in lines if " W-1 " in x)
        self.assertRegex(w1, r"^\d\d:\d\d W-1 in-progress · move: coordinator — plan review")  # not stuck: no `!`
        self.assertFalse(any(i.stuck for i in session.items_of(model.Tracker(self.root))))

    def test_a_coordinator_that_comes_back_counts_from_its_return(self):
        now = time.time()
        prior = {"role": "coordinator", "started": now - 3 * 3600, "ended": now - 2.5 * 3600, "at": now - 2.5 * 3600,
                 "snapshot": {"tickets": {}, "errors": []}, "seq": 0}  # it stopped before the plan came
        lines, _ = self.coordinator_claims(prior)
        self.assertFalse([x for x in lines if " ! " in x or "stuck W-" in x], lines)
        self.assertFalse(any(i.stuck for i in session.items_of(model.Tracker(self.root))))

    def test_a_watch_that_takes_the_role_counts_from_then(self):
        now = time.time()
        prior = {"started": now - 3 * 3600, "ended": now - 60, "at": now - 60,  # a plain watch, re-run a minute ago
                 "snapshot": {"tickets": {}, "errors": []}, "seq": 0}
        lines, _ = self.coordinator_claims(prior)
        self.assertFalse([x for x in lines if " ! " in x or "stuck W-" in x], lines)
        self.assertFalse(any(i.stuck for i in session.items_of(model.Tracker(self.root))))

    def test_a_stuck_move_is_said_by_its_stuck_line_alone(self):
        tr = model.Tracker(self.root)
        self.coordinator()
        run(*self.t, "set", "W-1", "status=in-progress")
        run(*self.t, "submit", "W-1", "--what", "plan")  # no session on W-1: stuck at once
        facts = session.ticket_facts(model.Tracker(self.root), tr.lookup("W-1"))
        self.assertEqual((facts["move"], facts["stuck_from"]),
                         ("human — plan review, stuck with coordinator", "coordinator — plan review"))
        before = {**facts, "move": "coordinator — plan review", "stuck_from": "", "human": False}
        new = {"tickets": {"W-1": {**facts, "start": ""}}, "errors": []}
        self.assertEqual(watcher.ticket_events({"tickets": {"W-1": {**before, "start": ""}}}, new), [])
        tr = model.Tracker(self.root)  # with several open PRs, the move names the PR, before it turned stuck too
        tr.lookup("W-1").save({"prs": ["12:open", "13:open"]})
        tr.save_state({**tr.raw_state(), "prs": {"#12": {}, "#13": {}}})
        facts = session.ticket_facts(model.Tracker(self.root), model.Tracker(self.root).lookup("W-1"))
        self.assertEqual((facts["move"], facts["stuck_from"]),
                         ("human — #12 plan review, stuck with coordinator", "coordinator — #12 plan review"))

    def test_a_ticket_whose_move_becomes_a_stuck_item_is_said(self):
        self.coordinator()
        w, now = self.watcher(), time.time()
        w.claim(now)
        run(*self.t, "set", "W-1", "status=in-progress")
        run(*self.t, "decide", "Pick a store", "--blocks", "W-1", "W-2")  # no session on W-1: stuck at once
        self.assertIn("! stuck D-01: ", "".join(w.poll(now)))
        run(*self.t, "set", "W-2", "status=in-progress")  # W-2 now waits on that stuck question too: news for W-2
        w2 = next(x for x in w.poll(now) if " W-2: " in x)
        self.assertRegex(w2, r"^\d\d:\d\d ! W-2: .*move: human — D-01 open, stuck with coordinator")

    def test_a_watchs_first_lines_say_a_stuck_item_once(self):
        self.coordinator()
        run(*self.t, "set", "W-1", "status=in-progress")
        run(*self.t, "submit", "W-1", "--what", "plan")  # no session on W-1: stuck at once
        w, now = self.watcher(), time.time()
        first = w.claim(now)
        self.assertEqual(len(self.stuck_lines(first)), 1)
        self.assertEqual(self.stuck_lines(w.poll(now)), [])

    def test_an_item_stuck_again_is_said_again(self):
        item = moves.Item("question", "question:D-01", ["W-1"], 0, "coordinator has not answered D-01 for 31 min",
                          moves.Move("human", "D-01 open", 5, human=True), stuck=True)
        looks = [[item], [dataclasses.replace(item, stuck=False)], [item]]  # passed on, then left again
        w = self.watcher()
        with mock.patch.object(watcher, "items_of", side_effect=looks):
            said = [len(w.stuck(model.Tracker(self.root), time.time())) for _ in looks]
        self.assertEqual(said, [1, 0, 1])

    def opened(self, *flags: str) -> list[dict]:
        """D-01 opened with `flags`: its events, as the watch reads them."""
        run(*self.t, "decide", "Pick a store", *flags)
        return [e for e in model.read_events(model.Tracker(self.root)) if e.get("decision") == "D-01"]

    def coordinator(self, sid: str = "qb") -> None:
        """A coordinator's watch of the tracker, running now."""
        watcher.set_grant(sid, True)
        watcher.state_path(self.s, sid).parent.mkdir(parents=True, exist_ok=True)
        store.write_json(watcher.state_path(self.s, sid), {"who": "session qb", "sid": sid, "pid": os.getpid(),
                                                            "role": "coordinator"})

    def test_a_question_for_a_human_is_urgent(self):
        events = self.opened()
        self.assertEqual([e.urgent for e in watcher.log_events(events, model.Tracker(self.root))], [True])
        self.assertEqual([e.urgent for e in watcher.log_events(events)], [False])  # no tracker: none looked up

    def test_a_question_reaches_the_coordinators_watch(self):
        self.opened()
        run(*self.t, "decide", "D-1", "--note", "why")
        events = [e for e in model.read_events(model.Tracker(self.root)) if e.get("decision") == "D-01"]
        self.assertEqual([e["kind"] for e in events], ["decision.opened", "decision.updated"])
        self.coordinator()
        tr = model.Tracker(self.root)
        self.assertEqual([e.urgent for e in watcher.log_events(events, tr, "coordinator")], [True, True])
        self.assertEqual([e.urgent for e in watcher.log_events(events, tr)], [False, False])
        own = [{**e, "session": "qb"} if e["kind"] == "decision.updated" else e for e in events]
        self.assertEqual([e.urgent for e in watcher.log_events(own, tr, "coordinator", "qb")], [True, False])

    def test_a_question_for_a_human_is_urgent_to_the_session_that_wrote_it(self):
        self.coordinator()
        run(*self.t, "decide", "Pick a store", "--owner", "Kim")  # passed to a person, by the coordinator itself
        events = [{**e, "session": "qb"} for e in model.read_events(model.Tracker(self.root))
                  if e.get("decision") == "D-01"]
        tr = model.Tracker(self.root)
        for role in ("coordinator", ""):
            self.assertEqual([e.urgent for e in watcher.log_events(events, tr, role, "qb")], [True])

    def test_a_settled_question_is_not_urgent(self):
        events = self.opened()
        run(*self.t, "decide", "D-1", "--resolve", "S3")
        events = [e for e in model.read_events(model.Tracker(self.root)) if e.get("decision") == "D-01"]
        self.assertEqual(len(events), 2)
        for role in ("", "coordinator"):
            self.assertEqual([e.urgent for e in watcher.log_events(events, model.Tracker(self.root), role)],
                             [False, False])

    def test_a_new_action_needs_the_user(self):
        events = watcher.log_events([
            {"kind": "action.added", "refs": ["A-01", "T-1"], "text": "Action for the user: Ask Sam"},
            {"kind": "action.closed", "refs": ["A-01", "T-1"], "text": "A-01 done: Ask Sam"},
            {"kind": "legacy", "refs": ["A-02"], "text": "Action for the user: Ask Kim", "day": "2026-01-01"}])
        self.assertEqual([(e.key, e.urgent) for e in events], [("A-01", True), ("A-01", False)])  # legacy: never new

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
        running("ui", "idle", now - watcher.IDLE_S - 90)  # mid-minute: a ms of float error keeps 11 min
        w = self.watcher()
        first = "\n".join(w.claim(now))
        self.assertIn("! W-1 in-progress · move: you — no PR yet · agent api busy 0 min · agent ui idle 11 min", first)
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
        running("api", "idle", late + 5)
        self.assertEqual(w.poll(late + 5), [f"{time.strftime('%H:%M', time.localtime(late + 5))} agent api: turn ended "
                                            "on W-1"])
        running("api", "idle", late + 9)  # a whole turn between two looks
        self.assertIn("agent api: turn ended on W-1", "".join(w.poll(late + 9)))
        self.assertEqual(w.poll(late + 10), [])

    def test_agents_command(self):
        """`tracker agents`: each live agent's state, tickets, branch and folder; the ids Claude Code and Herdr know it
        by; the transcript its hooks gave."""
        run(*self.t, "set", "W-1", "status=in-progress", cwd=self.work)
        self.assertIn(f"no agent session on {self.s}", run("agents", self.s))
        sid, herdr = f"tr{time.monotonic_ns()}", {"HC_TERMINAL_ID": "t7"}
        session.save_session(sid, tracker=self.s, cwd=str(self.work))
        hook("prompt", sid, self.work, herdr, prompt="go on", transcript_path="/t/one.jsonl")
        hook("waiting", sid, self.work, {"HC_TERMINAL_ID": "t9"}, tool_name="Bash", agent_id="sub",
             transcript_path="/t/sub.jsonl")  # a subagent's: not the session's
        hook("stop", sid, self.work, herdr)  # with no transcript, the one known stays
        where = f"on W-1 · branch feat/W-1 · {self.work}"
        self.assertEqual(run("agents", self.s).strip(), f"{sid[:8]} idle 0 min · {where} · herdr t7 · "
                                                         "transcript /t/one.jsonl")

        claude = session.CLAUDE_SESSIONS / f"{sid}.json"  # Claude Code runs it, by the name it lists it by
        claude.parent.mkdir(parents=True, exist_ok=True)
        claude.write_text(json.dumps({"pid": os.getpid(), "sessionId": sid, "name": "api", "status": "idle",
                                      "statusUpdatedAt": time.time() * 1000}))
        self.addCleanup(claude.unlink)
        hook("stop", sid, self.work, {"HC_TERMINAL_ID": ""})  # out of Herdr now
        self.assertEqual(run("agents", self.s).strip(), f"api idle 0 min · {where} · claude api · "
                                                         "transcript /t/one.jsonl")

    def test_only_and_hold(self):
        """`--only` keeps the kinds asked for; `--hold` makes one batch of what needs no one, an urgent event comes at
        once with it, and a restart prints what was held."""
        now = time.time()
        w = watcher.Watcher(model.Tracker(self.root), only=frozenset({"ticket", watcher.URGENT}), hold=60)
        w.claim(now)
        run(*self.t, "set", "W-2", "next=Plan it")
        run(*self.t, "log", "A note", "--ref", "W-2")  # a log line: not asked for
        self.assertEqual(w.poll(now), [])
        self.assertEqual(w.poll(now + 59), [])
        lines = w.poll(now + 60)
        self.assertEqual(len(lines), 1)
        self.assertIn("W-2: next: Plan it", lines[0])
        self.assertNotIn("A note", lines[0])

        run(*self.t, "set", "W-1", "next=Build it")
        self.assertEqual(w.poll(now + 61), [])
        ticket(self.root, "W-9", depends_on=["W-99"])  # a `check` error needs the user
        lines = "\n".join(w.poll(now + 62))
        self.assertIn("! check: ", lines)
        self.assertIn("W-1: next: Build it", lines)

        run(*self.t, "set", "W-2", "next=Plan it again")
        self.assertEqual(w.poll(now + 63), [])
        w.release(now + 63)
        self.assertIn("W-2: next: Plan it again", "".join(self.watcher().claim(now + 64)))
        self.assertIn("--only takes ticket, log", run("watch", self.s, "--only", "tickets", code=2))

    def test_an_agent_that_asks(self):
        run(*self.t, "set", "W-1", "status=in-progress", cwd=self.work)
        now, sid = time.time(), f"asks{time.monotonic_ns()}"
        w = self.watcher()
        w.claim(now)
        session.save_session(sid, tracker=self.s, cwd=str(self.work),
                             activity={"status": "waiting", "since": now, "at": now})
        w.poll(now)  # it started; a prompt the user is at is no news
        self.assertEqual(w.poll(now + watcher.ASKING_S - 1), [])
        told = w.poll(now + watcher.ASKING_S)
        self.assertEqual(len(told), 1)
        self.assertRegex(told[0], r"! agent \w+: asks for a permission or an answer on W-1$")
        self.assertEqual(w.poll(now + watcher.ASKING_S + 1), [])  # once per wait

    def test_github(self):
        """The sync runs in a thread: a slow `gh` holds up no look, and the look after it ends tells how it went."""
        (s, t), now = new_tracker("Synced", "a/x"), time.time()
        w = watcher.Watcher(model.tracker_at(s))
        gate = threading.Event()

        def failed(*_, **__):
            gate.wait(5)
            return ["sync skipped: gh failed or timed out for a/x"]

        def synced(at: float) -> list[str]:
            w.poll(at)
            w.job.join()
            return w.poll(at)

        with mock.patch.object(watcher, "sync", side_effect=failed):
            w.claim(now)
            self.assertEqual(w.poll(now), [])  # the sync starts
            run(*t, "log", "A note")
            self.assertIn("log: A note", "".join(w.poll(now)))
            gate.set()
            w.job.join()
            self.assertIn("! github: sync skipped: gh failed", "".join(w.poll(now)))
            self.assertEqual(synced(now + schema.LIVE_SYNC_S), [])  # said once
        with mock.patch.object(watcher, "sync", return_value=[]):
            self.assertEqual(synced(now + 2 * schema.LIVE_SYNC_S), [])  # skipped for its interval: not proof it works
            model.tracker_at(s).save_state({"last_sync": now + 1})
            self.assertIn("github: sync works again", "".join(synced(now + 3 * schema.LIVE_SYNC_S)))

    def test_a_session_watches_when_told(self):
        """In an agent session `tracker watch` gives the session the watch: the model runs it only when told to (its
        help and the skill say so). `--stop`, or the user's `/work-tracker:watch stop` at once, ends it. A watching
        session writes, and may be on a tracker."""
        sid, cwd = f"sid{time.monotonic_ns()}", self.work
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):  # a Claude session whose hooks did not run: no id
            self.assertIn("cannot watch", run("watch", self.s, "--once", code=4))
        for other in ("/watchlist", "/watch", "$watch payments"):  # another plugin's command
            self.assertIsNone(hook("prompt", sid, cwd, prompt=other))
            self.assertFalse(watcher.granted(sid))
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            out = run("watch", self.s, "--once")
            self.assertTrue(watcher.granted(sid))
            self.assertIn(f"watching {self.s}: 0 under way", out)
            self.assertIn("Next: the same `tracker watch` command again", out)
            run(*self.t, "log", "a note")  # it writes
            run("start", self.s, cwd=cwd)  # and may be on the tracker
        self.assertIn("Watched by session sid", data(self.s)["now"]["watch"]["text"])  # between two `--once` it watches
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            self.assertIn("The watch has ended", run("watch", "--stop"))
        self.assertFalse(watcher.granted(sid))
        st = watcher.load_state(self.s, sid)
        watcher.state_path(self.s, sid).write_text(json.dumps({**st, "ended": time.time() - session.REARM_S - 1}))
        self.assertEqual(data(self.s)["now"]["watch"]["running"], False)
        self.assertRegex(data(self.s)["now"]["watch"]["text"], "^Watch by session sid")

        self.assertIsNone(hook("prompt", sid, cwd, prompt=f"/work-tracker:watch {self.s} tell me when W-1 merges"))
        self.assertTrue(watcher.granted(sid))  # the user's command gives it before its skill starts the watch
        self.assertIsNone(hook("prompt", sid, cwd, prompt="/work-tracker:watch stop"))
        self.assertFalse(watcher.granted(sid))
        self.assertIn("a terminal's watch ends with Ctrl-C", run("watch", "--stop", code=2))

    def test_one_watch_per_session_and_tracker(self):
        """Several sessions, and a terminal, watch one tracker at once, each from its own state; a second watch of
        the same session is refused."""
        now = time.time()
        for sid in ("", "qb", "overseer"):
            self.assertIn("watching", self.watcher(sid).claim(now)[0])
        self.assertEqual([w["who"] for w in watcher.watches_of(model.Tracker(self.root))], ["terminal"] * 3)
        self.assertIn("the watch by terminal, terminal, terminal",
                      lifecycle.in_use(model.Tracker(self.root)))
        other = subprocess.Popen(["sleep", "30"])
        try:
            st = watcher.load_state(self.s, "qb")
            watcher.state_path(self.s, "qb").write_text(json.dumps({**st, "pid": other.pid}))
            with self.assertRaises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()):
                self.watcher("qb").claim(now)
            self.assertEqual(refused.exception.code, watcher.REFUSED)
            self.watcher("overseer").claim(now)  # another session's watch goes on
        finally:
            other.kill()
            other.wait()
        lifecycle.forget(self.s)
        self.assertEqual(watcher.watches_of(model.Tracker(self.root)), [])

    def test_a_watch_leaves_out_its_own_session(self):
        """A watching session on the tracker it watches: its own turns are no news to it, or each would wake it."""
        run(*self.t, "set", "W-1", "status=in-progress", cwd=self.work)
        now, sid = time.time(), f"qb{time.monotonic_ns()}"

        def turn(status: str, at: float) -> None:
            session.save_session(sid, tracker=self.s, cwd=str(self.work),
                                 activity={"status": status, "since": at, "at": at})
        turn("busy", now)
        own, terminal = self.watcher(sid), self.watcher()
        self.assertIn("0 agent(s)", own.claim(now)[0])
        self.assertIn("1 agent(s)", terminal.claim(now)[0])
        turn("idle", now + 5)
        self.assertEqual(own.poll(now + 5), [])
        self.assertIn(f"agent {sid[:8]}: turn ended", "".join(terminal.poll(now + 5)))

    def test_a_watching_session_gets_no_change_news(self):
        """The prompt hook's news of what others changed: a watching session has them from its watch."""
        run(*self.t, "set", "W-1", "status=in-progress", cwd=self.work)
        for n, watching in enumerate((False, True)):
            sid = f"news{n}{time.monotonic_ns()}"
            with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
                run("start", self.s, cwd=self.work)
            hook("prompt", sid, self.work, prompt="go on")  # what it has seen
            if watching:  # its full watch of this tracker runs
                self.addCleanup(watcher.set_grant, sid, False)
                watcher.Watcher(model.Tracker(self.root), sid).claim(time.time())
            run(*self.t, "decide", f"Pick {n}", "--refs", "W-1", "--resolve", "A", "--by", "me")  # another session
            news = said(hook("prompt", sid, self.work, prompt="go on"))
            (self.assertNotIn if watching else self.assertIn)("by another session or a PR sync", news)

    def test_codex_watch_grant(self):
        sid, cwd = f"codex{time.monotonic_ns()}", self.work
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": sid}):
            self.assertIn(f"watching {self.s}", run("watch", self.s, "--once"))
            self.assertTrue(watcher.granted(sid))
            run(*self.t, "log", "it writes")
            hook("prompt", sid, cwd, prompt="$work-tracker:watch stop")
            self.assertFalse(watcher.granted(sid))


class MineWatch(TwoSessions):
    """`tracker watch --mine`: a session's watch of its own tickets wakes it with what others wrote on them, shares
    the prompt hook's baseline (`session.news`), follows the session and ends by itself. With the watch's real settle
    time; MineWatchAtOnce runs each again with none."""

    SETTLE: float | None = None  # None: watcher.SETTLE_S as it is

    def setUp(self, repos: str = ""):
        if self.SETTLE is not None:
            at_once = mock.patch.object(watcher, "SETTLE_S", self.SETTLE)
            at_once.start()
            self.addCleanup(at_once.stop)
        super().setUp(repos)
        self.clock = time.time()

    def mine(self) -> watcher.Watcher:
        return watcher.Watcher(model.tracker_at(self.s), self.a, "session sa", mine=True)

    def look(self, w: watcher.Watcher) -> list[str]:
        """What one change prints: a look now, and one once the change has settled."""
        self.clock += 1
        lines = w.poll(self.clock)
        self.clock += watcher.SETTLE_S
        return lines + w.poll(self.clock)

    def texts(self, lines: list[str]) -> list[str]:
        return [x.split(" ", 1)[1] for x in lines]

    def test_a_note_on_my_ticket_wakes_me(self):
        w = self.mine()
        first = w.claim(self.clock)
        self.assertEqual(self.texts(first), [f"watching your tickets on {self.s}: T-1 in-progress · move: you — no "
                                             "PR yet"])
        self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved")
        self.assertEqual(self.texts(self.look(w)), ["T-1: base branch moved"])
        self.assertEqual(watcher.load_state(self.s, self.a).get("mine"), True)
        self.assertNotIn("snapshot", watcher.load_state(self.s, self.a))

    def test_a_news_line_is_printed_as_time_and_text(self):
        w = self.mine()
        w.claim(self.clock)
        self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved")
        lines = self.look(w)
        self.assertEqual(lines, [f"{time.strftime('%H:%M', time.localtime(self.clock))} T-1: base branch moved"])

    def test_a_review_wakes_me_with_my_move(self):
        w = self.mine()
        w.claim(self.clock)
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.assertEqual(self.look(w), [])  # its own: the move goes to a reviewer
        self.as_b(*self.t, "review", "T-1", "--what", "plan", "--verdict", "changes", "--finding", "Split it")
        told = self.texts(self.look(w))
        self.assertIn("T-1 plan review: changes, 1 finding: 1. Split it", told)
        self.assertTrue(any(x.startswith("T-1 move now: you") for x in told), told)

    def test_a_pr_review_the_watchs_own_sync_reads_wakes_me_with_my_move(self):
        self.setUp("a/x")
        rounds = [review_round(101, "rev", "CHANGES_REQUESTED", "2026-09-02T01:02:03Z")]
        fake = answers([pull(11, branch="f1", decision="CHANGES_REQUESTED", rounds=rounds)])
        with mock.patch.object(github, "gh", fake), mock.patch.dict(os.environ, {"TRACKER_SESSION": self.a}):
            w = self.mine()
            first = w.claim(self.clock)  # its first look starts the sync, under this session's id: in a thread, which
            w.job.join()  # may write before that look reads the tracker
            told = self.texts(first[1:] + self.look(w))
        mine = [e for e in model.read_events(model.tracker_at(self.s)) if e["kind"] == "pr.review"]
        self.assertEqual([e["session"] for e in mine], [self.a])  # the watch's own sync read it
        self.assertIn("T-1: PR #11 review by rev: changes requested", told)
        self.assertTrue(any(x.startswith("T-1 move now: you") for x in told), told)

    def test_my_own_step_prints_nothing(self):
        w = self.mine()
        w.claim(self.clock)
        self.as_a(*self.t, "step", "T-1", "Built the parser", "--next", "Test it")
        self.as_a(*self.t, "log", "--ref", "T-1", "my own note")
        self.assertEqual(self.look(w), [])

    def test_another_tickets_events_agents_and_stuck_items_print_nothing(self):
        w, full = self.mine(), watcher.Watcher(model.tracker_at(self.s))
        w.claim(self.clock)
        full.claim(self.clock)
        self.as_b("start", self.s)  # B on T-1 too: an agent the full watch reports
        hook("prompt", self.b, self.work, env=no_gh(), prompt="go")
        hook("stop", self.b, self.work, env=no_gh())
        self.as_b(*self.t, "log", "--ref", "T-2", "only about two")
        run(*self.t, "decide", "Pick a store", "--refs", "T-2")  # a question for a human: `!` on the full watch
        Watch.coordinator(self, "qb")  # a coordinator watches, and T-2's plan waits on it with no session on T-2
        self.addCleanup(watcher.set_grant, "qb", False)
        run(*self.t, "set", "T-2", "status=in-progress")
        run(*self.t, "submit", "T-2", "--what", "plan")
        self.assertEqual(self.look(w), [])
        shown = "\n".join(full.poll(self.clock) + full.poll(self.clock + watcher.SETTLE_S))
        for line in (f"agent {self.b[:8]}: started", "only about two", "D-01: Opened D-01", "! stuck T-2"):
            self.assertIn(line, shown)  # the same changes, which a full watch says

    def test_what_the_watch_printed_is_not_in_the_next_message(self):
        w = self.mine()
        w.claim(self.clock)
        self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved")
        self.assertEqual(self.texts(self.look(w)), ["T-1: base branch moved"])
        self.assertNotIn("base branch moved", self.message())

    def test_what_a_message_told_is_not_printed(self):
        w = self.mine()
        w.claim(self.clock)
        self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved")
        self.assertIn("T-1: base branch moved", self.message())  # a session watching its own tickets is told too
        self.assertEqual(self.look(w), [])

    def test_a_full_watch_still_keeps_news_out_of_the_message(self):
        watcher.Watcher(model.tracker_at(self.s), self.a).claim(self.clock)
        self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved")
        self.assertNotIn("Changed on", self.message())

    def test_the_watch_follows_the_session_to_other_tickets(self):
        w = self.mine()
        w.claim(self.clock)
        run(*self.t, "set", "T-2", "status=in-progress")
        self.as_a("start", self.s, "--on", "T-2")
        self.assertEqual(self.look(w), [])  # the change of tickets is no news
        self.as_b(*self.t, "log", "--ref", "T-2", "about two")
        self.assertEqual(self.texts(self.look(w)), ["T-2: about two"])
        self.as_b(*self.t, "log", "--ref", "T-1", "about one")
        self.assertEqual(self.look(w), [])

    def test_a_note_survives_a_ticket_of_mine_closing(self):
        run(*self.t, "new", "T-3", "--title", "Three", "--branch", "f1")
        run(*self.t, "set", "T-3", "status=in-progress")
        self.as_a("start", self.s)  # on T-1 and T-3
        w = self.mine()
        w.claim(self.clock)
        self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved")
        self.as_b(*self.t, "set", "T-3", "status=done", "summary=Shipped")
        self.assertEqual(sorted(self.texts(self.look(w))), ["T-1: base branch moved", "T-3 is now done"])

    def ended(self, out: str) -> list[str]:
        return [x for x in out.splitlines()
                if x.endswith("no ticket of this session is under way: the watch has ended")]

    def test_the_watch_ends_when_no_ticket_is_under_way(self):
        run(*self.t, "new", "T-3", "--title", "Three", "--branch", "f1")  # on A's branch too, not started
        out, _ = self.run_watch(self.mine(), during=lambda: self.as_b(*self.t, "set", "T-1", "status=done",
                                                                       "summary=Shipped"))
        lines = out.splitlines()
        self.assertIn("watching your tickets on", lines[0])
        self.assertEqual(self.texts(lines[1:]), ["T-1 is now done", "no ticket of this session is under way: the "
                                                 "watch has ended"])
        self.assertFalse(watcher.granted(self.a))
        st = watcher.load_state(self.s, self.a)
        self.assertTrue(st.get("ended"))
        self.assertFalse(session.watch_running(st))
        self.assertNotIn("Next:", out)

    def test_my_own_closes_print_only_the_end(self):
        run(*self.t, "new", "T-3", "--title", "Three", "--branch", "f1")
        run(*self.t, "set", "T-3", "status=in-progress")
        self.as_a("start", self.s)  # on T-1 and T-3

        def close_both():
            self.as_a(*self.t, "set", "T-3", "status=done", "summary=Shipped")
            self.as_a(*self.t, "set", "T-1", "status=done", "summary=Shipped")

        out, _ = self.run_watch(self.mine(), during=close_both)
        self.assertEqual(len(out.splitlines()), 2, out)  # its first line, and the end
        self.assertEqual(len(self.ended(out)), 1, out)

    def test_a_note_on_a_ticket_that_joined_my_branch_wakes_me(self):
        run(*self.t, "new", "T-3", "--title", "Three", "--branch", "f1")
        w = self.mine()
        w.claim(self.clock)
        run(*self.t, "set", "T-3", "status=in-progress")
        self.as_b(*self.t, "log", "--ref", "T-3", "the API changed under you")
        self.assertEqual(self.texts(self.look(w)), ["T-3: the API changed under you"])

    def test_a_watch_of_a_session_with_nothing_open_says_so_and_ends(self):
        run(*self.t, "set", "T-1", "status=done", "summary=Shipped")
        out, _ = self.run_watch(self.mine())
        self.assertEqual(self.texts(out.splitlines()), [f"watching your tickets on {self.s}: none open",
                                                        "T-1 is now done",  # a terminal closed it: news to A
                                                        "no ticket of this session is under way: the watch has ended"])

    def test_my_own_hand_edited_close_is_not_printed_before_its_hook_counts_it(self):
        if not watcher.SETTLE_S:
            self.skipTest("with no settle time, a look can fall between the edit and its hook")
        w = self.mine()
        w.claim(self.clock)
        path = store.HOME / self.s / "tickets/T-1.md"
        path.write_text(re.sub(r"^status: .*", "status: done", path.read_text(), flags=re.M))
        self.clock += 1
        self.assertEqual(w.poll(self.clock), [])  # the edit is written; its hook has not run yet
        self.assertFalse(w.ended)
        tool("Edit", self.a, self.work, env=no_gh(), tool_input={"file_path": str(path)})
        self.assertEqual(w.poll(self.clock + watcher.SETTLE_S), [])
        self.assertTrue(w.ended)

    def test_the_watch_follows_the_session_to_another_checkout(self):
        run(*self.t, "new", "T-3", "--title", "Three", "--branch", "f3")
        run(*self.t, "set", "T-3", "status=in-progress")
        w = self.mine()
        w.claim(self.clock)
        other = repo("f3")  # the session works there now, in this turn: no message of the user's has seen it
        self.as_a(*self.t, "submit", "T-3", "--what", "plan", cwd=other)
        self.assertIsNone(hook("stop", self.a, other, env={**no_gh(), "CLAUDE_CODE_SESSION_ATTENDED": "1"},
                               stop_hook_active=False))  # its watch runs: nothing to ask
        self.as_b(*self.t, "review", "T-3", "--what", "plan", "--verdict", "changes", "--finding", "Rename it")
        self.assertIn("T-3 plan review: changes, 1 finding: 1. Rename it", self.texts(self.look(w)))

    def test_a_watch_that_ends_itself_leaves_the_sessions_other_watches(self):
        other, _ = new_tracker("Elsewhere")
        watcher.Watcher(model.tracker_at(other), self.a).claim(time.time())  # its full watch of another tracker runs
        out, _ = self.run_watch(self.mine(), during=lambda: self.as_b(*self.t, "set", "T-1", "status=done",
                                                                       "summary=Shipped"))
        self.assertEqual(len(self.ended(out)), 1, out)
        self.assertTrue(watcher.granted(self.a))  # the other watch goes on
        self.assertTrue(session.watch_running(watcher.load_state(other, self.a)))
        self.assertFalse(session.watch_running(watcher.load_state(self.s, self.a)))  # this one is over

    def test_a_watch_of_another_tracker_keeps_no_news_out_of_the_message(self):
        other, _ = new_tracker("Elsewhere")
        watcher.Watcher(model.tracker_at(other), self.a).claim(self.clock)
        self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved")
        self.assertIn("T-1: base branch moved", self.message())  # no watch of this tracker tells it

    def test_the_watch_ends_when_the_session_moves_to_another_tracker(self):
        other, _ = new_tracker("Elsewhere")
        out, _ = self.run_watch(self.mine(), during=lambda: self.as_a("start", other))
        self.assertEqual(len(self.ended(out)), 1, out)
        self.assertEqual(len(out.splitlines()), 2, out)  # its first line, and the end
        self.assertFalse(watcher.granted(self.a))
        self.assertTrue(watcher.load_state(self.s, self.a).get("ended"))
        self.assertNotIn("seen_seq", session.load_session(self.a))  # the new tracker's baseline: not this watch's

    def test_a_restarted_mine_watch_repeats_no_first_line(self):
        w = self.mine()
        self.assertTrue(w.claim(self.clock))
        w.release(self.clock)
        again = self.mine()
        self.assertEqual(again.claim(self.clock + 1), [])  # nothing new: no line wakes the session
        again.release(self.clock + 1)
        out, sleeps = self.run_watch(self.mine(), once=True,
                                     during=lambda: self.as_b(*self.t, "log", "--ref", "T-1", "base branch moved"))
        self.assertGreater(sleeps, 0)  # it waited for news
        self.assertNotIn("watching your tickets", out)
        self.assertIn(" T-1: base branch moved\n", out)
        later = self.mine()
        self.assertIn("watching your tickets", later.claim(time.time() + watcher.CATCH_UP_S + 1)[0])
        later.release(time.time())
        watcher.Watcher(model.tracker_at(self.s), self.a).claim(time.time())  # a full watch of the session
        self.assertNotIn("mine", watcher.load_state(self.s, self.a))

    def test_the_command_prints_the_first_line_and_exits_once(self):
        out = self.as_a("watch", "--mine", "--once")
        self.assertIn(f"watching your tickets on {self.s}: T-1 in-progress", out)
        self.assertTrue(watcher.granted(self.a))

    def test_the_users_stop_survives_tracker_start(self):
        self.as_a("watch", "--stop")
        self.assertTrue(session.load_session(self.a).get("no_watch"))
        self.as_a("start", self.s, "--on", "T-2")
        self.assertTrue(session.load_session(self.a).get("no_watch"))
        hook("prompt", self.a, self.work, env=no_gh(), prompt="/work-tracker:watch")  # a start clears it
        self.assertNotIn("no_watch", session.load_session(self.a))
        hook("prompt", self.a, self.work, env=no_gh(), prompt="/work-tracker:watch stop")
        self.as_a("start", self.s)
        self.assertTrue(session.load_session(self.a).get("no_watch"))

    def test_mine_refuses_at_a_terminal_with_a_role_with_only_and_on_another_tracker(self):
        other, _ = new_tracker("Elsewhere")
        c = f"mc{time.monotonic_ns()}"  # a session on no tracker
        self.addCleanup(watcher.set_grant, c, False)

        def kept() -> tuple:
            return (session.load_session(self.a), session.load_session(c), watcher.granted(self.a),
                    watcher.granted(c), watcher.state_path(self.s, self.a).exists(),
                    watcher.state_path(other, self.a).exists())

        before = kept()
        for case in ((None, self.s), (self.a, self.s, "--as", "coordinator"), (self.a, self.s, "--only", "ticket"),
                     (self.a, self.s, "--hold", "60"), (self.a, other), (c, self.s)):
            with self.subTest(case=case), mock.patch.dict(os.environ, {"TRACKER_SESSION": case[0]} if case[0] else {}):
                self.assertIn("--mine", run("watch", *case[1:], "--mine", "--once", cwd=self.work, code=2))
                self.assertEqual(kept(), before)

    def test_mine_on_no_tracker_offers_no_tracker_to_name(self):
        c = f"mc{time.monotonic_ns()}"
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": c}):
            said_ = run("watch", "--mine", "--once", cwd=self.work, code=2)  # not 3, a list of slugs to pass
        self.assertIn("it is on no tracker: `tracker start <name>`", said_)
        self.assertFalse(watcher.granted(c))

    def test_stop_marks_the_session_and_a_new_watch_clears_it(self):
        self.as_a("watch", "--stop")
        self.assertTrue(session.load_session(self.a).get("no_watch"))
        self.mine().claim(self.clock)
        self.assertNotIn("no_watch", session.load_session(self.a))
        c = f"mc{time.monotonic_ns()}"
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": c}):
            run("watch", "--stop")
        self.assertFalse(session.session_path(c).exists())  # a session with no file gets none


class MineWatchAtOnce(MineWatch):
    """MineWatch with no settle time: a change is printed at the first look."""

    SETTLE = 0


class WatchAsk(TwoSessions):
    """The turn-end hook: a session whose ticket under way waits on someone else, with no watch running, is held back
    once per wait and asked to start `tracker watch --mine` (R5)."""

    def setUp(self):
        attended = mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ATTENDED": "1"})  # a person uses each session
        attended.start()
        self.addCleanup(attended.stop)
        super().setUp()

    def stop(self, cwd: Path | None = None, env: dict | None = None, **extra) -> dict | None:
        """A's turn ends, as Claude Code sends it: `stop_hook_active` false unless `extra` says otherwise."""
        return hook("stop", self.a, cwd or self.work, env={**no_gh(), **(env or {})},
                    **{"stop_hook_active": False, **extra})

    def asked(self, out: dict | None, *tickets: str) -> None:
        self.assertEqual(set(out or {}), {"decision", "reason"}, out)
        self.assertEqual(out["decision"], "block")
        self.assertIn("`tracker watch --mine`", out["reason"])
        for t in tickets:
            self.assertIn(f"{t} waits on ", out["reason"])

    def test_a_session_whose_ticket_waits_is_asked_once_to_watch(self):
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")  # the move is no longer A's
        out = self.stop()
        self.asked(out, "T-1")
        self.assertEqual(out["reason"], hooks.WATCH_ASK.format(waits="T-1 waits on human: plan review"))
        self.assertIsNone(self.stop())

    def test_a_session_whose_move_is_its_own_is_not_asked(self):
        self.assertIsNone(self.stop())

    def test_a_session_that_watches_is_not_asked(self):
        for mine in (False, True):
            with self.subTest(mine=mine):
                self.setUp()
                watcher.Watcher(model.tracker_at(self.s), self.a, mine=mine).claim(time.time())
                self.as_a(*self.t, "submit", "T-1", "--what", "plan")
                self.assertIsNone(self.stop())

    def test_the_hook_never_asks_while_it_holds_the_session(self):
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.assertIsNone(self.stop(stop_hook_active=True))
        self.asked(self.stop(), "T-1")  # the held stop used nothing up

    def test_a_wait_that_comes_back_is_asked_again(self):
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.asked(self.stop(), "T-1")
        self.as_b(*self.t, "review", "T-1", "--what", "plan", "--verdict", "changes", "--finding", "Split it")
        self.assertIsNone(self.stop())  # the move is A's again
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.asked(self.stop(), "T-1")

    def test_after_watch_stop_the_session_is_not_asked(self):
        self.as_a("watch", "--stop")
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.assertIsNone(self.stop())
        # A watch it starts forgets the stop; once that watch ends by itself, a new wait is asked for again.
        out, _ = self.run_watch(watcher.Watcher(model.tracker_at(self.s), self.a, mine=True),
                                during=lambda: self.as_b(*self.t, "set", "T-1", "status=done", "summary=Done"))
        self.assertIn("the watch has ended", out)
        self.as_a("start", self.s, "--on", "T-2")
        self.as_a(*self.t, "set", "T-2", "status=in-progress")
        self.as_a(*self.t, "submit", "T-2", "--what", "plan")
        self.asked(self.stop(), "T-2")

    def test_an_unattended_session_a_subagent_and_a_session_end_are_not_asked(self):
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.assertIsNone(self.stop(env={"CLAUDE_CODE_SESSION_ATTENDED": "0"}))
        self.assertIsNone(self.stop(agent_id="sub"))
        self.assertIsNone(hook("session-end", self.a, self.work, env=no_gh(), reason="other",
                               stop_hook_active=False))
        self.asked(self.stop(), "T-1")  # none of them used the ask up

    def test_a_host_that_sends_no_stop_hook_active_is_not_asked(self):
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.assertIsNone(hook("stop", self.a, self.work, env=no_gh()))
        self.asked(self.stop(), "T-1")

    def test_a_session_on_a_tracker_with_no_branch_is_asked(self):
        plain = Path(tempfile.mkdtemp(dir=BASE))  # no git here
        self.as_a("start", self.s, "--on", "T-1", cwd=plain)
        self.as_a(*self.t, "submit", "T-1", "--what", "plan", cwd=plain)
        self.asked(self.stop(cwd=plain), "T-1")

    def test_a_held_turn_still_shows_busy(self):
        hook("prompt", self.a, self.work, env=no_gh(), prompt="go")
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.asked(self.stop(), "T-1")
        self.assertEqual(session.load_session(self.a)["activity"]["status"], "busy")  # its turn goes on
        self.assertIsNone(self.stop())
        self.assertEqual(session.load_session(self.a)["activity"]["status"], "idle")

    def test_the_watch_it_asks_for_looks_where_the_session_works_now(self):
        other = repo("f1")  # another checkout of the branch, which the session moved to in this turn
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.asked(self.stop(cwd=other), "T-1")
        self.assertEqual(session.load_session(self.a)["cwd"], str(other))

    def test_a_stop_in_the_tracker_folder_does_not_ask_again(self):
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.asked(self.stop(), "T-1")
        self.assertIsNone(self.stop(cwd=store.HOME / self.s))  # a `cd` there: the same session, on the same wait
        self.assertIsNone(self.stop())

    def test_the_ask_survives_tracker_start(self):
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        self.asked(self.stop(), "T-1")
        self.as_a("start", self.s, "--on", "T-1")
        self.assertIsNone(self.stop())

    def test_a_refused_tracker_gets_no_ask(self):
        for refuse in (NewerFormat.newer, NewerFormat.unmigrated):
            for plain in (False, True):
                with self.subTest(refuse.__name__, plain=plain):
                    self.setUp()
                    cwd = Path(tempfile.mkdtemp(dir=BASE)) if plain else self.work
                    self.as_a("start", self.s, "--on", "T-1", cwd=cwd)
                    self.as_a(*self.t, "submit", "T-1", "--what", "plan", cwd=cwd)
                    refuse(self, self.s)
                    before = tree(store.HOME / self.s)
                    self.assertIsNone(self.stop(cwd=cwd))  # exits 0: `hook` checks
                    self.assertEqual(tree(store.HOME / self.s), before)

    def test_the_commits_are_still_logged_when_the_hook_asks(self):
        self.stop()  # the branch's mark
        self.as_a(*self.t, "submit", "T-1", "--what", "plan")
        commit(self.work, "terminal work")  # no post-bash hook sees it
        self.asked(self.stop(), "T-1")
        self.assertIn("terminal work", (store.HOME / self.s / "log.md").read_text())


class WatchingAgents(unittest.TestCase):
    """A session that waits on its own watch is not idle and waiting on you, nor stuck while busy, in a watch's lines
    and its first summary: it is woken by that watch. Session A is on T-1; a terminal watches the tracker."""

    def setUp(self):
        self.s, self.t = new_tracker("Watching")
        run(*self.t, "new", "T-1", "--title", "One", "--branch", "f1")
        run(*self.t, "set", "T-1", "status=in-progress")
        self.work = repo("f1")
        self.a = f"wg{time.monotonic_ns()}"
        self.addCleanup(session.drop_session, self.a)
        self.addCleanup(watcher.set_grant, self.a, False)
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": self.a}):
            run("start", self.s, cwd=self.work)
        self.now = time.time()

    def own_watch(self) -> watcher.Watcher:
        """A's watch of its own tickets, running in this process."""
        w = watcher.Watcher(model.tracker_at(self.s), self.a, "session wg", mine=True)
        w.claim(self.now)
        return w

    def terminal(self, at: float | None = None, sid: str = "") -> tuple[watcher.Watcher, list[str]]:
        """A new watch of the tracker (a terminal's, or another session's), and its first lines."""
        w = watcher.Watcher(model.tracker_at(self.s), sid)
        self.addCleanup(watcher.set_grant, sid, False)
        return w, w.claim(self.now if at is None else at)

    def test_a_watching_session_is_not_said_to_wait_on_you(self):
        for watching in (True, False):
            with self.subTest(watching=watching):
                self.setUp()
                hook("stop", self.a, self.work, env=no_gh())  # its turn ended: idle from now
                if watching:
                    self.own_watch()
                late = self.now + watcher.IDLE_S + 90
                [t1] = [x for x in self.terminal(late, "observer")[1] if " T-1 " in x]
                if watching:
                    self.assertRegex(t1, rf"^\d\d:\d\d T-1 in-progress · .* · agent {self.a[:8]} watching$")
                else:
                    self.assertRegex(t1, rf"^\d\d:\d\d ! T-1 in-progress · .* · agent {self.a[:8]} idle \d+ min$")
                w = self.terminal()[0]  # one that started before: its later look
                said = "\n".join(w.poll(late))
                (self.assertNotIn if watching else self.assertIn)(f"! agent {self.a[:8]}: idle", said)

    def test_a_session_that_waits_on_its_watch_once_is_not_said_to_be_stuck(self):
        for watching in (True, False):
            with self.subTest(watching=watching):
                self.setUp()
                hook("prompt", self.a, self.work, env=no_gh(), prompt="go")  # busy: a host that waits in a turn
                if watching:
                    self.own_watch()
                w = self.terminal()[0]
                said = "\n".join(w.poll(self.now + watcher.BUSY_S + 90))
                (self.assertNotIn if watching else self.assertIn)(f"agent {self.a[:8]}: busy", said)

    def test_a_watching_session_that_asks_for_a_permission_is_still_said(self):
        self.own_watch()
        hook("waiting", self.a, self.work, env=no_gh(), tool_name="Bash")
        w = self.terminal()[0]
        self.assertIn(f"! agent {self.a[:8]}: asks for a permission or an answer on T-1",
                      "\n".join(w.poll(time.time() + watcher.ASKING_S)))

    def test_once_its_watch_ends_it_is_said_idle_again(self):
        hook("stop", self.a, self.work, env=no_gh())
        late = self.now + watcher.IDLE_S + 90
        own = self.own_watch()
        own.release(self.now)  # between two runs (`--once`): it still watches
        w = self.terminal()[0]
        self.assertEqual([x for x in w.poll(late) if "waits on you" in x], [])
        own.release(self.now - session.REARM_S - 1)  # it ended long ago and was not started again
        self.assertIn(f"! agent {self.a[:8]}: idle", "\n".join(w.poll(late + 1)))


class Delete(unittest.TestCase):
    """`tracker archive` and `delete`, and a UI's tracker menu: an archived tracker leaves every list and lookup but a
    UI's; a deleted one goes to the system's trash, only by the user; neither while a session or a watch
    is on it. The tests use the freedesktop trash in BASE, not the user's."""

    def setUp(self):
        self.s, _ = new_tracker("Doomed")
        self.root = store.HOME / self.s
        self.trash = Path(BASE) / "xdg" / "Trash"
        patches = [mock.patch.object(lifecycle, "to_trash", system.xdg_trash),
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

    def test_the_ui_lists_and_deletes(self):
        def listed(group: str = "active") -> dict:
            return next((t for t in json.loads(run("list", "--json"))[group] if t["slug"] == self.s), {})

        self.assertEqual(listed(), {"slug": self.s, "title": "Doomed", "root": str(self.root), "in_use": ""})
        sid = self.live()
        self.assertEqual(listed()["in_use"], "agent session busy-one")
        self.assertIn(f"{self.s} is in use by agent session busy-one", run("delete", self.s, "--yes", code=4))
        (session.CLAUDE_SESSIONS / f"{sid}.json").unlink()

        # Archived: in its own list, it still shows, then back.
        run("archive", self.s)
        self.assertEqual((listed(), listed("archived")),
                         ({}, {"slug": self.s, "title": "Doomed", "root": str(model.ARCHIVE / self.s)}))
        self.assertTrue(data(self.s)["archived"])
        run("unarchive", self.s)
        self.assertTrue(listed())
        self.assertFalse(data(self.s)["archived"])
        run("delete", self.s, "--yes")
        self.assertEqual((listed(), listed("archived")), ({}, {}))

class Actions(unittest.TestCase):
    """A user's action: a task the agent cannot or should not do. The agent adds it with `tracker act`; the user's
    word closes it, from the CLI or a UI's Done and Drop. Open ones show above Current and in the brief; closed
    ones go to Reference."""

    def setUp(self):
        self.s, self.t = new_tracker("Work")
        run(*self.t, "new", "T-1", "--title", "Schema")
        self.root = store.HOME / self.s

    def tr(self) -> model.Tracker:
        return model.Tracker(self.root)

    def test_an_action_is_added_noted_and_closed(self):
        out = run(*self.t, "act", "--refs", "T-1", "Ask Sam whether the billing API keeps v1", "--note", "T-1 needs it")
        self.assertIn("A-01 added", out)
        a = self.tr().lookup("a-1")
        self.assertEqual((a.get("status"), a.list("refs")), ("open", ["T-1"]))
        self.assertIn("- T-1 needs it\n", a.body)
        run(*self.t, "act", "A-1", "--note", "Reply: v1 stays", "--note", "Reply: until March")
        self.assertIn("- T-1 needs it\n- Reply: v1 stays\n- Reply: until March", run(*self.t, "show", "A-01"))
        self.assertIn(f"- {schema.today()} [A-01 T-1] Updated A-01: Reply: v1 stays; Reply: until March",
                      (self.root / "log.md").read_text())
        run(*self.t, "act", "A-01", "--note", "x " * 210)  # a note of any length
        self.assertIn("x " * 209 + "x\n", self.tr().lookup("A-01").body)
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
        self.assertEqual(contract.check(self.tr()), ([], ["README.md: ## Context lists nothing"]))
        self.assertIn("Actions: An action is a task for the user", run("rules"))

    def test_the_brief_lists_open_actions_and_asks_about_old_ones(self):
        run(*self.t, "act", "Ask Sam whether the billing API keeps v1", "--refs", "T-1")
        run(*self.t, "act", "Get read access to the prod DB")
        run(*self.t, "act", "A-02", "--done")
        self.tr().lookup("A-01").save({"created_at": "2026-01-01T09:00:00Z"})
        index = run(*self.t, "index")
        self.assertIn("Open actions for the user", index)
        self.assertRegex(index, r"A-01 \(open \d+ days: ask the user whether it is done\): Ask Sam .* \(T-1\)")
        self.assertNotIn("A-02", index)
        self.assertIn('`act "<what, with whom>" --refs <ids>`', views.protocol(self.s))

    def test_a_ui_shows_them_and_closes_one(self):
        run(*self.t, "act", "Ask Sam whether the billing API keeps v1", "--refs", "T-1")
        run(*self.t, "act", "Get read access to the prod DB")
        got = data(self.s)
        self.assertEqual([a["id"] for a in got["actions"]], ["A-01", "A-02"])
        self.assertEqual(got["reference"]["actions"], [])
        run(*self.t, "act", "A-01", "--done")  # what its Done runs
        run(*self.t, "act", "A-09", "--drop", code=2)
        run(*self.t, "act", "A-02", "--drop")
        self.assertEqual([a.get("status") for a in self.tr().actions], ["done", "dropped"])
        got = data(self.s)
        self.assertEqual(got["actions"], [])
        self.assertEqual(sorted(a["id"] for a in got["reference"]["actions"]), ["A-01", "A-02"])

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
        due = {a["id"]: a["due"] for a in data(self.s)["actions"]}
        self.assertEqual(due, {"A-02": {"text": "15 Jan 2099", "late": ""},
                               "A-03": {"text": "2 Jan 2020", "late": "overdue"},
                               "A-01": None})  # A-01 has none, and shows no age instead
        run(*self.t, "act", "A-03", "--due", "none")
        self.assertEqual(self.tr().lookup("A-03").get("due"), "")
        self.tr().lookup("A-02").save({"due": "soon"})
        self.assertIn("A-02: due 'soon' is not a day (YYYY-MM-DD)", contract.check(self.tr())[0])

    def test_an_action_gets_a_new_title(self):
        run(*self.t, "act", "Reply to Sam on SC-1", "--refs", "T-1")
        run(*self.t, "act", "Reply to Kim", "--title", "Reply to Kim on SC-2", code=2)
        self.assertIn("A-01 updated", run(*self.t, "act", "A-01", "--title", "Reply to Sam on SC-1 (login copy)"))
        self.assertEqual(self.tr().lookup("A-01").get("title"), "Reply to Sam on SC-1 (login copy)")
        self.assertIn("A-01 renamed: Reply to Sam on SC-1 → Reply to Sam on SC-1 (login copy)",
                      (self.root / "log.md").read_text())

    def test_an_action_blocks_tickets_as_a_decision_does(self):
        run(*self.t, "new", "T-2", "--title", "Billing")
        self.assertIn("A-01 added, blocks T-1",
                      run(*self.t, "act", "Get read access to the prod DB", "--blocks", "T-1", "--refs", "T-1,T-2"))
        tr = self.tr()
        t1, a = tr.lookup("T-1"), tr.lookup("A-01")
        self.assertEqual((t1.list("depends_on"), a.list("refs")), (["A-01"], ["T-2"]))  # a block is not a ref
        self.assertEqual([d.ident for d in tr.blockers(t1)], ["A-01"])
        self.assertIn("waits on: A-01 open: Get read access to the prod DB", run(*self.t, "context", "T-1"))
        self.assertIn("A-01: Get read access to the prod DB (T-2); blocks T-1", "\n".join(views.action_lines(tr)))
        self.assertEqual(data(self.s)["actions"][0]["gate"], "blocks T-1")
        t1.save({"status": "in-progress"})
        self.assertEqual(moves.whose_move(self.tr(), self.tr().lookup("T-1")).text(), "you: A-01 open")

        run(*self.t, "wait", "T-2", "on", "a-1")  # the wait takes the ticket out of refs
        self.assertEqual((self.tr().lookup("T-2").list("depends_on"), self.tr().lookup("A-01").list("refs")),
                         (["A-01"], []))
        self.tr().lookup("A-01").save({"refs": ["T-2"]})
        self.assertIn("A-01: refs T-2, which also waits on it — drop it from refs (`tracker act A-01 --unref T-2`)",
                      "\n".join(contract.check(self.tr())[1]))
        run(*self.t, "act", "A-01", "--unref", "T-2")
        self.assertEqual(self.tr().lookup("A-01").list("refs"), [])
        self.assertIn("no action A-09", run(*self.t, "wait", "T-1", "on", "A-09", code=2))
        run(*self.t, "act", "A-01", "--done", "--blocks", "T-2", code=2)
        self.assertIn("nothing blocks T-1, T-2 now", run(*self.t, "act", "A-01", "--done"))
        self.assertEqual(self.tr().blockers(self.tr().lookup("T-1")), [])

    def test_an_actions_id_works_as_any_records(self):
        run(*self.t, "act", "Ask Sam whether v1 stays", "--refs", "T-1")
        added = model.LOG["action"].format(title="Ask Sam")
        self.assertIn(f"[A-01 T-1] {added}", run(*self.t, "history", "--ref", "a-1"))
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
        self.assertEqual(contract.check(self.tr())[0], [])

    def test_the_text_and_the_page_show_the_same_links(self):
        run(*self.t, "new", "T-2", "--title", "Billing")
        run(*self.t, "act", "Get read access to the prod DB", "--blocks", "T-1", "--refs", "T-2")
        self.assertIn("blocks: T-1 todo; touches: T-2 todo",
                      "; ".join(run(*self.t, "context", "A-01").splitlines()))
        self.assertIn("[waits on A-01] ", run(*self.t, "index"))
        got = data(self.s)
        self.assertEqual([(d["label"], [x["id"] for x in d["items"]]) for d in got["records"]["A-01"]["deps"]],
                         [("blocks", ["T-1"]), ("touches", ["T-2"])])
        self.assertEqual(got["actions"][0]["gate"], "blocks T-1")  # Tracker.gate, as the index's

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
        self.tr().readme.rewrite(self.root.joinpath("README.md").read_text().replace(
            "When a feedback ticket's PR opens, add one reply action per story.", long))
        text = views.instructions(self.tr())
        self.assertLess(len(text), views.BRIEF_INSTRUCTIONS_CHARS + 200)
        self.assertTrue(text.endswith("… the rest: `tracker show tracker --section instructions`"))


class Values(unittest.TestCase):
    """Each value has one form whatever its source: a level on a scale, a UTC time in an `_at` key, a day in `due`. A
    status is one of its kind's own words; closing a record stamps `closed_at`."""

    def setUp(self):
        self.s, self.t = new_tracker("Work")
        run(*self.t, "new", "T-1", "--title", "One")
        self.root = store.HOME / self.s

    def tr(self) -> model.Tracker:
        return model.Tracker(self.root)

    def test_a_scale_names_its_levels(self):
        for key, scale in schema.SCALES.items():
            self.assertEqual(len(scale.names), len(scale.levels))
            self.assertEqual(schema.KEYS["ticket"][key][0], "set")  # `tracker set` takes it, from the entry alone
            self.assertEqual(schema.ISSUE_FIELDS[key], key)  # so does `tracker issue --<key>`
        run(*self.t, "set", "T-1", "priority=0", "size=4")
        t = self.tr().lookup("T-1")
        self.assertEqual([model.level_name(t, k) for k in ("priority", "size")], ["P0", "L"])
        run(*self.t, "set", "T-1", "size=")
        self.assertEqual(model.level_name(self.tr().lookup("T-1"), "size"), "")

    def test_the_views_show_the_levels_and_ready_puts_the_urgent_first(self):
        run(*self.t, "new", "T-2", "--title", "Two", "--priority", "1", "--size", "3")
        run(*self.t, "new", "T-3", "--title", "Three", "--priority", "3")
        self.assertIn("priority: P1 · size: M", run(*self.t, "context", "T-2"))
        ready = run(*self.t, "ready").splitlines()
        self.assertEqual([x.split()[0] for x in ready], ["T-2", "T-3", "T-1"])  # no priority: last
        self.assertIn("· priority P1 · size M", ready[0])
        self.assertIn({"label": "priority", "text": "P1", "code": False}, data(self.s)["records"]["T-2"]["facts"])

    def test_every_kind_reads_one_lifecycle(self):
        self.assertEqual(schema.STATUSES["ticket"], ("todo", "in-progress", "done", "dropped"))  # the PR adds the rest
        run(*self.t, "act", "Ask Sam")
        run(*self.t, "act", "A-01", "--drop")
        a = self.tr().lookup("A-01")
        self.assertEqual((a.closed, a.dropped, a.started), (True, True, False))  # read as a ticket's would be

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
        self.assertRegex(self.tr().lookup("A-01").get("closed_at"), utc)
        self.tr().lookup("T-1").save({"started_at": "last week"})
        self.assertIn("T-1: started_at 'last week' is not a time", "\n".join(contract.check(self.tr())[0]))
        self.tr().lookup("T-1").save({"started_at": None, "status": "in-progress", "closed_at": "2026-09-01T00:00:00Z"})
        self.assertIn("T-1: closed_at, but its status in-progress is open", "\n".join(contract.check(self.tr())[1]))
        self.tr().readme.save({"schema": 3})
        run("list")
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
        self.assertIn("T-2: due 'soon' is not a day (YYYY-MM-DD)", contract.check(self.tr())[0])

    def test_migrate_names_keys_by_their_form(self):
        run(*self.t, "decide", "Auth scheme", "--resolve", "OAuth")
        run(*self.t, "act", "Ask Sam about v1")
        run(*self.t, "act", "A-01", "--done")
        tr = self.tr()
        for r in (tr.lookup("T-1"), tr.lookup("D-01"), tr.lookup("A-01")):
            old = {new: name for name, new in schema.RENAMED_KEYS[r.kind].items()}
            keys = {k: v for k, v in r.meta.items() if k in old or k == "closed_at"}
            r.save({**{k: None for k in keys}, **{old[k]: "2026-09-01" for k in keys if k in old}})
        tr.lookup("T-1").save({"issue_created": "2026-08-30T10:00:00Z"})
        readme = tr.readme
        readme.save({"created_at": None, "created": "2026-08-01", "schema": 2})
        run("list")
        tr = self.tr()
        t, d, a = tr.lookup("T-1"), tr.lookup("D-01"), tr.lookup("A-01")
        self.assertEqual((t.get("updated_at"), t.get("issue_created_at")), ("2026-09-01", "2026-08-30T10:00:00Z"))
        self.assertEqual((d.get("created_at"), d.get("closed_at")), ("2026-09-01", schema.today()))  # Resolution's day
        self.assertEqual(a.get("closed_at"), "2026-09-01")  # an action's `updated` was when it closed
        self.assertEqual((tr.meta.get("created_at"), tr.schema), ("2026-08-01", schema.SCHEMA))
        self.assertEqual(contract.check(tr), ([], ["README.md: ## Context lists nothing"]))


class DecisionOptions(unittest.TestCase):
    """A decision's ## Options holds only its choices, which the view offers as answers; the rest is in ## Notes."""

    def options(self, s: str, ident: str = "D-01") -> str:
        return markdown.section(model.tracker_at(s).lookup(ident).body, "Options")

    def test_choices_notes_and_the_recommended_one(self):
        s, t = new_tracker("Options")
        run(*t, "decide", "Which store", "--option", "Postgres — a migration", "--option", "SQLite",
            "--recommend", "SQLite", "--note", "Joins matter")
        self.assertEqual(self.options(s), "- Postgres — a migration\n- (recommended) SQLite")
        notes = markdown.section(model.tracker_at(s).lookup("D-01").body, "Notes")
        self.assertRegex(notes, r"^- \d{4}-\d{2}-\d{2}: Joins matter$")
        run(*t, "decide", "D-1", "--option", "postgres", "--recommend", "Redis")  # a choice it has is not added again
        self.assertEqual(self.options(s), "- Postgres — a migration\n- SQLite\n- (recommended) Redis")
        self.assertIn("Updated D-01: recommends Redis", run(*t, "history"))
        for bad in ("2026-01-02: a fact", "one\ntwo", "(recommended) Kafka"):
            with self.subTest(bad=bad):
                self.assertIn("a choice is one line", run(*t, "decide", "D-1", "--option", bad, code=2))
        self.assertIn("more than one choice is recommended",
                      run(*t, "add", "D-1", "options", "(recommended) Kafka", code=2))
        run(*t, "add", "D-1", "options", "Kafka")
        self.assertIn("- Kafka", self.options(s))

    def warned(self, s: str) -> list[str]:
        return [w for w in contract.check(model.tracker_at(s))[1] if "a decision for a human" in w]

    def test_check_warns_on_a_bare_question_for_a_human(self):
        s, t = new_tracker("Bare")
        said = run(*t, "decide", "Which store")
        self.assertEqual(self.warned(s), ['D-01: a decision for a human needs its choices, each with its cost, and one '
                                          'recommended: `tracker decide D-01 --option "<choice> — <cost>" '
                                          '--recommend "<choice>"`'])
        self.assertIn("D-01: a decision for a human needs its choices", said)  # the write that left it bare says so
        run(*t, "decide", "D-1", "--option", "Postgres — a migration", "--option", "SQLite")
        self.assertEqual(self.warned(s), ['D-01: a decision for a human needs its choices, each with its cost, and one '
                                          'recommended: `tracker decide D-01 --recommend "<choice>"`'])
        run(*t, "decide", "Which queue", "--owner", "Sam", "--option", "SQS", "--recommend", "SQS")
        self.assertEqual([w.split(":")[0] for w in self.warned(s)], ["D-01", "D-02"])  # one recommended, alone
        self.assertIn('`tracker decide D-02 --option "<choice> — <cost>"`', self.warned(s)[1])

    def test_a_complete_question_is_not_warned(self):
        s, t = new_tracker("Complete")
        run(*t, "decide", "Which store", "--option", "Postgres — a migration", "--option", "SQLite — no joins",
            "--recommend", "SQLite")
        self.assertEqual(self.warned(s), [])

    def test_a_coordinators_question_is_not_warned(self):
        s, t = new_tracker("Triage")
        run(*t, "new", "T-1", "--title", "One")
        run(*t, "set", "T-1", "status=in-progress")
        for title, owner in (("Which store", ""), ("Auth scheme", "coordinator"), ("Queue depth", "T-1")):
            run(*t, "decide", title, *(["--owner", owner] if owner else []))
        sid = f"qb{time.monotonic_ns()}"
        watcher.set_grant(sid, True)
        session.WATCH_DIR.joinpath(s).mkdir(parents=True, exist_ok=True)
        store.write_json(watcher.state_path(s, sid), {"who": "session qb", "sid": sid, "pid": os.getpid(),
                                                      "role": "coordinator"})
        self.assertEqual(self.warned(s), [])  # triage comes first; the session on T-1 answers its own
        watcher.set_grant(sid, False)
        store.write_json(watcher.state_path(s, sid), {"who": "session qb", "sid": sid, "pid": 0, "role": "coordinator",
                                                      "ended": 1})
        self.assertEqual([w.split(":")[0] for w in self.warned(s)], ["D-01", "D-02"])  # no coordinator: a human's

    def test_a_closed_decision_is_not_warned(self):
        s, t = new_tracker("Closed")
        run(*t, "decide", "Which store", "--resolve", "S3")
        self.assertEqual(self.warned(s), [])

    def test_check_names_a_line_that_is_no_choice(self):
        s, t = new_tracker("Options")
        run(*t, "decide", "Which store", "--option", "Postgres")
        d = model.tracker_at(s).lookup("D-01")
        d.rewrite(d.path.read_text().replace("- Postgres", "- Postgres\nSome prose"))
        errors, _ = contract.check(model.tracker_at(s))
        self.assertTrue(any("D-01 ## Options: 'Some prose' is no choice" in x for x in errors), errors)
        self.assertEqual(self.warned(s), [])  # its fix would be refused until the line is fixed: the error says it
        before = tree(store.HOME / s)
        self.assertIn("fix it first", run(*t, "decide", "D-1", "--option", "SQLite", "--owner", "Sam", code=2))
        self.assertEqual(tree(store.HOME / s), before)  # refused before it wrote the owner

    def test_the_view_offers_an_open_decisions_choices(self):
        s, t = new_tracker("Options")
        run(*t, "decide", "Which store", "--owner", "Sam", "--option", "Postgres — a migration",
            "--recommend", "SQLite")
        run(*t, "decide", "Which queue")
        records = data(s)["records"]
        self.assertEqual(records["D-01"]["answer"], {"by": "Sam", "options": [
            {"text": "Postgres", "cost": "a migration", "recommended": False},
            {"text": "SQLite", "cost": "", "recommended": True}]})
        self.assertNotIn("## Options", records["D-01"]["body"])  # the choices show as answers, not as text
        self.assertEqual(records["D-02"]["answer"], {"by": "user", "options": []})
        run(*t, "decide", "D-1", "--resolve", "SQLite", "--by", "Sam")
        run(*t, "decide", "D-1", "--note", "Settled in the call")  # a note after the answer goes before it
        d = data(s)["records"]["D-01"]
        self.assertIsNone(d["answer"])
        self.assertIn("## Options", d["body"])
        self.assertEqual(markdown.headings(model.tracker_at(s).lookup("D-01").body),
                         ["Question", "Options", "Notes", "Resolution"])


class Coordinator(unittest.TestCase):
    """A session takes the coordinator role by watching a tracker `--as coordinator`; while its watch runs, an open
    decision with no owner is the coordinator's move, and a human's again once it stops."""

    def setUp(self):
        self.s, self.t = new_tracker("Coordinated")
        run(*self.t, "new", "W-1", "--title", "One")
        run(*self.t, "set", "W-1", "status=in-progress")
        run(*self.t, "decide", "Which store", "--blocks", "W-1")
        self.sid = f"co{time.monotonic_ns()}"
        # A worker session on W-1 and W-2, so that a question on them is no one's stuck item (`moves.open_items`)
        worker, now = f"wk{time.monotonic_ns()}", time.time()
        session.save_session(worker, tracker=self.s, focus=["W-1", "W-2"], cwd=BASE,
                             activity={"status": "busy", "since": now, "at": now})
        self.addCleanup(session.drop_session, worker)

    def tr(self) -> model.Tracker:
        return model.tracker_at(self.s)

    def watch(self, *flags: str, code: int = 0) -> str:
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": self.sid}):
            return run("watch", self.s, "--once", *flags, code=code)

    def test_a_session_takes_the_role(self):
        self.assertFalse(session.coordinated(self.tr()))
        self.watch("--as", "coordinator")
        self.assertEqual([w["role"] for w in watcher.watches_of(self.tr())], ["coordinator"])
        self.assertTrue(session.coordinated(self.tr()))  # between two `--once` runs, it still watches
        self.assertIn("move: coordinator — D-01 open", run(*self.t, "context", "W-1"))

    def test_a_refused_watch_gives_no_grant(self):
        other = subprocess.Popen(["sleep", "30"])
        try:
            watcher.state_path(self.s, self.sid).parent.mkdir(parents=True, exist_ok=True)
            store.write_json(watcher.state_path(self.s, self.sid), {"who": "session co", "sid": self.sid,
                                                                    "pid": other.pid, "role": "coordinator",
                                                                    "ended": time.time()})
            self.watch(code=watcher.REFUSED)  # watched already: its first watch runs on
            self.assertFalse(watcher.granted(self.sid))
        finally:
            other.kill()
            other.wait()
        self.assertFalse(session.coordinated(self.tr()))  # stopped, and no refused restart kept it

    def test_a_terminal_cannot_take_the_role(self):
        before = tree(store.HOME / self.s), tree(session.WATCH_DIR) if session.WATCH_DIR.exists() else {}
        self.assertIn("--as coordinator: a session takes the role, not a terminal",
                      run("watch", self.s, "--once", "--as", "coordinator", code=2))
        self.assertEqual((tree(store.HOME / self.s), tree(session.WATCH_DIR) if session.WATCH_DIR.exists() else {}),
                         before)

    def test_a_dead_coordinator_hands_back_to_a_human(self):
        watcher.set_grant(self.sid, True)
        dead = subprocess.Popen(["true"])
        dead.wait()
        path = session.WATCH_DIR / self.s / f"{self.sid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        for ended, coordinated, move in ((time.time() - session.REARM_S - 1, False, "human — D-01 open"),
                                         (time.time() - 5, True, "coordinator — D-01 open")):
            with self.subTest(ended=ended):
                store.write_json(path, {"who": "session co", "sid": self.sid, "pid": dead.pid, "role": "coordinator",
                                        "ended": ended})
                self.assertEqual(session.coordinated(self.tr()), coordinated)
                self.assertIn(f"move: {move}", run(*self.t, "context", "W-1"))
        store.write_json(path, {"who": "session co", "sid": self.sid, "pid": "x", "role": "coordinator", "ended": "y"})
        self.assertFalse(session.coordinated(self.tr()))  # a damaged state: no coordinator, and nothing breaks
        self.assertIn("move: human — D-01 open", run(*self.t, "context", "W-1"))
        self.assertEqual(watcher.watches_of(self.tr()), [])
        self.assertIn('"move"', run("data", self.s))

    def test_the_coordinators_first_summary_knows_its_role(self):
        line = next(x for x in self.watch("--as", "coordinator").splitlines() if "W-1 " in x)
        self.assertRegex(line, r"^\d\d:\d\d W-1 in-progress · move: coordinator — D-01 open")
        other = f"plain{time.monotonic_ns()}"
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": other}):
            watcher.set_grant(self.sid, False)  # the coordinator has stopped
            line = next(x for x in run("watch", self.s, "--once").splitlines() if "W-1 " in x)
        self.assertRegex(line, r"^\d\d:\d\d ! W-1 in-progress · move: human — D-01 open")

    def test_a_restarted_watch_drops_a_role_it_was_not_given(self):
        self.watch("--as", "coordinator")
        again, now = watcher.Watcher(self.tr(), self.sid), time.time()  # the same session, given no role
        again.claim(now)
        self.assertNotIn("role", watcher.load_state(self.s, self.sid))  # from its claim on
        again.release(now)
        self.assertEqual([w["role"] for w in watcher.watches_of(self.tr())], [""])
        self.assertNotIn("role", watcher.load_state(self.s, self.sid))
        self.assertFalse(session.coordinated(self.tr()))

    def test_the_view_never_names_the_owner_as_decider(self):
        run(*self.t, "new", "W-2", "--title", "Two")
        run(*self.t, "set", "W-2", "status=in-progress")
        run(*self.t, "new", "W-3", "--title", "Three")  # not started: no session works on it
        for owner, by, move in (("coordinator", "user", "human: D-01 open"),
                                ("W-2", "user", "W-2's session: D-01 open"), ("W-3", "user", "human: D-01 open"),
                                ("Sam (PM)", "user", "Sam (PM): D-01 open"), ("Sam", "Sam", "Sam: D-01 open")):
            with self.subTest(owner=owner):
                model.tracker_at(self.s).lookup("D-01").save({"owner": owner})  # "Sam (PM)": `decide` refuses it
                d = data(self.s)["records"]["D-01"]
                self.assertEqual(d["answer"]["by"], by)
                self.assertEqual((d["move"]["text"], d["move"]["human"]), (move, owner != "W-2"))
                with mock.patch.dict(os.environ, {"TRACKER_VIA": "view"}):  # the view's answer is never refused
                    run(*self.t, "decide", "D-1", "--resolve", "x", f"--by={by}")
                model.tracker_at(self.s).lookup("D-01").change({"status": "open", "closed_at": None})

    def test_passing_a_question_to_a_human_is_logged(self):
        run(*self.t, "decide", "D-1", "--owner", "Sam")
        updated = [e for e in model.read_events(self.tr()) if e["kind"] == "decision.updated"]
        self.assertEqual([(e["owner"], e["text"]) for e in updated], [("Sam", "Updated D-01: owner Sam")])
        before = tree(store.HOME / self.s)
        for same in ("Sam", " Sam "):
            self.assertIn("D-01's owner is Sam already", run(*self.t, "decide", "D-1", "--owner", same, code=2))
        for bad in ("Sam (PM)", "Sam: PM"):  # the view answers as the owner: `--by` must hold it
            self.assertIn('--owner takes no ": " or ")"', run(*self.t, "decide", "D-1", "--owner", bad, code=2))
            self.assertIn('--owner takes no ": " or ")"', run(*self.t, "decide", "New one", "--owner", bad, code=2))
        self.assertEqual(tree(store.HOME / self.s), before)
        self.assertEqual(len([e for e in model.read_events(self.tr()) if e["kind"] == "decision.updated"]), 1)
        run(*self.t, "decide", "D-1", "--owner", "Sam", "--note", "asked")
        self.assertEqual(model.read_events(self.tr())[-1]["text"], "Updated D-01: asked")  # the owner did not change
        self.assertNotIn("owner", model.read_events(self.tr())[-1])
        run(*self.t, "decide", "Which queue", "--owner", " Kim\n Lee ")  # one line, as the view gives it as `--by`
        self.assertEqual((self.tr().lookup("D-02").get("owner"), model.read_events(self.tr())[-1]["owner"]),
                         ("Kim Lee", "Kim Lee"))
        run(*self.t, "decide", "D-2", "--owner", " Kim\n Ng ")
        self.assertEqual((self.tr().lookup("D-02").get("owner"), model.read_events(self.tr())[-1]["owner"]),
                         ("Kim Ng", "Kim Ng"))
        before = tree(store.HOME / self.s)  # one way to pass it on, and it is logged
        self.assertIn("owner: use `tracker decide`", run(*self.t, "set", "D-1", "owner=Kim", code=2))
        self.assertEqual(tree(store.HOME / self.s), before)


AGENT, VIEW, TERMINAL = {"TRACKER_SESSION": "s1"}, {"TRACKER_VIA": "view"}, {}


class WhoDecided(unittest.TestCase):
    """A resolution records who decided and how it was entered (R9): a human in the view, a human through an agent, an
    agent from a source it names, or a terminal, which is never taken for a human."""

    def setUp(self):
        self.s, self.t = new_tracker("Who")
        run(*self.t, "decide", "Auth scheme")

    def decide(self, env: dict, *args: str, code: int = 0) -> str:
        with mock.patch.dict(os.environ, env):
            return run(*self.t, "decide", *args, code=code)

    def resolved(self, env: dict, *flags: str) -> tuple[str, dict]:
        """D-01 resolved with JWT: its resolution line, and the event that settled it."""
        self.decide(env, "D-1", "--resolve", "JWT", *flags)
        tr = model.tracker_at(self.s)
        d = tr.lookup("D-01")
        self.assertEqual(model.answer(d), "JWT")  # the answer reads back without who
        return model.resolution(d), model.read_events(tr)[-1]

    def unchanged(self):
        """The tracker's files and the agent session's file, as they are now."""
        return tree(store.HOME / self.s), session.load_session("s1")

    def test_a_human_in_the_view(self):
        line, e = self.resolved(VIEW, "--by", "user")
        self.assertEqual(line, f"{schema.today()} (user, in the view): JWT")
        self.assertEqual((e["kind"], e["decided"], e["by"], e["via"]), ("decision.decided", "human", "user", "view"))
        self.assertIn("Decided D-01 Auth scheme (user, in the view): JWT", e["text"])

    def test_the_view_with_no_name_is_a_human(self):
        line, e = self.resolved(VIEW)
        self.assertEqual(line, f"{schema.today()} (human, in the view): JWT")
        self.assertEqual((e["decided"], e["by"]), ("human", "human"))

    def test_a_human_through_an_agent(self):
        line, e = self.resolved(AGENT, "--by", "Ryan")
        self.assertEqual(line, f"{schema.today()} (Ryan, entered by an agent): JWT")
        self.assertEqual((e["decided"], e["by"], e["cited"], e["session"], e["via"]),
                         ("human", "Ryan", False, "s1", "agent"))

    def test_an_agent_from_a_source(self):
        line, e = self.resolved(AGENT, "--source", "spec section 4")
        self.assertEqual(line, f"{schema.today()} (from spec section 4, entered by an agent): JWT")
        self.assertEqual((e["decided"], e["source"]), ("source", "spec section 4"))
        self.assertNotIn("by", e)
        self.assertNotIn("cited", e)

    def test_a_terminal_is_never_a_human(self):
        line, e = self.resolved(TERMINAL, "--by", "Ryan")
        self.assertEqual(line, f"{schema.today()} (unattributed, said Ryan): JWT")
        self.assertEqual((e["decided"], e["by"], e["via"]), ("unattributed", "Ryan", "terminal"))
        run(*self.t, "decide", "Queue")
        self.decide(TERMINAL, "D-2", "--resolve", "SQS")
        tr = model.tracker_at(self.s)
        self.assertEqual(model.resolution(tr.lookup("D-02")), f"{schema.today()} (unattributed): SQS")
        e = model.read_events(tr)[-1]
        self.assertEqual(e["decided"], "unattributed")
        self.assertNotIn("by", e)

    def test_a_terminal_keeps_the_source_it_names(self):
        line, e = self.resolved(TERMINAL, "--source", "the spec")
        self.assertEqual(line, f"{schema.today()} (unattributed, from the spec): JWT")
        self.assertEqual((e["decided"], e["source"]), ("unattributed", "the spec"))

    def test_an_agent_must_say_who(self):
        before = self.unchanged()
        out = self.decide(AGENT, "D-1", "--resolve", "JWT", code=2)
        self.assertIn('say who decided: --by <name> when a human told you, or --source "<where>" when the spec, a '
                      "settled decision or the code already answers it", out)
        self.assertEqual(self.unchanged(), before)
        self.assertFalse(model.tracker_at(self.s).lookup("D-01").closed)

    def test_by_and_source_together_are_refused(self):
        before = self.unchanged()
        for env in (AGENT, VIEW, TERMINAL):
            with self.subTest(env=env):
                out = self.decide(env, "D-1", "--resolve", "JWT", "--by", "Ryan", "--source", "spec", code=2)
                self.assertIn("--by and --source: a human decided or a source did, not both", out)
        self.assertEqual(self.unchanged(), before)

    def test_who_needs_a_resolve(self):
        before = self.unchanged()
        for flag in ("--by", "--source"):
            with self.subTest(flag=flag):
                out = self.decide(AGENT, "D-1", "--note", "n", flag, "x", code=2)
                self.assertIn("--by and --source say who settled it: pass them with --resolve", out)
        self.assertEqual(self.unchanged(), before)

    def test_who_cannot_break_the_resolution_line(self):
        before = self.unchanged()
        for flag, bad in (("--source", "spec.md: R7"), ("--by", "Ryan (PM)"), ("--by", "Ryan:\nPM")):
            with self.subTest(bad=bad):
                self.assertIn('": " or ")"', self.decide(AGENT, "D-1", "--resolve", "JWT", flag, bad, code=2))
        self.assertEqual(self.unchanged(), before)
        line, e = self.resolved(AGENT, "--source", "spec.md\n  R7")  # one line
        self.assertEqual(line, f"{schema.today()} (from spec.md R7, entered by an agent): JWT")
        self.assertEqual(e["source"], "spec.md R7")

    def test_a_refused_resolve_changes_nothing(self):
        run(*self.t, "new", "T-1", "--title", "One")
        before = self.unchanged()
        self.decide(AGENT, "D-1", "--note", "n", "--option", "JWT", "--owner", "Sam", "--refs", "T-1",
                    "--question", "Which?", "--resolve", "x", code=2)
        self.assertEqual(self.unchanged(), before)

    def test_decided_on_the_spot_follows_the_same_rules(self):
        before = self.unchanged()
        self.decide(AGENT, "Queue", "--resolve", "SQS", code=2)
        self.assertEqual(self.unchanged(), before)
        self.assertIsNone(model.tracker_at(self.s).lookup("D-02"))
        self.decide(AGENT, "Queue", "--resolve", "SQS", "--by", "Ryan")
        self.assertEqual(model.resolution(model.tracker_at(self.s).lookup("D-02")),
                         f"{schema.today()} (Ryan, entered by an agent): SQS")


class CitedWord(unittest.TestCase):
    """A resolution entered on a human's word cites the message that word came in (R18): the hooks keep a fingerprint
    and an excerpt of the human's last few messages and answers with the session, and `decide --cite` copies the one
    that holds the quote into the resolution's event."""

    STORE = {"questions": [{"question": "Which store for the app?",
                            "options": [{"label": "Postgres"}, {"label": "SQLite"}]}]}

    def setUp(self):
        self.s, self.t = new_tracker("Cited")
        self.root = store.HOME / self.s
        run(*self.t, "new", "T-1", "--title", "One")
        run(*self.t, "decide", "Which store")
        self.work = repo("main")
        self.sid = f"cw{time.monotonic_ns()}"
        self.addCleanup(session.drop_session, self.sid)
        self.agent = {"TRACKER_SESSION": self.sid}
        with mock.patch.dict(os.environ, self.agent):
            run("start", self.s, cwd=self.work)

    def prompt(self, text: str) -> None:
        hook("prompt", self.sid, self.work, prompt=text)

    def decide(self, *flags: str, env: dict | None = None, code: int = 0) -> str:
        with mock.patch.dict(os.environ, self.agent if env is None else env):
            return run(*self.t, "decide", "D-1", "--resolve", "Postgres", *flags, code=code)

    def settled(self) -> tuple[str, dict]:
        tr = model.tracker_at(self.s)
        return model.resolution(tr.lookup("D-01")), model.read_events(tr)[-1]

    def words(self) -> list[dict]:
        return session.load_session(self.sid).get("words", [])

    def unchanged(self):
        return tree(self.root), session.load_session(self.sid)

    def test_a_resolution_cites_the_message_that_holds_the_quote(self):
        self.prompt("Use Postgres for the store, not SQLite")
        self.decide("--by", "Ryan", "--cite", "use  POSTGRES")
        line, e = self.settled()
        self.assertEqual(line, f"{schema.today()} (Ryan, entered by an agent on their word): Postgres")
        self.assertEqual((e["kind"], e["decided"], e["by"], e["cited"]), ("decision.decided", "human", "Ryan", True))
        kept = self.words()[-1]
        self.assertEqual(e["word"], kept)
        self.assertEqual(set(kept), {"fingerprint", "at", "excerpt"})
        self.assertEqual(kept["excerpt"], "Use Postgres for the store, not SQLite")
        self.assertEqual(kept["fingerprint"],
                         hashlib.sha256(b"Use Postgres for the store, not SQLite").hexdigest()[:12])
        self.assertIsNotNone(schema.utc_seconds(kept["at"]))

    def test_the_newest_message_that_holds_the_quote_is_cited(self):
        self.prompt("Postgres or SQLite? Let me think")
        self.prompt("Something else")
        self.prompt("Go with postgres")
        self.prompt("Unrelated")
        self.decide("--by", "Ryan", "--cite", "postgres")
        self.assertEqual(self.settled()[1]["word"]["excerpt"], "Go with postgres")

    def test_a_session_that_kept_no_word_says_to_leave_cite_out(self):
        out = self.decide("--by", "Ryan", "--cite", "postgres", code=2)
        self.assertIn("leave --cite out", out)
        self.assertNotIn("Quote a few words", out)  # no quote can match

    def test_a_quote_no_kept_message_holds_refuses_and_changes_nothing(self):
        self.prompt("Use Postgres for the store")
        before = self.unchanged()
        out = self.decide("--by", "Ryan", "--cite", "use mysql", code=2)
        self.assertIn("--cite", out)
        self.assertIn("leave --cite out to enter the resolution uncited", out)
        self.assertEqual(self.unchanged(), before)
        for quote in ("", "  "):  # nothing to look for
            with self.subTest(quote=quote):
                self.decide("--by", "Ryan", "--cite", quote, code=2)
                self.assertEqual(self.unchanged(), before)

    def test_without_cite_the_resolution_is_uncited(self):
        self.prompt("Use Postgres for the store")
        self.decide("--by", "Ryan")
        line, e = self.settled()
        self.assertEqual(line, f"{schema.today()} (Ryan, entered by an agent): Postgres")
        self.assertFalse(e["cited"])
        self.assertNotIn("word", e)

    def test_cite_needs_by_and_an_agent(self):
        self.prompt("Use Postgres for the store")
        before = self.unchanged()
        for flags, env in ((["--source", "the spec", "--cite", "postgres"], None),
                           (["--cite", "postgres"], None),
                           (["--by", "Ryan", "--cite", "postgres"], {}),  # a terminal: no session's words
                           # the view, opened from the agent's shell: a human answers there, not an agent
                           (["--by", "Ryan", "--cite", "postgres"], {**self.agent, "TRACKER_VIA": "view"})):
            with self.subTest(flags=flags, env=env):
                self.assertIn("--cite", self.decide(*flags, env=env, code=2))
                self.assertEqual(self.unchanged(), before)
        self.assertIn("leave --cite out", self.decide("--by", "Ryan", "--cite", "postgres",
                                                      env={**self.agent, "TRACKER_VIA": "view"}, code=2))
        with mock.patch.dict(os.environ, self.agent):
            out = run(*self.t, "decide", "D-1", "--note", "n", "--cite", "postgres", code=2)
        self.assertIn("--cite", out)
        self.assertEqual(self.unchanged(), before)
        for env in ({}, {**self.agent, "TRACKER_VIA": "view"}):  # no agent: no flag added makes it work
            with self.subTest(env=env), mock.patch.dict(os.environ, env):
                out = run(*self.t, "decide", "D-1", "--note", "n", "--cite", "postgres", code=2)
                self.assertIn("leave --cite out", out)
                self.assertNotIn("--resolve", out)
        self.assertEqual(self.unchanged(), before)

    def test_only_the_last_five_are_kept_and_only_the_excerpt(self):
        long = "Postgres " + "x" * 491
        for i in range(5):
            self.prompt(f"Message {i}")
        self.prompt(long)
        kept = self.words()
        self.assertEqual(len(kept), 5)
        self.assertEqual([w["excerpt"] for w in kept[:-1]], [f"Message {i}" for i in range(1, 5)])
        self.assertEqual(kept[-1]["excerpt"], long[:200])
        self.assertNotIn(long, session.session_path(self.sid).read_text())
        self.decide("--by", "Ryan", "--cite", "message 0", code=2)  # the oldest went
        self.decide("--by", "Ryan", "--cite", "message 1")

    def test_an_answer_is_a_word_too(self):
        answer = {**self.STORE, "answers": {"Which store for the app?": "Postgres"}}
        tool("AskUserQuestion", self.sid, self.work, tool_input=self.STORE, tool_response=answer)
        self.assertEqual([w["excerpt"] for w in self.words()], ["Postgres"])  # the answer, never the question
        self.decide("--by", "Ryan", "--cite", "postgres")
        self.assertEqual(self.settled()[1]["word"]["excerpt"], "Postgres")
        two = {"questions": [*self.STORE["questions"], {"question": "Which queue?", "options": [{"label": "SQS"}]}],
               "answers": {"Which store for the app?": "SQLite", "Which queue?": "SQS"}}
        tool("AskUserQuestion", self.sid, self.work, tool_input=two, tool_response=two)
        self.assertEqual(self.words()[-1]["excerpt"], "SQLite; SQS")
        for name, response in (("AskUserQuestion", {}), ("AskUserQuestion", {"tool_response": "Postgres"}),
                               ("AskUserQuestion", {"tool_response": {"answers": ["Postgres"]}}),
                               ("AskUserQuestion", {"tool_response": {"answers": {"Store?": {"answers": ["Kafka"]}}}}),
                               ("request_user_input", {"tool_response": {"answers": {"db": {"answers": ["Kafka"]}}}}),
                               ("request_user_input", {"tool_response": {"answers": {"db": "Kafka"}}})):
            with self.subTest(name=name, response=response):  # another shape, another host's tool: nothing to keep
                tool(name, self.sid, self.work, tool_input=self.STORE, **response)
                self.assertEqual(len(self.words()), 2)

    def test_a_message_with_a_broken_character_is_kept_and_the_hook_goes_on(self):
        self.prompt("Use Postgres \ud83d for the store")  # half an emoji, as JSON can carry it
        self.assertEqual(self.words()[-1]["excerpt"], "Use Postgres ? for the store")  # the log can hold it
        self.assertEqual(session.load_session(self.sid)["prompts"], 1)  # the rest of the hook ran
        self.decide("--by", "Ryan", "--cite", "use postgres")
        self.assertEqual(self.settled()[1]["word"]["excerpt"], "Use Postgres ? for the store")

    def test_a_slash_command_is_no_word(self):
        for text in ("/work-tracker:tracker index", "$work-tracker:tracker index", "  /compact", ""):
            self.prompt(text)
        self.assertEqual(self.words(), [])
        for text in ("$5 a month is fine, go with the paid tier", "/tmp/data is where it goes", "$HOME is wrong"):
            self.prompt(text)
        self.assertEqual([w["excerpt"][:6] for w in self.words()], ["$5 a m", "/tmp/d", "$HOME "])

    def test_an_approval_or_a_subagent_keeps_no_word(self):
        go = {"questions": [{"question": "Push?", "options": [{"label": "Yes, push"}, {"label": "Not now"}]}]}
        tool("AskUserQuestion", self.sid, self.work, tool_input=go, tool_response={**go, "answers": {"Push?": "Yes"}})
        answer = {**self.STORE, "answers": {"Which store for the app?": "Postgres"}}
        tool("AskUserQuestion", self.sid, self.work, agent_id="a1", tool_input=self.STORE, tool_response=answer)
        self.assertEqual(self.words(), [])

    def test_a_session_on_no_tracker_keeps_no_word(self):
        hook("waiting", self.sid, self.work, tool_name="AskUserQuestion")
        hook("waiting", self.sid, self.work, tool_name="Bash", agent_id="sub")
        with mock.patch.dict(os.environ, self.agent):
            run("start", "--clear", cwd=self.work)  # off its tracker; its file stays for the open waits
        answer = {**self.STORE, "answers": {"Which store for the app?": "Postgres"}}
        tool("AskUserQuestion", self.sid, self.work, tool_input=self.STORE, tool_response=answer)
        self.assertIn("sub", session.load_session(self.sid)["waits"])  # the subagent still asks: the file stays
        self.assertEqual(self.words(), [])

    def test_a_session_on_the_tracker_that_TRACKER_names_keeps_words_too(self):
        sid = f"cwt{time.monotonic_ns()}"
        self.addCleanup(session.drop_session, sid)
        env = {"TRACKER": self.s}
        hook("session-start", sid, self.work, env=env, source="startup")
        hook("prompt", sid, self.work, env=env, prompt="Use Postgres for the store")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run(*self.t, "decide", "D-1", "--resolve", "Postgres", "--by", "Ryan", "--cite", "postgres")
        self.assertEqual(self.settled()[1]["word"]["excerpt"], "Use Postgres for the store")

    def test_the_answer_hook_asks_for_cite_only_when_it_kept_the_answer(self):
        answer = {**self.STORE, "answers": {"Which store for the app?": "Postgres"}}
        kept = tool("AskUserQuestion", self.sid, self.work, tool_input=self.STORE, tool_response=answer)
        self.assertIn(schema.CITE, kept["hookSpecificOutput"]["additionalContext"])
        other = tool("request_user_input", self.sid, self.work, tool_input=self.STORE, tool_response="SQS")
        other = other["hookSpecificOutput"]["additionalContext"]
        self.assertIn("direction decision", other)
        self.assertNotIn("--cite", other)  # nothing kept: a citation of it would be refused
        self.assertNotIn("both", other)  # what --source replaces reads right with --cite and without

    def test_a_refused_tracker_is_left_as_it_is(self):
        for refuse in (NewerFormat.newer, NewerFormat.unmigrated):
            with self.subTest(refuse.__name__):
                self.setUp()
                refuse(self, self.s)
                before = tree(self.root)
                self.prompt("Use Postgres for the store")  # `hook` checks that it exits 0
                answer = {**self.STORE, "answers": {"Which store for the app?": "Postgres"}}
                tool("AskUserQuestion", self.sid, self.work, tool_input=self.STORE, tool_response=answer)
                self.assertEqual(tree(self.root), before)

    def test_words_survive_tracker_start(self):
        self.prompt("Use Postgres for the store")
        with mock.patch.dict(os.environ, self.agent):
            run("start", self.s, "--on", "T-1", cwd=self.work)
        self.decide("--by", "Ryan", "--cite", "use postgres")
        self.assertTrue(self.settled()[1]["cited"])

    def test_an_uncited_word_never_reaches_the_log(self):
        self.prompt("Use Postgres for the store")
        self.prompt("A secret aside")
        self.decide("--by", "Ryan")
        logged = (self.root / schema.EVENTS_FILE).read_text() + (self.root / "log.md").read_text()
        for w in self.words():
            self.assertNotIn(w["excerpt"], logged)
            self.assertNotIn(w["fingerprint"], logged)


class Handoffs(unittest.TestCase):
    """A ticket's plan or built code handed to its reviewer (`tracker submit`), and the review that answers it
    (`tracker review`): events on the ticket, and no file."""

    def setUp(self):
        self.s, self.t = new_tracker("Handoffs")
        self.root = store.HOME / self.s
        run(*self.t, "new", "T-1", "--title", "One")
        run(*self.t, "set", "T-1", "status=in-progress")
        run(*self.t, "new", "T-2", "--title", "Two")
        run(*self.t, "set", "T-2", "status=done")
        run(*self.t, "new", "T-3", "--title", "Three")
        run(*self.t, "decide", "Which store")

    def last(self, kind: str = "") -> dict:
        return [e for e in model.read_events(model.Tracker(self.root)) if not kind or e["kind"] == kind][-1]

    def events(self) -> list[dict]:
        return model.read_events(model.Tracker(self.root))

    def test_a_submission_is_an_event_on_its_ticket(self):
        files = set(tree(self.root))
        self.assertEqual(run(*self.t, "submit", "T-1", "--what", "plan", "--note", "see Plan").splitlines()[0],
                         "submitted T-1's plan")
        e = self.last()
        self.assertEqual((e["kind"], e["refs"], e["what"], e["text"]),
                         ("submission", ["T-1"], "plan", "T-1 plan ready for review: see Plan"))
        run(*self.t, "submit", "T-1", "--what", "code")
        self.assertEqual(self.last()["text"], "T-1 built code ready for the plan conformance check")
        self.assertEqual(set(tree(self.root)), files)  # no file added

    def test_a_review_answers_the_open_submission(self):
        run(*self.t, "submit", "T-1", "--what", "plan")
        s = self.last()["seq"]
        said = run(*self.t, "review", "T-1", "--what", "plan", "--verdict", "changes", "--finding", "a",
                   "--finding", "b")
        self.assertEqual(said.splitlines()[0], "reviewed T-1's plan: changes, 2 findings")
        e = self.last()
        self.assertEqual((e["kind"], e["of"], e["verdict"], e["what"]), ("review", s, "changes", "plan"))
        self.assertEqual(e["findings"], [{"n": 1, "text": "a"}, {"n": 2, "text": "b"}])
        self.assertTrue(e["text"].startswith("T-1 plan review: changes, 2 findings"), e["text"])
        self.assertIn("1. a; 2. b", e["text"])  # a worker reads them in the log
        run(*self.t, "submit", "T-1", "--what", "code")
        run(*self.t, "review", "T-1", "--what", "code", "--verdict", "pass")
        self.assertEqual(self.last()["text"], "T-1 plan conformance check: pass")

    def test_a_review_with_no_submission_is_recorded(self):
        run(*self.t, "review", "T-1", "--what", "code", "--verdict", "pass")
        self.assertEqual((self.last()["kind"], self.last()["of"]), ("review", 0))

    def test_a_review_answers_only_its_own_kind(self):
        run(*self.t, "submit", "T-1", "--what", "plan")
        s = self.last()
        run(*self.t, "review", "T-1", "--what", "code", "--verdict", "pass")
        self.assertEqual(self.last()["of"], 0)
        self.assertEqual(moves.last_submission(self.events(), "T-1", "plan"), s)
        self.assertIsNone(moves.last_submission(self.events(), "T-2", "plan"))

    def test_a_later_submission_replaces_an_earlier_one(self):
        run(*self.t, "submit", "T-1", "--what", "plan")
        run(*self.t, "submit", "T-1", "--what", "plan", "--note", "again")
        second = self.last()["seq"]
        run(*self.t, "review", "T-1", "--what", "plan", "--verdict", "pass")
        self.assertEqual(self.last()["of"], second)
        self.assertIsNone(moves.last_submission(self.events(), "T-1", "plan"))
        self.assertEqual(moves.last_review(self.events(), "T-1", "plan"), self.last())

    def test_marking_findings_fixed(self):
        run(*self.t, "review", "T-1", "--what", "plan", "--verdict", "changes", "--finding", "a", "--finding", "b",
            "--finding", "c")
        r = self.last()["seq"]
        said = run(*self.t, "review", "T-1", "--what", "plan", "--fixed", "2,3")
        self.assertEqual(said.splitlines()[0], "marked findings 2, 3 of T-1's plan review fixed")
        e = self.last()
        self.assertEqual((e["kind"], e["of"], e["findings"], e["what"], e["text"]),
                         ("finding.fixed", r, [2, 3], "plan", "T-1 fixed findings 2, 3 of the plan review"))

    def test_each_refusal_changes_nothing(self):
        run(*self.t, "review", "T-1", "--what", "code", "--verdict", "changes", "--finding", "a", "--finding", "b")
        run(*self.t, "review", "T-3", "--what", "plan", "--verdict", "changes", "--finding", "a")
        before = tree(self.root)
        refusals = [
            (["submit", "D-01", "--what", "plan"], "D-01 is a decision: `tracker decide D-01 --note \"...\"`"),
            (["review", "D-01", "--what", "plan", "--verdict", "pass"],
             "D-01 is a decision: `tracker decide D-01 --note \"...\"`"),
            (["submit", "T-2", "--what", "plan"], "T-2 is closed: a closed ticket takes no submission or review"),
            (["review", "T-2", "--what", "plan", "--verdict", "pass"],
             "T-2 is closed: a closed ticket takes no submission or review"),
            (["review", "T-1", "--what", "plan", "--verdict", "changes"],
             "a review that asks for changes lists them: one --finding per change"),
            (["review", "T-1", "--what", "plan", "--verdict", "changes", "--finding", "a", "--finding", "  "],
             "a finding says what to change: finding 2 is empty"),
            (["review", "T-1", "--what", "plan", "--verdict", "changes", "--finding", " ", "--finding", ""],
             "a finding says what to change: findings 1, 2 are empty"),
            (["review", "T-1", "--what", "plan", "--verdict", "pass", "--finding", "a"],
             "a finding is a change asked for: use --verdict changes, or leave the finding out"),
            (["review", "T-1", "--what", "code", "--fixed", "1", "--verdict", "pass"], "--fixed"),
            (["review", "T-1", "--what", "code", "--fixed", "1", "--finding", "a"], "--fixed"),
            (["review", "T-1", "--what", "plan", "--fixed", "1"], "T-1 has no plan review"),
            (["review", "T-1", "--what", "code", "--fixed", "3"], "findings 1, 2"),
            (["review", "T-1", "--what", "code", "--fixed", "x"], "findings 1, 2"),
            (["review", "T-1", "--what", "code"], "--verdict"),
            (["review", "T-3", "--what", "plan", "--fixed", "2"], "T-3's latest plan review has finding 1: --fixed "
                                                                  "takes that number"),
        ]
        for args, sentence in refusals:
            with self.subTest(args=args):
                self.assertIn(sentence, run(*self.t, *args, code=2))
        self.assertEqual(tree(self.root), before)

    def test_the_history_reads_like_a_log(self):
        run(*self.t, "submit", "T-1", "--what", "plan")
        run(*self.t, "review", "T-1", "--what", "plan", "--verdict", "changes", "--finding", "a")
        run(*self.t, "review", "T-1", "--what", "plan", "--fixed", "1")
        lines = run(*self.t, "history", "--ref", "T-1").splitlines()[-3:]
        self.assertEqual([x.split(" ", 2)[2] for x in lines],
                         ["T-1 plan ready for review", "T-1 plan review: changes, 1 finding: 1. a",
                          "T-1 fixed finding 1 of the plan review"])


class HumanWaits(unittest.TestCase):
    """Each wait of a session on a human (a permission, an answer) is logged as a start and an end, by the hooks that
    keep the session's `waiting` status."""

    def setUp(self):
        self.s, self.t = new_tracker("Waits")
        self.root = store.HOME / self.s
        self.work = repo("feat/T-1")
        run(*self.t, "new", "T-1", "--title", "One", "--branch", "feat/T-1")
        run(*self.t, "set", "T-1", "status=in-progress", cwd=self.work)
        self.sid = f"w{time.monotonic_ns()}"
        self.addCleanup(session.drop_session, self.sid)
        session.save_session(self.sid, tracker=self.s, cwd=str(self.work))

    def waits(self) -> list[dict]:
        return [e for e in model.read_events(model.Tracker(self.root)) if e["kind"] in schema.QUIET_KINDS]

    def test_a_wait_is_a_start_and_an_end(self):
        hook("waiting", self.sid, self.work, tool_name="Bash")
        hook("resumed", self.sid, self.work, tool_name="Bash")
        start, end = self.waits()
        self.assertEqual((start["kind"], start["session"], start["refs"], start["asker"], start["why"]),
                         ("human.wait", self.sid, ["T-1"], "", "Bash"))
        self.assertEqual(start["text"], "Waiting on a human: Bash")
        self.assertEqual((end["kind"], end["session"], end["refs"], end["asker"], end["of"]),
                         ("human.wait.end", self.sid, ["T-1"], "", start["seq"]))
        self.assertGreaterEqual(end["seconds"], 0)
        self.assertNotIn("refused", end)
        self.assertEqual(end["text"], f"Waited {end['seconds']} s on a human")
        self.assertNotIn("waits", session.load_session(self.sid))

    def test_a_prompt_ends_an_open_wait(self):
        hook("waiting", self.sid, self.work, tool_name="AskUserQuestion")
        hook("prompt", self.sid, self.work, prompt="go")
        start, end = self.waits()
        self.assertEqual(end["of"], start["seq"])
        self.assertEqual(session.load_session(self.sid)["activity"]["status"], "busy")

    def test_a_session_end_ends_an_open_wait(self):
        hook("waiting", self.sid, self.work, tool_name="Bash")
        hook("session-end", self.sid, self.work, reason="other")
        start, end = self.waits()
        self.assertEqual(end["of"], start["seq"])

    def test_each_asker_has_its_own_wait(self):
        hook("waiting", self.sid, self.work, tool_name="Bash")
        hook("waiting", self.sid, self.work, tool_name="Bash", agent_id="a1")
        hook("resumed", self.sid, self.work, tool_name="Bash", agent_id="a1")
        mine, theirs, end = self.waits()
        self.assertEqual((mine["asker"], theirs["asker"]), ("", "a1"))
        self.assertEqual((end["kind"], end["asker"], end["of"]), ("human.wait.end", "a1", theirs["seq"]))
        self.assertEqual(session.load_session(self.sid)["activity"]["status"], "waiting")  # the session's own holds
        hook("resumed", self.sid, self.work, tool_name="Bash", agent_id="a2")  # an agent that did not ask
        self.assertEqual(len(self.waits()), 3)

    def test_asking_twice_is_one_wait(self):
        hook("waiting", self.sid, self.work, tool_name="Bash")
        hook("waiting", self.sid, self.work, tool_name="Bash")
        self.assertEqual([e["kind"] for e in self.waits()], ["human.wait"])

    def test_a_failed_tool_after_a_wait_is_recorded_as_failed(self):
        # The hook cannot tell a refused permission from an approved tool that then failed: it says what it knows.
        hook("waiting", self.sid, self.work, tool_name="Bash")
        hook("resumed", self.sid, self.work, tool_name="Bash", hook_event_name="PostToolUseFailure",
             error="Command failed with exit code 1")
        end = self.waits()[-1]
        self.assertEqual((end["failed"], end["text"]),
                         ("Command failed with exit code 1", f"Waited {end['seconds']} s on a human: the tool failed"))
        self.assertNotIn("refused", end)

    def test_a_session_on_no_tracker_writes_nothing(self):
        session.save_session(self.sid, tracker=None)
        hook("waiting", self.sid, self.work, tool_name="Bash")
        hook("resumed", self.sid, self.work, tool_name="Bash")
        self.assertEqual(self.waits(), [])
        self.assertNotIn("waits", session.load_session(self.sid))

    def moved(self, *start: str) -> list[dict]:
        """The session waits on a permission for `tracker start <start>`, which then runs, and the tool ends. The wait
        events of the tracker the wait started in."""
        hook("waiting", self.sid, self.work, tool_name="Bash")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": self.sid}):
            run("start", *start, cwd=self.work)
        hook("resumed", self.sid, self.work, tool_name="Bash")
        return self.waits()

    def test_a_wait_ends_in_the_tracker_it_started_in(self):
        other, _ = new_tracker("Elsewhere")
        start, end = self.moved(other)  # the command it asked about moves the session
        self.assertEqual((end["kind"], end["of"]), ("human.wait.end", start["seq"]))
        elsewhere = model.read_events(model.Tracker(store.HOME / other))
        self.assertEqual([e for e in elsewhere if e["kind"] in schema.QUIET_KINDS], [])
        entry = session.load_session(self.sid)
        self.assertEqual((entry["tracker"], entry["activity"]["status"], entry.get("waits")), (other, "busy", None))

    def test_a_wait_ends_when_its_command_starts_the_session_again(self):
        start, end = self.moved(self.s, "--on", "T-1")
        self.assertEqual((end["kind"], end["of"]), ("human.wait.end", start["seq"]))
        self.assertEqual([i for i in session.items_of(model.Tracker(self.root), time.time() + 60) if i.kind == "wait"],
                         [])

    def test_a_wait_ends_when_its_command_takes_the_session_off_its_tracker(self):
        for flag in ("--clear", "--decline"):
            with self.subTest(flag):
                self.setUp()
                start, end = self.moved(flag)
                self.assertEqual((end["kind"], end["of"], end["refs"]), ("human.wait.end", start["seq"], ["T-1"]))
                self.assertEqual(session.load_session(self.sid), {})  # on no tracker: no file is kept for it

    def test_a_session_that_moves_is_not_said_to_have_asked_on_its_new_tracker(self):
        other, _ = new_tracker("Elsewhere")
        hook("waiting", self.sid, self.work, tool_name="Bash")
        asked = session.load_session(self.sid)["activity"]
        session.save_session(self.sid, activity={**asked, "since": asked["since"] - 60})  # answered a minute later
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": self.sid}):
            run("start", other, cwd=self.work)
        (x,) = session.live_sessions(other)  # until the tool ends
        self.assertEqual(x.status, "waiting")
        self.assertFalse(watcher.waits(x, time.time()))  # the watch of `other` has no one asking

    def test_a_wait_left_by_a_session_that_comes_back_ends(self):
        (a, _), nogh = new_tracker("Back", "acme/back"), {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin"}
        mine, sid = repo(f"feat/{a}"), f"b{time.monotonic_ns()}"
        self.addCleanup(session.drop_session, sid)
        subprocess.run(["git", "-C", str(mine), "remote", "add", "origin", "git@github.com:acme/back.git"], check=True)
        with mock.patch.object(github, "gh", lambda *args, **kw: None):
            run("--tracker", a, "new", "U-1", "--title", "Work", "--branch", f"feat/{a}")
            run("--tracker", a, "set", "U-1", "status=in-progress", cwd=mine)
        session.save_session(sid, tracker=a, cwd=str(mine))
        hook("waiting", sid, mine, nogh, tool_name="Bash")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": sid}):
            run("start", "--clear", cwd=mine)
        hook("session-start", sid, mine, nogh, source="resume")  # the terminal closed; its branch links it again
        self.assertEqual(session.load_session(sid)["tracker"], a)
        hook("stop", sid, mine, nogh)
        start, end = [e for e in model.read_events(model.Tracker(store.HOME / a)) if e["kind"] in schema.QUIET_KINDS]
        self.assertEqual(end["of"], start["seq"])

    def test_a_session_on_no_tracker_starts_no_wait(self):
        hook("waiting", self.sid, self.work, tool_name="Bash", agent_id="a1")
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": self.sid}):
            run("start", "--clear", cwd=self.work)
        session.session_activity(self.sid, "waiting", asker="a2")  # raises nothing
        self.assertEqual(list(session.load_session(self.sid)["waits"]), ["a1"])
        (start,) = self.waits()

    def test_a_damaged_wait_in_the_session_file_stops_no_hook(self):
        waiting = {"status": "waiting", "since": 1, "at": 1}
        session.save_session(self.sid, waits={"": [3, time.time()]}, activity=waiting)  # no tracker in the entry
        hook("stop", self.sid, self.work)
        entry = session.load_session(self.sid)
        self.assertEqual((entry["activity"]["status"], entry.get("waits")), ("idle", None))
        session.save_session(self.sid, waits={"": [3, time.time()]})
        session.unlink_session(self.sid)
        self.assertEqual(session.load_session(self.sid), {})  # no wait to keep a file for

    def test_a_refused_tracker_gets_no_wait_event(self):
        for refuse in (NewerFormat.newer, NewerFormat.unmigrated):
            with self.subTest(refuse.__name__):
                self.setUp()
                refuse(self, self.s)
                before = tree(self.root)
                hook("waiting", self.sid, self.work, tool_name="Bash")  # each exits 0: `hook` checks
                self.assertEqual(session.load_session(self.sid)["activity"]["status"], "waiting")
                hook("resumed", self.sid, self.work, tool_name="Bash")
                self.assertEqual(session.load_session(self.sid)["activity"]["status"], "busy")
                hook("waiting", self.sid, self.work, tool_name="Bash")
                hook("session-end", self.sid, self.work, reason="clear")
                self.assertEqual(session.load_session(self.sid)["activity"]["status"], "ended")
                self.assertEqual(tree(self.root), before)
                after = f"{self.sid}b"
                self.addCleanup(session.drop_session, after)
                hook("session-start", after, self.work, source="clear")
                self.assertEqual(session.load_session(after)["tracker"], self.s)  # the /clear take-over found it

    def test_waits_are_quiet(self):
        with mock.patch.dict(os.environ, {"TRACKER_SESSION": self.sid}):
            run("step", "T-1", "First step", cwd=self.work)
            run("step", "T-1", "Second step", cwd=self.work)
        log = (self.root / "log.md").read_text()
        hook("waiting", self.sid, self.work, tool_name="Bash")
        hook("resumed", self.sid, self.work, tool_name="Bash")
        self.assertEqual(len(self.waits()), 2)
        self.assertEqual((self.root / "log.md").read_text(), log)
        self.assertNotIn("human", run(*self.t, "history", "--last", "0"))
        tr = model.Tracker(self.root)
        self.assertEqual(watcher.log_events(self.waits(), tr), [])
        recent = views.recent_log(tr, tr.lookup("T-1"), views.BRIEF_LOG)
        self.assertEqual([x.split("] ", 1)[1] for x in recent[2:]], ["First step", "Second step"])
        self.assertNotIn("human", json.dumps(data(self.s)["reference"]["log"]))

    def test_a_wait_does_not_read_the_event_file(self):
        def unread(tr):
            raise AssertionError("the event file was read")

        for event in ("waiting", "resumed"):
            data_ = {"session_id": self.sid, "cwd": str(self.work), "tool_name": "Bash"}
            with mock.patch.object(model, "event_lines", unread), mock.patch.dict(os.environ), \
                    mock.patch.object(hooks, "hook_input", return_value=data_):
                hooks.run_hook(event)  # a failure would exit
        self.assertEqual([e["kind"] for e in self.waits()], ["human.wait", "human.wait.end"])


NOW = 2_000_000_000.0


def event(seq: int, kind: str, refs: list[str], age: float = 0, **data) -> dict:
    """An event as the event file holds it, written `age` seconds before NOW."""
    return {"seq": seq, "at": time.strftime(schema.TIME_FORMAT, time.gmtime(NOW - age)), "kind": kind,
            "session": data.pop("session", "s0"), "refs": refs, "text": kind, **data}


class OpenItems(unittest.TestCase):
    """What waits for a reply (a submission, open findings, a question, a wait on a human), whose move each is, and
    which are stuck: worked out from the log and the clock (`moves.open_items`), never stored."""

    def setUp(self):
        self.s, self.t = new_tracker("Open")
        self.work = repo("feat/T-1")  # a branch name that says nothing of what is checked
        for ident, status in (("T-1", "in-progress"), ("T-2", "todo"), ("T-3", "done")):
            run(*self.t, "new", ident, "--title", ident)
            run(*self.t, "set", ident, f"status={status}", cwd=self.work)
        self.tr = model.tracker_at(self.s)

    def items(self, events: list[dict], coordinated: bool = True, on: set | None = None, live: dict | None = None,
              now: float = NOW) -> list[moves.Item]:
        return moves.open_items(model.tracker_at(self.s), events, now, coordinated, {"T-1"} if on is None else on,
                                live or {})

    def keys(self, *args, **kw) -> list[str]:
        return [i.key for i in self.items(*args, **kw)]

    def test_a_submission_is_open_until_reviewed(self):
        submitted = [event(1, "submission", ["T-1"], what="plan")]
        (item,) = self.items(submitted)
        self.assertEqual((item.kind, item.key, item.refs, item.what, item.at),
                         ("submission", "submission:1", ["T-1"], "T-1 plan ready for review", NOW))
        self.assertEqual(self.keys(submitted + [event(2, "review", ["T-1"], what="plan", verdict="pass", of=1)]), [])

    def test_a_second_submission_replaces_the_first(self):
        twice = [event(1, "submission", ["T-1"], what="code"), event(2, "submission", ["T-1"], what="code")]
        self.assertEqual(self.keys(twice), ["submission:2"])
        self.assertEqual(self.keys(twice + [event(3, "review", ["T-1"], what="code", verdict="pass", of=2)]), [])

    def test_a_closed_ticket_has_no_open_submission_or_findings(self):
        findings = [{"n": 1, "text": "a"}]
        self.assertEqual(self.keys([event(1, "submission", ["T-3"], what="plan")]), [])
        self.assertEqual(self.keys([event(1, "review", ["T-3"], what="plan", verdict="changes", findings=findings)]),
                         [])

    def test_findings_are_open_until_fixed_or_resubmitted(self):
        review = event(1, "review", ["T-1"], what="plan", verdict="changes", of=0,
                       findings=[{"n": 1, "text": "a"}, {"n": 2, "text": "b"}])
        (item,) = self.items([review])
        self.assertEqual((item.kind, item.key, item.what), ("findings", "findings:1", "T-1: 2 findings of the plan "
                                                                                       "review open"))
        one = event(2, "finding.fixed", ["T-1"], what="plan", of=1, findings=[1])
        self.assertEqual([i.what for i in self.items([review, one])], ["T-1: 1 finding of the plan review open"])
        self.assertEqual(self.keys([review, one, event(3, "finding.fixed", ["T-1"], what="plan", of=1,
                                                       findings=[2])]), [])
        self.assertEqual(self.keys([review, event(2, "finding.fixed", ["T-1"], what="code", of=1, findings=[1, 2])]),
                         ["findings:1"])  # another kind's fix
        self.assertEqual(self.keys([review, event(2, "submission", ["T-1"], what="plan")]), ["submission:2"])
        again = event(3, "review", ["T-1"], what="plan", verdict="changes", of=0, findings=review["findings"])
        stale = event(4, "finding.fixed", ["T-1"], what="plan", of=1, findings=[1, 2])  # names the first review
        self.assertEqual(self.keys([review, again, stale]), ["findings:3"])

    def test_a_wait_is_open_until_its_end(self):
        wait = event(1, "human.wait", ["T-1"], session="s1", asker="", why="Bash")
        live = {"s1": "worker"}
        (item,) = self.items([wait], live=live)
        self.assertEqual((item.kind, item.key, item.session, item.asker, item.what),
                         ("wait", "wait:1", "s1", "", "worker waits on a human: Bash"))
        self.assertEqual(self.keys([wait, event(2, "human.wait.end", ["T-1"], session="s1", asker="", of=0)],
                                   live=live), ["wait:1"])  # an end of no wait closes nothing
        self.assertEqual(self.keys([wait, event(2, "human.wait.end", ["T-1"], session="s1", asker="", of=1)],
                                   live=live), [])

    def test_a_later_wait_from_the_same_asker_replaces_an_earlier_one(self):
        waits = [event(1, "human.wait", [], session="s1", asker=""), event(2, "human.wait", [], session="s1",
                                                                          asker="a1"),
                 event(3, "human.wait", [], session="s1", asker="")]
        self.assertEqual(sorted(self.keys(waits, live={"s1": "worker"})), ["wait:2", "wait:3"])

    def test_a_wait_of_a_session_that_is_gone_is_not_open(self):
        self.assertEqual(self.keys([event(1, "human.wait", [], session="s1", asker="")], live={}), [])

    def test_an_open_decision_is_a_question(self):
        run(*self.t, "decide", "Which store", "--refs", "T-1")
        (item,) = self.items([], now=time.time())
        self.assertEqual((item.kind, item.key, item.refs, item.what), ("question", "question:D-01", ["T-1"],
                                                                       "D-01 open"))
        self.assertEqual(item.move.text(), "coordinator: D-01 open")

    def test_moves(self):
        live = {"s1": "worker"}
        events = [event(1, "submission", ["T-1"], what="plan"),
                  event(2, "review", ["T-2"], what="code", verdict="changes", findings=[{"n": 1, "text": "a"}]),
                  event(3, "human.wait", [], session="s1", asker="")]
        got = {i.kind: i.move for i in self.items(events, live=live)}
        self.assertEqual(got["submission"].text(), "coordinator: plan review")
        self.assertEqual(got["findings"].text(), "you: 1 finding of the plan conformance check open")
        self.assertTrue(got["wait"].human)
        self.assertEqual(got["wait"].who, "human")
        alone = {i.kind: i.move for i in self.items(events, coordinated=False, live=live)}
        self.assertEqual((alone["submission"].who, alone["submission"].human), ("human", True))

    def test_a_reply_not_given_in_time_is_stuck_and_a_humans(self):
        (item,) = self.items([event(1, "submission", ["T-1"], 1801, what="plan")])
        self.assertTrue(item.stuck)
        self.assertTrue(item.move.human)
        self.assertEqual(item.what, "coordinator has not reviewed T-1's plan for 30 min")
        (item,) = self.items([event(1, "submission", ["T-1"], 1800, what="plan")])
        self.assertEqual((item.stuck, item.move.human), (False, False))
        run(*self.t, "decide", "Which store", "--refs", "T-1")
        created = schema.utc_seconds(model.tracker_at(self.s).lookup("D-01").get("created_at"))
        (item,) = self.items([], now=created + 1801)
        self.assertEqual((item.stuck, item.move.human, item.what),
                         (True, True, "coordinator has not answered D-01 for 30 min"))

    def coordinator(self, started: float = 0) -> None:
        """A coordinator's watch of the tracker, running now; started at `started`."""
        path = watcher.state_path(self.s, "qb")
        path.parent.mkdir(parents=True, exist_ok=True)
        store.write_json(path, {"who": "session qb", "sid": "qb", "pid": os.getpid(), "role": "coordinator",
                                **({"started": started} if started else {})})
        self.addCleanup(path.unlink, True)

    def test_the_coordinators_watch_says_when_its_clock_starts(self):
        two_hours = time.strftime(schema.TIME_FORMAT, time.gmtime(time.time() - 7200))
        with store.locked(), mock.patch.object(model, "utc_now", return_value=two_hours):
            model.append_event(model.tracker_at(self.s), "submission", "T-2 plan ready for review", ["T-2"],
                               what="plan")  # T-2 has not started: only its deadline can make it stuck
        self.coordinator(started=time.time() - 60)
        (item,) = session.items_of(model.tracker_at(self.s))
        self.assertEqual((item.move.who, item.stuck), ("coordinator", False))

    def test_a_stuck_question_is_a_humans_wherever_it_shows(self):
        run(*self.t, "decide", "Which store", "--blocks", "T-1")
        self.coordinator()
        self.assertTrue(session.coordinated(model.tracker_at(self.s)))
        got = data(self.s)  # no session is on T-1, which is under way: stuck at once
        self.assertEqual(got["records"]["D-01"]["move"], got["records"]["T-1"]["move"])
        self.assertEqual((got["records"]["D-01"]["move"]["text"], got["records"]["D-01"]["move"]["human"]),
                         ("human: D-01 open, stuck with coordinator", True))  # said as a human's wherever it shows
        self.assertIn("human: D-01 open, stuck with coordinator — T-1", run(*self.t, "index"))

    def test_a_questions_clock_starts_when_it_became_that_partys(self):
        run(*self.t, "decide", "Which store", "--refs", "T-1", "--owner", "Sam")
        later = schema.utc_seconds(model.tracker_at(self.s).lookup("D-01").get("created_at")) + 7200
        with mock.patch.object(model, "utc_now", return_value=time.strftime(schema.TIME_FORMAT, time.gmtime(later))):
            run(*self.t, "decide", "D-01", "--owner", "coordinator")  # passed on two hours after it opened
        events = model.read_events(model.tracker_at(self.s))
        (item,) = self.items(events, now=later + 60)
        self.assertEqual((item.stuck, item.at), (False, later))
        self.assertTrue(self.items(events, now=later + 1801)[0].stuck)

    def test_a_coordinator_that_arrives_is_not_blamed_for_what_came_before(self):
        run(*self.t, "decide", "Which store", "--refs", "T-1")
        created = schema.utc_seconds(model.tracker_at(self.s).lookup("D-01").get("created_at"))
        submitted = [event(1, "submission", ["T-1"], 7200, what="plan")]
        arrived = NOW - 60
        items = moves.open_items(model.tracker_at(self.s), submitted, NOW, True, {"T-1"}, {}, arrived)
        self.assertEqual([(i.kind, i.stuck, i.at) for i in items],
                         [("submission", False, arrived), ("question", False, max(created, arrived))])

    def test_a_ticket_owners_clock_starts_when_the_ticket_starts_again(self):
        run(*self.t, "decide", "Which store", "--owner", "T-1")
        restarted = [event(1, "status", ["T-1"], 60, status="in-progress")]
        (item,) = self.items(restarted)
        self.assertEqual((item.move.who, item.stuck, item.at), ("T-1's session", False, NOW - 60))

    def test_a_damaged_coordinator_state_breaks_no_view(self):
        self.coordinator()
        path = watcher.state_path(self.s, "qb")
        for bad in ('"yesterday"', "1e400", "1e300"):  # no number, one JSON reads as infinity, one far off
            with self.subTest(bad=bad):
                state = json.loads(path.read_text())
                path.write_text(json.dumps({**state, "started": 0}).replace('"started": 0', f'"started": {bad}'))
                self.assertTrue(session.coordinated(model.tracker_at(self.s)))
                run(*self.t, "check")
                data(self.s)
        path.unlink()  # no watch running: the view says when the last one ended
        ended = watcher.state_path(self.s, "old")  # a watch that ended, with no grant: its end damaged
        for bad in ("1e400", "1e300"):
            with self.subTest(ended=bad):
                ended.write_text(f'{{"who": "session old", "sid": "old", "pid": 0, "started": 5, "ended": {bad}}}')
                self.addCleanup(ended.unlink, True)
                data(self.s)
        watcher.set_grant("old", True)  # one that holds the grant: an end of infinity is no watch that runs on
        self.addCleanup(watcher.set_grant, "old", False)
        for bad in ("1e400", str(time.time() * 1000)):  # infinity, and an end saved in milliseconds
            with self.subTest(granted_ended=bad):
                ended.write_text('{"who": "session old", "sid": "old", "pid": 0, "role": "coordinator", '
                                 f'"ended": {bad}}}')
                self.assertFalse(session.coordinated(model.tracker_at(self.s)))
        ended.write_text('{"who": "session old", "sid": "old", "pid": 1e400, "role": "coordinator", "ended": 5}')
        self.assertFalse(session.coordinated(model.tracker_at(self.s)))  # a pid of infinity runs nothing
        run(*self.t, "check")
        data(self.s)
        claimed = watcher.Watcher(model.tracker_at(self.s), "old").claim(time.time())  # and takes its place
        self.assertTrue(claimed)

    def test_a_pid_that_is_no_whole_number_runs_no_watch(self):
        path = watcher.state_path(self.s, "old")
        self.addCleanup(path.unlink, True)
        for bad in ("true", '"1"', "1.9", "1e300", "1" + "0" * 40):  # as pid 1, which mostly runs, or too large
            with self.subTest(pid=bad):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'{{"who": "session old", "sid": "old", "pid": {bad}, "role": "coordinator"}}')
                self.assertEqual(session.state_pid(json.loads(path.read_text())), 0)
                self.assertFalse(session.coordinated(model.tracker_at(self.s)))
        self.assertFalse(session.is_time(True))
        self.assertEqual(session.state_pid({"pid": os.getpid()}), os.getpid())
        self.assertEqual(session.state_pid({"pid": 1}), 1)  # a watch can be a container's first process

    def test_a_running_watch_with_a_damaged_start_names_no_time(self):
        self.coordinator()
        path = watcher.state_path(self.s, "qb")
        store.write_json(path, {**json.loads(path.read_text()), "started": -5})
        self.assertEqual(data(self.s)["now"]["watch"]["text"], "Watched by session qb")

    def test_a_wait_is_ended_only_by_its_own_session(self):
        live = {"s1": "worker"}
        wait = event(1, "human.wait", [], 5, session="s1", asker="")
        # A session that moved here from another tracker ends that tracker's wait 1, not this one's.
        self.assertEqual(self.keys([wait, event(2, "human.wait.end", [], session="s2", of=1)], live=live), ["wait:1"])
        self.assertEqual(self.keys([wait, event(2, "human.wait.end", [], session="s1", of=1)], live=live), [])

    def test_a_running_watch_with_a_damaged_end_still_watches(self):
        self.coordinator()
        path = watcher.state_path(self.s, "qb")
        store.write_json(path, {**json.loads(path.read_text()), "started": time.time() - 60, "ended": -5})
        tr = model.tracker_at(self.s)
        self.assertEqual([w["running"] for w in watcher.watches_of(tr)], [True])
        self.assertIn("Watched by session qb since", data(self.s)["now"]["watch"]["text"])
        self.assertIn("the watch by session qb", lifecycle.in_use(tr))

    def test_a_humans_own_question_is_never_stuck(self):
        run(*self.t, "decide", "Which store", "--refs", "T-1", "--owner", "Sam")
        (item,) = self.items([], now=time.time() + 2 * 86400, on=set())
        self.assertEqual((item.stuck, item.move.who, item.what), (False, "Sam", "D-01 open"))

    def test_a_wait_is_stuck_after_30_s(self):
        live = {"s1": "worker"}
        (item,) = self.items([event(1, "human.wait", [], 31, session="s1", asker="")], live=live)
        self.assertEqual((item.stuck, item.what), (True, "worker has waited on a human for 31 s"))
        (item,) = self.items([event(1, "human.wait", [], 30, session="s1", asker="")], live=live)
        self.assertFalse(item.stuck)

    def test_a_ticket_under_way_with_nobody_on_it_is_stuck_at_once(self):
        fresh = [event(1, "submission", ["T-1"], what="plan")]
        (item,) = self.items(fresh, on=set())
        self.assertEqual((item.stuck, item.move.human), (True, True))
        self.assertEqual(item.what, "T-1 plan ready for review, and no session is on T-1")
        self.assertFalse(self.items(fresh, on={"T-1"})[0].stuck)

    def test_a_ticket_not_started_is_not_stuck(self):
        (item,) = self.items([event(1, "submission", ["T-2"], what="plan")], on=set())
        self.assertFalse(item.stuck)

    def test_stuck_first_then_oldest(self):
        live = {"s1": "worker"}
        events = [event(1, "submission", ["T-1"], 600, what="plan"), event(2, "submission", ["T-2"], 900, what="code"),
                  event(3, "human.wait", [], 40, session="s1", asker="")]
        self.assertEqual(self.keys(events, live=live), ["wait:3", "submission:2", "submission:1"])

    def stuck(self) -> str:
        """T-1's plan submitted while a coordinator watches: no session is on T-1, so it is stuck at once. Its line."""
        self.coordinator()
        run(*self.t, "submit", "T-1", "--what", "plan")
        (item,) = [i for i in session.items_of(model.tracker_at(self.s)) if i.stuck]
        return item.what

    def test_check_lists_stuck_items_and_writes_nothing(self):
        what = self.stuck()
        before = tree(store.HOME / self.s)
        out = run(*self.t, "check")  # exits 0: a stuck item is no error
        self.assertEqual(out.splitlines()[-1], f"stuck: {what}")
        self.assertEqual(out.count(what), 1)  # said once, as stuck, not as a problem
        self.assertEqual(tree(store.HOME / self.s), before)

    def test_a_write_does_not_report_stuck_items_as_new_problems(self):
        self.coordinator()
        run(*self.t, "submit", "T-2", "--what", "plan")  # not started: not stuck
        self.assertFalse(any(i.stuck for i in session.items_of(model.tracker_at(self.s))))
        said = run(*self.t, "set", "T-2", "status=in-progress", cwd=self.work)  # under way, and no session on it
        self.assertTrue(any(i.stuck for i in session.items_of(model.tracker_at(self.s))))
        self.assertNotIn("no session is on", said)
        self.assertIn("`check`: no new problems", said)

    def test_the_brief_does_not_ask_an_agent_to_fix_a_stuck_item(self):
        what = self.stuck()
        tr = model.tracker_at(self.s)
        brief = views.brief(session.Match(tr, [tr.lookup("T-1")], "feat/T-1", "branch"), Path(BASE))
        self.assertIn("move: human — plan review, stuck with coordinator", brief)  # whose it is now, as a fact
        self.assertNotIn(what, brief)
        self.assertNotIn("stuck:", brief)  # and no request to fix it

    def test_history_filters_by_kind(self):
        run(*self.t, "log", "A note", "--ref", "T-1")
        run(*self.t, "step", "T-1", "A step")
        with store.locked():
            model.append_event(model.tracker_at(self.s), "human.wait", "Waiting on a human", ["T-1"], asker="", why="")
        notes = run(*self.t, "history", "--kind", "note", "--last", "0")
        self.assertIn("A note", notes)
        self.assertNotIn("A step", notes)
        both = run(*self.t, "history", "--kind", "note,step,human.wait")
        self.assertEqual([x.split("] ", 1)[1] for x in both.splitlines()], ["A note", "A step", "Waiting on a human"])
        self.assertNotIn("Waiting on a human", run(*self.t, "history"))  # a quiet kind, only when named
        refused = run(*self.t, "history", "--kind", "note,nope", code=2)
        self.assertIn("nope", refused)
        self.assertIn(", ".join(schema.EVENTS), refused)


if __name__ == "__main__":
    unittest.main()
