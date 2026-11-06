// The tracker as a Herdr mod. All of Herdr that the UI uses is here.
import { agents, exec, focused, home, nextCommand, props, readTextFile, settings, terminals, theme, watchFolder } from "herdr";
import { trackerView } from "./view.js";

const AT_LEAST_MS = 15000; // a session that ends with no hook, and another machine's tracker, change nothing watched here

export default trackerView({
  home,
  readText: (path, machine) => readTextFile(path, { on: machine ?? null }),
  run: (command, args, machine) => exec(command, args, { on: machine ?? null }),
  // A folder that cannot be watched yet, or at all, rejects: wait, and look again.
  changed: async (cx, dir) => {
    await Promise.race([watchFolder(dir).catch(() => cx.sleep(AT_LEAST_MS)), cx.sleep(AT_LEAST_MS)]);
  },
  colors: () => theme(),
  font: () => settings()?.font_family || "JetBrains Mono",
  sized: (el, set) => el.on_size_change((size, cx) => set(size.width, cx)),
  start: () => ({ slug: props()?.slug ?? null, id: props()?.open ?? null }),
  // `tracker open` on a tab that runs: `work-tracker.open:<slug>/<id>`.
  nextOpen: async () => {
    for (;;) {
      const command = await nextCommand();
      if (command.startsWith("open:")) return command.slice(5);
    }
  },
  trackersDir: () => props()?.trackers ?? null,
  framed: () => !props()?.slug, // a layout's block; the tab of `tracker open` asks for a tracker, and has a pane's frame
  agentAt: () => {
    const focus = focused();
    const address = focus?.shown ?? focus?.terminal ?? focus?.agent;
    if (!address) return undefined;
    const terminal = terminals().find(x => x.address === address && x.state === "live");
    if (!terminal || !(terminal.program?.agent || terminal.status || agents().some(x => x.address === address))) return null;
    return { address, machine: address.includes("/") ? address.split("/")[0] : null, // another machine's: "macbook/t1"
      pid: terminal.foreground?.pid ?? null, cwd: terminal.foreground?.cwd ?? terminal.cwd ?? null };
  },
});
