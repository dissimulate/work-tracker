"""The `tracker` commands: one function per command, and what they share. The parser is cli's."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

from .system import ROOT, die, entered_via, open_with_system
from .markdown import (BULLET, format_value, headings, parse_links, render_frontmatter, section, section_block,
    split_frontmatter, strip_comments)
from .store import HOME, atomic_file, atomic_write, create, locked, make_folder, record_cli
from .schema import (BLOCKER, DEFAULT_LABELS, EVENTS, EVENTS_FILE, EVIDENCE, EVIDENCE_DIR, ISSUE, ISSUE_FIELDS, KEYS,
    KINDS, LIVE_SYNC_S, MERGED_CARRY_FORWARD_MAX, NO_STAGE, OPEN_STAGES, OPTION_RULE, OWNER_HINT, README_TOKEN_BUDGET,
    SCALES, SCHEMA, STAGES, STATUSES, a_kind, csv, dated, day_text, days_since, fit, id_kind, kind_names,
    options_problems, parse_options, read_value, resolution_who, stage_of, today, utc_now, Option)
from .model import (LOG, SAFE_NAME, all_trackers, any_tracker, append_event, append_notes, append_to_section,
    archived_at, archived_trackers, blocker_link, close_action, drop_from_section, id_list, link_url, load_record,
    names, put_section, read_events, relabel, replace_in_section, resolution, same_repo, sequence, set_branch, short,
    sort_key, tracker_at, unknown_dep, Record, Tracker)
from .moves import HANDED, count, finding_numbers, last_review, last_submission
from .git import branch_of, contains, default_branches, worktree_key
from .session import (DECLINE_S, NO_TRACKERS, attached_tracker, branch_matches, decline, drop_session, find_tracker,
    in_repos, items_of, load_session, locate, mark_up_to_date, match_cwd, matched, no_tracker, on_branch,
    record_commits, remember, resolve, save_session, session_id, session_tracker, trackers_for_repo, work_dir)
from .contract import check, other_format, refuse_other_format, rules_lines
from .views import (HISTORY_LAST, ISSUE_HOW, ISSUE_WRITE_HOW, brief, context_lines, dep_lines, history_lines,
    index_lines, levels_text, order_lines, span_lines, start_text, updates_told)
from .github import match_pr, sync
from .watcher import (EVENT_KINDS, REFUSED, URGENT, agent_session, agents_lines, await_change, grant_path,
                      session_name, set_grant, Watcher)
from .lifecycle import archive_tracker, delete_tracker, unarchive_tracker
from .viewmodel import trackers_view, view_model
from .ui import (GPUI_SHELL_VERSION, MOD_OFFER, NO_HOST, herdr_home, install_mod, install_window, mod_dir, open_ui,
    update_mod)

# ---------------------------------------------------------------- templates

TEMPLATES = ROOT / "templates"


def from_template(name: str, meta: dict, labels: list[str]) -> str:
    """A new file from templates/<name>.md. In its comments, `{{labels}}` becomes the tracker's link labels, and the
    other `{{...}}` the contract's limits."""
    text = (TEMPLATES / f"{name}.md").read_text()
    lines, body = split_frontmatter(text)
    lines = render_frontmatter(lines, meta)
    for key, value in (("labels", ", ".join(labels)), ("carry_max", MERGED_CARRY_FORWARD_MAX),
                       ("readme_tokens", f"{README_TOKEN_BUDGET:,}")):
        body = body.replace(f"{{{{{key}}}}}", str(value))
    return "---\n" + "\n".join(lines) + "\n---\n" + body


def new_record(tr: Tracker, kind: str, ident: str, title: str, **meta) -> Record:
    """Write a new record of the kind from its template: its id, title, first status and `meta`, then `created_at`
    and `updated_at` now. It joins the tracker's records."""
    now = utc_now()
    path = tr.root / KINDS[kind].folder / f"{ident}.md"
    make_folder(path.parent)
    create(path, from_template(kind, {"id": ident, "title": title, "status": STATUSES[kind][0], **meta,
                                      "created_at": now, "updated_at": now}, tr.labels))
    rec = load_record(path, kind)
    tr.records_of(kind).append(rec)
    return rec


# ---------------------------------------------------------------- commands

REFRESH_SYNC_S = 15  # a Refresh syncs GitHub unless a sync ran this recently
EVIDENCE_OPENS = {".csv", ".gif", ".jpeg", ".jpg", ".json", ".log", ".md", ".pdf", ".png", ".txt"}  # run no code


def cmd_list(args):
    if args.json:
        print(json.dumps(trackers_view()))
        return
    trackers = all_trackers()
    if not trackers:
        print(NO_TRACKERS)
    for t in trackers:
        active = sum(1 for x in t.tickets if not x.closed)
        print(f"{t.slug:<36} {t.headline()} · {len(t.tickets)} tickets, {active} active · "
              f"{len(t.open_decisions())} open decisions")
    archived = archived_trackers()
    if args.archived:
        print("\n".join(["archived:", *(f"  {t.slug:<34} {t.headline()}" for t in archived)] if archived
                        else ["no archived trackers"]))
    elif archived:
        print(f"{len(archived)} archived (`tracker list --archived`)")


def cmd_init(args):
    if not SAFE_NAME.fullmatch(args.slug):
        die(f"'{args.slug}' is not a slug: use letters, digits, `.`, `_` and `-`, starting with a letter or digit")
    root = HOME / args.slug
    if (root / "README.md").exists():
        die(f"{root} already exists")
    if archived_at(args.slug):
        die(f"{no_tracker(args.slug)}, or pick another slug")
    (root / "tickets").mkdir(parents=True, exist_ok=True)
    (root / "decisions").mkdir(exist_ok=True)
    repos = csv(args.repo)
    meta = {"title": args.title, "repo": repos if len(repos) > 1 else "".join(repos), "status": "planning",
            "owner": args.owner, "created_at": utc_now(), "schema": SCHEMA}
    create(root / "README.md", from_template("README", meta, DEFAULT_LABELS))
    create(root / "log.md", f"# Log — {args.title}\n\nAppend-only. Newest at the bottom.\n\n")
    create(root / EVENTS_FILE, "")
    create(root / ".gitignore", ".state.json\n*.tmp\n")
    print(f"created {root}. Next: `tracker start {args.slug}` ties this session to it (in a terminal, pass "
          f"`--tracker {args.slug}` to each command), then `tracker new <ID> --title \"...\"` adds tickets")


def cmd_index(args):
    tr = resolve(args)
    stages = set(csv(args.status)) or None
    if stages and stages - set(STAGES):
        die(f"--status takes {'|'.join(STAGES)}")
    if args.active:
        stages = OPEN_STAGES
    lines = index_lines(tr, stages, args.group)
    print("\n".join(lines[:1] + span_lines(tr) + lines[1:]))


def cmd_here(args):
    m, cwd = matched(args)
    if not m:
        repo = trackers_for_repo(cwd)
        print(f"this session and branch '{branch_of(cwd) or '?'}' are on no ticket"
              + (f". Trackers for this repo: {', '.join(t.slug for t in repo)}" if repo else " in any tracker")
              + ". `tracker start <name>` ties this session to a tracker.")
        return
    print(brief(m, cwd, full=args.full, with_protocol=False))
    if why := other_format(m.tracker.root):  # as a bare `start` says it (cmd_start)
        print(f"{why}.")


def cmd_context(args):
    here = find_tracker(args)
    whole = None if here and here.lookup(args.id) else tracker_at(args.id)
    if whole:  # a tracker's name, where a ticket or decision id belongs: its open work, and how to read one record
        print("\n".join([f"{whole.slug} is a tracker; `tracker context <id>` takes one of its ticket or decision ids. "
                         "Its open work:", "", *index_lines(whole, OPEN_STAGES)]))
        return
    tr, rec = locate(args, args.id)
    print("\n".join(context_lines(tr, rec, full=not args.brief, deep=args.deep, log=args.log)))


def cmd_decisions(args):
    tr = resolve(args)
    status = args.status or ("" if args.all else "open")
    rows = [d for d in tr.decisions if not status or d.get("status", "open") == status]
    for d in rows:
        state = d.get("status", "open")
        touched = ", ".join(tr.touched_by(d))
        tail = f" → {resolution(d)}" if d.closed else (f" · owner {d.get('owner')}" if d.get("owner") else "")
        print(f"{d.id}  {state:<6}  {d.get('title')}" + (f"  ({touched})" if touched else "") + tail)
    if not rows:
        print("no decisions" if args.all or args.status else "no open decisions")
    settled = sum(d.closed for d in tr.decisions)
    if not (args.all or args.status) and settled:
        print(f"{settled} settled (`tracker decisions --all` lists them too; `tracker context D-<n>` gives one)")


def cmd_find(args):
    trackers = all_trackers() if args.all else [resolve(args)]
    pat = re.compile(re.escape(args.text), re.I)
    hits = 0
    for tr in trackers:
        files = [r.path for r in tr.records] + [tr.root / "README.md", tr.root / "log.md"]
        for p in files:
            if not p.exists():
                continue
            for n, line in enumerate(p.read_text().splitlines(), 1):
                if pat.search(line):
                    where = p.relative_to(HOME if args.all else tr.root)
                    print(f"{where}:{n}: {line.strip()[:160]}")
                    hits += 1
    if not hits:
        print("no matches")


def named_or_own(args) -> tuple[Tracker, Record]:
    """The record `args.id` names; with no id, this session's one ticket: its `tracker start --on` choice, else the
    one ticket under way (or else unfinished) on its branch."""
    if args.id:
        return locate(args, args.id)
    m = matched(args)[0]
    if not m or len(m.focus) != 1:
        ids = ", ".join(t.id for t in m.focus) if m else ""
        die("name the ticket: " + (f"this session works on {ids}" if ids else "this session is on no ticket"))
    return m.tracker, m.focus[0]


def cmd_set(args):
    tr, rec = record_for(args, args.id) if args.id else named_or_own(args)
    schema = KEYS[rec.kind]
    updates = {}
    for pair in args.pairs:
        if "=" not in pair:
            die(f"'{pair}' is not key=value")
        k, v = (x.strip() for x in pair.split("=", 1))
        if k not in schema:
            die(f"a {rec.kind} has no key '{k}' (keys: {', '.join(schema)}; `tracker rules` explains each)")
        if schema[k][0] != "set":
            die(f"{k}: {OWNER_HINT[schema[k][0]]}, not `set`")
        if k == "status":
            allowed = STATUSES[rec.kind]
            if v not in allowed:
                hint = "; in-review comes from the ticket's PR" if v in STAGES else ""
                die(f"status must be one of {'|'.join(allowed)}{hint}")
        if k == "next" and rec.kind == "ticket":
            fit(k, v)
        v = read_value(k, v)
        if k == "repo" and rec.kind == "ticket" and v and v not in tr.repos:
            die(f"repo must be one of the tracker's repos ({', '.join(tr.repos) or 'none'})")
        updates[k] = v
    notes = []
    to = stage_of("ticket", updates.get("status")) if rec.kind == "ticket" else NO_STAGE
    if to.closed and not (updates.get("summary") or rec.get("summary")):
        notes.append(f"set its summary: `tracker set {rec.id} summary=\"<what it delivered, or why dropped>\"`")
    if to.in_flight and "branch" not in updates:
        notes += start_here(tr, rec, updates)
    moved = "status" in updates and updates["status"] != rec.get("status")
    starts = rec.todo and to.in_flight
    if "status" in updates:
        updates.update(rec.status_update(updates["status"]))
    rec.change(updates)
    if updates.get("issue_updates") == "on":
        tr.start_issue_updates()
    if moved:
        append_event(tr, "status", status_line(rec), [] if rec.kind == "tracker" else [rec.id],
                     status=str(rec.get("status")))
    print(f"{rec.id}: " + ", ".join(k if len(str(v)) > 40 else f"{k}={format_value(v)}"
                                    for k, v in updates.items() if KEYS[rec.kind][k][0] != "auto"))
    if starts:
        notes += base_notes(tr, rec)
    if notes:
        print("\n".join(notes))


def status_line(rec: Record) -> str:
    """The log line of a status change: a ticket's start, close or reopening (a closed one with what its summary
    says), or the work's status."""
    status = str(rec.get("status"))
    if rec.kind == "tracker":
        return f"The work is {status} now"
    where = f" on {rec.get('branch')}" if rec.in_flight and rec.get("branch") else ""
    what = rec.get("summary") if rec.closed and rec.get("summary") else rec.get("title")
    return f"{rec.id} {status}{where}: {short(str(what))}"


def start_here(tr: Tracker, t: Record, updates: dict) -> list[str]:
    """A ticket set in progress from a branch of this tracker's repo, with no branch of its own: record where its work
    happens. On its own branch, the ticket's `branch` (the branch may hold other tickets too); on a shared branch such
    as main, a `use` choice for this worktree."""
    cwd = work_dir()
    branch = branch_of(cwd)
    if t.get("branch") or not branch or not in_repos(tr, cwd):
        return []
    if branch not in default_branches(cwd):
        updates["branch"] = branch
        return []
    key = worktree_key(cwd, branch)
    state = tr.state()
    chosen = state.get("use", {}).get(key, [])  # state() keeps only unfinished tickets
    if t.id not in chosen:
        state.setdefault("use", {})[key] = chosen + [t.id]
        tr.save_state(state)
    return [f"{t.id} is worked on in this worktree on {branch} (`tracker use --clear` undoes it)"]


def base_notes(tr: Tracker, t: Record) -> list[str]:
    """A ticket started on the current branch: name the branches of the tickets under way it waits on that this
    branch does not contain (START_RULE). Git is only read."""
    cwd = work_dir()
    under = [d.rec for d in tr.blockers(t) if d.rec and d.rec.in_flight and d.rec.get("branch")
             and same_repo(tr.repo_of(d.rec), tr.repo_of(t))]
    if not under or t.get("branch") != branch_of(cwd) or not in_repos(tr, cwd):  # git runs only past here
        return []
    missing = [b for b in under if contains(cwd, str(b.get("branch"))) is False]
    if not missing:
        return []
    where = ", ".join(f"{b.get('branch')} ({b.id}" + (f", {b.pr_refs}" if b.prs else "") + ")"
                      for b in missing)
    return [f"{t.id} waits on work under way that branch {t.get('branch')} does not contain: {where}. Start the branch "
            f"from it, unless the user chose otherwise (`tracker ready` names the branch to start from)."]


def cmd_log(args):
    tr = resolve(args)
    refs = [tr.find(r).id for r in id_list(args.ref)]
    append_event(tr, "note", args.message, refs)
    print("logged" + (f" [{' '.join(refs)}]" if refs else ""))


def cmd_history(args):
    tr = resolve(args)
    if args.since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
        die("--since takes a date: YYYY-MM-DD")
    kinds = csv(args.kind)
    if unknown := [k for k in kinds if k not in EVENTS]:
        die(f"no event kind {', '.join(unknown)}: the kinds are {', '.join(EVENTS)}")
    lines = history_lines(tr, [tr.find(r).id for r in id_list(args.ref)], args.since or "", args.last, kinds=kinds)
    print("\n".join(lines) if lines else "no log lines")


def cmd_new(args):
    fit("next", args.next)
    tr = resolve(args)
    if not SAFE_NAME.fullmatch(args.id):
        die(f"'{args.id}' is not a ticket id: use letters, digits, `.`, `_` and `-`, starting with a letter or digit")
    if (tr.root / "tickets" / f"{args.id}.md").exists() or tr.lookup(args.id):
        die(f"{args.id} already exists")
    if kind := id_kind(args.id):
        die(f"{KINDS[kind].prefix}-<n> ids are {kind}s: `tracker {KINDS[kind].command} \"<title>\"` adds one")
    deps = []
    for x in id_list(args.depends):
        rec = tr.lookup(x) or die(f"--depends {x}: {unknown_dep(tr, x)}; for an external blocker, `tracker wait "
                                  f"{args.id} on {x} --link \"<url> — <why>\"` after creating the ticket")
        deps.append(rec.id)
    if args.repo and args.repo not in tr.repos:
        die(f"--repo must be one of the tracker's repos ({', '.join(tr.repos) or 'none'})")
    if args.branch and len(tr.repos) > 1 and not args.repo:
        die(f"the tracker spans {len(tr.repos)} repos: pass --repo ({', '.join(tr.repos)})")
    meta = {"group": args.group or "", "branch": args.branch or "", "depends_on": [], "next": args.next or ""}
    for key in [*SCALES, "due"]:
        if getattr(args, key):
            meta[key] = read_value(key, getattr(args, key))
    if args.repo:
        meta["repo"] = args.repo
    t = new_record(tr, "ticket", args.id, args.title, **meta)
    if (args.plan or "").strip():
        put_section(t, "Plan", args.plan.strip("\n"))
    add_waits(tr, t, deps)
    append_event(tr, "ticket.added", f"Added {args.id} {args.title}"
                 + (f"; waits on {', '.join(deps)}" if deps else ""), [args.id])
    print(f"created {args.id}: {t.path}")


SAY_WHO = ('say who decided: --by <name> when a human told you, or --source "<where>" when the spec, a settled '
           "decision or the code already answers it")


def who_settled(by: str | None, source: str | None) -> tuple[str, dict]:
    """Who settled a decision and how it was entered (R9), from `--by` or `--source` and `entered_via`: the `<who>` of
    its resolution line and its `decision.decided` event's data. In the view, a human (`by`, else "human"); through an
    agent, the human it names (not cited yet) or the source it names, and one of them it must name; at a terminal,
    unattributed, with what it names kept as said. Dies on flags that cannot say it: the caller writes nothing
    before."""
    by, source = resolution_who(by), resolution_who(source)
    if by is None or source is None:
        die('--by and --source take no ": " or ")": the resolution line is "<day> (<who>): <answer>"')
    if by and source:
        die("--by and --source: a human decided or a source did, not both")
    via = entered_via()
    if via == "view":
        by = by or "human"
        return f"{by}, in the view", {"decided": "human", "by": by}
    if via == "agent":
        if by:
            return f"{by}, entered by an agent", {"decided": "human", "by": by, "cited": False}
        if source:
            return f"from {source}, entered by an agent", {"decided": "source", "source": source}
        die(SAY_WHO)
    data, said = {"decided": "unattributed"}, ""
    if by:
        data["by"], said = by, f", said {by}"
    elif source:
        data["source"], said = source, f", from {source}"
    return f"unattributed{said}", data


def resolve_decision(tr: Tracker, rec: Record, answer: str, settled: tuple[str, dict]) -> str:
    """Close a decision with its answer and who settled it (`who_settled`)."""
    who, data = settled
    rec.change(rec.status_update("closed"))
    append_to_section(rec, "Resolution", f"{today()} ({who}): {answer.strip()}")
    append_event(tr, "decision.decided", LOG["decided"].format(id=rec.id, title=rec.get("title"), who=f" ({who})",
                                                               answer=short(answer)),
                 [rec.id, *tr.touched_by(rec)], decision=rec.id, **data)
    return f"{rec.id} closed"


def add_waits(tr: Tracker, t: Record, idents: list[str]) -> list[str]:
    """Add items to a ticket's depends_on, refusing one that would close a cycle. Returns the items added."""
    before = t.list("depends_on")
    have = {x.lower() for x in before}
    added = [x for x in dict.fromkeys(idents) if x.lower() not in have]
    if not added:
        return []
    t.change({"depends_on": before + added})
    cycle = next((c for c in sequence(tr).cycles if t.id in c), None)
    if cycle:
        t.save({"depends_on": before})
        die(f"that would make a dependency cycle: {' → '.join(cycle)}")
    return added


def cmd_wait(args):
    args.items = id_list(args.items)
    tr, t = locate(args, args.id)
    if t.kind != "ticket":
        die(f"{t.id} is {a_kind(t.kind)}; a ticket waits on it: `tracker wait <ticket> on {t.id}`")
    if args.mode == "off":
        drop = {tr.canonical(x).lower() for x in args.items}
        keep = [x for x in t.list("depends_on") if tr.canonical(x).lower() not in drop]
        if len(keep) == len(t.list("depends_on")):
            stacked = [o.id for o in tr.stacked_on(t) if o.id.lower() in drop]
            die(f"{t.id} waits on {', '.join(stacked)} because its PR is based on their branch "
                f"{t.get('base')}; that ends when the PR is retargeted" if stacked else
                f"{t.id} does not wait on {', '.join(args.items)}")
        t.change({"depends_on": keep})
        for x in args.items:  # a Blocker line for something no longer waited on becomes a plain link
            link = None if tr.lookup(x) else blocker_link(t, x)
            if link:
                relabel(t, link, "Related")
        append_event(tr, "wait.removed", f"{t.id} no longer waits on {', '.join(args.items)}", [t.id],
                     on=[tr.canonical(x) for x in args.items])
    else:
        items = []
        external = [x for x in args.items if not tr.lookup(x) and not blocker_link(t, x)]
        for x in args.items:
            rec = tr.lookup(x)
            if rec and rec.id == t.id:
                die("a ticket cannot wait on itself")
            if not rec and not blocker_link(t, x):
                named = next((link for link in t.links if names(link, x)), None)
                if named and not args.link:
                    relabel(t, named, BLOCKER)  # an existing link line already says where it is
                elif not args.link or len(external) > 1:
                    die(f"{unknown_dep(tr, x)}. For an external blocker, say where it is and why it blocks, one "
                        f"blocker per command: `--link \"<url> — <why>\"` (adds a `- {BLOCKER}:` line to ## Links)")
                else:
                    url, _, why = args.link.partition(" ")
                    text = (f"[{x}]({url})" + (f" — {why.strip().lstrip('—-').strip()}" if why.strip() else "")
                            if url.startswith("http") else f"{x} — {args.link}")
                    append_to_section(t, "Links", f"- {BLOCKER}: {text}")
            items.append(rec.id if rec else x)
        added = add_waits(tr, t, items)
        if not added:
            die(f"{t.id} already waits on {', '.join(items)}")
        for d in (tr.lookup(x) for x in added):
            if d and t.id in d.list("refs"):  # a decision's or action's: depends_on now says so
                d.change({"refs": [r for r in d.list("refs") if r != t.id]})
        append_event(tr, "wait.added", f"{t.id} now waits on {', '.join(added)}", [t.id], on=added)
    print("\n".join(dep_lines(tr, t)) or f"{t.id} waits on nothing")


def cmd_seq(args):
    tr = resolve(args)
    seq = sequence(tr)
    rows = sorted((t for t in tr.tickets if not t.dropped and not (args.active and t.closed)),
                  key=lambda t: (seq.step.get(t.id, 0), sort_key(t.id)))
    w = max([len(t.id) for t in rows] + [2])
    wg = max([len(str(t.get("group") or "—")) for t in rows] + [1])
    for t in rows:
        waits = " ".join(d.ident + ("✓" if d.done else "") for d in tr.deps(t))
        later = ", ".join(x.id for x in tr.waiting_on(t.id))
        mark = "◆" if t.id in seq.critical else " "
        title = str(t.get("title"))
        print(f"{mark}{seq.step.get(t.id, ''):>2}  {t.id:<{w}}  {str(t.get('group') or '—'):<{wg}}  "
              f"{t.stage:<11} {title[:40]:<40}" + (f"  waits {waits}" if waits else "")
              + (f"  → {later}" if later else ""))
    print("\nStep: 1 + the longest chain of tickets it waits on. ✓ = satisfied. ◆ = critical path.")
    print("\n".join(order_lines(tr)))


def cmd_ready(args):
    """The todo tickets that can start now, the most urgent first: from the default branch, or stacked on work under
    way (START_RULE), with their levels and what each unblocks."""
    tr = resolve(args)
    starts = tr.startable()
    for t, s in starts:
        later = ", ".join(o.id for o in tr.waiting_on(t.id))
        print(" · ".join(x for x in (f"{t.id}  {t.get('group') or '—'}  {t.get('title')}"
                                     + (f" — {start_text(tr, s)}" if s.stacked else ""), levels_text(t),
                                     f"unblocks {later}" if later else "") if x))
    if not starts:
        print("nothing can start now")


# ---------------------------------------------------------------- body edits
# `add` and `drop` change one line of a section of a ticket, a decision or the README, and `put` the whole section, so
# a fact goes in without a hand edit of the file. A list section takes one bullet per `add`; link lines are checked as
# `check` would. The edits themselves are in `model`.

LIST_SECTIONS = {"Carry forward", "Links", "Context", "Options", "Notes"}
LINK_SECTIONS = {"Links", "Context"}
OWNED_SECTIONS = {"Resolution": "`tracker decide D-<n> --resolve`"}


def record_for(args, ident: str) -> tuple[Tracker, Record]:
    """A record of any kind, or the README for `tracker` / `readme`."""
    if ident.lower() in ("readme", "tracker"):
        tr = resolve(args)
        return tr, tr.readme
    return locate(args, ident)


def section_named(rec: Record, word: str, new: bool = False) -> str:
    """The section a word names: a case-free prefix of one of the record's sections (`carry` → Carry forward), else a
    part of one (`order` → Why this order). `new`: a README section that none matches is a new one, by that name (any
    sections may follow the required ones)."""
    known = (KINDS[rec.kind].sections if rec.kind in KINDS else ()) or headings(rec.body)
    w = " ".join(word.lower().split())
    hits = [h for h in known if h.lower().startswith(w)] or [h for h in known if w in h.lower()]
    if not hits and new and rec.kind == "tracker" and w:
        name = " ".join(word.split())
        return name[0].upper() + name[1:]
    if len(hits) != 1:
        die(f"'{word}' names {'none' if not hits else 'more than one'} of {rec.id}'s sections: {', '.join(known)}"
            + ("; `put tracker \"<new section>\" -` adds a README section" if rec.kind == "tracker" else ""))
    if hits[0] in OWNED_SECTIONS:
        die(f"## {hits[0]}: use {OWNED_SECTIONS[hits[0]]}")
    return hits[0]


def require_links(tr: Tracker, text: str) -> None:
    items, bad = parse_links(text)
    if bad or not items:
        die("a link line is `- Label: [title](url) — why it matters`"
            + (f", not: {short(bad[0], 80)}" if bad else ""))
    for x in items:
        if x.label not in tr.labels:
            die(f"label '{x.label}' is not one of {', '.join(tr.labels)} (`tracker set tracker labels=...` adds one)")


def require_options(text: str) -> None:
    if problems := options_problems(text):
        die(f"{'; '.join(problems)}. {OPTION_RULE}")


def section_edit(args, new: bool = True) -> tuple[Tracker, Record, str]:
    """The record and section an `add`, `put` or `drop` changes. A started ticket's Plan is the agreed plan: it
    changes only with a reason (--why), which the log keeps."""
    tr, rec = record_for(args, args.id)
    heading = section_named(rec, args.section, new=new)
    if heading == "Plan" and rec.kind == "ticket" and not rec.todo and not args.why:
        die(f"{rec.id} has started, so its Plan is the agreed plan: pass why it changes, for the log: "
            f"--why \"<why the plan changes>\"")
    return tr, rec, heading


def section_done(tr: Tracker, rec: Record, heading: str, why: str | None, did: str) -> None:
    rec.change()
    if why:
        readme = rec.kind == "tracker"
        append_event(tr, "body.changed", f"{'README ' * readme}{heading} changed: {why}", [] if readme else [rec.id],
                     heading=heading)
    print(f"{rec.id} {heading}: {did}")


def cmd_add(args):
    tr, rec, heading = section_edit(args)
    text = args.text.strip()
    if args.replace:
        replace_in_section(rec, heading, args.replace, text)
        section_done(tr, rec, heading, args.why, "replaced")
        return
    if heading in LIST_SECTIONS and not BULLET.match(text):
        text = "- " + text
    if heading in LINK_SECTIONS:
        require_links(tr, text)
    if heading == "Options":
        require_options(f"{strip_comments(section(rec.body, heading))}\n{text}")
    append_to_section(rec, heading, text)
    section_done(tr, rec, heading, args.why, "added")


def cmd_put(args):
    """Replace a whole section: a rewritten Plan, a Carry forward kept short, a new set of Links."""
    tr, rec, heading = section_edit(args)
    text = args.text.strip("\n")
    if not text.strip():
        die("put takes the section's whole new text: pass it, or `-` and pipe it (a heredoc: <<'EOF')")
    if heading in LINK_SECTIONS:
        require_links(tr, text)
    if heading == "Options":
        require_options(text)
    put_section(rec, heading, text)
    section_done(tr, rec, heading, args.why, "replaced")


def cmd_drop(args):
    tr, rec, heading = section_edit(args, new=False)
    n = drop_from_section(rec, heading, args.text)
    section_done(tr, rec, heading, args.why, f"dropped {n} line(s)")


def cmd_show(args):
    """Records' own text, whole or by section, found as any command finds them: what `cat` of their files gives,
    without the template's comments."""
    words = csv(args.section)
    out = []
    for ident in id_list(args.ids):
        if ident.lower() == "log":  # the log holds no sections: its last lines, as `history` gives them
            out += ["== log (`tracker history` filters it)", *history_lines(resolve(args), [], "", HISTORY_LAST), ""]
            continue
        rec = record_for(args, ident)[1]
        state = rec.stage if rec.kind == "ticket" else rec.get("status")
        out.append("== " + " · ".join(str(x) for x in (rec.id, rec.get("title"), state) if x))
        if not words:
            text = re.sub(r"[ \t]*<!--.*?-->", "", rec.path.read_text(), flags=re.S)
            out.append(re.sub(r"\n{3,}", "\n\n", text).strip())
        parts = []
        for word in words:
            hits = [h for h in headings(rec.body) if h.lower().startswith(word.lower().strip())]
            parts += [section_block(rec.body, h).strip() for h in hits] or [f"(no section '{word}')"]
        out += ["\n\n".join(parts), ""] if parts else [""]
    print("\n".join(out).rstrip())


def append_evidence(folder: Path, file: str, text: str) -> str:
    """`attach --append`: add text at the end of a file the evidence/ folder holds, named by its path or its name
    there. Returns its name there."""
    real, path = folder.resolve(), Path(file).expanduser().resolve()
    name = str(path.relative_to(real)) if path.is_relative_to(real) else file
    dest = folder / name
    if not SAFE_NAME.fullmatch(dest.name) or not dest.resolve().is_relative_to(real) or not dest.is_file():
        die(f"no file {EVIDENCE_DIR}/{name} to append to: `tracker attach <file>` keeps a new one")
    old = dest.read_text()
    gap = "" if not old or old.endswith("\n\n") else "\n" if old.endswith("\n") else "\n\n"
    atomic_write(dest, f"{old}{gap}{text.strip(chr(10))}\n")
    return name


def cmd_attach(args):
    """Keep a file in the tracker's evidence/ folder and link it from the records it supports."""
    tr = resolve(args)
    folder = tr.root / EVIDENCE_DIR  # as named: a write resolves a path only to check it (`store.tracker_root`)
    if not folder.resolve().is_relative_to(tr.root.resolve()):
        die(f"{EVIDENCE_DIR}/ must stay inside the tracker directory")
    recs = [tr.find(ident) for ident in ids_of(tr, args.ref, {"ticket", "decision"}, "--ref")]
    if args.append is not None:
        if args.name or args.force:
            die("--append adds to the file named: no --name or --force with it")
        name = append_evidence(folder, args.file, args.append)
        attached = "Appended to"
    else:
        name, attached = keep_evidence(args, folder), "Attached"
    link = f"[{name}]({EVIDENCE_DIR}/{name})" + (f" — {args.note.strip()}" if args.note else "")
    refs = []
    for rec in recs:
        if rec.kind == "ticket":
            if not any(f"({EVIDENCE_DIR}/{name})" in x.text for x in rec.links):
                append_to_section(rec, "Links", f"- {EVIDENCE}: {link}")
        elif not (args.append is not None and f"({EVIDENCE_DIR}/{name})" in rec.body):
            append_to_section(rec, "Notes", f"- {today()}: {EVIDENCE}: {link}")
        rec.change()
        refs.append(rec.id)
    append_event(tr, "evidence.attached", f"{attached} {EVIDENCE_DIR}/{name}"
                 + (f": {short(args.note)}" if args.note else ""), refs, file=f"{EVIDENCE_DIR}/{name}")
    print(f"{folder / name}" + (f" · linked from {', '.join(refs)}" if refs else ""))


def keep_evidence(args, folder: Path) -> str:
    """Copy the file into the evidence/ folder, unless it is there; its name there."""
    real, src = folder.resolve(), Path(args.file).expanduser().resolve()
    if not src.is_file():
        die(f"no file {args.file}")
    if src.is_relative_to(real):
        name = str(src.relative_to(real))
    else:
        name = args.name or re.sub(r"[^A-Za-z0-9._-]+", "-", src.name)
        if not SAFE_NAME.fullmatch(name):
            die(f"'{name}' is not a file name: use letters, digits, `.`, `_` and `-`")
        dest = folder / name
        if dest.is_symlink() or not dest.resolve().is_relative_to(real):
            die(f"refusing to write {EVIDENCE_DIR}/{name}: destination is a symlink or outside {EVIDENCE_DIR}/")
        if dest.exists() and dest.read_bytes() != src.read_bytes() and not args.force:
            die(f"{EVIDENCE_DIR}/{name} exists with other content: pass --name, --force to replace it, or `--append -` "
                "to add text to it")
        make_folder(folder)
        with src.open("rb") as source, atomic_file(dest) as target:
            shutil.copyfileobj(source, target)
    return name


def similar(a: str, b: str) -> float:
    wa, wb = ({w for w in re.findall(r"[a-z0-9]{4,}", x.lower())} for x in (a, b))
    return len(wa & wb) / max(1, len(wa | wb))


def ids_of(tr: Tracker, raw: list[str] | None, kinds, flag: str) -> list[str]:
    """The ids a flag names, each a record of one of `kinds`."""
    out = []
    for r in id_list(raw):
        rec = tr.find(r)
        if rec.kind not in kinds:
            die(f"{flag} takes {kind_names(kinds)} ids; {r} is {a_kind(rec.kind)}")
        out.append(rec.id)
    return out


def link_args(tr: Tracker, kind: str, args) -> tuple[list[str], set[str], list[str]]:
    """A decision's or action's --refs, --unref and --blocks: the ids of the records it touches (KINDS refs) and of
    the tickets it blocks."""
    blocks = ids_of(tr, args.blocks, {"ticket"}, "--blocks")
    refs = [r for r in ids_of(tr, args.refs, KINDS[kind].refs, "--refs") if r not in blocks]
    return refs, set(ids_of(tr, args.unref, KINDS[kind].refs, "--unref")), blocks


def existing(tr: Tracker, kind: str, target: str) -> Record | None:
    """The record a command's target names by its numbered id (`D-3`); None for a new one's title."""
    if id_kind(target) != kind:
        return None
    return tr.one_of(kind, target) or die(f"no {kind} {target}; to add one, pass its title instead")


def refuse_similar(tr: Tracker, kind: str, title: str, force: bool) -> None:
    """A new record titled like an open one of its kind is likely that one: name it, and exit 3 unless --force."""
    clash = [r for r in tr.records_of(kind) if not r.closed and similar(str(r.get("title")), title) >= 0.5]
    if clash and not force:
        print("\n".join(f"similar open {kind}: {r.id} {r.get('title')}" for r in clash))
        k = KINDS[kind]
        die(f"add to that one with `tracker {k.command} {k.prefix}-<n> --note ...`, or pass --force if this is a "
            f"different {kind}", 3)


def block_tickets(tr: Tracker, ident: str, blocks: list[str]) -> list[str]:
    """Record that each ticket in `blocks` waits on this decision or action. Returns the tickets newly blocked."""
    return [t for t in blocks if add_waits(tr, tr.find(t), [ident])]


def blocks_line(tr: Tracker, rec: Record, blocks: list[str]) -> str:
    """`--blocks` on an existing decision or action: record and log the tickets newly blocked; the line, or ""."""
    newly = block_tickets(tr, rec.id, blocks)
    if not newly:
        return ""
    msg = f"{', '.join(newly)} now {'waits' if len(newly) == 1 else 'wait'} on {rec.id}"
    append_event(tr, "wait.added", msg, [rec.id, *newly], on=[rec.id])
    return msg


def refs_after(tr: Tracker, rec: Record, refs: list[str], unrefs: set[str], blocks: list[str]) -> list[str]:
    """A decision's or action's refs after `--refs`, `--unref` and `--blocks`: what it touches but does not block. A
    ticket that waits on it leaves refs: depends_on says so."""
    waiting = {t.id for t in tr.waiting_on(rec.id)} | set(blocks)
    return sorted((set(rec.list("refs")) | set(refs)) - unrefs - waiting, key=sort_key)


def choice(text: str) -> Option:
    """One choice as `--option` or `--recommend` gives it (OPTION_RULE)."""
    found, bad = parse_options(f"- {text.strip()}")
    if bad or len(found) != 1 or found[0].recommended:
        die(f"a choice is one line, `<choice>` or `<choice> — <what it costs>`, not: {short(text, 60)}")
    return found[0]


def options_of(d: Record) -> list[Option]:
    """A decision's choices; dies when ## Options holds a line that is no choice, which a person fixes first."""
    options, bad = parse_options(strip_comments(section(d.body, "Options")))
    if bad:
        die(f"{d.id} ## Options holds a line that is no choice ({short(bad[0], 60)}): fix it first. {OPTION_RULE}")
    return options


def add_choices(d: Record, given: list[Option], pick: Option | None) -> list[str]:
    """Add the choices a decision's ## Options lacks, and mark the one recommended, which is added when new; what
    changed, for the log."""
    options = options_of(d)
    known = {o.text.lower() for o in options}
    said = []
    for o in [*given, *([pick] if pick else [])]:
        if o.text.lower() not in known:
            options.append(o)
            known.add(o.text.lower())
            if o is not pick:
                said.append(f"option {short(o.text, 40)}")
    if pick:
        options = [replace(o, recommended=o.text.lower() == pick.text.lower()) for o in options]
        said.append(f"recommends {short(pick.text, 40)}")
    put_section(d, "Options", "\n".join(o.line() for o in options))
    return said


def cmd_decide(args):
    tr = resolve(args)
    if (args.by or args.source) and not args.resolve:
        die("--by and --source say who settled it: pass them with --resolve")
    settled = who_settled(args.by, args.source) if args.resolve else ("", {})
    owner = resolution_who(args.owner)  # the view answers as the owner it names (`--by`)
    if owner is None:
        die('--owner takes no ": " or ")": the view gives the owner as who decided, in "<day> (<who>): <answer>"')
    refs, unrefs, blocks = link_args(tr, "decision", args)
    if blocks and args.resolve:
        die("--blocks with --resolve: a settled decision blocks nothing")
    given = [choice(x) for x in args.option or []]
    pick = choice(args.recommend) if args.recommend else None
    d = existing(tr, "decision", args.target)
    if d:
        others = args.note or given or pick or args.resolve or refs or unrefs or blocks or args.question
        if owner and owner == str(d.get("owner") or "").strip():
            if not others:
                die(f"{d.id}'s owner is {owner} already")
            owner = ""  # no change to write or log
        if not (others or owner):
            die("nothing to change: pass --note, --option, --recommend, --resolve, --refs, --unref, --blocks, --owner "
                "or --question")
        if given or pick:
            options_of(d)  # a refusal writes nothing: before the first write
        upd = {"refs": refs_after(tr, d, refs, unrefs, blocks)} if refs or unrefs or blocks else {}
        owned = {"owner": owner} if owner else {}
        d.change({**upd, **owned})
        if args.question:
            append_to_section(d, "Question", dated(args.question))
        said = [f"owner {owner}"] if owner else []  # whose move it is changes: a watch reads it from the event
        said += add_choices(d, given, pick) if given or pick else []
        if args.note:
            append_to_section(d, "Notes", f"- {dated(args.note)}")
            said.append(short(args.note))
        if said:
            append_event(tr, "decision.updated", f"Updated {d.id}: {'; '.join(said)}", [d.id, *tr.touched_by(d)],
                         decision=d.id, **owned)
        if msg := blocks_line(tr, d, blocks):
            print(msg)
        print(resolve_decision(tr, d, args.resolve, settled) if args.resolve else f"{d.id} updated")
        return
    if unrefs:
        die("--unref changes an existing decision; pass its D-id")
    refuse_similar(tr, "decision", args.target, args.force)
    d = new_record(tr, "decision", tr.next_id("decision"), args.target, refs=refs, owner=owner)
    append_to_section(d, "Question", args.question or args.target)
    if given or pick:
        add_choices(d, given, pick)
    if args.note:
        append_to_section(d, "Notes", f"- {dated(args.note)}")
    if args.resolve:
        resolve_decision(tr, d, args.resolve, settled)
        print(f"{d.id} recorded as settled: {d.path}")
    else:
        newly = block_tickets(tr, d.id, blocks)
        append_event(tr, "decision.opened", LOG["opened"].format(id=d.id, title=args.target)
                     + (f"; blocks {', '.join(newly)}" if newly else ""), [d.id, *refs, *newly], decision=d.id,
                     **({"owner": owner} if owner else {}))
        print(f"{d.id} opened" + (f", blocks {', '.join(newly)}" if newly else "") + f": {d.path}")


def cmd_act(args):
    tr = resolve(args)
    refs, unrefs, blocks = link_args(tr, "action", args)
    if args.done and args.drop:
        die("--done or --drop, not both")
    if blocks and (args.done or args.drop):
        die("--blocks with --done or --drop: a closed action blocks nothing")
    notes = args.note or []
    due = None if args.due is None else read_value("due", args.due)
    a = existing(tr, "action", args.target)
    if a:
        if not (args.done or args.drop or args.note or refs or unrefs or blocks or due is not None or args.title):
            die("nothing to change: pass --done, --drop, --note, --refs, --unref, --blocks, --due or --title")
        if args.title:
            old = a.get("title")
            a.change({"title": " ".join(args.title.split())})
            append_event(tr, "action.updated", f"{a.id} renamed: {old} → {a.get('title')}", [a.id, *a.list("refs")],
                         action=a.id)
        if refs or unrefs or blocks:
            a.change({"refs": refs_after(tr, a, refs, unrefs, blocks)})
        if msg := blocks_line(tr, a, blocks):
            print(msg)
        if due is not None:
            a.change({"due": due})
            append_event(tr, "action.updated", f"{a.id} due {due}" if due else f"{a.id} has no due date now",
                         [a.id, *a.list("refs")], action=a.id)
        if args.done or args.drop:
            print(close_action(tr, a, "done" if args.done else "dropped", notes))
            return
        if notes:
            append_notes(a, notes)
            a.change()
            append_event(tr, "action.updated", f"Updated {a.id}: {short('; '.join(notes))}", [a.id, *a.list("refs")],
                         action=a.id)
        print(f"{a.id} updated")
        return
    if args.done or args.drop or args.title or unrefs:
        die("--done, --drop, --title and --unref change an action: pass its A-id")
    refuse_similar(tr, "action", args.target, args.force)
    a = new_record(tr, "action", tr.next_id("action"), args.target, refs=refs, due=due or "")
    append_notes(a, notes)
    newly = block_tickets(tr, a.id, blocks)
    append_event(tr, "action.added", LOG["action"].format(title=args.target) + (f" (due {due})" if due else "")
                 + (f"; blocks {', '.join(newly)}" if newly else ""), [a.id, *refs, *newly], action=a.id)
    print(f"{a.id} added" + (f", blocks {', '.join(newly)}" if newly else "") + f": {a.path}")


def cmd_actions(args):
    tr = resolve(args)
    rows = tr.actions if args.all else tr.open_actions()
    for a in rows:
        state = a.get("status", "open")
        refs = ", ".join(a.list("refs"))
        age = days_since(a.get("created_at"))
        when = day_text(a.get("closed_at")) if a.closed else \
            f"due {a.get('due')}" if a.get("due") else f"open {age} d" if age else "added today"
        print(f"{a.id}  {state:<7}  {a.get('title')}" + (f"  ({refs})" if refs else "") + f" · {when}")
    if not rows:
        print("no actions" if args.all else "no open actions")
    closed = len(tr.actions) - len(tr.open_actions())
    if not args.all and closed:
        print(f"{closed} closed (`tracker actions --all` lists them too)")
    if tr.actions:
        print("`tracker show A-<n>` prints one with its notes")


def cmd_rules(args):
    print("\n".join(rules_lines(find_tracker(args))))


def problem_lines(tr: Tracker) -> list[str]:
    errors, warnings = check(tr)
    return [f"✗ {x}" for x in errors] + [f"⚠ {x}" for x in warnings]


def cmd_check(args):
    """The tracker's problems (`contract.check`), then the open items that are stuck (`items_of`): those are no
    problem in its files, so neither a write's report nor a brief, which ask the agent to fix what `check` finds,
    shows them."""
    tr = resolve(args)
    lines = problem_lines(tr)
    print("\n".join(lines) or " · ".join(["ok", *(f"{len(tr.records_of(k))} {KINDS[k].folder}" for k in KINDS)]))
    stuck = [f"stuck: {i.what}" for i in items_of(tr) if i.stuck]
    if stuck:
        print("\n".join(stuck))
    sys.exit(1 if any(x.startswith("✗") for x in lines) else 0)


def cmd_sync(args):
    tr = resolve(args)
    changes = sync(tr, every=LIVE_SYNC_S if args.live else 0)
    print("\n".join(changes) if changes else "no changes")


def cmd_issue(args):
    """Record what the model read from a ticket's issue tracker, or with `--written` that it wrote the updates the
    listing gave; with no id, list the tickets whose issue fields are to read and the issue updates to write, and
    remember that listing for `--written` (STATE_RULES["issues"])."""
    if not args.id:
        tr = resolve(args)
        due = tr.issues_to_read()
        print("\n".join([*(f"{t.id}  " + issue_urls(t) for t in due),
                         "", ISSUE_HOW]) if due else "no ticket's issue fields are to read")
        updates = tr.issue_updates_due()
        if updates:
            tr.list_issue_updates(updates)  # what `--written` records
            print("\n".join(["", f"Issue updates to write for {len(updates)} ticket(s):",
                             *(f"{t.id}  {issue_urls(t)}\n" + "\n".join(f"  - {x}" for x in items)
                               for t, items in updates), "", ISSUE_WRITE_HOW]))
        return
    tr, t = locate(args, args.id)
    if t.kind != "ticket" or not t.aliases:
        die(f"{t.id} has no Issue link: add one first (`tracker add {t.id} link \"Issue: [ID Title](url)\"`)")
    updates = {}
    for flag, key in ISSUE_FIELDS.items():
        given = getattr(args, flag)
        if given is not None:
            updates[key] = read_value(key, given) or None  # empty: the issue has none now
    if args.written:
        if not tr.mark_issue_written(t):
            die(f"{t.id}: `tracker issue` listed no update for it to write; run `tracker issue` first")
        left = next((items for x, items in tr.issue_updates_due() if x.id == t.id), None)
        print(f"{t.id}: written as listed" + (f"; due since the listing: {'; '.join(left)}" if left else ""))
        if not updates:
            return  # no fields given: the issue was written, not read
    if updates:
        t.save(updates)  # not `updated_at`: reading the issue is no change to the work
    state = tr.raw_state()
    state.setdefault("issues", {}).setdefault("read", {})[t.id] = time.time()
    tr.save_state(state)
    print(f"{t.id}: " + (", ".join(f"{k}={v or '(none)'}" for k, v in updates.items()) or "read; nothing to record"))


def issue_urls(t: Record) -> str:
    return ", ".join(link_url(x) for x in t.links if x.label == ISSUE)


def only_tickets(r: Record) -> None:
    """Refuse a record that is no ticket, naming the command that takes a note on it."""
    if r.kind != "ticket":
        die(f"{r.id} is {a_kind(r.kind)}: `tracker {KINDS[r.kind].command} {r.id} --note \"...\"`")


def open_ticket(args) -> tuple[Tracker, Record]:
    """The ticket a submission or review is about: refused when it is no ticket or is closed."""
    tr, t = locate(args, args.id)
    only_tickets(t)
    if t.closed:
        die(f"{t.id} is closed: a closed ticket takes no submission or review")
    return tr, t


def cmd_submit(args):
    """Hand a ticket's plan or built code to its reviewer: an event on the ticket, which its move then shows."""
    tr, t = open_ticket(args)
    note = f": {args.note}" if args.note else ""
    append_event(tr, "submission", f"{t.id} {HANDED[args.what].ready}{note}", [t.id], what=args.what)
    print(f"submitted {t.id}'s {args.what}")


def cmd_review(args):
    """Record a review of a ticket's plan or built code, with its numbered findings, answering the submission before it
    (`last_submission`); or, with --fixed, mark findings of the latest review fixed. Every check runs before the
    write."""
    if args.fixed is not None and (args.verdict or args.finding):
        die("--fixed marks findings of the latest review fixed, and a new review takes --verdict (with --finding): "
            "pass one or the other")
    if args.fixed is None and not args.verdict:
        die("give the review's --verdict pass|changes, or --fixed <numbers> to mark its findings fixed")
    if args.verdict == "changes" and not args.finding:
        die("a review that asks for changes lists them: one --finding per change")
    if args.verdict == "pass" and args.finding:
        die("a finding is a change asked for: use --verdict changes, or leave the finding out")
    tr, t = open_ticket(args)
    events, name = read_events(tr), HANDED[args.what].review
    if args.fixed is not None:
        review = last_review(events, t.id, args.what) or die(f"{t.id} has no {name} whose findings to mark fixed")
        have = finding_numbers(review)
        given = csv(args.fixed)
        if not given or any(not x.isdigit() or int(x) not in have for x in given):
            die(f"{t.id}'s latest {name} has " + (f"findings {', '.join(map(str, have))}: --fixed takes those numbers"
                                                    if have else "no findings"))
        fixed = list(dict.fromkeys(int(x) for x in given))
        listed = ", ".join(map(str, fixed))
        append_event(tr, "finding.fixed", f"{t.id} fixed findings {listed} of the {name}", [t.id], what=args.what,
                     of=review["seq"], findings=fixed)
        print(f"marked findings {listed} of {t.id}'s {name} fixed")
        return
    findings = [{"n": i, "text": " ".join(x.split())} for i, x in enumerate(args.finding or [], 1)]
    said = f"{args.verdict}, {count(len(findings), 'finding')}" if findings else args.verdict
    listed = "; ".join(f"{f['n']}. {f['text']}" for f in findings)
    submission = last_submission(events, t.id, args.what)
    append_event(tr, "review", f"{t.id} {name}: {said}" + (f": {listed}" if listed else ""), [t.id], what=args.what,
                 verdict=args.verdict, findings=findings, of=submission["seq"] if submission else 0)
    print(f"reviewed {t.id}'s {args.what}: {said}")


def cmd_step(args):
    """A step in one command: the ticket's next action (or its close), Carry forward and a log line; then, when the
    ticket is on the current branch, the branch's commits that the hooks have not logged are logged, its mark moves
    and its handoff goes (with --pause, one is left). A step on another branch's ticket leaves this branch's mark and
    handoff to its own tickets. With no arguments, only the branch: its commits, its mark and its handoff."""
    if not (args.id or args.message or args.next or args.done or args.pause or args.carry):
        m, cwd = on_branch(args)
        print("; ".join(mark_up_to_date(m, cwd)))
        return
    fit("next", args.next)
    tr, t = named_or_own(args)
    only_tickets(t)
    if args.done and (args.next or args.pause):
        die("--done closes the ticket: a closed ticket has no next action, and its branch keeps no handoff. Pass "
            "--next or --pause without --done")
    cwd = work_dir()
    m = match_cwd(cwd, tracker=tr) if branch_of(cwd) else None
    here = m if m and t.id in {x.id for x in m.tickets} else None
    if args.pause and not here:
        die(f"--pause leaves a handoff on {t.id}'s branch: run it there (here: {m.branch if m else 'no git branch'})")
    updates, done = {}, []
    if args.next:
        updates["next"] = args.next
        done.append("next set")
    if args.done:
        updates.update(t.status_update("done"), summary=args.done)
        done.append("done")
    for fact in args.carry or []:
        append_to_section(t, "Carry forward", fact if BULLET.match(fact) else f"- {fact}")
    if args.carry:
        done.append(f"{len(args.carry)} carry forward")
    if updates or args.carry:
        t.change(updates)
    if args.message:
        append_event(tr, "step", args.message, [t.id])
        done.append("logged")
    if args.done:
        append_event(tr, "status", status_line(t), [t.id], status=str(t.get("status")))
    parts = [f"{t.id}: {', '.join(done) or 'nothing recorded'}"]
    if here:
        parts += mark_up_to_date(here, cwd, args.pause, list(dict.fromkeys([t.id, *(x.id for x in here.focus)])))
    elif m:
        parts.append(f"{m.branch} not marked: {t.id} is not on it")
    print("; ".join(parts))


def tracker_matches(words: list[str]) -> tuple[list[Tracker], bool]:
    """The trackers a name names, and whether the match is sure: an exact slug, or the trackers whose slug and title
    hold every word; else, as options only, the trackers that hold any word."""
    trackers = all_trackers()
    want = [w.lower() for w in words]
    exact = [t for t in trackers if t.slug.lower() == "-".join(want)]
    if exact:
        return exact, True

    def text(t: Tracker) -> str:
        return f"{t.slug} {t.title}".lower().replace("-", " ")

    every = [t for t in trackers if all(w.replace("-", " ") in text(t) for w in want)]
    return (every, True) if every else ([t for t in trackers if any(w.replace("-", " ") in text(t) for w in want)],
                                        False)


def named_trackers(words: list[str]) -> tuple[list[Tracker], bool, str]:
    """`tracker_matches`, and the head `one_tracker` prints when they are not one sure tracker."""
    hits, sure = tracker_matches(words)
    head = f"'{' '.join(words)}' " + ("matches more than one tracker:" if hits and sure else
                                      "names no tracker for sure; the closest:" if hits else
                                      "matches no tracker; trackers:")
    return hits, sure, head


def one_tracker(hits: list[Tracker], sure: bool, head: str, cmd: str) -> Tracker:
    """The one tracker a name matched for sure; else `head`, the options and exit 3, so the model asks the user."""
    if len(hits) == 1 and sure:
        return hits[0]
    options = hits or all_trackers() or die(NO_TRACKERS)
    print("\n".join([head, *(f"  {t.slug} — {t.headline()}" for t in options),
                     f"Pass the slug: `tracker {cmd} <slug>`"]))
    sys.exit(3)


def cmd_start(args):
    """Tie a tracker to this agent session: its commands and hooks then use it; `--on` puts the session on some of
    its tickets alone. With no name: the tracker that holds the `--on` tickets, else the tracker with an open ticket
    on this branch; in a session that has a tracker, its brief, then the `other_format` sentence when this code may
    not write it. Never changes git; writes the tracker only to record a branch that its PR ties to one ticket, and to
    log the branch's new commits, as the hooks do. Refuses a tracker in another format (`refuse_other_format`) before
    the session moves."""
    sid, cwd = session_id(), work_dir()
    if args.clear:
        drop_session(sid)
        print("this session is on no tracker now" + ("" if sid else " (no session id: nothing to clear)"))
        return
    if args.decline:
        branch = branch_of(cwd) or die("not on a git branch")
        decline(cwd)
        own = session_tracker(sid)
        if own:  # any link goes; most often one the session start made (AUTO_LINKED)
            drop_session(sid)
        print((f"this session is off {own.slug} now; " if own else "") + f"branch '{branch}': no link or offer of a "
              f"tracker for {DECLINE_S // 3600} h; `tracker start <name>` links one now")
        return
    focus = id_list(args.on)
    if not args.name and not focus and session_tracker(sid):
        m = match_cwd(cwd, sid)
        why = other_format(m.tracker.root)
        # A session told its tracker takes no write got that line in place of its brief (hooks.tell_refusal), so it
        # has no protocol: the first brief once this code may write the tracker brings it.
        told = bool(load_session(sid).get("told_refusal")) and not why
        print(brief(m, cwd, with_protocol=told))
        if told:
            save_session(sid, told_refusal=None)
        if why:  # the brief asks for writes that refuse_other_format refuses
            print(f"{why}.")
        tell_updates(m.tracker)
        return
    if focus and not args.name:
        hits, sure, head = [locate(args, focus[0])[0]], True, ""
    elif args.name:
        hits, sure, head = named_trackers(args.name)
    else:
        found = branch_matches(cwd)
        hits, sure = [m.tracker for m in found], True
        head = (f"branch '{found[0].branch}' has open tickets in more than one tracker:" if found else
                f"branch '{branch_of(cwd) or '?'}' is on no open ticket in any tracker; trackers:")
    tr = one_tracker(hits, sure, head, "start")
    picks = [tr.find(i) for i in focus]
    closed = [f"{r.id} is {r.stage if r.kind == 'ticket' else a_kind(r.kind)}" for r in picks
              if r.kind != "ticket" or r.closed]
    if closed:
        die(f"--on takes open tickets: {'; '.join(closed)}")
    if not sid:
        die("no agent session id: set TRACKER_SESSION, or pass --tracker "
            f"{tr.slug} to each command, or set TRACKER={tr.slug}")
    refuse_other_format(tr.root)  # before the session moves: a start writes tr's mark and commits (record_commits)
    drop_session(sid)  # a new start: nothing seen yet
    save_session(sid, tracker=tr.slug, focus=[r.id for r in picks] or None)
    m, found = match_pr(match_cwd(cwd, sid), cwd)
    record_commits(m, cwd)
    remember(sid, m, cwd)
    print(brief(m, cwd, note=found))
    tell_updates(m.tracker)


def cmd_watch(args):
    """A watch, in a terminal or in an agent session, which it gives the grant (the hooks and the UIs know a watching
    session by it); `--stop` ends the session's watches. The model runs it only when told to: its help and the watch
    skill say so."""
    inside, sid = agent_session()
    if args.role and not inside:
        die(f"--as {args.role}: a session takes the role, not a terminal")
    if inside and not grant_path(sid):
        die("this session has no id the tracker knows (its hooks did not run): it cannot watch", REFUSED)
    if args.stop:
        if not inside:
            die("a terminal's watch ends with Ctrl-C")
        set_grant(sid, False)
        print("The watch has ended: a `tracker watch` of this session still running stops within seconds.")
        return
    only = frozenset(k.strip() for k in (args.only or "").split(",") if k.strip())
    if only - {*EVENT_KINDS, URGENT}:
        die(f"--only takes {', '.join([*EVENT_KINDS, URGENT])}")
    if args.hold < 0:
        die("--hold takes seconds: 0 or more")
    tr = named_tracker(args, "watch")
    Watcher(tr, sid if inside else "", f"session {session_name(sid)}" if inside else "terminal", only,
            args.hold, args.role or "").run(args.once)


def cmd_agents(args):
    print("\n".join(agents_lines(named_tracker(args, "agents"), time.time())))


def named_tracker(args, cmd: str) -> Tracker:
    """The tracker the words in `args.name` name; with none, the one here (`find_tracker`). For a command a session on
    no tracker runs (the watch's)."""
    if args.name:
        hits, sure, head = named_trackers(args.name)
    else:
        found = find_tracker(args)
        hits, sure, head = ([found], True, "") if found else ([], False, "which tracker? trackers:")
    return one_tracker(hits, sure, head, cmd)


def move_folder(move, failed: str) -> None:
    """Archive, unarchive or delete a tracker: print what `move` did. A refusal exits REFUSED inside `move`; a failure
    says `failed` and why."""
    try:
        print(move())
    except OSError as exc:
        die(f"{failed}: {exc}")


def cmd_archive(args):
    tr = tracker_at(args.slug) or die(no_tracker(args.slug))
    move_folder(lambda: archive_tracker(tr, session_id()), "not archived")


def cmd_unarchive(args):
    tr = archived_at(args.slug) or die(f"no archived tracker '{args.slug}' (`tracker list --archived` lists them)")
    move_folder(lambda: unarchive_tracker(tr), "not brought back")


def cmd_delete(args):
    """The user's: in a terminal, or from the UI's tracker menu. The model never deletes a tracker: the command
    refuses an agent session."""
    if agent_session()[0]:
        die("only the user deletes a tracker: in a terminal (`tracker delete <slug>`) or from the UI's tracker "
            "menu. Do not delete one yourself.", REFUSED)
    tr = any_tracker(args.slug) or die(f"no tracker '{args.slug}' (`tracker list --archived` lists them)")
    if not args.yes:
        if sys.stdin is None or not sys.stdin.isatty():
            die("pass --yes to delete without the prompt")
        answer = input(f"Delete {tr.slug} ({tr.title}): move {tr.root} to the system's trash? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            die("not deleted", 1)
    move_folder(lambda: delete_tracker(tr), "not deleted")


def cmd_use(args):
    cwd = work_dir()
    branch = branch_of(cwd) or die("not on a git branch")
    key = worktree_key(cwd, branch)
    if args.clear:
        chose = [(t, state) for t in all_trackers() if (state := t.state()).get("use", {}).pop(key, None) is not None]
        for t, _ in chose:  # a refusal changes nothing: each tracker is checked before any is written
            refuse_other_format(t.root)
        for t, state in chose:
            t.save_state(state)
            print(f"{branch}: cleared the choice in {t.slug}")
        return
    ids = id_list(args.ids)
    if not ids:
        die("name the ticket(s) this branch works on, or pass --clear")
    tr, first = locate(args, ids[0])
    recs = [first, *(tr.find(i) for i in ids[1:])]
    if any(r.kind != "ticket" for r in recs):
        die("`use` takes ticket ids")
    if branch not in default_branches(cwd):
        # Recorded on each ticket (set_branch); the branch keeps its other tickets.
        for t in recs:
            if t.get("branch") != branch:
                set_branch(tr, t, branch, f"was {t.get('branch')}" if t.get("branch") else "")
        print(f"{branch}: {', '.join(t.id for t in tr.tickets_on(branch))}")
        return
    state = tr.state()
    state.setdefault("use", {})[key] = [r.id for r in recs]
    tr.save_state(state)
    print(f"{key}: works on {', '.join(r.id for r in recs)} (this worktree and branch only; `tracker use --clear` "
          f"undoes it)")


def cmd_open(args):
    here = find_tracker(args) if args.id else None
    whole = args.id and not (here and here.lookup(args.id)) and any_tracker(args.id)
    tr, rec = (whole, None) if whole else locate(args, args.id) if args.id else (resolve(args), None)
    record_cli()  # a mod finds the CLI by this, also before any hook has run
    update_mod()  # and shows this version
    host = open_ui(tr.slug, rec.id if rec else "", window=args.window)
    if not host:
        die(NO_HOST)
    print(f"opened {tr.slug}{f' {rec.id}' if rec else ''} in {host}")
    if host != "Herdr" and not args.window and herdr_home().is_dir() and not mod_dir().exists():
        print(MOD_OFFER)


def cmd_install_window(args):
    print(f"gpui-shell {GPUI_SHELL_VERSION} is in {install_window()}. `tracker open --window` opens the view.")


def cmd_install_mod(args):
    print(f"the work-tracker mod is in {install_mod()}. `tracker open` shows it in a Herdr tab; each later plugin "
          "version updates it.")


def shown_tracker(args) -> Tracker:
    """The tracker a UI asks about: an archived one too, as `open` shows it."""
    return (any_tracker(args.slug) or die(f"no tracker '{args.slug}'")) if args.slug else resolve(args)


def cmd_data(args):
    """A UI's one read of a tracker: viewmodel.view_model as JSON."""
    print(json.dumps(view_model(shown_tracker(args))))


def cmd_await_change(args):
    """A UI's wait for something new to read, when its host has no folder watch."""
    await_change(shown_tracker(args))


def cmd_attached(args):
    """A UI's question: which tracker the agent it follows is on."""
    print(json.dumps({"slug": attached_tracker(args.pid, args.cwd) or None}))


def cmd_refresh(args):
    """A UI's Refresh: ask again for every open ticket's issue fields, and pull PR state unless a sync just ran. Only
    the model can read an issue tracker (through its tool), so this records the request; the next prompt hook passes it
    on."""
    tr = resolve(args)
    with locked():
        state = tr.raw_state()
        state.setdefault("issues", {})["requested"] = time.time()
        tr.save_state(state)
    print("\n".join(sync(tr, every=REFRESH_SYNC_S)) or "no changes")


def cmd_evidence(args):
    """A UI's evidence link: the user's click opens the file, and only a kind of file that runs no code; any other kind
    shows in its folder. The agent reads evidence itself, and has no reason to put a window on the user's screen."""
    if agent_session()[0]:
        die("only the user opens evidence this way: read the file instead", REFUSED)
    slug = args.tracker
    tr = (slug and any_tracker(slug)) or resolve(args)  # an archived tracker still opens in a UI
    folder = (tr.root / EVIDENCE_DIR).resolve()
    path = folder.joinpath(*Path(args.file).parts).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        die(f"no file '{args.file}' in {folder}")
    try:
        open_with_system(path, reveal=path.suffix.lower() not in EVIDENCE_OPENS)
    except (OSError, subprocess.CalledProcessError) as exc:
        die(f"could not open {path}: {exc}")
    print(path)


def tell_updates(tr: Tracker) -> None:
    """This session was just told tr's issue updates due (a brief, a write's request): its prompt hook does not
    repeat them."""
    sid = session_id()
    own = session_tracker(sid) if sid else None
    if own and own.slug == tr.slug:
        save_session(sid, told_updates=updates_told(tr))
