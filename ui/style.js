// How the tracker looks, in one place: every status's colour, and the parts the view builds its rows from. Colours come
// by the theme's names (theme() in herdr.d.ts), so a part follows the host's light and dark themes.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { meaningOf } from "./model.js";

// A colour fainter than the theme's: a theme's colour is "#rrggbb", and two more digits are its alpha.
const faint = (color, alpha) => `${String(color).slice(0, 7)}${alpha}`;
const ROW = "73", FILL = "1f", HOVER = "12"; // alphas: a row's background, a toggle's while on and under the pointer

// Each meaning's colour, by the theme's names: red, yellow, the terminal's green, blue, grey. The theme's `working` and
// `done` say nothing of a status here: an agent at work is green, and `done`'s colour differs from host to host.
const COLORS = { "needs-you": "blocked", held: "working", moving: "ansi2", "can-start": "accent", quiet: "muted" };
// A status's colour: the only way the view colours one. model.js's MEANINGS says which statuses mean what.
export const tone = (status, t) => t[COLORS[meaningOf(status)]];

// Every size the view draws with. The frame, the header and the text sizes are those of the other panels of a layout.
export const TEXT = { title: 15, body: 13, small: 12, tiny: 11, mark: 10 };
export const SPACE = { hair: 1, xxs: 2, xs: 4, sm: 6, md: 8, lg: 10, xl: 13, page: 14, indent: 18 };
export const RADIUS = { tag: 4, control: 5, card: 8, frame: 10 };
export const SIZE = {
  header: 42, card: 40, row: 28, line: 26, control: 24, // heights
  arrow: 12, dot: 8, // marks
  label: 76, modal: 620, // an opened record's labels and most width
  dropdown: 420, submenu: 260, menu: 320, // a dropdown's most width and height, and a dropdown's own dropdown's width
};

// A status as a pill: its colour behind the text.
const pill = (el, state, t) => el.flex_none().px(SPACE.sm).rounded(RADIUS.tag).text_size(TEXT.tiny).font_medium().bg(tone(state, t)).text_color(t.background);
export const chip = (text, state, t) => pill(div(), state, t).child(text);

// A pill that takes a click, which does nothing while `off`.
export function chipButton(id, text, state, t, click, { off = false } = {}) {
  return pill(div().id(id), state, t).child(text).opacity(off ? 0.5 : 1)
    .when(!off, el => el.cursor_pointer().hover(s => s.opacity(0.8)).on_click((_e, cx) => { cx.stop_propagation(); click(cx); }));
}

export function button(id, label, t, click, { on = false, off = false } = {}) {
  return h_flex().id(id).h(SIZE.control).px(SPACE.md).items_center().flex_none().rounded(RADIUS.control).text_size(TEXT.small)
    .text_color(off ? t.dim : on ? t.foreground : t.muted).when(on, el => el.bg(t.selected))
    .when(!off, el => el.cursor_pointer().hover(s => s.bg(t.hover)).on_click((_e, cx) => click(cx)))
    .child(label);
}

// A button that stays on or off: a rounded border, filled in faintly while on.
export function toggle(id, label, t, click, on) {
  return h_flex().id(id).h(SIZE.control).px(SPACE.md).items_center().flex_none().rounded(SIZE.control / 2).border_1().border_color(t.border)
    .text_size(TEXT.small).text_color(on ? t.foreground : t.muted).when(on, el => el.bg(faint(t.foreground, FILL)))
    .cursor_pointer().hover(s => s.bg(faint(t.foreground, HOVER))).on_click((_e, cx) => click(cx))
    .child(label);
}

// A dropdown's surface, above the page: as wide as it may be in a view of this width.
export function dropdown(id, t, viewWidth, most = SIZE.dropdown) {
  const room = viewWidth - 2 * SPACE.page;
  return v_flex().id(id).w(room > 0 ? Math.min(room, most) : most).max_h(SIZE.menu).overflow_y_scrollbar()
    .mt(SPACE.xs).p(SPACE.xs).gap(SPACE.hair).rounded(RADIUS.card).bg(t.popover).border_1().border_color(t.border).shadow_lg()
    .text_color(t.foreground).text_size(TEXT.body);
}

// A surface over the whole view, which keeps the page behind it still: a press outside it, or Escape, calls `close`.
// `keys` is the focus handle that takes Escape.
export function modal(id, t, viewWidth, keys, close, content) {
  const room = viewWidth - 2 * SPACE.page;
  const inside = cx => cx.stop_propagation();
  return div().absolute().inset_0()
    .child(div().absolute().inset_0().bg(t.background).opacity(0.75))
    .child(h_flex().id(`${id}-outside`).absolute().inset_0().p(SPACE.page).items_center().justify_center()
      .track_focus(keys)
      .on_key_down((e, cx) => { if (e.key === "escape") { cx.stop_propagation(); close(cx); } })
      .on_mouse_down("left", (_e, cx) => inside(cx))
      .on_scroll_wheel((_e, cx) => inside(cx))
      .on_click((_e, cx) => { inside(cx); close(cx); })
      .child(v_flex().id(id).w(room > 0 ? Math.min(room, SIZE.modal) : SIZE.modal).max_h_full().min_h(0).overflow_y_scroll()
        .px(SPACE.page).py(SPACE.lg).gap(SPACE.md).rounded(RADIUS.card).bg(t.popover).border_1().border_color(t.border).shadow_lg()
        .on_mouse_down("left", (_e, cx) => inside(cx))
        .on_click((_e, cx) => inside(cx))
        .children(content)));
}

// A row as a rounded card: `head` is its row's parts, `click` makes the row take a click, and `after` holds controls at
// the row's end with clicks of their own. A row's plain text brightens under the pointer: a part in a colour of its own
// keeps it.
export function card(id, head, t, { click = null, selected = false, after = [] } = {}) {
  return v_flex().w_full().flex_none().rounded(RADIUS.card).bg(selected ? t.selected : faint(t.raised, ROW))
    .child(h_flex().w_full().min_h(SIZE.card).items_center().rounded(RADIUS.card)
      .child(h_flex().id(id).flex_1().min_w(0).min_h(SIZE.card).px(SPACE.xl).gap(SPACE.sm).items_center().rounded(RADIUS.card)
        .text_size(TEXT.small).text_color(t.muted).when(after.length > 0, el => el.pr(0))
        .when(Boolean(click), el => el.cursor_pointer().hover(s => s.text_color(t.foreground)).on_click((_e, cx) => click(cx)))
        .children(head))
      .when(after.length > 0, el => el.pr(SPACE.xl).gap(SPACE.sm).children(after)));
}

// A list of cards. Text on a card that the page would draw dim takes t.muted: dim is too faint on t.raised.
export const cards = items => v_flex().w_full().gap(SPACE.sm).children(items);
