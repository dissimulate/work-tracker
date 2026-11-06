// The tracker in its own window, for `gpui-shell run --dir ui`: no Herdr needed.
import os from "os";
import { readFile } from "fs/promises";
import { TitleBar } from "gpui-component";
import { parseOpen } from "./model.js";
import { trackerView } from "./view.js";

const COLORS = {
  background: "#17181c", foreground: "#e6e7ea", muted: "#a6a9b2", dim: "#6e717b", border: "#2c2e35", raised: "#23252b", popover: "#23252b",
  hover: "#23252b", selected: "#2c2f38", accent: "#6fb3ea", working: "#e8b35f", blocked: "#ee7d7d", ansi2: "#7ecf86",
};

const FONTS = { darwin: "Menlo", win32: "Consolas" }; // each platform's own monospace font

export default trackerView({
  home: () => os.homedir(),
  readText: async path => { try { return await readFile(path, "utf8"); } catch { return null; } },
  run: (command, args) => process.run(command, args),
  changed: cx => cx.sleep(3000), // no folder watch here: an unchanged answer draws nothing
  colors: () => COLORS,
  font: () => FONTS[os.platform()] ?? "DejaVu Sans Mono",
  sized: (el, set) => { set(window.viewport_size().width); return el; },
  start: () => parseOpen(process.env.TRACKER_OPEN),
  cli: () => JSON.parse(process.env.TRACKER_CLI || "null"), // `tracker open` says; run by hand, its hooks' record
  // gpui-shell's window draws no title bar. This one moves the window, keeps clear of the macOS buttons, and draws the
  // buttons on Windows and Linux. It sets its own left inset, and its own height, which lines up with the macOS buttons.
  titleBar: (row, t) => TitleBar.new().bg(t.background).border_color(t.border).child(row.flex_1().min_w(0).h_full().pl(0)),
});
