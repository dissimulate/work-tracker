"""The `tracker` command line: one function per command, and the parser."""

from __future__ import annotations

import datetime as dt
import re
import shutil
import sys
import time
from pathlib import Path

from .markdown import (BULLET, bullets, format_value, headings, parse_links, render_frontmatter, section_block,
    split_frontmatter)
from .model import (BLOCKER, CLOSED_TICKET, DECISION_ID, DECISION_SECTIONS, DECISION_STATUSES, DEFAULT_LABELS,
    EVIDENCE, EVIDENCE_DIR, HOME, IN_FLIGHT, ISSUE, KEYS, LIST_KEYS, LIST_OR_ONE, OWNER_HINT, ROOT, SAFE_NAME, SCHEMA,
    STAGES, TICKET_SECTIONS, TICKET_STATUSES, TRACKER_STATUSES, all_trackers, append_log, append_to_section,
    blocker_link, create, csv, dated, die, drop_from_section, fit, id_list, link_url, load_record, locked, names,
    norm_id, put_section, relabel, replace_in_section, resolution, same_repo, sequence, short, sort_key, spawn, today,
    unblocked, utc_now, Busy, Record, Tracker)
from .git import branch_of, contains, default_branches, worktree_key
from .session import (DECLINE_S, NO_TRACKERS, branch_matches, decline, drop_session, find_tracker, in_repos,
    load_session, locate, mark_up_to_date, match_cwd, on_branch, record_commits, remember, resolve, save_session,
    session_id, session_tracker, trackers_for_repo, watch, work_dir)
from .contract import check, migrate, rules_lines
from .views import (CHAIN_CARRY_FORWARD_MAX, CONTEXT_LOG, brief, context_lines, dep_lines, index_lines, order_lines,
    span_lines, start_text)
from .github import match_pr, sync
from .watcher import REFUSED, Watcher, agent_session, granted, session_name, watching

# ---------------------------------------------------------------- templates

TEMPLATES = ROOT / "templates"


def from_template(name: str, meta: dict, labels: list[str]) -> str:
    """A new file from templates/<name>.md; `{{labels}}` in its comments becomes the tracker's link labels."""
    text = (TEMPLATES / f"{name}.md").read_text()
    lines, body = split_frontmatter(text)
    lines = render_frontmatter(lines, meta)
    return "---\n" + "\n".join(lines) + "\n---\n" + body.replace("{{labels}}", ", ".join(labels))


# ---------------------------------------------------------------- commands

def cmd_list(args):
    trackers = all_trackers()
    if not trackers:
        print(NO_TRACKERS)
    for t in trackers:
        active = sum(1 for x in t.tickets if x.stage not in CLOSED_TICKET)
        print(f"{t.slug:<36} {t.headline()} · {len(t.tickets)} tickets, {active} active · "
              f"{len(t.open_decisions())} open decisions")


def cmd_init(args):
    if not SAFE_NAME.fullmatch(args.slug):
        die(f"'{args.slug}' is not a slug: use letters, digits, `.`, `_` and `-`, starting with a letter or digit")
    root = HOME / args.slug
    if (root / "README.md").exists():
        die(f"{root} already exists")
    (root / "tickets").mkdir(parents=True, exist_ok=True)
    (root / "decisions").mkdir(exist_ok=True)
    repos = csv(args.repo)
    meta = {"title": args.title, "repo": repos if len(repos) > 1 else "".join(repos), "status": "planning",
            "owner": args.owner, "created": today(), "schema": SCHEMA}
    create(root / "README.md", from_template("README", meta, DEFAULT_LABELS))
    create(root / "log.md", f"# Log — {args.title}\n\nAppend-only. Newest at the bottom.\n\n")
    create(root / ".gitignore", ".state.json\n*.tmp\n")
    print(f"created {root}. Next: `tracker start {args.slug}` ties this session to it (in a terminal, pass "
          f"`--tracker {args.slug}` to each command), then `tracker new <ID> --title \"...\"` adds tickets")


def cmd_index(args):
    tr = resolve(args)
    stages = set(csv(args.status)) or None
    if stages and stages - set(STAGES):
        die(f"--status takes {'|'.join(STAGES)}")
    if args.active:
        stages = set(STAGES) - CLOSED_TICKET
    lines = index_lines(tr, stages, args.group)
    print("\n".join(lines[:1] + span_lines(tr) + lines[1:]))


def cmd_here(args):
    cwd = work_dir()
    m = match_cwd(cwd, tracker=find_tracker(args))
    if not m:
        repo = trackers_for_repo(cwd)
        if repo:
            print(f"this session and branch '{branch_of(cwd)}' are on no ticket. Trackers for this repo: "
                  + ", ".join(t.slug for t in repo) + ". `tracker start <name>` ties this session to a tracker.")
        else:
            print(f"this session and branch '{branch_of(cwd) or '?'}' are on no ticket in any tracker. "
                  "`tracker start <name>` ties this session to a tracker.")
        return
    print(brief(m, cwd, full=args.full, with_protocol=False))


def cmd_context(args):
    tr, rec = locate(args, args.id)
    print("\n".join(context_lines(tr, rec, full=not args.brief, deep=args.deep, log=args.log)))


def cmd_decisions(args):
    tr = resolve(args)
    status = args.status or ("" if args.all else "open")
    rows = [d for d in tr.decisions if not status or d.get("status", "open") == status]
    for d in rows:
        state = d.get("status", "open")
        touched = ", ".join(tr.touched_by(d))
        tail = f" → {resolution(d)}" if state == "closed" else (f" · owner {d.get('owner')}" if d.get("owner") else "")
        print(f"{d.id}  {state:<6}  {d.get('title')}" + (f"  ({touched})" if touched else "") + tail)
    if not rows:
        print("no decisions" if args.all or args.status else "no open decisions")
    settled = sum(d.get("status") == "closed" for d in tr.decisions)
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
    m = match_cwd(work_dir(), tracker=find_tracker(args))
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
            allowed = TICKET_STATUSES if rec.kind == "ticket" else TRACKER_STATUSES
            if v not in allowed:
                hint = "; in-review and merged come from the ticket's PR" if v in STAGES else ""
                die(f"status must be one of {'|'.join(allowed)}{hint}")
        if k in ("next", "summary") and rec.kind == "ticket":
            fit(k, v)
        if k == "repo" and rec.kind == "ticket" and v and v not in tr.repos:
            die(f"repo must be one of the tracker's repos ({', '.join(tr.repos) or 'none'})")
        items = csv(v)
        updates[k] = items if k in LIST_KEYS or (k in LIST_OR_ONE and len(items) > 1) else v
    notes = []
    if rec.kind == "ticket" and updates.get("status") in ("done", "dropped"):
        if rec.get("next") and "next" not in updates:
            updates["next"] = ""  # a closed ticket has no next action; its summary says what it delivered
        if not (updates.get("summary") or rec.get("summary")):
            notes.append(f"set its summary: `tracker set {rec.id} summary=\"<what it delivered, or why dropped>\"`")
    if rec.kind == "ticket" and updates.get("status") == "in-progress":
        if "branch" not in updates:
            notes += start_here(tr, rec, updates)
        # The first start, from todo: a cycle time runs from it. A ticket started before 0.29 gets none, not a guess.
        if not rec.get("started_at") and rec.get("status") == "todo":
            updates["started_at"] = utc_now()
    if rec.kind != "tracker":
        updates["updated"] = today()
    blocked = {t.id for t in tr.tickets if tr.blockers(t)}
    was = rec.stage if rec.kind == "ticket" else ""
    rec.save(updates)
    print(f"{rec.id}: " + ", ".join(k if len(str(v)) > 40 else f"{k}={format_value(v)}"
                                    for k, v in updates.items() if KEYS[rec.kind][k][0] != "auto"))
    if was == "todo" and updates.get("status") == "in-progress":
        notes += base_notes(tr, rec)
    if rec.kind == "ticket" and "status" in updates:
        notes += unblocked(tr, blocked)
    if notes:
        print("\n".join(notes))


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
    chosen = state.get("use", {}).get(key, [])  # unfinished tickets only
    if t.id not in chosen:
        state.setdefault("use", {})[key] = chosen + [t.id]
        tr.save_state(state)
    return [f"{t.id} is worked on in this worktree on {branch} (`tracker use --clear` undoes it)"]


def base_notes(tr: Tracker, t: Record) -> list[str]:
    """A ticket started on the current branch: name the branches of the tickets under way it waits on that this
    branch does not contain (START_RULE). Git is only read."""
    cwd = work_dir()
    under = [d.rec for d in tr.blockers(t) if d.kind == "ticket" and d.rec.stage in IN_FLIGHT and d.rec.get("branch")
             and same_repo(tr.repo_of(d.rec), tr.repo_of(t))]
    if not under or t.get("branch") != branch_of(cwd) or not in_repos(tr, cwd):  # git runs only past here
        return []
    missing = [b for b in under if contains(cwd, str(b.get("branch"))) is False]
    if not missing:
        return []
    where = ", ".join(f"{b.get('branch')} ({b.id}" + (f", #{b.get('pr')}" if b.get("pr") else "") + ")"
                      for b in missing)
    return [f"{t.id} waits on work under way that branch {t.get('branch')} does not contain: {where}. Start the branch "
            f"from it, unless the user chose otherwise (`tracker ready` names the branch to start from)."]


def cmd_log(args):
    fit("log", args.message)
    tr = resolve(args)
    refs = [tr.find(r).id for r in id_list(args.ref)]
    append_log(tr, args.message, refs)
    print("logged" + (f" [{' '.join(refs)}]" if refs else ""))


def cmd_new(args):
    fit("next", args.next)
    tr = resolve(args)
    if not SAFE_NAME.fullmatch(args.id):
        die(f"'{args.id}' is not a ticket id: use letters, digits, `.`, `_` and `-`, starting with a letter or digit")
    path = tr.root / "tickets" / f"{args.id}.md"
    if path.exists() or tr.lookup(args.id):
        die(f"{args.id} already exists")
    if DECISION_ID.fullmatch(args.id):
        die("D-<n> ids are decisions; open one with `tracker decide \"<title>\"`")
    deps = []
    for x in id_list(args.depends):
        rec = tr.lookup(x) or die(f"--depends {x}: no such ticket or decision; for an external blocker, "
                                  f"`tracker wait {args.id} on {x} --link \"<url> — <why>\"` after creating the ticket")
        deps.append(rec.id)
    if args.repo and args.repo not in tr.repos:
        die(f"--repo must be one of the tracker's repos ({', '.join(tr.repos) or 'none'})")
    if args.branch and len(tr.repos) > 1 and not args.repo:
        die(f"the tracker spans {len(tr.repos)} repos: pass --repo ({', '.join(tr.repos)})")
    meta = {"id": args.id, "title": args.title, "group": args.group or "", "status": "todo",
            "branch": args.branch or "", "depends_on": [], "next": args.next or "", "updated": today()}
    if args.repo:
        meta["repo"] = args.repo
    path.parent.mkdir(exist_ok=True)
    create(path, from_template("ticket", meta, tr.labels))
    t = load_record(path, "ticket")
    tr.tickets.append(t)
    add_waits(tr, t, deps)
    append_log(tr, f"Added {args.id} {args.title}" + (f"; waits on {', '.join(deps)}" if deps else ""), [args.id])
    print(f"created {args.id}: {path}")


def resolve_decision(tr: Tracker, rec: Record, answer: str, by: str | None) -> str:
    who = f" ({by})" if by else ""
    blocked = {t.id for t in tr.waiting_on(rec.id) if tr.blockers(t)}
    rec.save({"status": "closed", "updated": today()})
    append_to_section(rec, "Resolution", f"{today()}{who}: {answer.strip()}")
    append_log(tr, f"Decided {rec.id} {rec.get('title')}{who}: {short(answer)}", [rec.id, *tr.touched_by(rec)])
    return "\n".join([f"{rec.id} closed", *unblocked(tr, blocked)])


def add_waits(tr: Tracker, t: Record, idents: list[str]) -> list[str]:
    """Add items to a ticket's depends_on, refusing one that would close a cycle. Returns the items added."""
    before = t.list("depends_on")
    have = {x.lower() for x in before}
    added = [x for x in dict.fromkeys(idents) if x.lower() not in have]
    if not added:
        return []
    t.save({"depends_on": before + added, "updated": today()})
    cycle = next((c for c in sequence(tr).cycles if t.id in c), None)
    if cycle:
        t.save({"depends_on": before})
        die(f"that would make a dependency cycle: {' → '.join(cycle)}")
    return added


def cmd_wait(args):
    args.items = id_list(args.items)
    tr, t = locate(args, args.id)
    if t.kind != "ticket":
        die(f"{t.id} is a decision; a ticket waits on it: `tracker wait <ticket> on {t.id}`")
    if args.mode == "off":
        drop = {(tr.lookup(x).id if tr.lookup(x) else x).lower() for x in args.items}
        keep = [x for x in t.list("depends_on") if (tr.lookup(x).id if tr.lookup(x) else x).lower() not in drop]
        if len(keep) == len(t.list("depends_on")):
            stacked = [o.id for o in tr.stacked_on(t) if o.id.lower() in drop]
            die(f"{t.id} waits on {', '.join(stacked)} because its PR is based on their branch "
                f"{t.get('base')}; that ends when the PR is retargeted" if stacked else
                f"{t.id} does not wait on {', '.join(args.items)}")
        t.save({"depends_on": keep, "updated": today()})
        for x in args.items:  # a Blocker line for something no longer waited on becomes a plain link
            link = None if tr.lookup(x) else blocker_link(t, x)
            if link:
                relabel(t, link, "Related")
        append_log(tr, f"{t.id} no longer waits on {', '.join(args.items)}", [t.id])
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
                    what = (f"no decision {x}" if DECISION_ID.fullmatch(x)
                            else f"{x} is not a ticket or decision in {tr.slug}")
                    die(f"{what}. For an external blocker, say where it is and why it blocks, one blocker per "
                        f"command: `--link \"<url> — <why>\"` (adds a `- {BLOCKER}:` line to ## Links)")
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
            if d and d.kind == "decision" and t.id in d.list("refs"):  # depends_on now says so
                d.save({"refs": [r for r in d.list("refs") if r != t.id], "updated": today()})
        append_log(tr, f"{t.id} now waits on {', '.join(added)}", [t.id])
    print("\n".join(dep_lines(tr, t)) or f"{t.id} waits on nothing")


def cmd_seq(args):
    tr = resolve(args)
    seq = sequence(tr)
    rows = sorted((t for t in tr.tickets if t.stage != "dropped" and (not args.active or t.stage not in CLOSED_TICKET)),
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
    """The todo tickets that can start now: from the default branch, or stacked on work under way (START_RULE), with
    what each unblocks."""
    tr = resolve(args)
    starts = tr.startable()
    for t, s in starts:
        later = ", ".join(o.id for o in tr.waiting_on(t.id))
        print(f"{t.id}  {t.get('group') or '—'}  {t.get('title')}" + (f" — {start_text(tr, s)}" if s.stacked else "")
              + (f" · unblocks {later}" if later else ""))
    if not starts:
        print("nothing can start now")


# ---------------------------------------------------------------- body edits
# `add` and `drop` change one line of a section of a ticket, a decision or the README, and `put` the whole section, so
# a fact goes in without a hand edit of the file. A list section takes one bullet per `add`; link lines are checked as
# `check` would. The edits themselves are model's.

LIST_SECTIONS = {"Carry forward", "Links", "Context"}
LINK_SECTIONS = {"Links", "Context"}
OWNED_SECTIONS = {"Resolution": "`tracker decide D-<n> --resolve`"}


def record_for(args, ident: str) -> tuple[Tracker, Record]:
    """A ticket or decision, or the README for `tracker` / `readme`."""
    if ident.lower() in ("readme", "tracker"):
        tr = resolve(args)
        rec = load_record(tr.root / "README.md", "tracker")
        rec.meta["id"] = "README"
        return tr, rec
    return locate(args, ident)


def section_named(rec: Record, word: str) -> str:
    """The section a word names: a case-free prefix of one of the record's sections (`carry` → Carry forward)."""
    known = {"ticket": TICKET_SECTIONS, "decision": DECISION_SECTIONS}.get(rec.kind) or headings(rec.body)
    hits = [h for h in known if h.lower().startswith(word.lower().strip())]
    if len(hits) != 1:
        die(f"'{word}' names {'none' if not hits else 'more than one'} of {rec.id}'s sections: {', '.join(known)}")
    if hits[0] in OWNED_SECTIONS:
        die(f"## {hits[0]}: use {OWNED_SECTIONS[hits[0]]}")
    return hits[0]


def require_links(tr: Tracker, text: str) -> None:
    items, bad = parse_links(text)
    if bad or not items:
        die("a link line is `- Label: [title](url) — why it matters`" + (f", not: {short(bad[0], 80)}" if bad else ""))
    for x in items:
        if x.label not in tr.labels:
            die(f"label '{x.label}' is not one of {', '.join(tr.labels)} (`tracker set tracker labels=...` adds one)")


def touch(rec: Record) -> None:
    if rec.kind != "tracker":
        rec.save({"updated": today()})


def cmd_add(args):
    tr, rec = record_for(args, args.id)
    heading = section_named(rec, args.section)
    text = args.text.strip()
    if heading == "Carry forward":
        fit("carry", text)
    if args.replace:
        replace_in_section(rec, heading, args.replace, text)
        touch(rec)
        print(f"{rec.id} {heading}: replaced")
        return
    if heading in LIST_SECTIONS and not BULLET.match(text):
        text = "- " + text
    if heading in LINK_SECTIONS:
        require_links(tr, text)
    append_to_section(rec, heading, text)
    touch(rec)
    print(f"{rec.id} {heading}: added")


def cmd_put(args):
    """Replace a whole section: a rewritten Plan, a Carry forward kept short, a new set of Links."""
    tr, rec = record_for(args, args.id)
    heading = section_named(rec, args.section)
    text = args.text.strip("\n")
    if not text.strip():
        die("put takes the section's whole new text: pass it, or `-` and pipe it (a heredoc: <<'EOF')")
    if heading in LINK_SECTIONS:
        require_links(tr, text)
    if heading == "Carry forward":
        for b in bullets(text):
            fit("carry", b)
    put_section(rec, heading, text)
    touch(rec)
    print(f"{rec.id} {heading}: replaced")


def cmd_drop(args):
    rec = record_for(args, args.id)[1]
    heading = section_named(rec, args.section)
    n = drop_from_section(rec, heading, args.text)
    touch(rec)
    print(f"{rec.id} {heading}: dropped {n} line(s)")


def cmd_show(args):
    """Records' own text, whole or by section, found as any command finds them: what `cat` of their files gives,
    without the template's comments."""
    words = csv(args.section)
    out = []
    for ident in id_list(args.ids):
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


def cmd_attach(args):
    """Keep a file in the tracker's evidence/ folder and link it from the records it supports."""
    tr = resolve(args)
    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        die(f"no file {args.file}")
    folder = (tr.root / EVIDENCE_DIR).resolve()
    if src.is_relative_to(folder):
        name = str(src.relative_to(folder))
    else:
        name = args.name or re.sub(r"[^A-Za-z0-9._-]+", "-", src.name)
        if not SAFE_NAME.fullmatch(name):
            die(f"'{name}' is not a file name: use letters, digits, `.`, `_` and `-`")
        dest = folder / name
        if dest.exists() and dest.read_bytes() != src.read_bytes() and not args.force:
            die(f"{EVIDENCE_DIR}/{name} exists with other content: pass --name, or --force to replace it")
        folder.mkdir(exist_ok=True)
        shutil.copy2(src, dest)
    link = f"[{name}]({EVIDENCE_DIR}/{name})" + (f" — {args.note.strip()}" if args.note else "")
    refs = []
    for ident in id_list(args.ref):
        rec = tr.find(ident)
        if rec.kind == "ticket":
            if not any(f"({EVIDENCE_DIR}/{name})" in x.text for x in rec.links):
                append_to_section(rec, "Links", f"- {EVIDENCE}: {link}")
        else:
            append_to_section(rec, "Options", f"- {today()}: {EVIDENCE}: {link}")
        touch(rec)
        refs.append(rec.id)
    append_log(tr, f"Attached {EVIDENCE_DIR}/{name}" + (f": {short(args.note)}" if args.note else ""), refs)
    print(f"{folder / name}" + (f" · linked from {', '.join(refs)}" if refs else ""))


def similar(a: str, b: str) -> float:
    wa, wb = ({w for w in re.findall(r"[a-z0-9]{4,}", x.lower())} for x in (a, b))
    return len(wa & wb) / max(1, len(wa | wb))


def ticket_ids(tr: Tracker, raw: list[str] | None, flag: str) -> list[str]:
    out = []
    for r in id_list(raw):
        rec = tr.find(r)
        if rec.kind != "ticket":
            die(f"{flag} takes ticket ids; {r} is not a ticket")
        out.append(rec.id)
    return out


def block_tickets(tr: Tracker, ident: str, blocks: list[str]) -> list[str]:
    """Record that each ticket in `blocks` waits on this decision. Returns the tickets newly blocked."""
    return [t for t in blocks if add_waits(tr, tr.find(t), [ident])]


def cmd_decide(args):
    tr = resolve(args)
    blocks = ticket_ids(tr, args.blocks, "--blocks")
    refs = [r for r in ticket_ids(tr, args.refs, "--refs") if r not in blocks]  # depends_on already records a block
    unrefs = set(ticket_ids(tr, args.unref, "--unref"))
    if blocks and args.resolve:
        die("--blocks with --resolve: a settled decision blocks nothing")
    existing = next((d for d in tr.decisions if norm_id(d.id) == norm_id(args.target)), None)
    if existing:
        if not (args.note or args.resolve or refs or unrefs or blocks or args.owner or args.question):
            die("nothing to change: pass --note, --resolve, --refs, --unref, --blocks, --owner or --question")
        upd = {"updated": today()}
        if refs or unrefs or blocks:
            waiting = {t.id for t in tr.waiting_on(existing.id)} | set(blocks)
            upd["refs"] = sorted((set(existing.list("refs")) | set(refs)) - unrefs - waiting, key=sort_key)
        if args.owner:
            upd["owner"] = args.owner
        existing.save(upd)
        if args.question:
            append_to_section(existing, "Question", dated(args.question))
        if args.note:
            append_to_section(existing, "Options", f"- {dated(args.note)}")
            append_log(tr, f"Updated {existing.id}: {short(args.note)}", [existing.id, *tr.touched_by(existing)])
        newly = block_tickets(tr, existing.id, blocks)
        if newly:
            msg = f"{', '.join(newly)} now {'waits' if len(newly) == 1 else 'wait'} on {existing.id}"
            append_log(tr, msg, [existing.id, *newly])
            print(msg)
        if args.resolve:
            print(resolve_decision(tr, existing, args.resolve, args.by))
        else:
            print(f"{existing.id} updated")
        return
    if DECISION_ID.fullmatch(args.target):
        die(f"no decision {args.target}; to open one, pass its title instead")
    if unrefs:
        die("--unref changes an existing decision; pass its D-id")
    clash = [d for d in tr.open_decisions() if similar(str(d.get("title")), args.target) >= 0.5]
    if clash and not args.force:
        for d in clash:
            print(f"similar open decision: {d.id} {d.get('title')}")
        die("update that one with `tracker decide D-<n> --note ...`, or pass --force if this is a different "
            "decision", 3)
    nums = [int(m[1]) for d in tr.decisions if (m := re.fullmatch(r"D-(\d+)", d.id))]
    ident = f"D-{max(nums, default=0) + 1:02d}"
    meta = {"id": ident, "title": args.target, "status": "open", "refs": refs, "owner": args.owner or "",
            "opened": today(), "updated": today()}
    path = tr.root / "decisions" / f"{ident}.md"
    path.parent.mkdir(exist_ok=True)
    create(path, from_template("decision", meta, tr.labels))
    rec = load_record(path, "decision")
    append_to_section(rec, "Question", args.question or args.target)
    if args.note:
        append_to_section(rec, "Options", f"- {args.note}")
    tr.decisions.append(rec)
    if args.resolve:
        resolve_decision(tr, rec, args.resolve, args.by)
        print(f"{ident} recorded as settled: {path}")
    else:
        newly = block_tickets(tr, ident, blocks)
        append_log(tr, f"Opened {ident} {args.target}" + (f"; blocks {', '.join(newly)}" if newly else ""),
                   [ident, *refs, *newly])
        print(f"{ident} opened" + (f", blocks {', '.join(newly)}" if newly else "") + f": {path}")


def cmd_rules(args):
    print("\n".join(rules_lines(find_tracker(args))))


def cmd_migrate(args):
    tr = resolve(args)
    plan = migrate(tr, apply=False)
    if not plan:
        print("nothing to migrate")
        return
    print("\n".join(plan))
    if args.dry_run:
        return
    backup = HOME / ".backups" / f"{tr.slug}-{dt.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copytree(tr.root, backup, ignore=shutil.ignore_patterns(".DS_Store", "*.tmp"))  # Finder's, half-writes
    migrate(Tracker(tr.root), apply=True)
    append_log(tr, f"Migrated to the current tracker rules ({len(plan)} changes; backup {backup})", [])
    print(f"\nmigrated · backup: {backup}")
    errors, warnings = check(Tracker(tr.root))
    print(f"check: {len(errors)} errors, {len(warnings)} warnings" + (" — run `tracker check`" if errors else ""))


def problem_lines(tr: Tracker) -> list[str]:
    errors, warnings = check(tr)
    return [f"✗ {x}" for x in errors] + [f"⚠ {x}" for x in warnings]


def cmd_check(args):
    tr = resolve(args)
    lines = problem_lines(tr)
    print("\n".join(lines) or f"ok · {len(tr.tickets)} tickets · {len(tr.decisions)} decisions")
    sys.exit(1 if any(x.startswith("✗") for x in lines) else 0)


def cmd_sync(args):
    tr = resolve(args)
    changes = sync(tr, force=True)
    print("\n".join(changes) if changes else "no changes")


def cmd_issue(args):
    """Record what the model read from a ticket's issue tracker, or list the tickets whose issue fields are due."""
    if args.due:
        tr = resolve(args)
        due = tr.issue_due()
        print("\n".join(f"{t.id}  " + ", ".join(link_url(x) for x in t.links if x.label == ISSUE) for t in due)
              if due else "no ticket's issue fields are due")
        return
    if not args.id:
        die("give a ticket id, or --due for the tickets whose issue fields are due")
    tr, t = locate(args, args.id)
    if t.kind != "ticket" or not t.aliases:
        die(f"{t.id} has no Issue link: add one first (`tracker add {t.id} link \"Issue: [ID Title](url)\"`)")
    updates = {}
    if args.priority is not None:
        fit("priority", args.priority.strip())
        updates["priority"] = args.priority.strip() or None  # empty: the issue has no priority now
    if args.created is not None:
        updates["issue_created"] = utc_time(args.created)
    if updates:
        t.save(updates)
    state = tr.raw_state()
    state.setdefault("issues", {}).setdefault("read", {})[t.id] = time.time()
    tr.save_state(state)
    print(f"{t.id}: " + (", ".join(f"{k}={v or '(none)'}" for k, v in updates.items()) or "read; nothing to record"))


def utc_time(text: str) -> str:
    """An ISO 8601 time with its zone, as UTC to the second; refused without a zone, which would be a guess."""
    try:
        at = dt.datetime.fromisoformat(text.strip())
    except ValueError:
        die(f"'{text}' is not an ISO 8601 time (2026-10-01T09:30:00Z)")
    if at.tzinfo is None:
        die(f"'{text}' needs a time zone (Z or +10:00): the issue tracker gives one")
    return at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_synced(args):
    m, cwd = on_branch(args)
    print("; ".join(mark_up_to_date(m, cwd)))


def cmd_pause(args):
    m, cwd = on_branch(args)
    print("; ".join(mark_up_to_date(m, cwd, args.text)))


def cmd_step(args):
    """A step in one command: the ticket's next action (or its close), Carry forward and a log line; then, when the
    ticket is on the current branch, the branch's commits that the hooks have not logged are logged, its mark moves
    and its handoff goes (with --pause, one is left). A step on another branch's ticket leaves this branch's mark and
    handoff to its own tickets."""
    for kind, text in [("log", args.message), ("next", args.next), ("summary", args.done),
                       *(("carry", c) for c in args.carry or [])]:
        fit(kind, text)
    tr, t = named_or_own(args)
    if t.kind != "ticket":
        die(f"{t.id} is a decision: `tracker decide {t.id} --note \"...\"`")
    if args.done and (args.next or args.pause):
        die("--done closes the ticket: a closed ticket has no next action, and its branch keeps no handoff. Pass "
            "--next or --pause without --done")
    cwd = work_dir()
    m = match_cwd(cwd, tracker=tr) if branch_of(cwd) else None
    here = m if m and t.id in {x.id for x in m.tickets} else None
    if args.pause and not here:
        die(f"--pause leaves a handoff on {t.id}'s branch: run it there (here: {m.branch if m else 'no git branch'})")
    blocked = {x.id for x in tr.tickets if tr.blockers(x)}
    updates, done = {}, []
    if args.next:
        updates["next"] = args.next
        done.append("next set")
    if args.done:
        updates.update(status="done", summary=args.done, next="")
        done.append("done")
    for fact in args.carry or []:
        append_to_section(t, "Carry forward", fact if BULLET.match(fact) else f"- {fact}")
    if args.carry:
        done.append(f"{len(args.carry)} carry forward")
    if updates or args.carry:
        t.save({**updates, "updated": today()})
    if args.message:
        append_log(tr, args.message, [t.id])
        done.append("logged")
    parts = [f"{t.id}: {', '.join(done) or 'nothing recorded'}"]
    if here:
        parts += mark_up_to_date(here, cwd, args.pause, list(dict.fromkeys([t.id, *(x.id for x in here.focus)])))
    elif m:
        parts.append(f"{m.branch} not marked: {t.id} is not on it")
    notes = unblocked(tr, blocked) if args.done else []  # `check` after the write names a long Carry forward
    print("\n".join(["; ".join(parts), *notes]))


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
    on this branch; in a session that has a tracker, its brief. Never changes git; writes the tracker only to record a
    branch that its PR ties to one ticket, and to log the branch's new commits, as the hooks do."""
    sid, cwd = session_id(), work_dir()
    if args.clear:
        drop_session(sid)
        print("this session is on no tracker now" + ("" if sid else " (no session id: nothing to clear)"))
        return
    if args.decline:
        branch = branch_of(cwd) or die("not on a git branch")
        decline(cwd)
        print(f"branch '{branch}': no offer to link a tracker for {DECLINE_S // 3600} h; `tracker start <name>` "
              "links one now")
        return
    focus = id_list(args.on)
    if not args.name and not focus and session_tracker(sid):
        print(brief(match_cwd(cwd, sid), cwd, with_protocol=False))
        return
    if focus and not args.name:
        hits, sure, head = [locate(args, focus[0])[0]], True, ""
    elif args.name:
        hits, sure = tracker_matches(args.name)
        head = f"'{' '.join(args.name)}' " + ("matches more than one tracker" if len(hits) > 1 and sure
                                              else "matches no tracker")
        head += " for sure; the closest:" if hits and not sure else ":" if hits else "; trackers:"
    else:
        found = branch_matches(cwd)
        hits, sure = [m.tracker for m in found], True
        head = (f"branch '{found[0].branch}' has open tickets in more than one tracker:" if found else
                f"branch '{branch_of(cwd) or '?'}' is on no open ticket in any tracker; trackers:")
    tr = one_tracker(hits, sure, head, "start")
    picks = [tr.find(i) for i in focus]
    closed = [f"{r.id} is {r.stage if r.kind == 'ticket' else 'a decision'}" for r in picks
              if r.kind != "ticket" or r.stage in CLOSED_TICKET]
    if closed:
        die(f"--on takes open tickets: {'; '.join(closed)}")
    if not sid:
        die("no agent session id: set TRACKER_SESSION, or pass --tracker "
            f"{tr.slug} to each command, or set TRACKER={tr.slug}")
    drop_session(sid)  # a new start: nothing seen yet
    save_session(sid, tracker=tr.slug, focus=[r.id for r in picks] or None)
    m, found = match_pr(match_cwd(cwd, sid), cwd)
    record_commits(m, cwd)  # as the hooks do: the commits since the branch's mark, or its baseline
    remember(sid, m, cwd)
    print(brief(m, cwd, note=found))


def cmd_watch(args):
    """The user's watch: in a terminal, or in the agent session their watch command gave it (the prompt hook's
    grant). The model never starts one: the command refuses a session without the grant."""
    inside, sid = agent_session()
    if inside and not granted(sid):
        die("`tracker watch` starts only when the user asks: they run it in a terminal, or invoke the watch skill "
            "in an agent session. Do not start it yourself.", REFUSED)
    if args.name:
        hits, sure = tracker_matches(args.name)
        head = f"'{' '.join(args.name)}' " + ("matches more than one tracker:" if hits and sure else
                                              "names no tracker for sure; the closest:" if hits else
                                              "matches no tracker; trackers:")
    else:
        found = find_tracker(args)
        hits, sure, head = ([found], True, "") if found else ([], False, "which tracker? trackers:")
    tr = one_tracker(hits, sure, head, "watch")
    Watcher(tr, sid if inside else "", f"session {session_name(sid)}" if inside else "terminal").run(args.once)


def cmd_use(args):
    cwd = work_dir()
    branch = branch_of(cwd) or die("not on a git branch")
    key = worktree_key(cwd, branch)
    if args.clear:
        for t in all_trackers():
            state = t.state()
            if state.get("use", {}).pop(key, None) is not None:
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
        # Recorded on each ticket, where sync and every worktree find it; the branch keeps its other tickets.
        for t in recs:
            if t.get("branch") != branch:
                moved = f" (was {t.get('branch')})" if t.get("branch") else ""
                t.save({"branch": branch, "updated": today()})
                append_log(tr, f"{t.id} is built on branch {branch}{moved}", [t.id])
        print(f"{branch}: {', '.join(t.id for t in tr.tickets_on(branch))}")
        return
    state = tr.state()
    state.setdefault("use", {})[key] = [r.id for r in recs]
    tr.save_state(state)
    print(f"{key}: works on {', '.join(r.id for r in recs)} (this worktree and branch only; `tracker use --clear` "
          f"undoes it)")


def cmd_open(args):
    from .viewer import viewer_ping, viewer_port
    tr, rec = locate(args, args.id) if args.id else (resolve(args), None)
    old = viewer_ping()  # a viewer on other code is replaced on its port, so its open pages carry on
    port = viewer_port()
    if not port:
        spawn("serve", "--port", str(old[0] if old else 0))
        for _ in range(50):
            time.sleep(0.1)
            if port := viewer_port():
                break
        else:
            die("viewer did not start")
    url = f"http://127.0.0.1:{port}/t/{tr.slug}/" + (f"#{rec.id}" if rec else "")
    if not args.no_browser:
        import webbrowser
        webbrowser.open(url)
    print(url)


def cmd_serve(args):
    from .viewer import serve
    serve(args.port)


# ---------------------------------------------------------------- main

IDS = {"action": "extend", "nargs": "+", "metavar": "ID"}  # a comma list, separate words, or both


def build_parser():
    import argparse  # here, not at the top: a hook run never needs it
    p = argparse.ArgumentParser(prog="tracker", description=__doc__.split("\n\n")[0], epilog="Any text argument "
                                "can be `-`: the text then comes from stdin, as a heredoc passes it (<<'EOF').")
    p.add_argument("--tracker", help="tracker slug (default: the session's, from `tracker start`; else TRACKER, "
                                     "the cwd, or the repo's only tracker)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.add_argument("--tracker", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.set_defaults(fn=fn)
        return sp

    add("list", cmd_list, "list all trackers")
    sp = add("init", cmd_init, "create a tracker")
    sp.add_argument("slug")
    sp.add_argument("--title", required=True)
    sp.add_argument("--owner", required=True)
    sp.add_argument("--repo", help="GitHub owner/name (comma list if the work spans repos); enables `sync`")
    add("rules", cmd_rules, "the tracker's contract: files, keys and who writes them, statuses, link labels, "
                            "machine state, the decision bar and the order rule")
    sp = add("index", cmd_index, "one line per ticket, whose move each ticket under way waits on, then open decisions")
    sp.add_argument("--status", help=f"comma list of stages ({'|'.join(STAGES)})")
    sp.add_argument("--active", action="store_true", help="hide closed tickets (merged, done, dropped)")
    sp.add_argument("--group")
    sp = add("here", cmd_here, "the brief for this branch: its handoff, work the tracker may not show yet, its "
                               "tickets under way in full, or what can start")
    sp.add_argument("--full", action="store_true", help="include the tickets' bodies")
    sp = add("context", cmd_context, "a ticket or decision with dependency carry-forward and the decisions "
                                     "that touch it")
    sp.add_argument("id", help="ticket or decision id, an id a ticket's Issue link names, a PR (#123) or a branch")
    sp.add_argument("--brief", action="store_true", help="omit the body")
    sp.add_argument("--deep", action="store_true", help="all carry-forward from earlier in the chain, not the first "
                                                        f"{CHAIN_CARRY_FORWARD_MAX}")
    sp.add_argument("--log", type=int, default=CONTEXT_LOG, metavar="N",
                    help=f"show the last N log lines for it (default {CONTEXT_LOG}; 0 for none)")
    sp = add("decisions", cmd_decisions, "the open decisions and the tickets they touch, and a count of the settled "
                                         "ones; --all adds those, with their answers")
    sp.add_argument("--status", choices=DECISION_STATUSES)
    sp.add_argument("--all", action="store_true", help="every decision, settled ones with their answers")
    sp = add("find", cmd_find, "search tickets, decisions, README and log")
    sp.add_argument("text")
    sp.add_argument("--all", action="store_true", help="search every tracker")
    sp = add("set", cmd_set, "set frontmatter keys that `set` owns (see `rules`): "
                             "set T-8 status=in-progress next=\"...\"; set tracker status=active")
    sp.add_argument("id", nargs="?", help="ticket or decision id, or `tracker` for the work itself; left out: this "
                                          "session's one ticket")
    sp.add_argument("pairs", nargs="+", metavar="key=value")
    sp = add("log", cmd_log, "append a dated line to log.md: log \"<what changed and why>\" --ref T-8 D-2")
    sp.add_argument("message", nargs="?")
    sp.add_argument("--ref", **IDS, help="ids it concerns")
    sp = add("add", cmd_add, "add a line to a section of a ticket, decision or the README (`tracker`): "
                             "add T-8 carry \"...\" / add T-8 link \"PR: [title](url) — why\" / "
                             "add T-8 plan \"...\" --replace \"old text\"")
    sp.add_argument("id", help="ticket or decision id, or `tracker` for the README")
    sp.add_argument("section", help="a section, by a prefix of its name: plan, carry, links, question, options, "
                                    "context, goal, scope")
    sp.add_argument("text", help="the line; a bullet in a list section (Carry forward, Links, Context)")
    sp.add_argument("--replace", metavar="OLD", help="replace this text, which occurs once in the section, instead")
    sp = add("put", cmd_put, "replace a whole section of a ticket, a decision or the README (`tracker`), with the text "
                             "from stdin: put T-8 carry - <<'EOF' … EOF")
    sp.add_argument("id", help="ticket or decision id, or `tracker` for the README")
    sp.add_argument("section", help="a section, by a prefix of its name: plan, carry, links, question, options, "
                                    "context, goal, scope")
    sp.add_argument("text", nargs="?", default="-", help="the section's whole new text; `-` or left out: from stdin")
    sp = add("drop", cmd_drop, "remove the one line of a section that holds this text (a bullet goes with its "
                               "nested lines): drop T-8 carry \"old fact\"")
    sp.add_argument("id", help="ticket or decision id, or `tracker` for the README")
    sp.add_argument("section")
    sp.add_argument("text")
    sp = add("show", cmd_show, "records' own text by id, whole or only some sections, without the template's "
                               "comments: show AS-20 --section carry / show D-01 D-02 --section resolution")
    sp.add_argument("ids", nargs="+", metavar="ID", help="ticket or decision ids (a comma list works too), or "
                                                          "`tracker` for the README")
    sp.add_argument("--section", help="comma list of sections, by a prefix of the name: plan, carry, links, question, "
                                      "options, resolution, context, goal, scope")
    sp = add("attach", cmd_attach, f"keep a file in the tracker's {EVIDENCE_DIR}/ folder and link it from the "
                                   "tickets and decisions it supports")
    sp.add_argument("file", nargs="?")
    sp.add_argument("--ref", **IDS, help="tickets (a Links line) and decisions (an Options line) it supports")
    sp.add_argument("--note", help="what it shows")
    sp.add_argument("--name", help=f"its file name in {EVIDENCE_DIR}/ (default: the file's own)")
    sp.add_argument("--force", action="store_true", help="replace a file of that name with other content")
    sp = add("new", cmd_new, "create a ticket from its template (decisions: `decide`)")
    sp.add_argument("id", nargs="?")
    sp.add_argument("--title", required=True)
    sp.add_argument("--group", help="a free label that groups tickets in the views; order comes from depends_on")
    sp.add_argument("--branch")
    sp.add_argument("--repo", help="the ticket's repo, when the tracker spans several")
    sp.add_argument("--depends", **IDS, help="ticket or decision ids it waits on")
    sp.add_argument("--next")
    sp = add("decide", cmd_decide, "open a direction decision (by title), or update / resolve one (by D-id)")
    sp.add_argument("target", nargs="?", help="a new decision's title, or an existing D-id")
    sp.add_argument("--question", help="what must be decided and why it matters (new: defaults to the title)")
    sp.add_argument("--note", help="an option, proposal or new fact; added to ## Options")
    sp.add_argument("--resolve", help="the answer; closes the decision")
    sp.add_argument("--by", help="who decided")
    sp.add_argument("--refs", **IDS, help="ticket ids it touches but does not block")
    sp.add_argument("--unref", **IDS, help="ticket ids to drop from refs")
    sp.add_argument("--blocks", **IDS, help="ticket ids that cannot proceed until it is settled (adds it to their "
                                            "depends_on)")
    sp.add_argument("--owner", help="who must decide")
    sp.add_argument("--force", action="store_true", help="open even though a similar decision is open")
    sp = add("wait", cmd_wait, "record what a ticket waits on, or remove it: wait T-14 on T-7 D-10 / "
                               "wait T-6 on X-1 --link \"<url> — why\" / wait T-14 off D-10")
    sp.add_argument("id", help="the ticket that waits")
    sp.add_argument("mode", choices=["on", "off"])
    sp.add_argument("items", nargs="+", help="ticket ids, decision ids, or an external blocker's id")
    sp.add_argument("--link", help=f"for a new external blocker: \"<url> — <why it blocks>\"; adds a "
                                   f"`- {BLOCKER}:` line to ## Links")
    sp = add("seq", cmd_seq, "the order: step, waits on and unblocks per ticket; ready list; critical path")
    sp.add_argument("--active", action="store_true", help="hide closed tickets")
    add("ready", cmd_ready, "the todo tickets that can start now: nothing blocks them (from the default branch), or "
                            "they wait only on tickets under way and stack on the branch named; with what each "
                            "unblocks")
    add("check", cmd_check, "validate the tracker; exit 1 on errors")
    sp = add("migrate", cmd_migrate, "bring a tracker made by an older version to the current rules "
                                     "(backs it up to $TRACKER_HOME/.backups first)")
    sp.add_argument("--dry-run", action="store_true", help="only list the changes")
    add("sync", cmd_sync, "pull PR number/state/merge from GitHub into tickets, and the open PRs' reviews, checks and "
                          "merge state, from which each ticket's move is computed")
    sp = add("issue", cmd_issue, "record a ticket's issue fields as read from its issue tracker (priority, when the "
                                 "issue was created), or with --due list the tickets whose fields are due")
    sp.add_argument("id", nargs="?", help="ticket id, or an id its Issue link names")
    sp.add_argument("--priority", help="the issue's priority in the issue tracker's words; empty: it has none")
    sp.add_argument("--created", help="when the issue was created, ISO 8601 with a time zone (2026-10-01T09:30:00Z)")
    sp.add_argument("--due", action="store_true", help="list the tickets whose issue fields are due, with their "
                                                      "issue links")
    add("synced", cmd_synced, "log the current branch's commits that the hooks have not logged (they log each "
                              "commit on a branch with a ticket under way), mark it at HEAD, and clear its handoff")
    sp = add("pause", cmd_pause, "stopping mid-work: leave the state of the branch's unfinished work for the next "
                                 "session (shown first in its brief), and mark the branch at HEAD")
    sp.add_argument("text", help="what is done, what is half-done and uncommitted, and the next step")
    sp = add("step", cmd_step, "record a step in one command: the next action (or --done), Carry forward and a log "
                               "line; then, for a ticket on this branch, log the commits the hooks have not and clear "
                               "its handoff, as `synced` does (with --pause, leave one, as `pause`)")
    sp.add_argument("id", nargs="?", help="the ticket; left out: this session's one ticket")
    sp.add_argument("message", nargs="?", help="what changed and why that the commit subjects do not say (the hooks "
                                              "log those): a log line")
    sp.add_argument("--next", help="the next action, true as of now")
    sp.add_argument("--carry", action="append", metavar="FACT", help="a fact a later ticket must know (repeatable)")
    sp.add_argument("--done", metavar="SUMMARY", help="close the ticket: what it delivered")
    sp.add_argument("--pause", metavar="HANDOFF", help="stopping mid-work: what is done, what is half-done and "
                                                       "uncommitted, and the next step")
    sp = add("start", cmd_start, "tie a tracker to this agent session, by its slug or words of its title "
                                 "(`start analytics studio`); prints the brief. Lists options when unsure (exit 3). "
                                 "Never changes git")
    sp.add_argument("name", nargs="*", help="the tracker's slug, or words of its slug or title; none: the tracker "
                                            "with an open ticket on this branch (in a session with a tracker: its "
                                            "brief)")
    sp.add_argument("--on", **IDS, help="the ticket(s) this session works on, when its branch holds more: the brief, "
                                        "the hooks, and `step` and `set` without an id use them alone. After the "
                                        "name; `tracker start <slug>` alone clears it")
    sp.add_argument("--clear", action="store_true", help="tie this session to no tracker")
    sp.add_argument("--decline", action="store_true", help="\"Not now\": no offer to link a tracker on this branch "
                                                           "for a day")
    sp = add("use", cmd_use, "put ticket(s) on the current branch: sets each one's branch (the branch keeps its "
                             "other tickets); on a shared branch such as main, records the choice for this worktree "
                             "only")
    sp.add_argument("ids", nargs="*", help="ticket ids")
    sp.add_argument("--clear", action="store_true", help="forget this worktree and branch's choice")
    sp = add("watch", cmd_watch, "for the user: print each change that may need them, one line per ticket or agent "
                                 "(`!` marks what does), until Ctrl-C: a move that becomes theirs, a step, a decision, "
                                 "a PR event, an agent idle and waiting on them, a `check` error. One watcher per "
                                 "tracker. In an agent session it runs only after the user invokes the watch skill")
    sp.add_argument("name", nargs="*", help="the tracker's slug, or words of its slug or title")
    sp.add_argument("--once", action="store_true", help="print the first batch (at the first run: the state now), "
                                                        "then exit")
    sp = add("open", cmd_open, "open the live viewer in the browser (starts it if needed; it stops itself when idle)")
    sp.add_argument("id", nargs="?", help="ticket or decision to open")
    sp.add_argument("--no-browser", action="store_true", help="only start the viewer and print its URL")
    sp = sub.add_parser("serve")
    sp.add_argument("--port", type=int, default=0)
    sp.set_defaults(fn=cmd_serve)
    return p, sub


WRITE_COMMANDS = {"init", "set", "log", "new", "decide", "wait", "migrate", "synced", "pause", "step", "use", "add",
                  "drop", "attach", "issue"}  # `sync` locks itself


def with_check(run, args) -> None:
    """Run a write command, then print the `check` problems it added to the tracker, so no `tracker check` needs to
    follow a write. `init` and `migrate` check for themselves."""
    tr = find_tracker(args) if args.cmd not in ("init", "migrate") else None
    before = set(problem_lines(tr)) if tr else set()
    run()
    added = [x for x in problem_lines(Tracker(tr.root)) if x not in before] if tr else []
    if added:
        print("\n".join(["This change added `tracker check` problems:", *added]))


def own_changes(run) -> None:
    """Run a write command, and count what it changed about the session's tickets as seen: the prompt hook reports
    only what others changed."""
    sid = session_id()
    m = match_cwd(work_dir(), sid) if sid else None
    before = watch(m.tracker, m.focus) if m and m.focus else None
    run()
    if before is None:
        return
    tr = Tracker(m.tracker.root)
    after = watch(tr, [t for t in (tr.lookup(x.id) for x in m.focus) if t])
    entry = load_session(sid)
    if entry.get("seen_for") == [t.id for t in m.focus]:
        seen = entry.get("seen", {})
        seen.update({k: v for k, v in after.items() if before.get(k) != v})
        save_session(sid, seen=seen)


# The positional that an id list can swallow: `log --ref A B "msg"` gives the list "msg" too, as argparse's usage line
# puts options first. The list's last word goes back to the positional.
AFTER_IDS = {"log": "message", "attach": "file", "decide": "target", "new": "id"}
IDS_DESTS = ("ref", "refs", "unref", "blocks", "depends")


# The text arguments that take `-`: the text then comes from stdin, so a heredoc (<<'EOF') passes quotes, backticks
# and lines as they are. `set` takes `key=-`.
TEXT_DESTS = ("message", "text", "next", "done", "pause", "carry", "question", "note", "resolve", "replace", "pairs")


def no_id(args) -> None:
    """`set next=...` and `step "<what and why>"` leave the id out, for this session's ticket: the first word is then a
    pair or the message."""
    if args.cmd == "set" and args.id and "=" in args.id:
        args.pairs.insert(0, args.id)
        args.id = None
    if args.cmd == "step" and args.id and not args.message and (args.id == "-" or re.search(r"\s", args.id)):
        args.id, args.message = None, args.id


def stdin_text(args) -> None:
    if args.cmd == "find":  # its text is a search
        return
    spots = []
    for dest in TEXT_DESTS:
        value = getattr(args, dest, None)
        if value == "-":
            spots.append((dest, None))
        elif isinstance(value, list):
            spots += [(dest, i) for i, v in enumerate(value) if v == "-" or (dest == "pairs" and v.endswith("=-"))]
    if not spots:
        return
    if len(spots) > 1:
        die("only one text can come from stdin (`-`)")
    if sys.stdin is None or sys.stdin.isatty():
        die("`-` reads the text from stdin, and none is piped: use a heredoc (<<'EOF')")
    text, (dest, i) = sys.stdin.read().strip("\n"), spots[0]
    if i is None:
        setattr(args, dest, text)
    else:
        values = getattr(args, dest)
        values[i] = values[i][:-1] + text if dest == "pairs" else text


def reclaim(args, argv: list[str], parser) -> None:
    name = AFTER_IDS.get(args.cmd)
    if not name or getattr(args, name):
        return
    last = max(((i, d) for i, tok in enumerate(argv) for d in IDS_DESTS
                if tok == f"--{d}" and getattr(args, d, None)), default=None)
    if last:
        setattr(args, name, getattr(args, last[1]).pop())
    if not getattr(args, name):
        parser.error(f"{args.cmd}: the following arguments are required: {name}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()[0]
    args = parser.parse_args(argv)
    reclaim(args, argv, parser)
    no_id(args)
    stdin_text(args)
    if (args.cmd in WRITE_COMMANDS or args.cmd == "start") and watching():
        die("this session watches trackers for the user (/work-tracker:watch): it writes nothing to them, and works "
            "on no tracker", REFUSED)
    try:
        if args.cmd in WRITE_COMMANDS:
            with locked():
                own_changes(lambda: with_check(lambda: args.fn(args), args))
        else:
            args.fn(args)
    except Busy as exc:
        die(str(exc))
