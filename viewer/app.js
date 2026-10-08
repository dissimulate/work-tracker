// work-tracker viewer: keeps the page in step with the tracker files.
// Every 3 s it asks the server for a version "<data>.<code>.<synced>". A data change swaps <main> in place, keeping
// open tickets and the active filter; a code change (this file, style.css, page.html, the Python) reloads the page;
// synced is when the server last pulled PR state from GitHub.

const slug = document.body.dataset.slug;
const live = document.getElementById('live');
let version = document.body.dataset.version;
let filter = 'all';

// The sequence's sort: a column and a direction, kept in `?sort=` (`-` first for descending) so a reload or a shared
// link keeps it. `step` is the dependency order the server sends, and the default.
// Each column's value per row: a number, or text compared with numbers in it as numbers ("T-2" before "T-10").
const SORTS = {
  step: d => Number(d.dataset.o),
  ticket: d => d.dataset.id,
  priority: d => d.dataset.p === '' ? '' : Number(d.dataset.p),
  wait: d => d.dataset.sw === '' ? '' : Number(d.dataset.sw),
  cycle: d => d.dataset.sc === '' ? '' : Number(d.dataset.sc),
  group: d => d.dataset.g,
  status: d => Number(d.dataset.r),
  waits: d => Number(d.dataset.w),
  unblocks: d => Number(d.dataset.u),
};
const LABELS = { step: 'dependency order', ticket: 'ticket', group: 'group', status: 'status', priority: 'priority',
  wait: 'wait time', cycle: 'cycle time',
  waits: 'waits on', unblocks: 'unblocks' };
let sort = readSort();

function readSort() {
  const raw = new URLSearchParams(location.search).get('sort') || '';
  const key = raw.replace(/^-/, '');
  // Own keys only: `?sort=constructor` would find Object's and break the page.
  return Object.hasOwn(SORTS, key) && key !== 'step' ? { key, desc: raw.startsWith('-') }
    : { key: 'step', desc: false };
}

// Rows with no value (no group) go last in either direction; equal values keep the dependency order.
function compare(a, b) {
  const get = SORTS[sort.key], x = get(a), y = get(b);
  if ((x === '') !== (y === '')) return x === '' ? 1 : -1;
  const by = typeof x === 'number' ? x - y : x.localeCompare(y, undefined, { numeric: true, sensitivity: 'base' });
  return (sort.desc ? -by : by) || SORTS.step(a) - SORTS.step(b);
}

function applySort() {
  const seq = document.querySelector('.seq');
  if (!seq) return;
  [...seq.querySelectorAll(':scope > details.t')].sort(compare).forEach(d => seq.append(d));
  seq.querySelectorAll('.seq-head button').forEach(b => {
    const on = b.dataset.sort === sort.key && sort.key !== 'step';
    if (on) b.parentElement.dataset.dir = sort.desc ? 'desc' : 'asc'; else delete b.parentElement.dataset.dir;
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
    : `Sorted by ${LABELS[sort.key]}, ${sort.desc ? 'descending' : 'ascending'}`;
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

async function poll() {
  try {
    const next = await (await fetch(`/t/${slug}/version`, { cache: 'no-store' })).text();
    const [data, code, synced] = next.split('.');
    const [oldData, oldCode] = version.split('.');
    if (code !== oldCode) return location.reload();
    if (data !== oldData) {
      // Keep each part open or closed as it was, also a section that starts open; new parts take their default.
      const was = new Map([...document.querySelectorAll('details[data-id]')].map(d => [d.dataset.id, d.open]));
      document.querySelector('main').innerHTML =
        await (await fetch(`/t/${slug}/main`, { cache: 'no-store' })).text();
      was.forEach((open, id) => { const d = detailsFor(id); if (d) d.open = open; });
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
    live.textContent = 'viewer stopped — run `tracker open`';
    live.className = 'off';
  }
}

// Refresh: pull PR state from GitHub now, and ask the next session prompt for the issue fields (only the model can
// read an issue tracker). The token proves the request comes from this page.
const refresh = document.getElementById('refresh');
async function askRefresh() {
  refresh.disabled = true;
  try {
    await fetch(`/t/${slug}/refresh`, { method: 'POST', headers: { 'X-Tracker-Token': document.body.dataset.token } });
    await poll();
  } finally {
    refresh.disabled = false;
  }
}

if (slug) {
  refresh.hidden = false;
  refresh.addEventListener('click', askRefresh);
  document.addEventListener('click', e => {
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
