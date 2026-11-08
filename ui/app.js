// The tracker in its own window, for `gpui-shell run --dir ui`: no Herdr needed.
import os from "os";
import { TitleBar } from "gpui-component";
import { parseOpen } from "./model.js";
import { trackerView } from "./view.js";

const COLORS = {
  background: "#17181c", foreground: "#e6e7ea", muted: "#a6a9b2", dim: "#6e717b", border: "#2c2e35", raised: "#23252b", popover: "#23252b",
  hover: "#23252b", selected: "#2c2f38", accent: "#6fb3ea", working: "#e8b35f", blocked: "#ee7d7d", ansi2: "#7ecf86",
};

const FONTS = { darwin: "Menlo", win32: "Consolas" }; // each platform's own monospace font

const cli = () => JSON.parse(process.env.TRACKER_CLI || "null"); // `tracker open` says; run by hand, `tracker` on PATH

export default trackerView({
  run: (command, args) => process.run(command, args),
  // No folder watch here: the CLI waits, in a process of its own, which ends by itself once the window has gone. It
  // cannot say what changed: null.
  changed: async (cx, _dir, slug, atLeastMs) => {
    const [command, ...rest] = cli() ?? [];
    const waited = command && slug ? await process.run(command, [...rest, "await-change", slug]).catch(() => null) : null;
    if (!waited || waited.code) await cx.sleep(atLeastMs);
    return null;
  },
  colors: () => COLORS,
  font: () => FONTS[os.platform()] ?? "DejaVu Sans Mono",
  sized: (el, set) => { set(window.viewport_size().width); return el; },
  start: () => parseOpen(process.env.TRACKER_OPEN),
  cli,
  // gpui-shell's window draws no title bar. This one moves the window, keeps clear of the macOS buttons, and draws the
  // buttons on Windows and Linux. It sets its own left inset, and its own height, which lines up with the macOS buttons.
  titleBar: (row, t) => TitleBar.new().bg(t.background).border_color(t.border).child(row.flex_1().min_w(0).h_full().pl(0)),
});
