// What the view works out from `tracker data`, with no UI in it: which rows show, in what order, and the graph's
// geometry. Runs on any JavaScript engine: node runs its tests (model.test.js), QuickJS runs it in a host.

const GATES = ["ready", "blocked"]; // matched on the gate's tag, not the status

export function filterRows(tickets, filter) {
  return tickets.filter(t => filter === "all" || (filter === "active" ? !t.closed
    : GATES.includes(filter) ? t.gate.tag === filter : t.stage === filter));
}

// The numbers in a value compare as numbers ("T-2" before "T-10"). QuickJS's localeCompare takes no `numeric` option.
export function naturalCompare(a, b) {
  const parts = s => String(s).toLowerCase().match(/\d+|\D+/g) ?? [];
  const x = parts(a), y = parts(b);
  for (let i = 0; i < Math.min(x.length, y.length); i++) {
    if (x[i] === y[i]) continue;
    const both = /^\d/.test(x[i]) && /^\d/.test(y[i]);
    return both ? Number(x[i]) - Number(y[i]) : x[i] < y[i] ? -1 : 1;
  }
  return x.length - y.length;
}

// Rows with no value go last in either direction; equal values keep the dependency order.
export function sortRows(rows, { key, desc }) {
  const empty = v => v === null || v === undefined || v === "";
  return [...rows].sort((a, b) => {
    const x = a.sort[key], y = b.sort[key];
    if (empty(x) !== empty(y)) return empty(x) ? 1 : -1;
    const by = empty(x) ? 0 : naturalCompare(x, y);
    return (desc ? -by : by) || a.order - b.order;
  });
}

// The next sort after a click on a column's heading. Step is the dependency order, and has no direction.
export function nextSort(sort, key) {
  return key === "step" ? { key, desc: false } : { key, desc: sort.key === key && !sort.desc };
}

const HOLLOW = new Set(["closed", "dropped"]);
const lineTone = state => (HOLLOW.has(state) ? "closed" : state === "going" ? "going" : "blocked");

export const GRAPH = {
  width: 8, // a column's
  space: 4, // between two columns with no tracks between them
  pad: 3, // between a column and its gap's tracks
  track: 4, // between two tracks
  radius: 3.5, // a dot's
  halo: 1, // round a dot, in the panel colour, over the lines behind it
  ring: 1.5, // a hollow dot's stroke
  turn: 4, // a corner's radius
};

// The dependency graph over the rows as they are shown, each rowHeight high. A ticket's dot sits in the column of its
// step; a step with no row shown takes no column. A ticket's links leave its dot along its own column, as a branch
// does in a git graph, when that column is free over their rows: no other dot there, and no other ticket's run.
// Otherwise they leave level and run in a track in the gap just left of each waiting ticket's column, so a link never
// passes through another dot. A dot takes its ticket's state, and a link the state of the ticket waited on. With a
// focused row, only what its ticket waits on and unblocks keeps its colour, through any chain.
//
// Gives { width, lines, dots }: a line's `path` is a list of ["move" | "line", x, y] and ["curve", x, y, cx, cy], in px
// from the graph's top left.
export function graphLayout(rows, rowHeight, focusId = null) {
  const empty = { width: 0, lines: [], dots: [] };
  if (!rows.length) return empty;
  const at = new Map(rows.map((t, i) => [t.id, i]));
  const steps = [...new Set(rows.map(t => t.step))].sort((a, b) => a - b);
  const column = new Map(steps.map((s, k) => [s, k + 1]));
  const col = i => column.get(rows[i].step); // a row's column, from 1
  const cols = steps.length;
  const links = rows.flatMap((t, to) => t.links.filter(id => at.has(id)).map(id => [at.get(id), to]));
  const widen = ([first, last], to) => [Math.min(first, to), Math.max(last, to)];

  // Column runs: a ticket's links along its own column, taken in row order while the column is free over them.
  const reach = new Map(); // per ticket waited on: the rows of its links
  links.forEach(([from, to]) => reach.set(from, widen(reach.get(from) ?? [from, from], to)));
  const own = new Set(); // the tickets whose links run in their column
  const taken = []; // per column: the rows its runs hold
  [...reach].sort((a, b) => a[0] - b[0]).forEach(([from, [first, last]]) => {
    const c = col(from);
    const crowded = rows.some((_, i) => i !== from && i >= first && i <= last && col(i) === c);
    if (crowded || (taken[c] ?? []).some(([a, b]) => a <= last && first <= b)) return;
    (taken[c] ??= []).push([first, last]);
    own.add(from);
  });

  // Gap runs, for the rest: one per ticket and column it links to. A run takes the first track free by its first row.
  const runs = new Map();
  const runKey = (from, to) => `${from} ${col(to)}`;
  links.filter(([from]) => !own.has(from)).forEach(([from, to]) => {
    const key = runKey(from, to);
    runs.set(key, { gap: col(to), rows: widen(runs.get(key)?.rows ?? [from, from], to) });
  });
  const tracks = Array.from({ length: cols + 1 }, () => []); // per gap: each track's last row
  [...runs.values()].sort((a, b) => a.rows[0] - b.rows[0]).forEach(run => {
    const ends = tracks[run.gap];
    run.track = ends.findIndex(end => end < run.rows[0]);
    if (run.track < 0) run.track = ends.length;
    ends[run.track] = run.rows[1];
  });

  // Columns and gaps, left to right: a gap is as wide as its tracks need, or only spaces the dots.
  const colX = [0, GRAPH.width / 2];
  const trackX = [0, 0]; // per gap: its first track
  for (let c = 2; c <= cols; c++) {
    const n = tracks[c].length;
    trackX[c] = colX[c - 1] + GRAPH.width / 2 + GRAPH.pad;
    colX[c] = colX[c - 1] + GRAPH.width + (n ? 2 * GRAPH.pad + (n - 1) * GRAPH.track : GRAPH.space);
  }
  const x = i => colX[col(i)];
  const y = i => rowHeight * (i + 0.5);

  // The focused ticket's chains: the links up to what it waits on and down to what it unblocks, and their tickets.
  const focus = at.get(focusId);
  const near = new Set(focus === undefined ? [] : [focus]);
  const nearLinks = new Set();
  const walk = (i, end, next) => links.forEach((link, k) => {
    if (link[end] !== i || nearLinks.has(k)) return;
    nearLinks.add(k);
    near.add(link[next]);
    walk(link[next], end, next);
  });
  if (focus !== undefined) {
    walk(focus, 1, 0);
    walk(focus, 0, 1);
  }

  // The focused chains go last, over the grey.
  const lines = links.map((link, k) => [link, k]).sort(([, a], [, b]) => nearLinks.has(a) - nearLinks.has(b))
    .map(([[from, to], k]) => {
      const [x1, y1, x2, y2] = [x(from), y(from), x(to), y(to)];
      const r = Math.min(GRAPH.turn, Math.abs(y2 - y1) / 2);
      const dy = Math.sign(y2 - y1) * r;
      const run = runs.get(runKey(from, to));
      const tx = run ? trackX[run.gap] + run.track * GRAPH.track : x1;
      const path = [["move", x1, y1]];
      if (run) path.push(["line", tx - r, y1], ["curve", tx, y1 + dy, tx, y1]); // a gap run leaves level
      path.push(["line", tx, y2 - dy], ["curve", tx + r, y2, tx, y2], ["line", x2, y2]);
      return { tone: lineTone(rows[from].state), dim: focus !== undefined && !nearLinks.has(k), path };
    });
  const dots = rows.map((t, i) => ({
    id: t.id, x: x(i), y: y(i), state: t.state, hollow: HOLLOW.has(t.state),
    dim: focus !== undefined && !near.has(i),
  }));
  return { width: colX[cols] + GRAPH.width / 2, lines, dots };
}

// Where a link in a record's text goes: `record` an id here, `evidence` a file of the tracker's evidence/, `web` a URL
// for the system. Tracker text is written by agents, so any other scheme stays plain text: a click must not reach
// `javascript:`, `file:` or an app's own scheme.
export function linkTarget(url) {
  if (/^https?:\/\//i.test(url)) return { kind: "web", url }; // all that cx.open_url takes
  if (url.startsWith("#")) return { kind: "record", id: decodeURIComponent(url.slice(1)) };
  const file = /^evidence\/(.+)$/.exec(url)?.[1];
  return file ? { kind: "evidence", file: decodeURIComponent(file) } : null;
}

// A one-line row's text: Markdown's marks off, a link by its text. An opened record draws the Markdown.
export const plainInline = text => String(text)
  .replace(/\[([^\]]+)\]\([^)\s]+\)/g, "$1").replace(/`([^`]+)`/g, "$1")
  .replace(/\*\*([^*]+)\*\*/g, "$1").replace(/(?<![*\w])\*([^*\s][^*]*)\*(?!\w)/g, "$1");

// `tracker open`'s request: "<slug>/<id>", the id optional.
export function parseOpen(text) {
  const [slug, ...rest] = String(text ?? "").split("/");
  return { slug: slug || null, id: rest.join("/") || null };
}

// Why a view that follows the focused agent shows no tracker; "" when it shows one. `agent` is what host.agentAt() gave.
export function followNotice(agent, slug) {
  if (slug) return "";
  if (!agent) return "No agent is focused.";
  return agent.machine ? `The focused agent on ${agent.machine} has no tracker.` : "The focused agent has no tracker.";
}

export const hhmm = date => `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;

// What the live line says: when the data was read, when GitHub was, and the issue fields' state.
export function liveLine(view, now) {
  return [`live ${hhmm(now)}`, view.synced ? `PRs from GitHub ${hhmm(new Date(view.synced * 1000))}` : "", view.issues]
    .filter(Boolean).join(" · ");
}

// The heading of Current: how many, how many are your move, how many agents.
export function nowTitle({ busy, yours, agents }) {
  return [`${busy}`, yours ? `${yours} your move` : "", agents ? `${agents} agent${agents > 1 ? "s" : ""}` : ""]
    .filter(Boolean).join(" · ");
}
