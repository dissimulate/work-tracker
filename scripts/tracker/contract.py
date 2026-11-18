"""The contract in use: `check` enforces it, `rules` prints it, `migrate` brings an older tracker to it, and
`refuse_newer` keeps this code from writing a newer one."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from pathlib import Path

from .markdown import BULLET, headings, link_ident, parse_links, section, strip_comments
from .system import warn
from .store import HOME, guard_writes, locked, read_json
from .schema import (ACTION_BAR, BLOCKER, DAY_KEYS, DECISION_BAR, DEFAULT_LABELS, EVIDENCE_DIR, FORMS, ISOLATION_RULE,
    ISSUE, KEYS, KINDS, LABEL_RULES, MERGED_CARRY_FORWARD_MAX, OPEN_DECISIONS_WARN, OPEN_PR, OPTION_RULE, OWNER_HINT,
    OWN_VALUE_RULE, PRIORITY_WORDS, PR_STAGE, README_INSTRUCTIONS, README_KEYS, README_SECTIONS, README_TOKEN_BUDGET,
    RENAMED_KEYS, SCALES, SCALE_RULE, SCHEMA, SCOPE_PARTS, STAGES, STALE_DECISION_DAYS, STALE_TICKET_DAYS, START_RULE,
    STATE_RULES, STATUSES, TEXT_MAX, WAIT_RULE, days_since, kind_names, options_problems, parse_options, stage_of,
    valid_value, value_form)
from .model import (LOG, all_trackers, append_log, append_to_section, archived_trackers, blocker_link, level, names,
    norm_id, pr_key, put_section, relabel, resolution, sequence, unknown_dep, Record, Tracker)
from .moves import MOVE_RULE

# ---------------------------------------------------------------- check

def check(tr: Tracker) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    ids = {}
    for r in tr.records:
        if norm_id(r.id) in ids:
            errors.append(f"{r.id}: duplicate id ({r.path.name}, {ids[norm_id(r.id)]})")
        ids[norm_id(r.id)] = r.path.name
        if r.path.stem != r.id:
            errors.append(f"{r.id}: file name {r.path.name} does not match id")
        if not r.get("title"):
            errors.append(f"{r.id}: missing title")
        check_meta(tr, r.id, r.kind, r.meta, errors, warnings)
        errors += [f"{r.path.relative_to(tr.root)}: {x}" for x in r.problems]
        if KINDS[r.kind].refs:
            check_refs(tr, r, errors, warnings)
        if r.get("closed_at") and not stage_of(r.kind, r.get("status")).closed:
            warnings.append(f"{r.id}: closed_at, but its status {r.get('status')} is open — remove the key")
    check_meta(tr, "README.md", "tracker", tr.meta, errors, warnings)
    if tr.meta.get("issue_updates") == "on" and "history" not in tr.raw_state().get("issues", {}):
        warnings.append("README.md: issue_updates is on, but .state.json has no record of what its issues show, so "
                        "no update is due — `tracker set tracker issue_updates=on` starts one: the decisions settled "
                        "so far count as written, and each linked ticket out of todo is due once for its status")
    if tr.schema < SCHEMA:
        warnings.append(f"README.md: tracker format {tr.schema} is older than {SCHEMA}: it could not migrate — the "
                        f"next command tries again, and says why on stderr")
    elif tr.schema > SCHEMA:
        errors.append(f"README.md: tracker format {tr.schema} is newer than this plugin's {SCHEMA} — update the "
                      f"plugin")
    errors += [f"README.md: {x}" for x in tr.readme_problems]
    aliases: dict[str, str] = {}
    for t in tr.tickets:
        check_deps(tr, t, errors, warnings)
        if t.closed and not t.dropped and len(t.carry_forward) > MERGED_CARRY_FORWARD_MAX:
            warnings.append(f"{t.id}: {t.stage} but Carry forward has {len(t.carry_forward)} bullets "
                            f"(limit {MERGED_CARRY_FORWARD_MAX}) — keep what a later ticket needs; the detail is in "
                            f"the PR (`tracker drop {t.id} carry \"<text>\"`)")
        if t.todo and t.pr_state in PR_STAGE:
            warnings.append(f"{t.id}: todo, but its PRs {t.pr_refs} are {t.pr_state} — a PR overlays only started "
                            f"work: `tracker set {t.id} status=in-progress`")
        if t.stage == "in-progress" and idle_days(t) > STALE_TICKET_DAYS:
            warnings.append(f"{t.id}: in-progress, no update for {idle_days(t)} days")
        for a in t.aliases:
            other = ids.get(norm_id(a), "").removesuffix(".md") or aliases.get(norm_id(a))
            if other and other != t.id:
                errors.append(f"{t.id}: Issue id {a} also names {other} — an id finds one ticket")
            aliases[norm_id(a)] = t.id
        if (t.get("branch") or t.prs) and len(tr.repos) > 1 and not t.get("repo"):
            errors.append(f"{t.id}: the tracker spans {len(tr.repos)} repos — set the ticket's repo")
        if t.get("repo") and t.get("repo") not in tr.repos:
            errors.append(f"{t.id}: repo {t.get('repo')} is not one of the tracker's repos ({', '.join(tr.repos)})")
    for d in tr.decisions:
        if not d.closed and idle_days(d) > STALE_DECISION_DAYS:
            warnings.append(f"{d.id}: open, no update for {idle_days(d)} days")
    errors += [f"dependency cycle: {' → '.join(c)}" for c in sequence(tr).cycles]
    open_t = [t.id for t in tr.tickets if not t.closed]
    work = tr.meta.get("status")
    if tr.tickets and not open_t and work in ("planning", "active"):
        warnings.append(f"README.md: every ticket is closed but the work is {work} — `tracker set tracker "
                        f"status=done`, or add the tickets still to do")
    if work == "done" and open_t:
        warnings.append(f"README.md: the work is done but {', '.join(open_t)} are open — close them, or reopen "
                        f"the work")
    check_contract(tr, errors, warnings)
    if len(tr.open_decisions()) > OPEN_DECISIONS_WARN:
        warnings.append(f"{len(tr.open_decisions())} open decisions (> {OPEN_DECISIONS_WARN}): keep only direction "
                        f"decisions; move ticket-level questions into their ticket's Plan or next")
    tokens = len(tr.readme_body) // 4
    if tokens > README_TOKEN_BUDGET:
        warnings.append(f"README.md ~{tokens} tokens > {README_TOKEN_BUDGET} limit — move detail to tickets or log")
    return errors, warnings


def check_refs(tr: Tracker, r: Record, errors: list[str], warnings: list[str]) -> None:
    """A record's refs: each names a record of a kind its kind's refs may name (KINDS), and none a ticket that waits
    on it, which its depends_on says."""
    kinds = KINDS[r.kind].refs
    waiting = {t.id for t in tr.waiting_on(r.id)}
    for ref in r.list("refs"):
        rec = tr.lookup(ref)
        if not rec or rec.kind not in kinds:
            errors.append(f"{r.id}: refs {ref}: no such {kind_names(kinds)}")
        elif rec.id in waiting:
            warnings.append(f"{r.id}: refs {ref}, which also waits on it — drop it from refs "
                            f"(`tracker {KINDS[r.kind].command} {r.id} --unref {ref}`); depends_on already says so")


def check_meta(tr: Tracker, where: str, kind: str, meta: dict, errors: list[str], warnings: list[str]) -> None:
    """A file's frontmatter: known keys, a status of its kind's, and each value in its key's form (value_form)."""
    warnings += [f"{where}: unknown frontmatter key '{k}' (keys: {', '.join(KEYS[kind])})"
                 for k in meta if k not in KEYS[kind]]
    statuses, status = STATUSES[kind], meta.get("status", "")
    if (kind != "tracker" or "status" in meta) and status not in statuses:
        stage = " — it comes from the ticket's PR" if kind == "ticket" and status in STAGES else ""
        errors.append(f"{where}: status '{status}' not one of {'|'.join(statuses)}{stage}")
    for k, v in meta.items():
        if not valid_value(k, v):
            errors.append(f"{where}: {k} '{v}' is not {FORMS[value_form(k)].describe(k)}")


def check_deps(tr: Tracker, t: Record, errors: list[str], warnings: list[str]) -> None:
    stage, seen = t.stage, set()
    for d in tr.deps(t):
        if d.ident.lower() in seen:
            warnings.append(f"{t.id}: depends_on lists {d.ident} twice")
        seen.add(d.ident.lower())
        if d.kind == "external":
            if not d.link:
                errors.append(f"{t.id}: depends_on {d.ident}: {unknown_dep(tr, d.ident)}, and no `- {BLOCKER}:` line "
                              f"in ## Links names it — add one with the URL and why it blocks, or fix the id")
        elif d.ident == t.id:
            errors.append(f"{t.id}: depends_on itself")
        elif d.kind == "ticket" and d.rec.dropped:  # a dropped action was only not needed
            warnings.append(f"{t.id}: waits on dropped {d.ident} — remove it, or wait on what replaced it")
        elif t.started and d.rec.todo:
            warnings.append(f"{t.id}: {stage}, but {d.ident}, which it waits on, is still todo")
    open_deps = [d.ident for d in tr.deps(t) if not d.done]
    if stage == "done" and open_deps:
        warnings.append(f"{t.id}: done but still waits on {', '.join(open_deps)}")
    waits = {x.lower() for x in t.list("depends_on")}
    for x in t.links:
        if x.label == BLOCKER and link_ident(x).lower() not in waits:
            warnings.append(f"{t.id}: `- {BLOCKER}:` line names {link_ident(x)}, which depends_on does not list — "
                            f"`tracker wait {t.id} on {link_ident(x)}`, or relabel the line Related")


def check_links(tr: Tracker, where: str, text: str, errors: list[str]) -> None:
    items, bad = parse_links(text)
    errors += [f"{where}: not a `- Label: link` line: {b.strip()[:70]}" for b in bad]
    errors += [f"{where}: label '{x.label}' is not one of {', '.join(tr.labels)} — use one, or add it to the "
               f"tracker's labels (`tracker set tracker labels=...`)" for x in items if x.label not in tr.labels]


def check_contract(tr: Tracker, errors: list[str], warnings: list[str]) -> None:
    heads = headings(tr.readme_body)
    errors += [f"README.md: missing ## {h}" for h in README_SECTIONS if h not in heads]
    if all(h in heads for h in README_SECTIONS) and heads[:len(README_SECTIONS)] != README_SECTIONS:
        warnings.append(f"README.md: sections should start {', '.join(README_SECTIONS)}, in that order")
    errors += [f"README.md: missing frontmatter {k}" for k in README_KEYS if k not in tr.meta]
    if "Context" in heads:
        if not tr.context:
            warnings.append("README.md: ## Context lists nothing")
        check_links(tr, "README.md Context", section(tr.readme_body, "Context"), errors)
    scope = re.findall(r"^### (.+?)\s*$", section(tr.readme_body, "Scope"), re.M)
    if "Scope" in heads and scope != SCOPE_PARTS:
        errors.append(f"README.md: ## Scope must hold ### {' and ### '.join(SCOPE_PARTS)}, in that order")
    for r in tr.records:
        kind = KINDS[r.kind]
        if not kind.sections:  # its body is notes
            continue
        heads = headings(r.body)
        errors += [f"{r.id}: missing ## {h}" for h in kind.required if h not in heads]
        if kind.closing and r.closed and kind.closing not in heads:
            errors.append(f"{r.id}: closed without ## {kind.closing}")
        warnings += [f"{r.id}: unexpected ## {h} (sections are {', '.join(kind.sections)})"
                     for h in heads if h not in kind.sections]
        if [h for h in heads if h in kind.sections] != [h for h in kind.sections if h in heads]:
            warnings.append(f"{r.id}: sections out of order (expected {', '.join(kind.sections)})")
        if "Links" in kind.sections:
            check_links(tr, f"{r.id} Links", section(r.body, "Links"), errors)
        if problems := ("Options" in kind.sections and options_problems(strip_comments(section(r.body, "Options")))):
            errors.append(f"{r.id} ## Options: {'; '.join(problems)}. {OPTION_RULE}")


def idle_days(r: Record) -> int:
    """Days since a ticket or decision last changed: its `updated_at` (else `created_at`), or a later direct edit of
    the file."""
    edited = dt.date.fromtimestamp(r.path.stat().st_mtime).isoformat() if r.path.exists() else ""
    return min(days_since(r.get("updated_at") or r.get("created_at")), days_since(edited) if edited else 10 ** 6)


def rules_lines(tr: Tracker | None) -> list[str]:
    """The tracker's contract, from the constants that `check`, `set` and the UI use."""
    def keys(kind: str) -> list[str]:
        return [f"    {k}" + ("" if w == "set" else f" ({w})") + f": {meaning}"
                for k, (w, meaning) in KEYS[kind].items()]

    def files(kind: str) -> list[str]:
        k = KINDS[kind]
        sections = (f"sections {', '.join('## ' + h + (' once closed' if h == k.closing else '') for h in k.sections)}"
                    ", in this order and no others" if k.sections else "no sections")
        return [f"  {k.folder}/{k.prefix + '-<n>' if k.prefix else '<ID>'}.md: {sections}. {k.rule} "
                "Frontmatter:", *keys(kind)]

    labels = tr.labels if tr else DEFAULT_LABELS
    owners = [f"({k})" for k in OWNER_HINT]
    return [
        "Isolation: " + ISOLATION_RULE,
        f"Files (a key marked {', '.join(owners[:-1])} or {owners[-1]} is written by that, never by `set`):",
        f"  README.md: sections {', '.join('## ' + h for h in README_SECTIONS)} first, in that order; ## Scope holds "
        f"{' and '.join('### ' + h for h in SCOPE_PARTS)}; any sections may follow. An optional "
        f"## {README_INSTRUCTIONS} holds this work's standing rules for the agent: every brief prints it, cut when "
        f"long. Under ~{README_TOKEN_BUDGET} tokens; rewritten, never appended to. Frontmatter:",
        *keys("tracker"),
        *(line for kind in KINDS for line in files(kind)),
        "  log.md: one dated line per change, append-only. Each write logs what the history needs; `tracker log` adds "
        "a note no other write holds. The hooks add the branch's new commits (`"
        + LOG["commits"].text(branch="<branch>", commits="...") + "`), `step --pause` the handoff's first words.",
        f"  {EVIDENCE_DIR}/: files the records cite (`tracker attach`), linked as `{EVIDENCE_DIR}/<name>`.",
        "  .state.json (machine state, per repo and branch; never edit): " + "; ".join(f"{k}: {v}" for k, v in
                                                                             STATE_RULES.items()) + ".",
        "Body edits: `tracker add <id> <section> \"...\" [--replace OLD]` and `tracker drop <id> <section> \"...\"` "
        "change one line of a section of a ticket, a decision or the README (`tracker`), by a prefix of its name, and "
        "`tracker put <id> <section> -` the whole section, from stdin; edit the file only for what they do not cover. "
        "`--why \"...\"` logs why a section changes; a started ticket's Plan changes only with it. "
        "Any text argument but a title, a name or a link can be `-`: the text then comes from stdin (a heredoc, "
        "<<'EOF'). Never edit frontmatter by hand. `tracker show <ids> --section <name>` prints sections as they are.",
        "Text limits in characters, refused when written: "
        + ", ".join(f"{what} {most}" for most, what, _ in TEXT_MAX.values()) + ".",
        f"Ticket status (you set it): {'|'.join(STATUSES['ticket'])}. Set in-progress when its work starts: "
        f"from a branch, that records the branch when the ticket has none (a branch holds any number of tickets). Once "
        f"in progress, its PRs overlay the stage, all equal, any open one first: "
        + ", ".join(f"{k} PR → {v}" for k, v in PR_STAGE.items())
        + f". Stages {', '.join(s for s, x in STAGES.items() if x.closed)} are closed; closing clears `next` and wants "
        "a `summary`.",
        *(f"{kind.capitalize()} status: {'|'.join(STATUSES[kind])}, changed only by `tracker {owner}`."
          for kind in KINDS if (owner := KEYS[kind]["status"][0]) != "set"),
        f"Work status: {'|'.join(STATUSES['tracker'])} (`tracker set tracker status=...`).",
        f"Times and days: a key ending `_at` holds {FORMS['time'].describe('')}; {', '.join(sorted(DAY_KEYS))} holds "
        f"{FORMS['day'].describe('')}.",
        "Issue tracker values: " + SCALE_RULE + ".",
        "Your own values: " + OWN_VALUE_RULE + ".",
        *(f"{key.capitalize()}: {scale.rule}." for key, scale in SCALES.items()),
        "Link lines (README ## Context, ticket ## Links): `- Label: [title](url) — why it matters`, nested bullets "
        f"for detail. Labels: {', '.join(labels)}. Add one for this tracker: `tracker set tracker labels=A,B` "
        "(the tracker's own labels; the defaults stay).",
        *[f"  {k}: {v}" for k, v in LABEL_RULES.items()],
        "Decisions: " + DECISION_BAR,
        "Actions: " + ACTION_BAR,
        "Order and blockers: " + WAIT_RULE,
        "Where a ticket starts: " + START_RULE,
        "Whose move: " + MOVE_RULE,
    ]


# Schema 1 kept an issue's priority in its issue tracker's words; these become levels (PRIORITY_WORDS), as P0-P4 do.
# Any other word goes, and the issue is read again.
def priority_from_word(word: str) -> int | None:
    word = word.strip().lower()
    if re.fullmatch(r"p\d", word) and int(word[1]) in SCALES["priority"].levels:
        return int(word[1])
    return PRIORITY_WORDS.get(word)


def renamed_keys(r: Record) -> dict:
    """Schema 3 names each key by its value's form (value_form): an older key becomes its new name, which keeps its
    value. A closed decision or action gets the `closed_at` it had no key for: the day of its latest ## Resolution
    line, or an action's `updated` (written when it closed). A date alone, as it was: the time was not kept."""
    upd = {}
    for old, new in RENAMED_KEYS[r.kind].items():
        if old in r.meta:
            upd[old] = None
            if str(r.meta[old]).strip() and not r.get(new):
                upd[new] = r.meta[old]
    if r.kind in ("decision", "action") and r.closed and not r.get("closed_at"):
        day = re.match(r"\d{4}-\d{2}-\d{2}", resolution(r)) if r.kind == "decision" else None
        closed = day[0] if day else r.get("updated") if r.kind == "action" else ""
        if closed:
            upd["closed_at"] = closed
    return upd


def move_notes(d: Record) -> None:
    """Format 5 keeps only choices in a decision's ## Options (OPTION_RULE): the rest (a dated note, evidence, prose, a
    bullet with nested lines) moves to ## Notes, in its order. An undated bullet was a choice."""
    lines = [ln for ln in strip_comments(section(d.body, "Options")).splitlines() if ln.strip()]
    blocks = []  # each bullet with its nested lines, and each other line on its own
    for ln in lines:
        if ln[:1].isspace() and blocks and BULLET.match(blocks[-1][0]):
            blocks[-1].append(ln)
        else:
            blocks.append([f"- {ln[2:]}" if ln.startswith("* ") else ln])
    choice = [len(b) == 1 and not parse_options(b[0])[1] for b in blocks]
    keep = [b[0] for b, c in zip(blocks, choice) if c]
    if keep == lines:
        return
    put_section(d, "Options", "\n".join(keep))
    for b, c in zip(blocks, choice):
        if not c:
            append_to_section(d, "Notes", "\n".join(b))


def migrate(tr: Tracker) -> None:
    """Bring a tracker written for an older version to the current rules. Each step looks for what it changes, so a run
    that stopped part way can run again; the schema goes last. Only here does code read an older format."""
    state, reread = tr.raw_state(), []
    before = json.dumps(state, sort_keys=True)
    for t in tr.tickets:
        upd = {}
        status = t.get("status")
        old_state = str(t.meta.get("pr_state") or "")
        if status == "in-review" or (status == "merged" and old_state == "merged"):
            upd["status"] = "in-progress"  # the PR shows it as in-review or merged
        elif status == "merged":
            upd["status"] = "done"
        elif status == "todo" and old_state in PR_STAGE:
            upd["status"] = "in-progress"  # its PR showed it started; a PR overlays only started work
        if t.meta.keys() & {"pr", "pr_state", "base"}:  # a ticket's one PR: `prs` holds any number
            upd |= {"pr": None, "pr_state": None, "base": None}
            number = str(t.meta.get("pr") or "")
            if number:
                upd["prs"] = [f"{number}:{old_state}" if old_state else number]
                facts = state.get("prs", {}).get(pr_key(tr.repo_of(t), number))
                if facts is not None and old_state in OPEN_PR and t.meta.get("base"):
                    facts["base"] = str(t.meta["base"])  # a PR's base is a fact of the PR; `sync` reads the rest
        if t.get("next") == "—":
            upd["next"] = ""
        if str(t.get("priority", "")).strip() and level(t, "priority") is None:
            upd["priority"] = priority_from_word(str(t.get("priority")))
            if upd["priority"] is None:
                reread.append(t.id)  # to read again: the model maps the issue's level onto the scale
        if "slice" in t.meta:
            upd["slice"] = None
            if t.get("slice") != "" and not t.get("group"):
                upd["group"] = str(t.get("slice"))
        # An external blocker was a link line of any label; it is now a Blocker line. Relabel first: an old Issue
        # line naming a blocker would otherwise make the blocker another name for this ticket.
        for ident in t.list("depends_on"):
            if tr.by_id(ident) or blocker_link(t, ident):
                continue
            named = next((x for x in t.links if names(x, ident)), None)
            if named:
                relabel(t, named, BLOCKER)  # `check` names a blocker left with no such line
        if "key" in t.meta:
            upd["key"] = None
            key = str(t.get("key") or "")
            if key and key.lower() not in (a.lower() for a in t.aliases):
                append_to_section(t, "Links", f"- {ISSUE}: {key}")
        if upd:
            t.save(upd)
    if tr.schema < 3:  # format 3 takes size and due from the issue, and reads an open ticket's issue only once
        reread += [t.id for t in tr.tickets if t.aliases and not t.closed and t.id not in reread]
    for ident in reread:
        state.get("issues", {}).get("read", {}).pop(ident, None)
    state.pop("reviews", None)  # the PRs' facts in GitHub's own words
    if "synced" in state:  # a mark that held only the HEAD
        state["synced"] = {k: {"head": v, "at": 0} if isinstance(v, str) else v for k, v in state["synced"].items()}
    if json.dumps(state, sort_keys=True) != before:
        tr.save_state(state)
    for d in tr.decisions:
        move_notes(d)
        waiting = {t.id for t in tr.waiting_on(d.id)}
        drop = [r for r in d.list("refs") if tr.canonical(r) in waiting]
        if drop:
            d.save({"refs": [r for r in d.list("refs") if r not in drop]})
        if "resolved" in d.meta:
            d.save({"resolved": None})
    for r in [tr.readme, *tr.records]:
        upd = renamed_keys(r)
        if upd:
            r.save(upd)
    for r in tr.records:  # an earlier version stamped it when a ticket started
        if r.get("closed_at") and not stage_of(r.kind, r.get("status")).closed:
            r.save({"closed_at": None})
    used = {x.label for x in tr.context} | {x.label for t in tr.tickets for x in t.links}
    extra = sorted(used - set(tr.labels) - {ISSUE, BLOCKER})
    if extra:
        tr.readme.save({"labels": [x for x in tr.labels if x not in DEFAULT_LABELS] + extra})
    tr.readme.save({"schema": SCHEMA})


BACKUPS = HOME / ".backups"
# How the migrate command of 0.31.1 and earlier named a backup: <slug>-<date>-<time>.
OLD_BACKUP = re.compile(r".+-\d{8}-\d{6}")


def upgrade(tr: Tracker) -> None:
    """Back a tracker up, once per format it leaves, and migrate it. A README that cannot be read is left to `check`."""
    import shutil  # only here: a hook loads this module, and rarely gets this far
    with locked():
        tr = Tracker(tr.root)  # another process may have done it while this one waited
        if tr.schema >= SCHEMA or tr.readme_problems:
            return
        backup = BACKUPS / f"{tr.slug}-schema{tr.schema}"
        if not backup.exists():
            # Not Finder's files, half-writes, or the evidence/ folder: no migration changes it, and it can be large.
            top = str(tr.root)
            shutil.copytree(tr.root, backup, ignore=lambda folder, names: {
                x for x in names if x == ".DS_Store" or x.endswith(".tmp") or folder == top and x == EVIDENCE_DIR})
        migrate(tr)
        append_log(tr, f"Migrated to tracker format {SCHEMA} (backup {backup})", [])


def prune_backups() -> None:
    """Remove the backups that the migrate command of 0.31.1 and earlier made: each holds a tracker as it was before
    that run, which this version's own backup (`<slug>-schema<n>`) replaces, and the evidence/ folder whole. Once per
    plugin version (`hooks.record_version`)."""
    import shutil
    for old in BACKUPS.glob("*"):
        if OLD_BACKUP.fullmatch(old.name) and old.is_dir() and not old.is_symlink():
            shutil.rmtree(old, ignore_errors=True)


OLD_VIEWER = HOME / ".viewer.json"  # the browser viewer's of 0.31.1 and earlier: its port and pid


def stop_old_viewer() -> None:
    """Stop the browser viewer of 0.31.1 or earlier: it runs on while any session is on a tracker, and writes the older
    format. A session of that version may start it again, so each run looks. Only a process that answers as that
    viewer is stopped: the pid of an old file may be another program's now."""
    import signal
    import urllib.request
    info = read_json(OLD_VIEWER)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(info['port'])}/ping", timeout=1) as answer:
            if answer.read().decode(errors="replace").startswith("work-tracker"):
                os.kill(int(info["pid"]), signal.SIGTERM)
    except (OSError, ValueError, KeyError, TypeError):
        pass  # nothing answers as the viewer, or it has gone
    OLD_VIEWER.unlink(missing_ok=True)


class NewerFormat(Exception):
    """`refuse_newer` refused a write."""


NEWER_FORMAT = ("{slug} was written by a newer version of the work-tracker plugin (tracker format {schema}; this "
                "one writes {own}), so this one writes nothing to it: reload the plugin or restart the session")


def refuse_newer(root: Path) -> None:
    """Refuse a write into a tracker in a format newer than SCHEMA: raise NewerFormat. A session runs the plugin
    version it loaded until it reloads, and the newer version that migrated the tracker never migrates it again: what
    this one wrote there would stay in the older format."""
    newer = Tracker(root).schema
    if newer > SCHEMA:
        raise NewerFormat(NEWER_FORMAT.format(slug=root.name, schema=newer, own=SCHEMA))


def upgrade_trackers() -> None:
    """Bring every tracker, archived ones too, to the current format (`migrate`): each command and hook starts here. A
    tracker that fails stays as it is, says why, and tries again next time. From here on, refuse each write into a
    tracker in a newer format (`refuse_newer`). Also stops the old viewer (`stop_old_viewer`)."""
    guard_writes(refuse_newer)
    if OLD_VIEWER.exists():
        stop_old_viewer()
    for tr in (*all_trackers(), *archived_trackers()):
        if tr.schema < SCHEMA:
            try:
                upgrade(tr)
            except (Exception, SystemExit) as exc:  # one bad tracker stops no command and no hook
                warn(f"could not migrate {tr.slug} to format {SCHEMA}: {exc}")
