"""Where the UI in `ui/` runs: a Herdr mod when Herdr is open, else its own window through gpui-shell. It reads and
changes a tracker only through this CLI (`viewmodel`), so nothing here serves it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .model import HOME, ROOT, die
from .system import WINDOWS, cli_for_ui

UI_DIR = ROOT / "ui"
MOD_NAME = "work-tracker"
GPUI_SHELL_VERSION = "0.9.0"  # ui/gpui-shell.json's, the one the window is checked on
WINDOW_START_S = 0.5  # gpui-shell refuses an app before it runs it: this long with no exit, the window is up
NO_HOST = (f"the tracker's UI needs Herdr (https://herdr.dev) open with its mod installed "
           f"({ROOT / 'scripts' / 'install-herdr-mod.sh'}), or gpui-shell {GPUI_SHELL_VERSION} on your PATH: "
           "https://github.com/longbridge/gpui-kit/releases. Without them, `tracker index` and `tracker context <id>` "
           "show the same facts as text.")


def herdr_home() -> Path:
    return Path(os.environ.get("HERDR_APP_HOME") or Path.home() / ".config" / "herdr-app")


def herdr(*args: str) -> dict | None:
    """`herdr-app <args>`'s JSON answer; None when Herdr is not installed, its window is not open, or it refuses."""
    app = shutil.which("herdr-app")
    if not app:
        return None
    try:
        done = subprocess.run([app, *args], capture_output=True, text=True, timeout=10)
        answer = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return answer if done.returncode == 0 and isinstance(answer, dict) and "error" not in answer else None


def open_in_herdr(slug: str, ident: str = "") -> bool:
    """Show the tracker in its tab: the one it has, or a new one. False when Herdr cannot, or has no mod to show it."""
    window = herdr("ls") if (herdr_home() / "mods" / MOD_NAME).is_dir() else None
    if window is None:
        return False
    name = f"tracker: {slug}"
    for space in window.get("spaces", []):
        for tab in space.get("tabs", []):
            if tab.get("name") == name and herdr("do", "tab.select", json.dumps({"id": tab["id"]})) is not None:
                if ident:  # the tab runs: the mod's nextCommand() takes it
                    herdr("do", f"{MOD_NAME}.open:{slug}/{ident}")
                return True
    props = {"slug": slug, **({"open": ident} if ident else {}),
             **({"trackers": str(HOME)} if "TRACKER_HOME" in os.environ else {})}  # the mod's own home is the default
    return herdr("do", "tab.new", json.dumps({"name": name, "mod": MOD_NAME, "props": props})) is not None


def open_in_window(slug: str, ident: str = "") -> bool:
    """Open the UI's own window, which outlives this command. False when gpui-shell is not installed. When gpui-shell
    refuses at once (a version it cannot run, a bad manifest), says why: the window itself reports nothing."""
    shell = shutil.which("gpui-shell")
    if not shell:
        return False
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
              else {"start_new_session": True})
    env = {**os.environ, "TRACKER_OPEN": f"{slug}/{ident}", "TRACKER_CLI": json.dumps(cli_for_ui())}
    with tempfile.TemporaryFile() as errors:  # a file: a pipe would break the window's stderr once this command ends
        started = subprocess.Popen([shell, "run", "--dir", str(UI_DIR)], env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=errors, **detach)
        try:
            code = started.wait(WINDOW_START_S)
        except subprocess.TimeoutExpired:
            return True
        if code:
            errors.seek(0)
            why = errors.read().decode(errors="replace").strip()
            die(f"gpui-shell could not open the view (exit {code}){f': {why}' if why else ''}")
    return True


def open_ui(slug: str, ident: str = "") -> str:
    """Show a tracker, and a record of it when `ident` names one: which host shows it, or "" when none can."""
    return "Herdr" if open_in_herdr(slug, ident) else "gpui-shell" if open_in_window(slug, ident) else ""
