// The tracker's one UI, in gpui-kit: the same code in a Herdr mod (main.js) and in its own window (app.js). All it
// knows of its host is the object they pass in, and all it knows of the tracker is the `tracker` CLI's JSON:
//
//   home()              the user's home folder
//   readText(path, machine)      a file's text, or null. A null machine is this one; on another, a path is from its home
//   run(command, args, machine)  a program's { code, stdout, stderr }
//   changed(cx, dir)    resolves when something under dir may have changed
//   colors()            the colours by Herdr's names (theme() in herdr.d.ts)
//   font()              the font for numbers and ids
//   sized(el, set)      tells set(width, cx) this view's width, and gives the element back. Herdr measures the element;
//                       public gpui-shell has no element measure, so its window is the view
//   start()             what `tracker open` asked for: { slug, id }, each or null
//   nextOpen()          optional: resolves with each later request, as "<slug>/<id>"
//   trackersDir()       optional: the trackers' folder, when it is not the default
//   cli()               optional: how to start the CLI, when the host was told
//   agentAt()           optional: the focused agent, { address, machine, pid, cwd } with machine null when it is here;
//                       null when the focus is on something else; undefined when it is on no terminal. With it the
//                       view follows that agent's tracker, unless it was opened on a tracker
import { PathBuilder, View, div } from "gpui-kit";
import { TextView, h_flex, v_flex } from "gpui-base";
import { filterRows, followNotice, GRAPH, graphLayout, liveLine, linkTarget, nextSort, nowTitle, parseOpen, plainInline,
  sortRows } from "./model.js";

const SCHEMA = 2; // viewmodel.VIEW_SCHEMA
const ROW = 28, LINE = 26;
const GROUP_MIN = 560, LEVELS_MIN = 800; // the sequence's width from which those columns show
const DETAIL_W = 420, DETAIL_MIN = 900; // the opened record sits beside the page from this width, else in its place
const LABEL_W = 76; // an opened record's labels
const REMOTE_DIR = ".claude/trackers"; // another machine's trackers, from its home: only a host here is told otherwise
const FOCUS_MS = 500; // how often the view looks at the focus: no program runs unless it moved

// Each status's colour, by the theme's names: blue when ready, amber under way, red blocked, green closed.
const TONES = {
  ready: "accent", going: "working", stack: "working", "in-progress": "working", "in-review": "working",
  open: "working", done: "done", merged: "done", closed: "done", blocked: "blocked", you: "blocked",
};
const tone = (state, t) => t[TONES[state]] ?? t.muted;

export function trackerView(host, first = null) {
  return class TrackerView extends View {
    init(_props, cx) {
      const start = host.start();
      this.filter = "all";
      this.sort = { key: "step", desc: false };
      this.focusId = null; // the hovered row, whose chains the graph shows
      this.selected = first?.selected ?? start.id;
      this.width = 0;
      this.panels = new Map(); // what the user opened or closed: it stays so as the data changes
      this.menu = Boolean(first?.menu); // the tracker menu
      this.moreOpen = first?.moreOpen ?? null; // the tracker menu's row with its Archive and Delete out
      this.archivedOpen = Boolean(first?.menu);
      this.doomed = first?.doomed ?? null; // the tracker the delete question asks about
      this.error = "";
      this.busy = false;
      this.view = first?.view ?? null;
      this.trackers = first?.trackers ?? null;
      this.slug = first?.view?.slug ?? start.slug;
      this.machine = null; // where this tracker lives: another machine when the agent followed runs there
      this.clis = new Map(); // per machine, how to start the CLI
      this.shown = "";
      this.live = "";
      this.follows = !first && Boolean(host.agentAt) && !start.slug;
      this.notice = first?.notice ?? ""; // why a view that follows shows no tracker
      this.agentAddress = undefined; // the agent followed
      this.followed = undefined; // that agent and its tracker: when they change, a tracker picked by hand gives way
      this.following = false;
      if (first) { // a check of the drawing: its data is given, and every part opens
        ["sec-check", "sec-reference", "links", "goal", "closed-decisions", "closed-actions", "more", "log"]
          .forEach(key => this.panels.set(key, true));
        return;
      }
      const dir = this.dir = host.trackersDir?.() ?? `${host.home()}/.claude/trackers`;
      cx.spawn(async cx => {
        await this.findCli(null);
        await this.listTrackers(cx);
        await this.followAgent(cx, true);
        await this.load(cx);
        if (this.follows) {
          cx.spawn(async cx => {
            for (;;) {
              await cx.sleep(FOCUS_MS);
              await this.followAgent(cx);
            }
          });
        }
        for (;;) {
          await host.changed(cx, dir);
          await cx.sleep(150); // a command writes several files
          await this.followAgent(cx, true); // `tracker start` links a session with the focus still on it
          await this.load(cx);
        }
      });
      // While this shows, PR state stays current. `sync --live` asks GitHub only if no sync ran lately.
      cx.spawn(async cx => {
        for (;;) {
          await cx.sleep((this.view?.sync_every ?? 120) * 1000);
          if (!this.slug || !this.view || this.view.archived) continue;
          try {
            await this.tracker("--tracker", this.slug, "sync", "--live");
          } catch {
            // gh missing, or offline: the next round tries again, and the live line says when GitHub was last read.
          }
          await this.load(cx);
        }
      });
      if (host.nextOpen) {
        cx.spawn(async cx => {
          for (;;) {
            const { slug, id } = parseOpen(await host.nextOpen());
            if (slug && (slug !== this.slug || this.machine)) this.show(slug, cx, null);
            if (id) this.selected = id;
            cx.notify();
          }
        });
      }
    }

    // Where a machine's CLI is: the plugin's path changes with each version, so its hooks record it. Here, else `tracker`
    // on PATH. On another machine PATH holds no plugin, so with no record the view says so.
    async findCli(machine) {
      if (this.clis.has(machine)) return this.clis.get(machine);
      let argv = machine ? null : host.cli?.();
      if (!argv?.length) {
        try {
          argv = JSON.parse(await host.readText(`${machine ? REMOTE_DIR : this.dir}/.cli.json`, machine))?.argv;
        } catch {
          // No hook has run yet, or the host cannot read it.
        }
      }
      if (!Array.isArray(argv) || !argv.length) {
        if (machine) throw new Error(`No tracker CLI recorded on ${machine}: it needs this plugin version, and a session on a tracker there`);
        argv = ["tracker"];
      }
      this.clis.set(machine, argv);
      return argv;
    }

    async trackerOn(machine, ...args) {
      const [command, ...rest] = await this.findCli(machine);
      const r = await host.run(command, [...rest, ...args], machine);
      if (r.code === 0) return r.stdout;
      this.clis.delete(machine); // a plugin update moves it: read the record again
      throw new Error(r.stderr.trim() || `tracker ${args.join(" ")} failed (${r.code})`);
    }

    tracker(...args) { return this.trackerOn(this.machine, ...args); }

    async listTrackers(cx) {
      const machine = this.machine;
      try {
        const list = JSON.parse(await this.tracker("list", "--json"));
        if (machine !== this.machine) return; // another machine came to show while this one answered
        this.trackers = list;
        if (!this.follows) this.slug ??= this.trackers.active[0]?.slug ?? null;
      } catch (e) {
        this.error = String(e.message ?? e);
      }
      cx.notify();
    }

    async attachedTo(agent) {
      if (!agent.pid) return null;
      const where = agent.cwd ? ["--cwd", agent.cwd] : [];
      return JSON.parse(await this.trackerOn(agent.machine, "attached", "--pid", String(agent.pid), ...where)).slug;
    }

    // Show the focused agent's tracker, or say why there is none. `recheck`: ask again with the focus where it was.
    async followAgent(cx, recheck = false) {
      if (!this.follows || this.following) return;
      const agent = host.agentAt();
      if (agent === undefined) return; // the focus is on no terminal, such as on this view: keep what shows
      const address = agent?.address ?? null, machine = agent?.machine ?? null;
      if (!recheck && address === this.agentAddress) return;
      this.following = true;
      try {
        const slug = agent ? await this.attachedTo(agent) : null;
        const now = host.agentAt();
        if (now !== undefined && (now?.address ?? null) !== address) return; // it moved on: the next look takes it
        this.agentAddress = address;
        if (!slug && this.error) {
          this.error = "";
          cx.notify();
        }
        const followed = `${address} ${slug}`;
        if (followed === this.followed) return;
        this.followed = followed;
        if (slug !== this.slug || machine !== this.machine) this.show(slug, cx, machine);
        this.notice = followNotice(agent, slug);
        cx.notify();
      } catch (e) {
        this.agentAddress = address; // no program runs again until the focus moves or the next look
        this.followed = undefined;
        if (machine !== this.machine) this.show(null, cx, machine);
        if (!this.slug) this.notice = "Could not ask the focused agent's machine for its tracker.";
        this.error = String(e.message ?? e);
        cx.notify();
      } finally {
        this.following = false;
      }
    }

    async load(cx) {
      if (!this.slug) return;
      const slug = this.slug, machine = this.machine;
      const moved = () => slug !== this.slug || machine !== this.machine; // another tracker came to show meanwhile
      try {
        const text = await this.trackerOn(machine, "data", slug);
        if (moved()) return;
        const live = this.view ? liveLine(this.view, new Date()) : "";
        if (text === this.shown && live === this.live && !this.error) return; // nothing to draw
        if (text !== this.shown) {
          const view = JSON.parse(text);
          if (view.schema !== SCHEMA) {
            throw new Error(`The tracker plugin gives data ${view.schema}; this UI reads ${SCHEMA}: run the plugin's scripts/install-herdr-mod.sh`);
          }
          this.shown = text;
          this.view = view;
        }
        this.live = liveLine(this.view, new Date());
        this.error = "";
      } catch (e) {
        if (moved()) return;
        this.error = String(e.message ?? e);
      }
      cx.notify();
    }

    show(slug, cx, machine = this.machine) {
      if (machine !== this.machine) this.trackers = null; // the menu's list is the machine's
      this.machine = machine;
      this.slug = slug;
      this.notice = "";
      this.menu = false;
      this.selected = null;
      this.filter = "all";
      this.view = null;
      this.shown = "";
      cx.notify();
      cx.spawn(async cx => { await this.load(cx); });
    }

    // A change the user asked for: a refusal shows in the error line, and the data reads again either way.
    change(cx, args, { list = false, then = null } = {}) {
      if (this.busy) return;
      this.busy = true;
      cx.notify();
      cx.spawn(async cx => {
        let refused = "";
        try {
          await this.tracker(...args);
          then?.();
        } catch (e) {
          refused = String(e.message ?? e);
        }
        this.busy = false;
        if (list) await this.listTrackers(cx);
        await this.load(cx);
        this.error = refused || this.error;
        cx.notify();
      });
    }

    follow(url, cx) {
      const target = linkTarget(url);
      if (target?.kind === "web") cx.open_url(target.url);
      else if (target?.kind === "evidence") this.change(cx, ["--tracker", this.slug, "evidence", target.file]);
      else if (target?.kind === "record" && this.view?.records[target.id]) this.select(target.id, cx);
    }

    select(id, cx) {
      this.selected = this.selected === id ? null : id;
      cx.notify();
    }

    isOpen(key, start) { return this.panels.get(key) ?? start; }

    mono(el) { return el.font_family(host.font()); }

    md(id, text) {
      return TextView.markdown(id, text).on_link_click((url, cx) => this.follow(url, cx));
    }

    button(id, label, t, click, { on = false, off = false } = {}) {
      return h_flex().id(id).h(24).px(8).items_center().flex_none().rounded(5).text_size(12)
        .text_color(off ? t.dim : on ? t.foreground : t.muted).when(on, el => el.bg(t.selected))
        .when(!off, el => el.cursor_pointer().hover(s => s.bg(t.hover)).on_click((_e, cx) => click(cx)))
        .child(label);
    }

    chip(text, state, t) {
      return div().flex_none().px(6).rounded(4).text_size(11).bg(t.raised).text_color(tone(state, t)).child(text);
    }

    // Every part of the page that opens: its head, and its body while open.
    panel(key, head, start, body, t) {
      const open = this.isOpen(key, start);
      return v_flex().w_full().flex_none()
        .child(h_flex().id(`panel-${key}`).w_full().min_h(LINE).gap(6).items_center().cursor_pointer().rounded(5)
          .hover(s => s.bg(t.hover))
          .on_click((_e, cx) => { this.panels.set(key, !open); cx.notify(); })
          .child(div().w(12).flex_none().text_size(10).text_color(t.dim).child(open ? "▾" : "▸"))
          .child(h_flex().flex_1().min_w(0).gap(6).items_center().child(head)))
        .when(open, el => el.child(body()));
    }

    // A section of the page: its heading, with the count, opens and closes it.
    section(key, title, count, start, body, t) {
      return div().w_full().flex_none().px(14).pt(10).child(this.panel(`sec-${key}`,
        div().text_size(13).font_semibold().child(count ? `${title} · ${count}` : title), start,
        () => v_flex().w_full().pl(18).pt(2).gap(1).child(body()), t));
    }

    named(name, meta, t) {
      return h_flex().flex_1().min_w(0).gap(8).items_center()
        .child(div().min_w(0).truncate().font_medium().child(name))
        .when(Boolean(meta), el => el.child(div().flex_none().text_size(11).text_color(t.dim).child(meta)));
    }

    // A record's id that opens it. `place` keeps the element's id the same from draw to draw, or a click is lost.
    idLink(place, id, t, color = null) {
      return this.mono(div().id(`link-${place}-${id}`).flex_none().cursor_pointer().text_size(12)
        .text_color(color ?? t.muted).on_click((_e, cx) => this.select(id, cx)).child(id));
    }

    // A record's row: it opens beside the page.
    recordRow(id, children, t, after = []) {
      return h_flex().w_full().min_h(LINE).gap(8).items_center().text_size(12).rounded(5)
        .when(this.selected === id, el => el.bg(t.selected))
        .child(h_flex().id(`record-${id}`).flex_1().min_w(0).min_h(LINE).gap(8).items_center().cursor_pointer()
          .hover(s => s.bg(t.hover)).on_click((_e, cx) => this.select(id, cx)).children(children))
        .children(after);
    }

    actionRow(a, buttons, t) {
      const color = a.due?.late === "overdue" ? t.blocked : a.due?.late === "today" ? t.working : t.dim;
      return this.recordRow(a.id, [
        this.mono(div().flex_none().text_color(t.muted).child(a.id)),
        ...(a.closed ? [this.chip(a.status, a.status, t)] : []),
        ...(a.gate ? [this.chip(a.gate, "blocked", t)] : []),
        div().flex_1().min_w(0).truncate().child(a.title),
        ...(a.when ? [div().flex_none().text_size(11).text_color(t.dim).child(a.when)] : []),
        ...(a.due ? [div().flex_none().text_size(11).text_color(color).child(`Due: ${a.due.text}`)] : []),
      ], t, buttons ? [["Done", "--done"], ["Drop", "--drop"]].map(([label, flag]) =>
        this.button(`${label}-${a.id}`, label, t, cx => this.change(cx, ["--tracker", this.slug, "act", a.id, flag]),
          { off: this.busy })) : []);
    }

    decisionRow(d, t) {
      return this.recordRow(d.id, [
        this.mono(div().flex_none().text_color(t.muted).child(d.id)),
        this.chip(d.status, d.status, t),
        ...(d.gate ? [this.chip(d.gate, "blocked", t)] : []),
        div().flex_1().min_w(0).truncate().font_medium().child(d.title),
      ], t);
    }

    agentMark(agents, t) {
      if (!agents.length) return [];
      const busy = agents.some(a => a.busy);
      return [div().flex_none().size(8).rounded(4).when(busy, el => el.bg(t.working))
        .when(!busy, el => el.border(1).border_color(t.muted))];
    }

    nowItem(item, t) {
      const more = () => v_flex().pl(18).pb(4).gap(2).text_size(12)
        .when(Boolean(item.line), el => el.child(div().text_color(t.muted).child(plainInline(item.line))))
        .when(Boolean(item.on?.length), el => el.child(h_flex().flex_wrap().gap(6).items_center()
          .child(div().text_color(t.dim).child("on"))
          .children(item.on.map(x => h_flex().gap(4).child(this.idLink(`on-${item.key}`, x.id, t))
            .child(div().text_color(t.dim).child(x.stage))))))
        .when(Boolean(item.fresh), el => el.child(div().text_size(11).text_color(t.dim).child(item.fresh)))
        .children(item.agents.map(a => div().text_size(11).text_color(t.dim)
          .child(`session ${a.name}: ${a.word}, in ${a.cwd}`)));
      const head = h_flex().flex_1().min_w(0).gap(6).items_center().text_size(12)
        .children(this.agentMark(item.agents, t))
        .when(item.kind === "ticket", el => el
          .child(this.idLink("now", item.id, t, tone(item.stage, t)))
          .child(this.chip(item.stage, item.stage, t))
          .when(Boolean(item.move), row => row.child(this.chip(item.move.text, item.move.mine ? "you" : "them", t))))
        .child(this.named(item.title, item.meta, t));
      return this.panel(`now-${item.key}`, head, false, more, t);
    }

    // The columns that fit the sequence's width, each [key, label, width, the cell's text and colour].
    columns(view, width, t) {
      const cols = [["status", "Status", 88, r => (r.gate.state === "ready" ? ["ready", t.accent] : [r.stage, tone(r.state, t)])]];
      if (width >= GROUP_MIN) cols.push(["group", "Group", 100, r => [r.group, t.muted]]);
      if (width >= LEVELS_MIN) {
        view.scales.forEach(key => cols.push([key, key[0].toUpperCase() + key.slice(1), 52, r => [r.levels[key], t.muted]]));
        cols.push(["waits", "← Waits on", 84, r => [r.waits.map(d => d.id + (d.done ? " ✓" : "")).join(", "), t.muted]]);
        cols.push(["unblocks", "→ Unblocks", 84, r => [r.unblocks.join(", "), t.muted]]);
      }
      return cols;
    }

    heading(key, label, t, size) {
      const on = this.sort.key === key && key !== "step";
      return size(div().id(`sort-${key}`).cursor_pointer().truncate().text_color(on ? t.foreground : t.dim)
        .hover(s => s.text_color(t.foreground))
        .on_click((_e, cx) => { this.sort = nextSort(this.sort, key); cx.notify(); })
        .child(label + (on ? (this.sort.desc ? " ↓" : " ↑") : "")));
    }

    row(r, cols, graphW, t) {
      return h_flex().id(`row-${r.id}`).w_full().h(ROW).pl(10).pr(14).gap(10).items_center().text_size(12).cursor_pointer()
        .when(this.selected === r.id, el => el.bg(t.selected))
        .when(this.focusId === r.id && this.selected !== r.id, el => el.bg(t.hover))
        .on_hover((hovered, cx) => {
          const id = hovered ? r.id : this.focusId === r.id ? null : this.focusId;
          if (id !== this.focusId) { this.focusId = id; cx.notify(); }
        })
        .on_click((_e, cx) => this.select(r.id, cx))
        .child(div().w(graphW).flex_none())
        .child(this.mono(div().flex_none().text_color(tone(r.state, t)).child(r.id)))
        .child(div().flex_1().min_w(0).truncate().text_color(r.closed ? t.muted : t.foreground).child(r.title))
        .when(Boolean(r.pr), el => el.child(this.mono(div().flex_none().text_size(11).text_color(t.dim).child(`PR ${r.pr}`))))
        .children(cols.map(([, , w, cell]) => {
          const [text, color] = cell(r);
          return div().w(w).flex_none().truncate().text_color(color).child(text || " ");
        }));
    }

    // One stroke per line: GPUI paints a path in one colour. The layer lies over the rows' graph column.
    graph(layout, t) {
      const lineColor = l => (l.dim ? t.border : tone(l.tone, t));
      const stroke = l => {
        const b = PathBuilder.stroke(1.5);
        for (const [op, x, y, cx, cy] of l.path) {
          if (op === "move") b.move_to(x, y);
          else if (op === "line") b.line_to(x, y);
          else b.curve_to(x, y, cx, cy);
        }
        return window.paint_path(b.build(), lineColor(l)).absolute().top(0).left(0).size_full();
      };
      const R = GRAPH.radius, H = R + GRAPH.halo;
      return div().absolute().top(0).left(10).bottom(0).w(layout.width)
        .children(layout.lines.map(stroke))
        .children(layout.dots.flatMap(d => {
          const color = d.dim ? t.border : tone(d.state, t);
          const dot = div().absolute().left(d.x - R).top(d.y - R).size(2 * R).rounded(R);
          return [div().absolute().left(d.x - H).top(d.y - H).size(2 * H).rounded(H).bg(t.background),
            d.hollow ? dot.border_color(color).border(GRAPH.ring) : dot.bg(color)];
        }));
    }

    sequence(view, width, t) {
      const rows = sortRows(filterRows(view.tickets, this.filter), this.sort);
      const layout = graphLayout(rows, ROW, this.focusId);
      const cols = this.columns(view, width, t);
      const graphW = Math.max(layout.width, 28);
      return v_flex().w_full()
        .children(view.spans.map(line => div().text_size(11).text_color(t.dim).child(line)))
        .child(h_flex().flex_wrap().py(4).gap(2).children(view.filters.map(([key, count]) =>
          this.button(`f-${key}`, `${key} ${count}`, t, cx => { this.filter = key; cx.notify(); }, { on: this.filter === key }))))
        .child(h_flex().h(24).pl(10).pr(14).gap(10).items_center().text_size(11).border_b(1).border_color(t.border)
          .child(this.heading("step", "Step", t, el => el.w(graphW).flex_none()))
          .child(this.heading("ticket", "Ticket", t, el => el.flex_1().min_w(0)))
          .children(cols.map(([key, label, w]) => this.heading(key, label, t, el => el.w(w).flex_none()))))
        .child(div().relative().w_full().h(ROW * rows.length).flex_none()
          .children(rows.map(r => this.row(r, cols, graphW, t)))
          .child(this.graph(layout, t)));
    }

    reference(view, t) {
      const { decisions, actions, more, log } = view.reference;
      const part = (key, name, meta, body) => this.panel(key,
        h_flex().flex_1().min_w(0).text_size(12).child(this.named(name, meta, t)), false,
        () => v_flex().w_full().pl(18).gap(1).child(body()), t);
      return v_flex().w_full().gap(1)
        .when(decisions.length > 0, el => el.child(part("closed-decisions", "Closed decisions", String(decisions.length),
          () => v_flex().gap(1).children(decisions.map(d => this.decisionRow(d, t))))))
        .when(actions.length > 0, el => el.child(part("closed-actions", "Closed actions", `${actions.length}, newest first`,
          () => v_flex().gap(1).children(actions.map(a => this.actionRow(a, false, t))))))
        .when(Boolean(more), el => el.child(part("more", "More about this work", more?.headings.join(", "),
          () => this.md("reference-more", more.body))))
        .child(part("log", "Log", `${log.length} entries, newest first`, () => (log.length
          ? this.md("reference-log", log.map(x => `- ${x}`).join("\n"))
          : div().text_size(12).text_color(t.dim).child("Empty."))));
    }

    page(view, width, t) {
      const { now, check } = view;
      const problems = [...check.errors.map(x => `✗ ${x}`), ...check.warnings.map(x => `⚠ ${x}`)];
      const labels = [...new Set(view.links.map(x => x.label.toLowerCase()))].join(", ");
      const top = v_flex().flex_none().px(14).pt(10).gap(2)
        .child(div().text_size(15).font_semibold().child(view.title))
        .child(div().text_size(12).text_color(t.dim).child(view.facts || " "))
        .child(div().text_size(11).text_color(t.dim).child(`${view.slug} · ${view.root}`))
        .when(view.archived, el => el.child(h_flex().mt(6).gap(8).items_center().text_size(12).text_color(t.muted)
          .child(div().flex_1().min_w(0).child("Archived: out of every list and hook, its PRs never synced."))
          .child(this.button("unarchive", "Unarchive", t, cx => this.change(cx, ["unarchive", view.slug], { list: true }), { off: this.busy }))))
        .when(view.links.length > 0, el => el.child(this.panel("links",
          h_flex().flex_1().min_w(0).text_size(12).child(this.named("Links", `${view.links.length}: ${labels}`, t)), false,
          () => v_flex().pl(18).gap(2).children(view.links.map((x, i) => this.labelled(x.label,
            this.linkText(`context-${i}`, x, t), t))), t)))
        .when(Boolean(view.goal), el => el.child(this.panel("goal",
          h_flex().flex_1().min_w(0).text_size(12).child(this.named(view.goal?.headings.join(", ") ?? "", "", t)), false,
          () => div().pl(18).child(this.md("goal", view.goal.body)), t)));
      return v_flex().id("page").flex_1().min_w(0).h_full().pb(14).overflow_y_scrollbar()
        .child(top)
        .when(view.actions.length > 0, el => el.child(this.section("actions", "Your actions", view.actions.length, true,
          () => v_flex().gap(1).children(view.actions.map(a => this.actionRow(a, !view.archived, t))), t)))
        .when(now.items.length > 0 || Boolean(now.idle) || Boolean(now.watch), el => el.child(this.section("now", "Current",
          nowTitle(now), true, () => v_flex().gap(1)
            .children(now.items.map(item => this.nowItem(item, t)))
            .when(Boolean(now.idle), box => box.child(h_flex().flex_wrap().gap(6).min_h(LINE).items_center().text_size(12).text_color(t.dim)
              .child(div().child(`Nothing in progress.${now.idle.ready.length ? " Ready:" : ""}`))
              .children(now.idle.ready.map(id => this.idLink("idle", id, t)))))
            .when(Boolean(now.watch), box => box.child(div().text_size(11).text_color(now.watch.running ? t.muted : t.dim).child(now.watch.text))), t)))
        .child(this.section("seq", "Sequence", `${view.open} open of ${view.total}`, true, () => this.sequence(view, width - 46, t), t))
        .when(problems.length > 0, el => el.child(this.section("check", "Check", problems.length, false,
          () => v_flex().gap(1).text_size(12).children(problems.map(x => div().child(x))), t)))
        .child(this.section("decisions", "Open decisions", view.decisions.length, true, () => (view.decisions.length
          ? v_flex().gap(1).children(view.decisions.map(d => this.decisionRow(d, t)))
          : div().text_size(12).text_color(t.dim).child("None.")), t))
        .child(this.section("reference", "Reference", 0, false, () => this.reference(view, t), t));
    }

    labelled(label, value, t) {
      return h_flex().w_full().gap(8).items_start().text_size(12)
        .child(div().w(LABEL_W).flex_none().text_color(t.dim).child(label))
        .child(div().flex_1().min_w(0).child(value));
    }

    linkText(key, x, t) {
      return v_flex().min_w(0).child(this.md(`${key}-text`, x.text))
        .children(x.sub.map((y, i) => div().text_size(11).text_color(t.dim).child(this.md(`${key}-sub-${i}`, y))));
    }

    refs(place, items, t) {
      return h_flex().flex_wrap().gap(6).children(items.map(d => {
        const color = tone(d.tone === "stack" ? "going" : d.tone, t);
        const text = d.id + (d.done ? " ✓" : "");
        return d.ref ? this.mono(div().id(`dep-${place}-${d.id}`).cursor_pointer().text_color(color)
          .on_click((_e, cx) => { this.selected = d.id; cx.notify(); }).child(text))
          : this.mono(div().text_color(color).child(d.title ? `${text} (${plainInline(d.title)})` : text));
      }));
    }

    // What a record shows when it opens: one grid of its lead lines, what it waits on and what waits on it, its facts
    // and links, then its body.
    details(view, id, wide, t) {
      const r = view.records[id];
      const word = r.gate.state === "ready" ? "ready" : r.stage;
      const rows = [
        ...(r.time.length ? [["time", div().child(r.time.map(x => `${x.name} ${x.text}`).join(" · "))]] : []),
        ...(r.line ? [[r.line.label, this.md(`line-${r.id}`, r.line.text)]] : []),
        ...(r.move ? [["move", div().text_color(r.move.mine ? t.blocked : t.muted).child(r.move.text)]] : []),
        ...r.deps.map(d => [d.label, d.items ? this.refs(`${r.id}-${d.label}`, d.items, t) : div().child(d.text)]),
        ...r.facts.map(f => [f.label, f.code ? this.mono(div()).child(f.text) : div().child(f.text)]),
        ...r.links.map((x, i) => [x.label, this.linkText(`link-${r.id}-${i}`, x, t)]),
      ];
      return v_flex().id("details").h_full().min_h(0).overflow_y_scrollbar().px(14).py(10).gap(8)
        .when(wide, el => el.w(DETAIL_W).flex_none().border_l(1).border_color(t.border))
        .when(!wide, el => el.flex_1().min_w(0))
        .child(h_flex().flex_none().gap(8).items_center().text_size(12)
          .child(this.mono(div().flex_none().text_color(t.muted).child(r.id)))
          .child(this.chip(word, word, t))
          .when(Boolean(r.gate.text) && r.gate.state !== "ready", el => el.child(div().min_w(0).truncate().text_color(t.dim).child(r.gate.text)))
          .child(div().flex_1())
          .child(this.button("close", "Close", t, cx => { this.selected = null; cx.notify(); })))
        .child(div().flex_none().text_size(15).font_medium().child(r.title))
        .child(v_flex().flex_none().gap(3).children(rows.map(([label, value]) => this.labelled(label, value, t))))
        .when(Boolean(r.body), el => el.child(div().flex_none().w_full().child(this.md(`body-${r.id}`, r.body))));
    }

    menuRow(x, archived, t) {
      const open = this.moreOpen === x.slug;
      const off = Boolean(x.in_use) || this.busy;
      return v_flex().w_full()
        .child(h_flex().w_full().h(26).gap(6).items_center().rounded(5).when(x.slug === this.slug, el => el.bg(t.selected))
          .child(h_flex().id(`tracker-${x.slug}`).flex_1().min_w(0).h_full().px(6).gap(8).items_center().cursor_pointer().rounded(5)
            .hover(s => s.bg(t.hover)).on_click((_e, cx) => this.show(x.slug, cx))
            .child(div().min_w(0).flex_1().truncate().text_size(12).child(x.title))
            .when(Boolean(x.in_use), el => el.child(div().flex_none().text_size(11).text_color(t.working).child(`in use by ${x.in_use}`)))
            .child(div().flex_none().text_size(11).text_color(t.dim).child(x.slug)))
          .child(this.button(`more-${x.slug}`, "⋯", t, cx => { this.moreOpen = open ? null : x.slug; cx.notify(); }, { on: open })))
        .when(open, el => el.child(h_flex().w_full().pl(10).gap(4).items_center()
          .child(this.button(`archive-${x.slug}`, archived ? "Unarchive" : "Archive", t,
            cx => this.change(cx, [archived ? "unarchive" : "archive", x.slug], { list: true, then: () => { this.moreOpen = null; } }), { off }))
          .child(this.button(`delete-${x.slug}`, "Delete", t, cx => { this.doomed = x; cx.notify(); }, { off }))));
    }

    trackerMenu(t) {
      const { active = [], archived = [] } = this.trackers ?? {};
      return v_flex().id("tracker-menu").flex_none().max_h(320).overflow_y_scrollbar().px(10).py(4).gap(1).border_b(1).border_color(t.border)
        .children(active.map(x => this.menuRow(x, false, t)))
        .when(archived.length > 0, el => el
          .child(this.button("archived", `Archived ${archived.length}`, t, cx => { this.archivedOpen = !this.archivedOpen; cx.notify(); }, { on: this.archivedOpen }))
          .when(this.archivedOpen, box => box.children(archived.map(x => this.menuRow(x, true, t)))));
    }

    deleteQuestion(x, t) {
      const gone = () => {
        this.doomed = null;
        this.moreOpen = null;
        if (x.slug !== this.slug) return;
        this.slug = null; // listTrackers then takes the next one left
        this.view = null;
        this.shown = "";
        this.selected = null;
      };
      return v_flex().flex_none().px(14).py(8).gap(6).text_size(12).border_b(1).border_color(t.border)
        .child(div().child(`Delete ${x.title}? Its folder goes to the system's trash: ${x.root}`))
        .child(h_flex().gap(4)
          .child(this.button("delete-no", "Cancel", t, cx => { this.doomed = null; cx.notify(); }))
          .child(this.button("delete-yes", "Delete", t, cx => this.change(cx, ["delete", x.slug, "--yes"], { list: true, then: gone }), { off: this.busy })));
    }

    bar(view, t) {
      return h_flex().flex_none().h(36).px(10).gap(8).items_center().border_b(1).border_color(t.border)
        .child(h_flex().id("switch").min_w(0).h(26).px(6).gap(6).items_center().cursor_pointer().rounded(5).hover(s => s.bg(t.hover))
          .on_click((_e, cx) => {
            this.menu = !this.menu;
            cx.notify();
            if (this.menu) cx.spawn(async cx => { await this.listTrackers(cx); });
          })
          .child(div().min_w(0).truncate().text_size(12).child([this.machine, view?.slug ?? this.slug ?? "Trackers"].filter(Boolean).join(" / ")))
          .child(div().flex_none().text_size(10).text_color(t.dim).child(this.menu ? "▴" : "▾")))
        .child(div().flex_1().min_w(0).truncate().text_right().text_size(11).text_color(t.dim).child(this.live || " "))
        .when(Boolean(view) && !view.archived, el => el.child(this.button("refresh", "Refresh", t,
          cx => this.change(cx, ["--tracker", this.slug, "refresh"]), { off: this.busy })));
    }

    render() {
      const t = host.colors();
      const root = host.sized(v_flex().id("tracker").size_full().min_h(0).overflow_hidden().bg(t.background)
        .text_color(t.foreground).text_size(13), (width, cx) => {
        if (Math.abs(width - this.width) > 1) { this.width = width; cx?.notify(); }
      });
      const view = this.view;
      root.child(this.bar(view, t));
      if (this.menu) root.child(this.trackerMenu(t));
      if (this.doomed) root.child(this.deleteQuestion(this.doomed, t));
      if (this.error) root.child(div().flex_none().px(14).py(6).text_size(12).text_color(t.blocked).child(this.error));
      if (this.notice && !view) {
        return root.child(v_flex().p(14).gap(4).text_size(12).text_color(t.dim)
          .child(div().text_size(15).font_semibold().text_color(t.foreground).child("No tracker to show"))
          .child(div().child(this.notice))
          .child(div().child("Pick one with the switcher above, or ask the agent to start one.")));
      }
      if (!view) {
        return root.child(div().p(14).text_size(12).text_color(t.dim)
          .child(this.slug ? "Loading…" : "No tracker open: pick one above, or ask the agent to make one."));
      }
      const wide = this.width >= DETAIL_MIN;
      const open = Boolean(this.selected && view.records[this.selected]);
      const body = h_flex().flex_1().min_h(0).w_full().items_start();
      if (!open || wide) body.child(this.page(view, open ? this.width - DETAIL_W : this.width, t));
      if (open) body.child(this.details(view, this.selected, wide, t));
      return root.child(body);
    }
  };
}
