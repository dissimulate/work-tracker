"""Writing the tracker's files: atomic writes, JSON files and the one write lock across every session and worktree.
What the files hold is schema's and model's."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .system import PACKAGE, WINDOWS, cli_for_ui, die

HOME = Path(os.environ.get("TRACKER_HOME", Path.home() / ".claude" / "trackers")).expanduser()
CLI_FILE = HOME / ".cli.json"  # how to start the CLI (record_cli), for a UI outside any session

_writes = 0  # tracker files this process wrote: a Tracker rebuilds its index after any write (Tracker.index)
_first_writes: tuple = ()  # while first_writes lasts: its callback, and the tracker folders written so far


@contextmanager
def first_writes(callback):
    """While this lasts, run `callback` with each tracker's folder before the first write in it."""
    global _first_writes
    _first_writes = (callback, set())
    try:
        yield
    finally:
        _first_writes = ()


def will_write(path: Path) -> None:
    """Note a write to come: when it is the first in a tracker's folder, first_writes' callback sees that folder as it
    still is."""
    if not _first_writes:
        return
    try:
        parts = path.resolve().relative_to(HOME.resolve()).parts
    except ValueError:
        return
    if len(parts) < 2 or parts[0].startswith("."):  # HOME's own files and folders: sessions, watches, the archive
        return
    callback, written = _first_writes
    root = HOME / parts[0]
    if root not in written:
        written.add(root)
        callback(root)


@contextmanager
def atomic_file(path: Path):
    """Write through an exclusively created file, then replace the destination. Keep its permissions; a new file
    is owner-only. Never open the destination for writing, including when another writer replaces it with a link."""
    global _writes
    will_write(path)
    mode = 0o600
    try:
        previous = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(previous.st_mode):
            die(f"refusing to replace {path}: not a regular file")
        mode = stat.S_IMODE(previous.st_mode)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)  # Windows: no CRLF
    try:
        with os.fdopen(fd, "wb") as f:
            yield f
        tmp.chmod(mode)
        for wait in (0.05, 0.1, 0.2, 0.4, None):  # Windows refuses to replace a file while another process reads it
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if wait is None:
                    raise
                time.sleep(wait)
        _writes += 1
    finally:
        if tmp.exists():
            tmp.chmod(0o600)  # Windows cannot remove a temporary file whose saved mode is read-only
            tmp.unlink()


def atomic_write(path: Path, text: str) -> None:
    with atomic_file(path) as f:
        f.write(text.encode("utf-8"))


def read_json(path: Path) -> dict:
    """A JSON file's object: the machine state, a session's, a watch's. {} when the file is missing, is not JSON or
    holds no object: one bad file never stops a command or a hook."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict, indent: int = 1) -> None:
    atomic_write(path, json.dumps(data, indent=indent) + "\n")


def record_cli() -> None:
    """Say how to start this CLI, for a UI outside any session: the plugin's path changes with each version. Only when
    that changes: a UI watches this folder, and each session has its own PATH."""
    if read_json(CLI_FILE).get("package") != str(PACKAGE):
        write_json(CLI_FILE, {"package": str(PACKAGE), "argv": cli_for_ui()})


def create(path: Path, text: str) -> None:
    """Write a new file; refuse one that exists, even one another session made a moment ago."""
    global _writes
    will_write(path)
    try:
        with open(path, "x") as f:
            f.write(text)
    except FileExistsError:
        die(f"{path} already exists")
    _writes += 1


class Busy(Exception):
    pass


LOCK_TIMEOUT_S = 20
_thread_lock = threading.Lock()  # the file lock is per process; this orders threads within one

if WINDOWS:  # a lock on the file's first byte
    import msvcrt

    def try_lock(f) -> bool:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def unlock(f) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def try_lock(f) -> bool:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def unlock(f) -> None:
        fcntl.flock(f, fcntl.LOCK_UN)

_held = threading.local()  # lock depth per thread: reentrant within a thread, exclusive across threads


@contextmanager
def locked():
    """One writer at a time, across every session and worktree: held around each read-modify-write of tracker
    files. Reentrant within a thread; other threads of this process wait."""
    if getattr(_held, "depth", 0):
        _held.depth += 1
        try:
            yield
        finally:
            _held.depth -= 1
        return
    HOME.mkdir(parents=True, exist_ok=True)
    with _thread_lock, open(HOME / ".lock", "a") as f:
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while not try_lock(f):
            if time.monotonic() > deadline:
                raise Busy(f"another tracker command has held the lock for {LOCK_TIMEOUT_S}s; try again")
            time.sleep(0.05)
        _held.depth = 1
        try:
            yield
        finally:
            _held.depth = 0
            unlock(f)
