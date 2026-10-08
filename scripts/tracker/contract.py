"""The contract in use: `check` enforces it, `rules` prints it, `migrate` brings an older tracker to it."""

from __future__ import annotations

import datetime as dt
import re

from .markdown import format_value, headings, link_ident, parse_links, section
from .model import (BLOCKER, CLOSED_TICKET, DECISION_BAR, DECISION_ID, DECISION_SECTIONS, DECISION_STATUSES,
    DEFAULT_LABELS, EVIDENCE_DIR, ISOLATION_RULE, ISSUE, KEYS, LABEL_RULES, MERGED_CARRY_FORWARD_MAX, MOVE_RULE,
    OPEN_DECISIONS_WARN, PR_STAGE, README_KEYS, README_SECTIONS, README_TOKEN_BUDGET, RETIRED_KEYS, SCHEMA, SCOPE_PARTS,
    STAGES, STALE_DECISION_DAYS, STALE_TICKET_DAYS, STARTED, START_RULE, STATE_RULES, TEXT_MAX, TICKET_SECTIONS,
    TICKET_STATUSES, TRACKER_STATUSES, WAIT_RULE, append_to_section, blocker_link, names, norm_id,
    relabel, sequence, Record, Tracker)

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
        check_keys(r.id, r.kind, r.meta, warnings)
        errors += [f"{r.path.relative_to(tr.root)}: {x}" for x in r.problems]
    check_keys("README.md", "tracker", tr.meta, warnings)
    if tr.schema < SCHEMA:
        warnings.append(f"README.md: tracker format {tr.schema} is older than {SCHEMA} — run `tracker migrate`")
    elif tr.schema > SCHEMA:
        errors.append(f"README.md: tracker format {tr.schema} is newer than this plugin's {SCHEMA} — update the plugin")
    errors += [f"README.md: {x}" for x in tr.readme_problems]
    aliases: dict[str, str] = {}
    for t in tr.tickets:
        status = t.get("status", "")
        if status not in TICKET_STATUSES:
            hint = " — run `tracker migrate`" if status in STAGES else ""
            errors.append(f"{t.id}: status '{status}' not one of {'|'.join(TICKET_STATUSES)}{hint}")
        check_deps(tr, t, errors, warnings)
        if t.stage in ("merged", "done") and len(t.carry_forward) > MERGED_CARRY_FORWARD_MAX:
            warnings.append(f"{t.id}: {t.stage} but Carry forward has {len(t.carry_forward)} bullets "
                            f"(limit {MERGED_CARRY_FORWARD_MAX}) — keep what a later ticket needs; the detail is in "
                            f"the PR (`tracker drop {t.id} carry \"<text>\"`)")
        if status == "todo" and t.get("pr_state") in PR_STAGE:
            warnings.append(f"{t.id}: todo, but its PR #{t.get('pr')} is {t.get('pr_state')} — `tracker migrate` "
                            f"sets it in progress; a PR overlays only started work")
        if t.stage == "in-progress" and idle_days(t) > STALE_TICKET_DAYS:
            warnings.append(f"{t.id}: in-progress, no update for {idle_days(t)} days")
        for a in t.aliases:
            other = ids.get(norm_id(a), "").removesuffix(".md") or aliases.get(norm_id(a))
            if other and other != t.id:
                errors.append(f"{t.id}: Issue id {a} also names {other} — an id finds one ticket")
            aliases[norm_id(a)] = t.id
        if (t.get("branch") or t.get("pr")) and len(tr.repos) > 1 and not t.get("repo"):
            errors.append(f"{t.id}: the tracker spans {len(tr.repos)} repos — set the ticket's repo")
        if t.get("repo") and t.get("repo") not in tr.repos:
            errors.append(f"{t.id}: repo {t.get('repo')} is not one of the tracker's repos ({', '.join(tr.repos)})")
    for d in tr.decisions:
        status = d.get("status", "")
        if status not in DECISION_STATUSES:
            errors.append(f"{d.id}: status '{status}' not one of {'|'.join(DECISION_STATUSES)}")
        waiting = {t.id for t in tr.waiting_on(d.id)}
        for ref in d.list("refs"):
            rec = tr.lookup(ref)
            if not rec or rec.kind != "ticket":
                errors.append(f"{d.id}: refs {ref}: no such ticket")
            elif rec.id in waiting:
                warnings.append(f"{d.id}: refs {ref}, which also waits on it — drop it from refs "
                                f"(`tracker decide {d.id} --unref {ref}`); depends_on already says so")
        if status == "open" and idle_days(d) > STALE_DECISION_DAYS:
            warnings.append(f"{d.id}: open, no update for {idle_days(d)} days")
    errors += [f"dependency cycle: {' → '.join(c)}" for c in sequence(tr).cycles]
    open_t = [t.id for t in tr.tickets if t.stage not in CLOSED_TICKET]
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


def check_keys(where: str, kind: str, meta: dict, warnings: list[str]) -> None:
    warnings += [f"{where}: frontmatter key '{k}' is retired — run `tracker migrate`" if k in RETIRED_KEYS[kind]
                 else f"{where}: unknown frontmatter key '{k}' (keys: {', '.join(KEYS[kind])})"
                 for k in meta if k not in KEYS[kind]]


def check_deps(tr: Tracker, t: Record, errors: list[str], warnings: list[str]) -> None:
    stage, seen = t.stage, set()
    for d in tr.deps(t):
        if d.ident.lower() in seen:
            warnings.append(f"{t.id}: depends_on lists {d.ident} twice")
        seen.add(d.ident.lower())
        if d.kind == "external":
            if not d.link:
                what = "no such decision" if DECISION_ID.fullmatch(d.ident) else "not a ticket or decision here"
                errors.append(f"{t.id}: depends_on {d.ident}: {what}, and no `- {BLOCKER}:` line in ## Links names "
                              f"it — add one with the URL and why it blocks, or fix the id")
        elif d.ident == t.id:
            errors.append(f"{t.id}: depends_on itself")
        elif d.kind == "ticket" and d.rec.stage == "dropped":
            warnings.append(f"{t.id}: waits on dropped {d.ident} — remove it, or wait on what replaced it")
        elif d.kind == "ticket" and stage in STARTED and d.rec.stage == "todo":
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
    if "status" in tr.meta and tr.meta["status"] not in TRACKER_STATUSES:
        errors.append(f"README.md: status '{tr.meta.get('status', '')}' not one of {'|'.join(TRACKER_STATUSES)}")
    if "Context" in heads:
        if not tr.context:
            warnings.append("README.md: ## Context lists nothing")
        check_links(tr, "README.md Context", section(tr.readme_body, "Context"), errors)
    scope = re.findall(r"^### (.+?)\s*$", section(tr.readme_body, "Scope"), re.M)
    if "Scope" in heads and scope != SCOPE_PARTS:
        errors.append(f"README.md: ## Scope must hold ### {' and ### '.join(SCOPE_PARTS)}, in that order")
    for t in tr.tickets:
        heads = headings(t.body)
        errors += [f"{t.id}: missing ## {h}" for h in TICKET_SECTIONS if h not in heads]
        warnings += [f"{t.id}: unexpected ## {h} (sections are {', '.join(TICKET_SECTIONS)})"
                     for h in heads if h not in TICKET_SECTIONS]
        if [h for h in heads if h in TICKET_SECTIONS] != [h for h in TICKET_SECTIONS if h in heads]:
            warnings.append(f"{t.id}: sections out of order (expected {', '.join(TICKET_SECTIONS)})")
        check_links(tr, f"{t.id} Links", section(t.body, "Links"), errors)
    for d in tr.decisions:
        heads = headings(d.body)
        if "Question" not in heads:
            errors.append(f"{d.id}: missing ## Question")
        if d.get("status") == "closed" and "Resolution" not in heads:
            errors.append(f"{d.id}: closed without ## Resolution")
        warnings += [f"{d.id}: unexpected ## {h} (sections are {', '.join(DECISION_SECTIONS)})"
                     for h in heads if h not in DECISION_SECTIONS]


def idle_days(r: Record) -> int:
    """Days since a ticket or decision last changed: its `updated`, or a later direct edit of the file."""
    edited = dt.date.fromtimestamp(r.path.stat().st_mtime).isoformat() if r.path.exists() else ""
    return min(days_since(r.get("updated") or r.get("opened")), days_since(edited) if edited else 10 ** 6)


def days_since(date: str) -> int:
    try:
        return (dt.date.today() - dt.date.fromisoformat(str(date))).days
    except ValueError:
        return 0


def rules_lines(tr: Tracker | None) -> list[str]:
    """The tracker's contract, from the constants that `check`, `set` and the viewer use."""
    def keys(kind: str) -> list[str]:
        return [f"    {k}" + ("" if w == "set" else f" ({w})") + f": {meaning}"
                for k, (w, meaning) in KEYS[kind].items()]
    labels = tr.labels if tr else DEFAULT_LABELS
    return [
        "Isolation: " + ISOLATION_RULE,
        "Files (a key marked (sync), (auto), (wait), (decide) or (new) is written by that, never by `set`):",
        f"  README.md: sections {', '.join('## ' + h for h in README_SECTIONS)} first, in that order; ## Scope holds "
        f"{' and '.join('### ' + h for h in SCOPE_PARTS)}; any sections may follow. Under ~{README_TOKEN_BUDGET} "
        f"tokens; rewritten, never appended to. Frontmatter:",
        *keys("tracker"),
        f"  tickets/<ID>.md: sections {', '.join('## ' + h for h in TICKET_SECTIONS)}, exactly. Carry forward ≤ "
        f"{MERGED_CARRY_FORWARD_MAX} bullets once merged or done. Ids: letters, digits, `.`, `_`, `-`. Frontmatter:",
        *keys("ticket"),
        "  decisions/D-<n>.md: sections ## Question, ## Options, and ## Resolution once closed (one dated line per "
        "answer, `YYYY-MM-DD (who): answer`; the latest holds). Frontmatter:",
        *keys("decision"),
        "  log.md: one dated line per change, append-only (`tracker log`); the hooks add the branch's new commits "
        "(`Commits on <branch>: ...`), `pause` the handoff's first words.",
        f"  {EVIDENCE_DIR}/: files the records cite (`tracker attach`), linked as `{EVIDENCE_DIR}/<name>`.",
        "  .state.json (machine state, per repo and branch; never edit): " + "; ".join(f"{k}: {v}" for k, v in
                                                                             STATE_RULES.items()) + ".",
        "Body edits: `tracker add <id> <section> \"...\" [--replace OLD]` and `tracker drop <id> <section> \"...\"` "
        "change one line of a section of a ticket, a decision or the README (`tracker`), by a prefix of its name, and "
        "`tracker put <id> <section> -` the whole section, from stdin; edit the file only for what they do not cover. "
        "Any text argument can be `-`: the text then comes from stdin (a heredoc, <<'EOF'). Never edit frontmatter by "
        "hand. `tracker show <ids> --section <name>` prints sections as they are.",
        "Text limits in characters, refused when written: "
        + ", ".join(f"{what} {most}" for most, what, _ in TEXT_MAX.values()) + ".",
        f"Ticket status (you set it): {'|'.join(TICKET_STATUSES)}. Set in-progress when its work starts: from a "
        f"branch, that records the branch when the ticket has none (a branch holds any number of tickets). Once in "
        f"progress, its PR overlays the stage: " + ", ".join(f"{k} PR → {v}" for k, v in PR_STAGE.items())
        + f". Stages {', '.join(sorted(CLOSED_TICKET))} are closed; closing clears `next` and wants a `summary`.",
        f"Decision status: {'|'.join(DECISION_STATUSES)}, changed only by `tracker decide`.",
        f"Work status: {'|'.join(TRACKER_STATUSES)} (`tracker set tracker status=...`).",
        "Link lines (README ## Context, ticket ## Links): `- Label: [title](url) — why it matters`, nested bullets "
        f"for detail. Labels: {', '.join(labels)}. Add one for this tracker: `tracker set tracker labels=A,B` "
        "(the tracker's own labels; the defaults stay).",
        *[f"  {k}: {v}" for k, v in LABEL_RULES.items()],
        "Decisions: " + DECISION_BAR,
        "Order and blockers: " + WAIT_RULE,
        "Where a ticket starts: " + START_RULE,
        "Whose move: " + MOVE_RULE,
    ]


def exact(tr: Tracker, ident: str) -> Record | None:
    return next((r for r in tr.records if norm_id(r.id) == norm_id(ident)), None)


def migrate(tr: Tracker, apply: bool) -> list[str]:
    """What it takes to bring a tracker written for an older version to the current rules; with apply, do it."""
    out = []
    for t in tr.tickets:
        upd = {}
        status = t.get("status")
        if status == "in-review" or (status == "merged" and t.get("pr_state") == "merged"):
            upd["status"] = "in-progress"  # the PR shows it as in-review or merged
        elif status == "merged":
            upd["status"] = "done"
        elif status == "todo" and t.get("pr_state") in PR_STAGE:
            upd["status"] = "in-progress"  # its PR showed it started; a PR now overlays only started work
        if t.get("next") == "—":
            upd["next"] = ""
        if "slice" in t.meta:
            upd["slice"] = None
            if t.get("slice") != "" and not t.get("group"):
                upd["group"] = str(t.get("slice"))
        # An external blocker was a link line of any label; it is now a Blocker line. Relabel first: an old Issue
        # line naming a blocker would otherwise make the blocker another name for this ticket.
        for ident in t.list("depends_on"):
            if exact(tr, ident) or blocker_link(t, ident):
                continue
            named = next((x for x in t.links if names(x, ident)), None)
            if named:
                out.append(f"{t.id}: `- {named.label}:` line for external blocker {ident} → `- {BLOCKER}:`")
                if apply and not relabel(t, named, BLOCKER):
                    out.append(f"{t.id}: could not relabel the line for {ident}; change its label by hand")
        if "key" in t.meta:
            upd["key"] = None
            key = str(t.get("key") or "")
            if key and key.lower() not in (a.lower() for a in t.aliases):
                out.append(f"{t.id}: key {key} → a `- {ISSUE}: {key}` line")
                if apply:
                    append_to_section(t, "Links", f"- {ISSUE}: {key}")
        if upd:
            out.append(f"{t.id}: " + ", ".join(f"{k}={'(removed)' if v is None else format_value(v)}"
                                               for k, v in upd.items()))
            if apply:
                t.save(upd)
    for d in tr.decisions:
        waiting = {t.id for t in tr.waiting_on(d.id)}
        drop = [r for r in d.list("refs") if tr.canonical(r) in waiting]
        if drop:
            out.append(f"{d.id}: refs drops {', '.join(drop)} (they wait on it; depends_on says so)")
            if apply:
                d.save({"refs": [r for r in d.list("refs") if r not in drop]})
        if "resolved" in d.meta:
            out.append(f"{d.id}: resolved=(removed) (the date is on the ## Resolution line)")
            if apply:
                d.save({"resolved": None})
    used = {x.label for x in tr.context} | {x.label for t in tr.tickets for x in t.links}
    extra = sorted(used - set(tr.labels) - {ISSUE, BLOCKER})
    if extra:
        out.append(f"README.md: labels += {', '.join(extra)} (labels in use that are not defaults)")
        if apply:
            own = [x for x in tr.labels if x not in DEFAULT_LABELS]
            tr.readme().save({"labels": own + extra})
    if tr.schema < SCHEMA:
        out.append(f"README.md: schema={SCHEMA} (the tracker format this version writes)")
        if apply:
            tr.readme().save({"schema": SCHEMA})
    for r in [tr.readme(), *tr.records]:
        unknown = [k for k in r.meta if k not in KEYS[r.kind] and k not in RETIRED_KEYS[r.kind]]
        if unknown:
            out.append(f"{r.path.name}: unknown keys {', '.join(unknown)} — not changed; remove or rename by hand")
    return out
