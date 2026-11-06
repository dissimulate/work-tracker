import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { GRAPH, MEANINGS, agentStatus, bodyParts, filterRows, fitWidth, idWidth, followNotice, graphLayout, hhmm, linkTarget, liveLine, meaningOf, moveStatus,
  naturalCompare, nextSort, parseOpen, plainInline, rowStatus, rowWord, sortRows } from "./model.js";

const row = (id, step, links = [], extra = {}) => ({
  id, step, links, state: "ready", stage: "todo", closed: false, gate: { state: "ready" }, order: step, sort: {}, ...extra,
});

test("filters: none is all, and several keep what any of them keeps", () => {
  const rows = [row("T-1", 1, [], { stage: "done", closed: true, gate: { state: "" } }), row("T-2", 2, [], { stage: "in-review", gate: { state: "" } }),
    row("T-3", 3), row("T-4", 4, [], { gate: { state: "blocked" } }), row("T-5", 5, [], { gate: { state: "stack" } })];
  const ids = (...f) => filterRows(rows, f).map(r => r.id);
  assert.deepEqual(ids(), ["T-1", "T-2", "T-3", "T-4", "T-5"]);
  assert.deepEqual(ids("blocked"), ["T-4", "T-5"]);
  assert.deepEqual(ids("blocked", "done"), ["T-1", "T-4", "T-5"]);
  assert.deepEqual(ids("done"), ["T-1"]);
  assert.deepEqual(ids("done", "in-review"), ["T-1", "T-2"]);
});

test("numbers compare as numbers", () => {
  assert.ok(naturalCompare("T-2", "T-10") < 0);
  assert.ok(naturalCompare("9", "10") < 0);
  assert.equal(naturalCompare("T-2", "t-2"), 0);
});

test("sort: no value goes last in both directions, equal values keep the dependency order", () => {
  const rows = [row("A", 1, [], { order: 0, sort: { group: "" } }), row("B", 2, [], { order: 1, sort: { group: "x" } }),
    row("C", 3, [], { order: 2, sort: { group: "x" } }), row("D", 4, [], { order: 3, sort: { group: "y" } })];
  const ids = desc => sortRows(rows, { key: "group", desc }).map(r => r.id);
  assert.deepEqual(ids(false), ["B", "C", "D", "A"]);
  assert.deepEqual(ids(true), ["D", "B", "C", "A"]);
});

test("a heading's second click turns the sort, and Step has no direction", () => {
  assert.deepEqual(nextSort({ key: "step", desc: false }, "ticket"), { key: "ticket", desc: false });
  assert.deepEqual(nextSort({ key: "ticket", desc: false }, "ticket"), { key: "ticket", desc: true });
  assert.deepEqual(nextSort({ key: "ticket", desc: true }, "step"), { key: "step", desc: false });
});

test("graph: no rows, no graph", () => {
  assert.deepEqual(graphLayout([], 28), { width: 0, lines: [], dots: [] });
});

test("graph: a chain runs down its own column and turns into each waiting dot", () => {
  const g = graphLayout([row("T-1", 1), row("T-2", 2, ["T-1"])], 20);
  assert.equal(g.dots[0].x, GRAPH.width / 2);
  assert.deepEqual(g.dots.map(d => d.y), [10, 30]);
  const [line] = g.lines;
  assert.deepEqual(line.path[0], ["move", g.dots[0].x, 10]);
  assert.deepEqual(line.path.at(-1), ["line", g.dots[1].x, 30]);
  assert.equal(line.path[1][1], g.dots[0].x); // leaves along its column
});

test("graph: a link to a hidden row is not drawn, and a step with no row takes no column", () => {
  const g = graphLayout([row("T-1", 1), row("T-3", 5, ["T-2"])], 20);
  assert.equal(g.lines.length, 0);
  assert.equal(g.dots[1].x - g.dots[0].x, GRAPH.width + GRAPH.space);
});

test("graph: a link that would pass through a dot in its column leaves level, into a gap's track", () => {
  // T-1 and T-2 share a column, and T-3 waits on T-1: its link cannot run down past T-2's dot.
  const g = graphLayout([row("T-1", 1), row("T-2", 1), row("T-3", 2, ["T-1"])], 20);
  const [line] = g.lines;
  assert.equal(line.path[1][2], g.dots[0].y); // level with its dot
  assert.ok(line.path[1][1] > g.dots[0].x);
  assert.ok(line.path.every(([, x, y]) => !(x === g.dots[1].x && y === g.dots[1].y)));
});

test("graph: a focused row keeps its chains in colour, through any chain, and greys the rest", () => {
  const rows = [row("T-1", 1), row("T-2", 2, ["T-1"]), row("T-3", 3, ["T-2"]), row("T-4", 1), row("T-5", 2, ["T-4"])];
  const g = graphLayout(rows, 20, "T-3");
  assert.deepEqual(g.dots.filter(d => !d.dim).map(d => d.id), ["T-1", "T-2", "T-3"]);
  assert.deepEqual(g.lines.map(l => l.dim), [true, false, false]); // the focused ones last, over the grey
});

test("graph: a link takes the state of the ticket waited on", () => {
  const tone = (state, stage = "todo") => graphLayout([row("T-1", 1, [], { state, stage }), row("T-2", 2, ["T-1"])], 20).lines[0].tone;
  assert.equal(tone("closed"), "closed");
  assert.equal(tone("dropped"), "closed");
  assert.equal(tone("going", "in-progress"), "in-progress");
  assert.equal(tone("going", "in-review"), "in-review");
  assert.equal(tone("ready"), "blocked");
});

test("a link reaches a record, an evidence file or the web, and nothing else", () => {
  assert.deepEqual(linkTarget("https://x.y/?q=1"), { kind: "web", url: "https://x.y/?q=1" });
  assert.deepEqual(linkTarget("#T-3"), { kind: "record", id: "T-3" });
  assert.deepEqual(linkTarget("evidence/run%201.txt"), { kind: "evidence", file: "run 1.txt" });
  for (const bad of ["mailto:a@b.c", "javascript:alert(1)", "file:///etc/passwd", "slack://x", "/etc/passwd", "evidence/", "../x"]) {
    assert.equal(linkTarget(bad), null, bad);
  }
});

test("a row's text has no Markdown marks", () => {
  assert.equal(plainInline("Run `make` on **all** of *it*: [the doc](https://x.y) a*b"),
    "Run make on all of it: the doc a*b");
});

test("tracker open's request", () => {
  assert.deepEqual(parseOpen("pay/T-3"), { slug: "pay", id: "T-3" });
  assert.deepEqual(parseOpen("pay/"), { slug: "pay", id: null });
  assert.deepEqual(parseOpen(undefined), { slug: null, id: null });
});

test("the live line says only what is there", () => {
  const now = new Date(2026, 0, 1, 9, 5);
  assert.equal(hhmm(now), "09:05");
  assert.equal(liveLine({ synced: 0, issues: "" }, now), "live 09:05");
  assert.equal(liveLine({ synced: 0, issues: "issues read 09:00" }, now), "live 09:05 · issues read 09:00");
  assert.match(liveLine({ synced: now / 1000, issues: "" }, now), /· PRs from GitHub 09:05$/);
});

test("a view that follows says why it shows no tracker", () => {
  assert.equal(followNotice({ address: "t1", machine: null }, "work"), "");
  assert.equal(followNotice(null, null), "No agent is focused.");
  assert.equal(followNotice({ address: "t1", machine: null }, null), "The focused agent has no tracker.");
  assert.match(followNotice({ address: "mac/t1", machine: "mac" }, null), /agent on mac has no tracker/);
});

test("a README's Goal and Scope become labelled rows, with their sub-headings as bold lines", () => {
  assert.deepEqual(bodyParts("## Goal\n\nDo it.\n\n## Scope\n\n### In\n- a\n- b\n### Out\n- c"), [
    { label: "Goal", text: "Do it." },
    { label: "Scope", text: "**In**\n\n- a\n- b\n\n**Out**\n\n- c" },
  ]);
  assert.deepEqual(bodyParts(""), []);
  assert.deepEqual(bodyParts("## Plan\n\n\n\n## Carry forward"), []);
  assert.deepEqual(bodyParts("## Plan\n\n## Carry forward\n\n- a fact"), [{ label: "Carry forward", text: "- a fact" }]);
  assert.deepEqual(bodyParts("- a note\n\n## Plan\n\nDo it."), [{ label: "", text: "- a note" }, { label: "Plan", text: "Do it." }]);
});

test("a column of text fits its longest value", () => {
  assert.equal(fitWidth(["todo", "in-progress"], 10), fitWidth(["in-progress"], 10));
  assert.ok(fitWidth(["waiting"], 12) < fitWidth(["in-progress"], 12));
  assert.ok(fitWidth(["in-progress"], 12) >= 11 * 12 * 0.48);
  assert.equal(fitWidth([], 12), 0);
  assert.equal(fitWidth([null, ""], 12), 0);
});

test("an id column fits the longest id, within limits", () => {
  assert.equal(idWidth(["T-1", "T-10"], 10), idWidth(["T-10"], 10));
  assert.ok(idWidth(["T-100"], 10) > idWidth(["T-10"], 10));
  assert.equal(idWidth([], 10), idWidth(["T"], 10));
  assert.equal(idWidth(["x".repeat(40)], 10), idWidth(["x".repeat(16)], 10));
});

test("a status has one meaning, and an unknown one is quiet", () => {
  const all = Object.values(MEANINGS).flat();
  assert.equal(new Set(all).size, all.length);
  assert.equal(meaningOf("blocked"), "needs-you");
  assert.equal(meaningOf("in-review"), "held");
  assert.equal(meaningOf("in-progress"), "moving");
  assert.equal(meaningOf("ready"), "can-start");
  assert.equal(meaningOf("done"), "quiet");
  assert.equal(meaningOf("a tracker's own"), "quiet");
  assert.equal(meaningOf(undefined), "quiet");
});

test("an agent dot and a move take a status from the same table", () => {
  assert.equal(meaningOf(agentStatus([])), "quiet");
  assert.equal(meaningOf(agentStatus([{}])), "held");
  assert.equal(meaningOf(agentStatus([{}, { busy: true }])), "moving");
  assert.equal(meaningOf(agentStatus([{ busy: true }, { waiting: true }])), "needs-you");
  assert.equal(meaningOf(moveStatus({ mine: true })), "needs-you");
  assert.equal(meaningOf(moveStatus({ mine: true, quiet: true })), "quiet");
  assert.equal(meaningOf(moveStatus({ mine: false })), "quiet");
});

test("a row's word and colour agree wherever it shows", () => {
  const ready = { state: "ready", stage: "todo" }, blocked = { state: "blocked", stage: "todo" };
  const going = { state: "going", stage: "in-review" };
  assert.deepEqual([rowWord(ready), rowStatus(ready)], ["ready", "ready"]);
  assert.deepEqual([rowWord(blocked), rowStatus(blocked)], ["todo", "blocked"]);
  assert.deepEqual([rowWord(going), rowStatus(going)], ["in-review", "in-review"]);
});

test("the view colours a status only through the table", () => {
  const view = readFileSync(new URL("./view.js", import.meta.url), "utf8");
  assert.deepEqual(view.match(/\bt\.(blocked|working|done|ansi2)\b/g), null);
  assert.equal(view.match(/\bt\.accent\b/g)?.length, 1); // a PR's link, which is no status
});
