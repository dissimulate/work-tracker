"""Where the UI in `ui/` runs: a Herdr mod when Herdr is open, else its own window through gpui-shell. It reads and
changes a tracker only through this CLI (`viewmodel`), so nothing here serves it."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .model import HOME, ROOT
from .system import WINDOWS, cli_for_ui

UI_DIR = ROOT / "ui"
MOD_NAME = "work-tracker"
# Only these: Herdr also reads a gpui-shell.json in a mod's folder, and refuses what its older runtime does not know.
MOD_FILES = ("main.js", "mod.json", "model.js", "view.js")
GPUI_SHELL_VERSION = "0.9.0"  # ui/gpui-shell.json's, the one the window is checked on
NO_HOST = (f"the tracker's UI needs Herdr (https://herdr.dev) open, or gpui-shell {GPUI_SHELL_VERSION} on your PATH: "
           "https://github.com/longbridge/gpui-kit/releases. Without them, `tracker index` and `tracker context <id>` "
           "show the same facts as text.")


def herdr_home() -> Path:
    return Path(os.environ.get("HERDR_APP_HOME") or Path.home() / ".config" / "herdr-app")


def install_mod(home: Path) -> Path:
    """Copy the UI into Herdr's mods, so a new version of the plugin brings a new version of the mod. Only what changed:
    a new file reloads a mod that runs, and it would lose what the user opened."""
    target = home / "mods" / MOD_NAME
    target.mkdir(parents=True, exist_ok=True)
    for name in MOD_FILES:
        if not (target / name).exists() or (target / name).read_bytes() != (UI_DIR / name).read_bytes():
            shutil.copyfile(UI_DIR / name, target / name)
    return target


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
    """Show the tracker in its tab: the one it has, or a new one. False when Herdr cannot."""
    window = herdr("ls")
    if window is None:
        return False
    install_mod(herdr_home())
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
    """Open the UI's own window, which outlives this command. False when gpui-shell is not installed."""
    shell = shutil.which("gpui-shell")
    if not shell:
        return False
    detach = ({"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS
              else {"start_new_session": True})
    env = {**os.environ, "TRACKER_OPEN": f"{slug}/{ident}", "TRACKER_CLI": json.dumps(cli_for_ui())}
    subprocess.Popen([shell, "run", "--dir", str(UI_DIR)], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, **detach)
    return True


def open_ui(slug: str, ident: str = "") -> str:
    """Show a tracker, and a record of it when `ident` names one: which host shows it, or "" when none can."""
    return "Herdr" if open_in_herdr(slug, ident) else "gpui-shell" if open_in_window(slug, ident) else ""
