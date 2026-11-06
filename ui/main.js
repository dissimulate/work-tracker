// The tracker as a Herdr mod. All of Herdr that the UI uses is here.
import { exec, home, nextCommand, props, readTextFile, settings, theme, watchFolder } from "herdr";
import { trackerView } from "./view.js";

const AT_LEAST_MS = 15000; // agent sessions report outside the watched folder

export default trackerView({
  home,
  readText: path => readTextFile(path),
  run: (command, args) => exec(command, args),
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
});
