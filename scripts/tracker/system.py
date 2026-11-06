"""The operating system: this Python and the CLI as processes, whether a process runs, and the system's trash. The
tracker's own files are model's."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

WINDOWS = os.name == "nt"
PACKAGE = Path(__file__).resolve().parent
# This Python, as scripts/python.sh says to start it: no module in the cwd replaces a stdlib one; UTF-8 mode
PYTHON = [sys.executable, "-I", "-X", "utf8"]
# The CLI on this Python, as bin/tracker starts it: Windows starts no shell script as a process
CLI_START = "import sys; sys.path.insert(0, sys.argv.pop(1)); from tracker.cli import main; main()"
CLI = [*PYTHON, "-c", CLI_START, str(PACKAGE.parent)]
# What the CLI and `gh` need of the environment. A UI's host can start programs with none (gpui-shell does).
CLI_ENV = ("TRACKER_HOME", "HOME", "USERPROFILE", "SYSTEMROOT", "PATH", "XDG_CONFIG_HOME", "GH_CONFIG_DIR", "GH_HOST")


def env_int(names: str | tuple[str, ...], default: int, least: int = 0) -> int:
    """A whole-number setting from the first of `names` that is set, and at least `least`. A value that is not a
    whole number gives `default`, with a warning on stderr: a typo in a setting stops no command and no hook."""
    for name in (names,) if isinstance(names, str) else names:
        text = os.environ.get(name, "").strip()
        if not text:
            continue
        try:
            return max(least, int(text))
        except ValueError:
            print(f"work-tracker: {name}={text!r} is not a whole number; using {default}", file=sys.stderr)
            return default
    return default


def spawn(*args: str) -> None:
    """Start `tracker <args>` in the background: it outlives this process and writes to no terminal."""
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
              else {"start_new_session": True})
    subprocess.Popen([*CLI, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **detach)


def cli_for_ui() -> list[str]:
    """How a UI starts the CLI: as CLI, with this environment as the default for what it finds unset. Never a secret:
    CLI_ENV names none, and `gh` keeps its login in its own config."""
    env = {k: os.environ[k] for k in CLI_ENV if k in os.environ}
    start = f"import os; os.environ.update({{**{env!r}, **os.environ}}); {CLI_START}"
    return [*PYTHON, "-c", start, str(PACKAGE.parent)]


def open_with_system(path: Path, reveal: bool = False) -> None:
    """Open a file in the app the system has for it, or with `reveal` show it in its folder. Raises OSError."""
    if WINDOWS:
        if reveal:
            subprocess.Popen(["explorer", f"/select,{path}"])
        else:
            os.startfile(path)  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.run(["open", *(["-R"] if reveal else []), str(path)], check=True, capture_output=True)
    else:
        subprocess.Popen(["xdg-open", str(path.parent if reveal else path)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)


def to_trash(path: Path) -> None:
    """Move a file or folder to the system's trash (macOS Trash, Windows Recycle Bin, the freedesktop trash
    elsewhere), from where the user can put it back. Raises OSError, and leaves the path, when it cannot."""
    if WINDOWS:
        windows_trash(path)
    elif sys.platform == "darwin":
        mac_trash(path)
    else:
        xdg_trash(path)


def mac_trash(path: Path) -> None:
    """`trash` (macOS 15 and later); before it, Finder, which asks the user once to let this app control it."""
    cmd = (["/usr/bin/trash", str(path)] if Path("/usr/bin/trash").exists() else
           ["osascript", "-e", "on run argv", "-e",
            'tell application "Finder" to delete (POSIX file (item 1 of argv) as alias)', "-e", "end run", str(path)])
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise OSError(f"{cmd[0]} did not finish") from exc
    if done.returncode or path.exists():
        raise OSError(f"could not move {path} to the Trash: {done.stderr.strip() or done.stdout.strip()}")


def windows_trash(path: Path) -> None:
    """SHFileOperationW with FOF_ALLOWUNDO: the Recycle Bin, with no dialog."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    fo_delete, flags = 3, 0x40 | 0x10 | 0x4 | 0x400  # FOF_ALLOWUNDO, NOCONFIRMATION, SILENT, NOERRORUI
    op = SHFILEOPSTRUCTW(None, fo_delete, str(path.resolve()) + "\0", None, flags, False, None, None)  # 2 NULs end it
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted or path.exists():
        raise OSError(f"could not move {path} to the Recycle Bin (error {code:#x})")


def xdg_trash(path: Path) -> None:
    """The freedesktop.org home trash: the file in Trash/files, and a .trashinfo in Trash/info that says where it
    came from, under a name no other item there has."""
    import shutil
    import urllib.parse
    trash = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Trash"
    (trash / "files").mkdir(parents=True, exist_ok=True)
    (trash / "info").mkdir(exist_ok=True)
    n = 1
    while True:
        name = path.name if n == 1 else f"{path.name}.{n}"
        info = trash / "info" / f"{name}.trashinfo"
        try:
            fd = os.open(info, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            n += 1
            continue
        if not (trash / "files" / name).exists():
            break
        os.close(fd)
        info.unlink()
        n += 1
    with os.fdopen(fd, "w") as f:
        f.write(f"[Trash Info]\nPath={urllib.parse.quote(str(path.absolute()))}\n"
                f"DeletionDate={time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    try:
        shutil.move(str(path), str(trash / "files" / name))  # a copy when the trash is on another disk
    except OSError:
        info.unlink(missing_ok=True)
        raise


def alive(pid: int) -> bool:
    if pid <= 0:  # 0 and -1 name process groups
        return False
    if WINDOWS:  # there os.kill(pid, 0) sends Ctrl+C (signal 0 is CTRL_C_EVENT): ask Windows instead
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: another user's
        code = ctypes.c_ulong()
        try:
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # signal 0: only asks whether the process exists
    except PermissionError:  # another user's
        return True
    except (OSError, OverflowError):
        return False
    return True
