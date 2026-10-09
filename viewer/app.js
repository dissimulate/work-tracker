// work-tracker viewer: keeps the page in step with the tracker files.
// Every 3 s it asks the server for a version "<data>.<code>.<synced>". A data change swaps <main> in place, keeping
// open tickets and the active filter; a code change (this file, style.css, page.html, the Python) reloads the page;
// synced is when the server last pulled PR state from GitHub.

const slug = document.body.dataset.slug;
const live = document.getElementById('live');
let version = document.body.dataset.version;
let filter = 'all';

// The sequence's sort: a column and a direction, kept in `?sort=` (`-` first for descending) so a reload or a shared
// link keeps it. `step` is the dependency order the server sends, and the default. The server names the keys: each
// heading button's data-sort, and each row's value for it in data-sort-<key> (Column in viewer.py).
let sort = readSort();

function sorter(key) {
  return document.querySelector(`.seq-head [data-sort="${CSS.escape(key)}"]`);
}

function readSort() {
  const raw = new URLSearchParams(location.search).get('sort') || '';
  const key = raw.replace(/^-/, '');
  return key && key !== 'step' && sorter(key) ? { key, desc: raw.startsWith('-') } : { key: 'step', desc: false };
}

const value = (d, key) => d.getAttribute('data-sort-' + key) ?? '';

// Values compare with the numbers in them as numbers ("T-2" before "T-10", "9" before "10"). Rows with no value (no
// group) go last in either direction; equal values keep the dependency order.
function compare(a, b) {
  const x = value(a, sort.key), y = value(b, sort.key);
  if ((x === '') !== (y === '')) return x === '' ? 1 : -1;
  const by = x.localeCompare(y, undefined, { numeric: true, sensitivity: 'base' });
  return (sort.desc ? -by : by) || value(a, 'step') - value(b, 'step');
}

function applySort() {
  const seq = document.querySelector('.seq');
  if (!seq) return;
  [...seq.querySelectorAll(':scope > details.t')].sort(compare).forEach(d => seq.append(d));
  seq.querySelectorAll('.seq-head button').forEach(b => {
    const on = b.dataset.sort === sort.key && sort.key !== 'step';
    if (on) b.dataset.dir = sort.desc ? 'desc' : 'asc'; else delete b.dataset.dir;
    b.setAttribute('aria-pressed', on);
  });
}

function setSort(key) {
  sort = key === 'step' ? { key, desc: false } : { key, desc: sort.key === key && !sort.desc };
  const q = new URLSearchParams(location.search);
  if (sort.key === 'step') q.delete('sort'); else q.set('sort', (sort.desc ? '-' : '') + sort.key);
  history.replaceState(null, '', location.pathname + (q.size ? '?' + q : '') + location.hash);
  applySort();
  applyFilter();
  const said = document.getElementById('sort-said');
  if (said) said.textContent = sort.key === 'step' ? 'Sorted in dependency order'
    : `Sorted by ${sorter(sort.key).dataset.said}, ${sort.desc ? 'descending' : 'ascending'}`;
}

const GATES = ['ready', 'blocked']; // matched on data-b, not the status

function applyFilter() {
  document.querySelectorAll('.filters button').forEach(b => b.setAttribute('aria-pressed', b.dataset.f === filter));
  document.querySelectorAll('details.t').forEach(d => {
    const { s, b, c } = d.dataset; // c: "1" when the server counts the ticket closed
    d.hidden = !(filter === 'all' ||
      (filter === 'active' ? c !== '1' : GATES.includes(filter) ? b === filter : s === filter));
  });
  // A step's number shows on its first shown row only, while the rows are in dependency order.
  let step = null;
  document.querySelectorAll('details.t:not([hidden])').forEach(d => {
    d.classList.toggle('rep', sort.key === 'step' && d.dataset.step === step);
    step = d.dataset.step;
  });
}

function detailsFor(id) {
  return document.querySelector(`details[data-id="${CSS.escape(id)}"]`);
}

function openRecord(id) {
  const d = detailsFor(id);
  if (d) {
    if (d.hidden) { filter = 'all'; applyFilter(); }
    for (let p = d; p; p = p.parentElement.closest('details')) p.open = true; // also the sections around it
    d.scrollIntoView({ block: 'start' });
  }
}

// `tracker open <id>` puts the id in the hash. It opens once: the hash is then cleared, so a reload starts collapsed.
function openHash() {
  const id = decodeURIComponent(location.hash.slice(1));
  if (!id) return;
  openRecord(id);
  history.replaceState(null, '', location.pathname + location.search);
}

let stopped = false; // the viewer did not answer: a new one has a new token, so the page reloads when it answers
async function poll() {
  try {
    const res = await fetch(`/t/${slug}/version`, { cache: 'no-store' });
    if (stopped) return location.reload();
    if (res.status === 404) { // deleted, from this page or another
      live.textContent = 'tracker deleted — open another from the tracker menu';
      live.className = 'off';
      live.title = live.textContent;
      return;
    }
    const next = await res.text();
    const [data, code, synced] = next.split('.');
    const [oldData, oldCode] = version.split('.');
    if (code !== oldCode) return location.reload();
    if (data !== oldData) {
      // Keep each part open or closed as it was, also a section that starts open; new parts take their default.
      const was = new Map([...document.querySelectorAll('details[data-id]')].map(d => [d.dataset.id, d.open]));
      document.querySelector('main').innerHTML =
        await (await fetch(`/t/${slug}/main`, { cache: 'no-store' })).text();
      was.forEach((open, id) => { const d = detailsFor(id); if (d) d.open = open; });
      showRefresh();
      applySort();
      applyFilter();
    }
    version = next;
    const at = t => t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const issues = document.getElementById('issue-state'); // from the server: when issue fields were read
    live.textContent = 'live · ' + at(new Date()) +
      (Number(synced) ? ' · PRs from GitHub ' + at(new Date(synced * 1000)) : '') +
      (issues ? ' · ' + issues.textContent : '');
    live.className = 'on';
  } catch {
    stopped = true;
    live.textContent = 'viewer stopped — a tracked agent session or `tracker open` starts it';
    live.className = 'off';
  }
  live.title = live.textContent; // The toolbar truncates long status text to keep the controls visible.
}

// Refresh: pull PR state from GitHub now, and ask the next session prompt for the issue fields (only the model can
// read an issue tracker).
const refresh = document.getElementById('refresh');
async function askRefresh() {
  refresh.disabled = true;
  await post(slug, 'refresh');
  await poll();
  refresh.disabled = false;
}

// An action's Done or Drop: the action closes, and the next poll moves it to Reference.
async function closeAction(button) {
  button.parentElement.querySelectorAll('button').forEach(b => { b.disabled = true; });
  const refused = await post(slug, `actions/${encodeURIComponent(button.dataset.ref)}/${button.dataset.close}`);
  if (refused) alert(refused);
  await poll();
}

// A tracker change the page asks the server for: `refresh`, `archive`, `unarchive`, `delete`, or an action's
// `actions/<id>/done|drop`. The token proves the request comes from this page. Gives the server's refusal, or ''
// when done.
async function post(trackerSlug, what) {
  try {
    const res = await fetch(`/t/${encodeURIComponent(trackerSlug)}/${what}`,
      { method: 'POST', headers: { 'X-Tracker-Token': document.body.dataset.token } });
    return res.ok ? '' : (await res.text()) || `${what} failed (${res.status})`;
  } catch {
    return 'The viewer stopped: run `tracker open`, then try again.';
  }
}

// The tracker menu (top left): a tracker opens in this tab. Its ⋯ opens the row menu: Archive (Unarchive for an
// archived one) and Delete, after a confirm. The server refuses an archive or a delete while an agent session or a
// watch is on the tracker, so those items are off then. The archived trackers sit in a closed section at the bottom.
// The list is read at each open. One row menu serves every row, fixed beside its ⋯: the list scrolls, and would
// clip a menu inside it.
const switchOpen = document.getElementById('switch-open');
const switchList = document.getElementById('switch-list');
const switchError = switchList.querySelector('.err');
const archivedToggle = document.getElementById('switch-archived');
const archivedList = document.getElementById('switch-archived-list');
const rowMenu = document.getElementById('row-menu');
const confirmBox = document.getElementById('confirm');
const confirmError = confirmBox.querySelector('.err');
let doomed = null; // the tracker the dialog asks about
let menuFor = null; // the row menu's tracker, its ⋯ and whether it is archived

function trackerRow(t, archived) {
  const li = document.getElementById('switch-row').content.firstElementChild.cloneNode(true);
  const a = li.querySelector('a');
  a.href = `/t/${encodeURIComponent(t.slug)}/`;
  a.querySelector('b').textContent = t.title;
  a.querySelector('.meta').textContent = t.slug;
  if (t.slug === slug) a.setAttribute('aria-current', 'page');
  const busy = a.querySelector('small');
  busy.textContent = `in use by ${t.in_use}`;
  busy.hidden = !t.in_use;
  const more = li.querySelector('.more');
  more.setAttribute('aria-label', `Actions for ${t.title}`);
  more.title = 'Archive or delete';
  more.addEventListener('click', () => (menuFor?.more === more ? closeRowMenu() : openRowMenu(t, more, archived)));
  return li;
}

function openRowMenu(t, more, archived) {
  closeRowMenu();
  menuFor = { t, more, archived };
  rowMenu.querySelectorAll('[data-act]').forEach(b => {
    b.hidden = b.dataset.act === (archived ? 'archive' : 'unarchive');
    b.disabled = Boolean(t.in_use);
    b.title = t.in_use ? `In use by ${t.in_use}` : '';
  });
  more.setAttribute('aria-expanded', 'true');
  rowMenu.hidden = false;
  const r = more.getBoundingClientRect();
  rowMenu.style.top = `${r.bottom + 4}px`;
  rowMenu.style.left = `${Math.max(8, r.right - rowMenu.offsetWidth)}px`;
  rowMenu.querySelector('button:not([hidden]):not(:disabled)')?.focus();
}

function closeRowMenu(focus) {
  if (!menuFor) return;
  menuFor.more.setAttribute('aria-expanded', 'false');
  if (focus) menuFor.more.focus();
  rowMenu.hidden = true;
  menuFor = null;
}

function rowAction(act) {
  const { t } = menuFor;
  closeRowMenu();
  if (act === 'delete') askDelete(t); else change(t, act);
}

async function openSwitch() {
  let trackers;
  try {
    trackers = await (await fetch('/trackers', { cache: 'no-store' })).json();
  } catch {
    return; // the live line says the viewer stopped
  }
  switchList.querySelector('[data-list="active"]').replaceChildren(...trackers.active.map(t => trackerRow(t, false)));
  archivedList.replaceChildren(...trackers.archived.map(t => trackerRow(t, true)));
  archivedToggle.hidden = !trackers.archived.length;
  archivedToggle.querySelector('.n').textContent = trackers.archived.length;
  switchError.hidden = true;
  switchList.hidden = false;
  switchOpen.setAttribute('aria-expanded', 'true');
}

function closeSwitch(focus) {
  closeRowMenu();
  if (switchList.hidden) return;
  switchList.hidden = true;
  switchOpen.setAttribute('aria-expanded', 'false');
  if (focus) switchOpen.focus();
}

function toggleArchived() {
  const open = archivedList.hidden;
  archivedList.hidden = !open;
  archivedToggle.setAttribute('aria-expanded', open);
}

// Archive or bring back: this page shows the change at once; another tracker's row moves in the menu.
async function change(t, act) {
  const refused = await post(t.slug, act);
  if (refused) {
    switchError.textContent = refused;
    switchError.hidden = false;
  } else if (t.slug === slug) {
    location.reload();
  } else {
    openSwitch();
  }
}

function askDelete(t) {
  doomed = t;
  confirmBox.querySelector('[data-title]').textContent = t.title;
  confirmBox.querySelector('[data-root]').textContent = t.root;
  confirmError.hidden = true;
  closeSwitch(false);
  confirmBox.showModal();
}

async function deleteDoomed(button) {
  button.disabled = true;
  const refused = await post(doomed.slug, 'delete');
  button.disabled = false;
  if (!refused) {
    confirmBox.close();
    if (doomed.slug === slug) location.assign('/');
    return;
  }
  confirmError.textContent = refused;
  confirmError.hidden = false;
}

// An archived tracker gets no Refresh: it is never synced, and no session reads its issues.
function showRefresh() {
  refresh.hidden = Boolean(document.getElementById('archived'));
}

if (slug) {
  document.getElementById('switch').hidden = false;
  switchOpen.addEventListener('click', () => (switchList.hidden ? openSwitch() : closeSwitch(false)));
  archivedToggle.addEventListener('click', toggleArchived);
  rowMenu.querySelectorAll('[data-act]').forEach(b => b.addEventListener('click', () => rowAction(b.dataset.act)));
  switchList.addEventListener('scroll', () => closeRowMenu());
  confirmBox.querySelector('[data-cancel]').addEventListener('click', () => confirmBox.close());
  confirmBox.querySelector('[data-delete]').addEventListener('click', e => deleteDoomed(e.currentTarget));
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (menuFor) closeRowMenu(true); else closeSwitch(true);
  });
  showRefresh();
  refresh.addEventListener('click', askRefresh);
  document.addEventListener('click', e => {
    if (!e.target.closest('#switch')) closeSwitch(false);
    else if (!e.target.closest('#row-menu, .more')) closeRowMenu();
    if (e.target.closest('#archived [data-act="unarchive"]')) { // the archived page's banner
      post(slug, 'unarchive').then(refused => (refused ? alert(refused) : location.reload()));
    }
    const close = e.target.closest('button[data-close]');
    if (close) { e.preventDefault(); closeAction(close); } // in the action's head: the click does not open it
    const b = e.target.closest('.filters button');
    if (b) { filter = b.dataset.f; applyFilter(); }
    const h = e.target.closest('.seq-head button');
    if (h) setSort(h.dataset.sort);
    const a = e.target.closest('a[href^="#"]');
    if (a) { e.preventDefault(); openRecord(decodeURIComponent(a.getAttribute('href').slice(1))); } // no hash, no history
  });
  window.addEventListener('hashchange', openHash);
  applySort();
  applyFilter();
  openHash();
  poll();
  setInterval(poll, 3000);
}
