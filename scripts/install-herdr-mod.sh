#!/bin/sh
# Install the tracker's UI as a Herdr mod. Run it again after you update the plugin: nothing else updates the mod.
# Only these files: Herdr also reads a gpui-shell.json in a mod's folder, and refuses what its older runtime does not know.
set -eu
ui="$(cd "$(dirname "$0")/../ui" && pwd)"
target="${HERDR_APP_HOME:-$HOME/.config/herdr-app}/mods/work-tracker"
mkdir -p "$target"
cp "$ui/icon.svg" "$ui/main.js" "$ui/mod.json" "$ui/model.js" "$ui/view.js" "$target/"
echo "installed the work-tracker mod in $target"
