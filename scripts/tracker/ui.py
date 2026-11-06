"""Where the UI in `ui/` runs: a Herdr mod when Herdr is open, else its own window through gpui-shell. It reads and
changes a tracker only through this CLI (`viewmodel`), so nothing here serves it."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .model import HOME, ROOT, die
from .system import WINDOWS, cli_for_ui

UI_DIR = ROOT / "ui"
MOD_NAME = "work-tracker"
GPUI_SHELL_VERSION = "0.9.0"  # ui/gpui-shell.json's, the one the window is checked on
RELEASES = "https://github.com/longbridge/gpui-kit/releases"
SHELL_ARCHIVES = {  # (platform.system(), platform.machine().lower()): GPUI_SHELL_VERSION's archive, and its SHA-256
    ("Darwin", "arm64"): ("gpui-shell-aarch64-apple-darwin.tar.gz",
                          "c454743247877d56a86ba9745a11e28af4d2ca6098d175b45976bfca76a3a79e"),
    ("Darwin", "x86_64"): ("gpui-shell-x86_64-apple-darwin.tar.gz",
                           "c0e91893a73d15d8772d720c7b5040c838818194a00e07ca81af040688c17a40"),
    ("Linux", "x86_64"): ("gpui-shell-x86_64-unknown-linux-gnu.tar.gz",
                          "65aec2b478f07eb180c996ed870d3c42b5a030fd3159e9f43dd481b1b929b79d"),
    ("Windows", "amd64"): ("gpui-shell-x86_64-pc-windows-msvc.zip",
                           "e6defad5d60cad9055cf34b9c5fe25c8cf630a486606a6268f2c29f0863dc389"),
}
DOWNLOAD_TIMEOUT_S = 60
WINDOW_START_S = 0.5  # gpui-shell refuses an app before it runs it: this long with no exit, the window is up
NO_HOST = (f"the tracker's UI needs Herdr (https://herdr.dev) open with its mod installed "
           f"({ROOT / 'scripts' / 'install-herdr-mod.sh'}), or its own window: run `tracker install-window` once. "
           "Without them, `tracker index` and `tracker context <id>` show the same facts as text.")


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


def own_shell() -> Path:
    """Where `tracker install-window` puts gpui-shell: by version, as the UI needs the version it is checked on."""
    return HOME / ".gpui-shell" / GPUI_SHELL_VERSION / ("gpui-shell.exe" if WINDOWS else "gpui-shell")


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as answer:
        return answer.read()


def unpack(archive: bytes, name: str, member: str) -> bytes:
    """One file of a release's archive: only the binary leaves it, so no path in the archive can write elsewhere."""
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as files:
            return files.read(member)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as files:
        return files.extractfile(member).read()


def install_window() -> Path:
    """Install gpui-shell for this system, unless it is there: the path of the binary."""
    shell = own_shell()
    if shell.is_file():
        return shell
    system, machine = platform.system(), platform.machine().lower()
    if (system, machine) not in SHELL_ARCHIVES:
        die(f"gpui-shell {GPUI_SHELL_VERSION} has no build for {system} {machine}. Use Herdr, or the text views: "
            "`tracker index` and `tracker context <id>`.")
    name, digest = SHELL_ARCHIVES[system, machine]
    try:
        archive = fetch(f"{RELEASES}/download/v{GPUI_SHELL_VERSION}/{name}")
    except (OSError, http.client.HTTPException) as e:
        die(f"could not download {name}: {e}. Try again, or install gpui-shell {GPUI_SHELL_VERSION} from {RELEASES} "
            "on your PATH.")
    if hashlib.sha256(archive).hexdigest() != digest:
        die(f"{name} is not the file this plugin version expects, so it is not installed.")
    binary = unpack(archive, name, f"{name.split('.')[0]}/{shell.name}")
    shell.parent.mkdir(parents=True, exist_ok=True)
    part = shell.with_name(shell.name + ".part")  # an interrupted install leaves no binary that seems whole
    part.write_bytes(binary)
    part.chmod(0o755)
    part.replace(shell)
    return shell


def open_in_window(slug: str, ident: str = "") -> bool:
    """Open the UI's own window, which outlives this command. False when gpui-shell is not installed. When gpui-shell
    refuses at once (a version it cannot run, a bad manifest), says why: the window itself reports nothing."""
    own = own_shell()
    shell = str(own) if own.is_file() else shutil.which("gpui-shell")
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


def open_ui(slug: str, ident: str = "", window: bool = False) -> str:
    """Show a tracker, and a record of it when `ident` names one: which host shows it, or "" when none can. `window`:
    its own window, also when Herdr is open."""
    if not window and open_in_herdr(slug, ident):
        return "Herdr"
    return "gpui-shell" if open_in_window(slug, ident) else ""
