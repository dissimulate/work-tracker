"""Archiving and deleting a tracker: moving its folder out of HOME, when nothing is on it."""

from __future__ import annotations

from pathlib import Path

from .system import die, to_trash
from .store import HOME, locked
from .model import ARCHIVE, Tracker
from .session import SESSIONS_DIR, drop_session, live_sessions, load_session, Live
from .watcher import REFUSED, drop_watches, watches_of

# ---------------------------------------------------------------- archive and delete
# A tracker leaves HOME only while no agent session or watch is on it: it would go from under them mid-work. Anyone
# may archive one, as `tracker unarchive` brings it back; only the user deletes one (`commands.cmd_delete`).


def in_use(tr: Tracker, own_sid: str = "", live: dict[str, list[Live]] | None = None) -> str:
    """What keeps the tracker in HOME: the agent sessions on it, but `own_sid` (the session that asks lets go of it),
    and the watch; empty when none, and for an archived tracker, which nothing can be on. `live`: `live_by_tracker`,
    read once for many trackers."""
    if tr.archived:
        return ""
    sessions = live_sessions(tr.slug) if live is None else live.get(tr.slug, [])
    names = [x.name for x in sessions if x.sid != own_sid]
    watchers = [w["who"] for w in watches_of(tr) if w["running"]]
    using = ([f"agent session{'s' * (len(names) > 1)} {', '.join(names)}"] if names else []) + \
        ([f"the watch by {', '.join(watchers)}"] if watchers else [])
    return " and ".join(using)


def refuse_in_use(tr: Tracker, verb: str, own_sid: str = "") -> None:
    using = in_use(tr, own_sid)
    if using:
        die(f"{tr.slug} is in use by {using}: end them (or run `tracker start --clear` in each session), "
            f"then {verb} it", REFUSED)


def forget(slug: str) -> None:
    """Drop the tracker's watch state and the sessions' ties to it: nothing points at it once it leaves HOME."""
    drop_watches(slug)
    for f in SESSIONS_DIR.glob("*.json"):
        if load_session(f.stem).get("tracker") == slug:
            drop_session(f.stem)


def move_tracker(tr: Tracker, dest: Path) -> None:
    """Move the folder; a linked folder's link moves, made absolute, so it still names its folder."""
    if dest.exists() or dest.is_symlink():
        die(f"{dest} exists: delete or rename it, then try again", REFUSED)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if tr.root.is_symlink():
        dest.symlink_to(tr.root.resolve(), target_is_directory=True)
        tr.root.unlink()
    else:
        tr.root.rename(dest)


def archive_tracker(tr: Tracker, own_sid: str = "") -> str:
    """Move the tracker to ARCHIVE (`model.ARCHIVE` says what that does). Exits REFUSED while it is in use."""
    with locked():
        if tr.archived:
            die(f"{tr.slug} is archived already", REFUSED)
        refuse_in_use(tr, "archive", own_sid)
        move_tracker(tr, ARCHIVE / tr.slug)
        forget(tr.slug)
    return (f"archived {tr.slug}: `tracker open` still shows it, and `tracker unarchive {tr.slug}` brings it back")


def unarchive_tracker(tr: Tracker) -> str:
    with locked():
        if not tr.archived:
            die(f"{tr.slug} is not archived", REFUSED)
        move_tracker(tr, HOME / tr.slug)
    return f"brought {tr.slug} back from the archive"


def delete_tracker(tr: Tracker) -> str:
    """Delete the tracker, archived or not: its folder to the system's trash (of a linked folder, only the link goes).
    Exits REFUSED while it is in use; raises OSError when the trash refuses the folder, which then stays. Says what it
    did."""
    with locked():
        refuse_in_use(tr, "delete")
        if tr.root.is_symlink():
            target = tr.root.resolve()
            tr.root.unlink()
            done = f"deleted the link {tr.root}; the folder it named stays at {target}"
        else:
            to_trash(tr.root)
            done = f"moved {tr.root} to the trash"
        forget(tr.slug)
    return done
