// How the tracker looks, in one place: every status's colour, and the parts the view builds its rows from. Colours come
// by the theme's names (theme() in herdr.d.ts), so a part follows the host's light and dark themes.
import { div } from "gpui-kit";
import { h_flex, v_flex } from "gpui-base";
import { meaningOf } from "./model.js";

// Each meaning's colour, by the theme's names: red, yellow, the terminal's green, blue, grey. The theme's `working` and
// `done` say nothing of a status here: an agent at work is green, and `done`'s colour differs from host to host.
const COLORS = { "needs-you": "blocked", held: "working", moving: "ansi2", "can-start": "accent", quiet: "muted" };
// A status's colour: the only way the view colours one. model.js's MEANINGS says which statuses mean what.
export const tone = (status, t) => t[COLORS[meaningOf(status)]];

// Every size the view draws with. The frame, the header and the text sizes are those of the other panels of a layout.
export const TEXT = { title: 15, body: 13, small: 12, tiny: 11, mark: 10 };
export const SPACE = { hair: 1, xxs: 2, xs: 4, sm: 6, md: 8, lg: 10, page: 14, indent: 18 };
export const RADIUS = { tag: 4, control: 5, card: 8, frame: 10 };
export const SIZE = {
  header: 42, card: 32, row: 28, line: 26, control: 24, // heights
  arrow: 12, dot: 8, // marks
  label: 76, detail: 420, // an opened record's labels and width
  dropdown: 420, submenu: 260, menu: 320, // a dropdown's most width and height, and a dropdown's own dropdown's width
};
export const COLUMN = { status: 88, group: 100, level: 52, link: 84 }; // the sequence's

// A status as a pill: its colour behind the text.
export function chip(text, state, t) {
  return div().flex_none().px(SPACE.sm).rounded(RADIUS.tag).text_size(TEXT.tiny).font_medium().bg(tone(state, t)).text_color(t.background).child(text);
}

export function button(id, label, t, click, { on = false, off = false } = {}) {
  return h_flex().id(id).h(SIZE.control).px(SPACE.md).items_center().flex_none().rounded(RADIUS.control).text_size(TEXT.small)
    .text_color(off ? t.dim : on ? t.foreground : t.muted).when(on, el => el.bg(t.selected))
    .when(!off, el => el.cursor_pointer().hover(s => s.bg(t.hover)).on_click((_e, cx) => click(cx)))
    .child(label);
}

// A dropdown's surface, above the page: as wide as it may be in a view of this width.
export function dropdown(id, t, viewWidth, most = SIZE.dropdown) {
  const room = viewWidth - 2 * SPACE.page;
  return v_flex().id(id).w(room > 0 ? Math.min(room, most) : most).max_h(SIZE.menu).overflow_y_scrollbar()
    .mt(SPACE.xs).p(SPACE.xs).gap(SPACE.hair).rounded(RADIUS.card).bg(t.popover).border_1().border_color(t.border).shadow_lg()
    .text_color(t.foreground).text_size(TEXT.body);
}

// A row as a rounded card: `head` is its row's parts, `click` makes the row take a click, `after` holds controls at the
// row's end with clicks of their own, and `more` draws what shows below the row.
export function card(id, head, t, { click = null, selected = false, after = [], more = null } = {}) {
  return v_flex().w_full().flex_none().rounded(RADIUS.card).bg(selected ? t.selected : t.raised)
    .child(h_flex().w_full().min_h(SIZE.card).items_center().rounded(RADIUS.card)
      .child(h_flex().id(id).flex_1().min_w(0).min_h(SIZE.card).px(SPACE.lg).gap(SPACE.sm).items_center().rounded(RADIUS.card)
        .text_size(TEXT.small)
        .when(Boolean(click), el => el.cursor_pointer().hover(s => s.bg(t.selected)).on_click((_e, cx) => click(cx)))
        .children(head))
      .when(after.length > 0, el => el.pr(SPACE.sm).gap(SPACE.xxs).children(after)))
    .when(Boolean(more), el => el.child(v_flex().w_full().px(SPACE.lg).pb(SPACE.md).gap(SPACE.xxs).text_size(TEXT.small).child(more())));
}

// A list of cards. Text on a card that the page would draw dim takes t.muted: dim is too faint on t.raised.
export const cards = items => v_flex().w_full().gap(SPACE.sm).children(items);
