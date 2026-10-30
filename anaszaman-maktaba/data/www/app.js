'use strict';
// Maktaba: a minimal front end for kiwix-serve. Talks only to same-origin kiwix endpoints
// (/catalog, /suggest, /search, /content, /random) proxied by nginx. No network calls elsewhere.

const $ = (sel, root = document) => root.querySelector(sel);

// Friendly names for the books we know; anything else falls back to the catalog's own title.
const KNOWN = {
  wikipedia_en_all:      { label: 'Wikipedia',     sub: 'Encyclopedia',          order: 1, css: '/reader.css' },
  wikipedia_en_medicine: { label: 'WikiMed',       sub: 'Health and medicine',   order: 2, css: '/reader.css' },
  ifixit_en_all:         { label: 'iFixit',        sub: 'Repair guides',         order: 3 },
  khanacademy_en_all:    { label: 'Khan Academy',  sub: 'Lessons and practice',  order: 4, css: '/khan.css', tweak: khanTweaks },
  wikipedia_ur_all:      { label: 'ویکیپیڈیا',     sub: 'Urdu encyclopedia',     order: 5, css: '/reader.css' },
};
// Khan Academy pages: shorten the long root name, and mark the licence line so it can be quiet.
function khanTweaks(doc) {
  const long = /^Khan Academy \(.*\)$/;
  doc.querySelectorAll('.breadcrumb-item a, h1').forEach(el => { if (long.test(el.textContent.trim())) el.textContent = 'Khan Academy'; });
  doc.querySelectorAll('.container > p').forEach(p => { if (/^License:/.test(p.textContent.trim())) p.classList.add('mk-license'); });
  doc.querySelectorAll('.children .card').forEach(card => { if (card.querySelector('img')) card.classList.add('mk-media'); });
}

const RTL_LANGS = new Set(['urd', 'ara', 'fas', 'pus', 'snd']);
const SIZES = [0.85, 1, 1.12, 1.25, 1.4];

let books = [];
const byCid = new Map();
const store = {
  get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
};

const fmtCount = n => new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(n);
const isRtlText = s => /[֐-ࣿיִ-﷿ﹰ-﻿]/.test(s);
const darkMQ = matchMedia('(prefers-color-scheme: dark)');

// Paths arrive in several encodings (suggest API, hash, iframe location). Normalise to one form.
function encPath(path) {
  let raw = path;
  try { raw = decodeURIComponent(path); } catch { /* already raw */ }
  return raw.split('/').map(encodeURIComponent).join('/');
}
const readHref = (cid, path = '') => `#/read/${cid}/${encPath(path)}`;

async function fetchXML(url, signal) {
  const res = await fetch(url, { signal });
  const text = await res.text();
  const doc = new DOMParser().parseFromString(text, 'application/xml');
  if (!res.ok || doc.querySelector('parsererror')) {
    const detail = [...doc.getElementsByTagName('detail')].map(d => d.textContent).join(' ');
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return doc;
}

// ---------- Catalog ----------
async function loadCatalog() {
  const xml = await fetchXML('/catalog/v2/entries?count=200');
  books = [...xml.getElementsByTagName('entry')].map(entry => {
    const text = tag => entry.getElementsByTagName(tag)[0]?.textContent?.trim() || '';
    const links = [...entry.getElementsByTagName('link')];
    const html = links.find(l => l.getAttribute('type') === 'text/html');
    const thumb = links.find(l => (l.getAttribute('rel') || '').includes('thumbnail'));
    const cid = (html?.getAttribute('href') || '').replace(/^\/content\//, '').replace(/\/$/, '');
    const name = text('name');
    const known = KNOWN[name] || {};
    const lang = text('language').split(',')[0];
    const tags = text('tags');
    return {
      cid, name, lang,
      title: text('title'),
      label: known.label || text('title'),
      sub: known.sub || text('summary'),
      order: known.order || 99,
      css: known.css || null,
      tweak: known.tweak || null,
      rtl: RTL_LANGS.has(lang),
      ftindex: !/_ftindex:no/.test(tags),
      articles: Number(text('articleCount')) || 0,
      icon: thumb?.getAttribute('href') || '',
    };
  }).filter(b => b.cid).sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));
  books.forEach(b => byCid.set(b.cid, b));
}

// ---------- Suggestions ----------
async function suggestAll(q, signal, perBook = 4) {
  const rtl = isRtlText(q);
  const pool = books.filter(b => b.rtl === rtl);
  const groups = await Promise.all(pool.map(async book => {
    try {
      const res = await fetch(`/suggest?content=${encodeURIComponent(book.cid)}&term=${encodeURIComponent(q)}&count=${perBook + 1}`, { signal });
      const list = await res.json();
      const items = list.filter(s => s.kind === 'path' && s.path).slice(0, perBook);
      return items.length ? { book, items } : null;
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      return null;
    }
  }));
  return groups.filter(Boolean);
}

// Highlight the query inside a plain-text title without using innerHTML.
function highlighted(text, q) {
  const frag = document.createDocumentFragment();
  const i = text.toLowerCase().indexOf(q.toLowerCase());
  if (!q || i < 0) { frag.append(text); return frag; }
  const b = document.createElement('b');
  b.textContent = text.slice(i, i + q.length);
  frag.append(text.slice(0, i), b, text.slice(i + q.length));
  return frag;
}

function attachSuggest(form, input, box) {
  let timer, ctrl, options = [], active = -1;

  const close = () => { box.hidden = true; input.setAttribute('aria-expanded', 'false'); active = -1; };
  const setActive = i => {
    options.forEach((o, n) => o.setAttribute('aria-selected', String(n === i)));
    active = i;
    if (i >= 0) { options[i].scrollIntoView({ block: 'nearest' }); input.setAttribute('aria-activedescendant', options[i].id); }
    else input.removeAttribute('aria-activedescendant');
  };

  async function run() {
    const q = input.value.trim();
    ctrl?.abort();
    if (q.length < 2) { close(); return; }
    ctrl = new AbortController();
    let groups;
    try { groups = await suggestAll(q, ctrl.signal); } catch { return; }
    box.replaceChildren();
    options = [];
    groups.forEach(({ book, items }) => {
      const g = document.createElement('div');
      g.className = 'suggest-group' + (book.rtl ? ' rtl' : '');
      g.setAttribute('role', 'group');
      const head = document.createElement('div');
      head.className = 'suggest-book';
      if (book.icon) { const img = document.createElement('img'); img.src = book.icon; img.alt = ''; head.append(img); }
      head.append(book.label);
      g.setAttribute('aria-label', book.label);
      g.append(head);
      items.forEach(s => {
        const a = document.createElement('a');
        a.className = 'suggest-item';
        a.id = `opt-${box.id}-${options.length}`;
        a.setAttribute('role', 'option');
        a.href = readHref(book.cid, s.path);
        if (book.rtl) a.dir = 'rtl';
        a.append(highlighted(s.value, q));
        g.append(a);
        options.push(a);
      });
      box.append(g);
    });
    const all = document.createElement('a');
    all.className = 'suggest-item suggest-all';
    all.id = `opt-${box.id}-${options.length}`;
    all.setAttribute('role', 'option');
    all.href = `#/search/${encodeURIComponent(q)}`;
    all.textContent = `Search all articles for “${q}”`;
    const g = document.createElement('div');
    g.className = 'suggest-group';
    g.append(all);
    box.append(g);
    options.push(all);
    box.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    setActive(-1);
  }

  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 120); });
  input.addEventListener('focus', () => { if (input.value.trim().length >= 2 && box.hidden) run(); });
  input.addEventListener('keydown', e => {
    if (box.hidden) return;
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(Math.min(active + 1, options.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(Math.max(active - 1, -1)); }
    else if (e.key === 'Escape') { close(); }
    else if (e.key === 'Enter' && active >= 0) { e.preventDefault(); location.hash = options[active].getAttribute('href'); close(); input.blur(); }
  });
  box.addEventListener('mousedown', e => e.preventDefault()); // keep focus so clicks register
  box.addEventListener('click', e => { if (e.target.closest('a')) { close(); input.blur(); } });
  input.addEventListener('blur', () => setTimeout(close, 100));
  form.addEventListener('submit', e => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    close();
    input.blur();
    location.hash = `#/search/${encodeURIComponent(q)}`;
  });
}

// ---------- Home ----------
function bookRow(book) {
  const li = $('#tpl-book').content.firstElementChild.cloneNode(true);
  const a = $('.row', li);
  a.href = readHref(book.cid, '');
  if (book.rtl) a.classList.add('rtl');
  const img = $('.row-icon', li);
  if (book.icon) img.src = book.icon; else img.remove();
  $('.row-title', li).textContent = book.label;
  if (book.rtl) $('.row-title', li).lang = 'ur';
  $('.row-sub', li).textContent = book.sub;
  $('.row-meta', li).textContent = book.articles ? `${fmtCount(book.articles)} articles` : '';
  return li;
}

function pageRow(entry, { removable } = {}) {
  const li = document.createElement('li');
  const a = document.createElement('a');
  a.className = 'row';
  a.href = readHref(entry.cid, entry.path);
  const text = document.createElement('span');
  text.className = 'row-text';
  const title = document.createElement('span');
  title.className = 'row-title';
  title.textContent = entry.title;
  if (isRtlText(entry.title)) title.dir = 'rtl';
  const sub = document.createElement('span');
  sub.className = 'row-sub';
  sub.textContent = byCid.get(entry.cid)?.label || '';
  text.append(title, sub);
  const meta = document.createElement('span');
  meta.className = 'row-meta';
  const chev = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  chev.setAttribute('viewBox', '0 0 20 20');
  chev.setAttribute('class', 'chev');
  chev.innerHTML = '<path d="m7.5 4 6 6-6 6"/>';
  a.append(text, meta, chev);
  li.append(a);
  if (removable) li.dataset.key = entry.cid + '/' + entry.path;
  return li;
}

function renderHome() {
  const shelf = $('#shelf');
  shelf.replaceChildren(...books.map(bookRow));
  const total = books.reduce((n, b) => n + b.articles, 0);
  if (total) {
    const words = new Intl.NumberFormat('en', { notation: 'compact', compactDisplay: 'long', maximumFractionDigits: 1 }).format(total);
    $('#home-q').placeholder = `Search ${words} articles`;
  }
  const recent = store.get('mk.recent', []).filter(r => byCid.has(r.cid)).slice(0, 5);
  $('#recent').replaceChildren(...recent.map(r => pageRow(r)));
  $('#recent-group').hidden = !recent.length;
  const saved = store.get('mk.saved', []).filter(r => byCid.has(r.cid));
  $('#saved').replaceChildren(...saved.map(r => pageRow(r)));
  $('#saved-group').hidden = !saved.length;
  // With nothing in the side column, centre the library under the search box.
  $('.home-grid').classList.toggle('solo', !recent.length && !saved.length);
}

// ---------- Search results ----------
let searchState = null;

function snippetFrom(node) {
  // kiwix returns <b>…</b> inside <description>; rebuild with text nodes only.
  const p = document.createElement('p');
  p.className = 'result-snippet';
  node?.childNodes.forEach(n => {
    if (n.nodeType === Node.TEXT_NODE) p.append(n.textContent);
    else if (n.nodeName.toLowerCase() === 'b') { const b = document.createElement('b'); b.textContent = n.textContent; p.append(b); }
    else p.append(n.textContent || '');
  });
  return p;
}

async function runSearch(append = false) {
  const s = searchState;
  const params = new URLSearchParams({ pattern: s.q, format: 'xml', pageLength: '20', start: String(s.start) });
  if (s.cid) params.set('books.name', s.cid);
  else params.set('books.filter.lang', s.rtl ? 'urd' : 'eng');
  const status = $('#results-status');
  const list = $('#results');
  if (!append) list.replaceChildren();
  status.textContent = 'Searching…';
  $('#more').hidden = true;
  let xml;
  try {
    xml = await fetchXML(`/search?${params}`);
  } catch (err) {
    status.textContent = s.cid && !byCid.get(s.cid)?.ftindex
      ? 'This book only supports title search. Use the matching titles above.'
      : `Search didn't work: ${err.message}`;
    return;
  }
  if (searchState !== s) return; // a newer search started
  const total = Number((xml.getElementsByTagName('opensearch:totalResults')[0]?.textContent || '0').replace(/[^\d]/g, ''));
  const items = [...xml.getElementsByTagName('item')];
  items.forEach(item => {
    const link = item.getElementsByTagName('link')[0]?.textContent || '';
    const m = link.match(/^\/content\/([^/]+)\/(.*)$/);
    if (!m) return;
    const [, cid, path] = m;
    const book = byCid.get(cid);
    const li = document.createElement('li');
    li.className = 'result' + (book?.rtl ? ' rtl' : '');
    const a = document.createElement('a');
    a.href = readHref(cid, path);
    const h = document.createElement('h3');
    h.className = 'result-title';
    h.textContent = item.getElementsByTagName('title')[0]?.textContent || path;
    const where = document.createElement('span');
    where.className = 'result-book';
    where.textContent = book?.label || item.getElementsByTagName('book')[0]?.textContent.trim() || '';
    a.append(h, where, snippetFrom(item.getElementsByTagName('description')[0]));
    li.append(a);
    list.append(li);
  });
  s.start += items.length;
  if (!list.children.length) {
    // Kiwix full-text search matches whole words only (no prefixes or wildcards).
    status.textContent = `No article text contains “${s.q}” as a whole word. Try complete words, or pick a matching title above.`;
  } else {
    status.textContent = total > s.start ? `Showing ${s.start.toLocaleString()} of about ${total.toLocaleString()}` : `${s.start.toLocaleString()} articles`;
  }
  $('#more').hidden = !(items.length && total > s.start);
}

async function renderResults(q, cid) {
  const input = $('#bar-q');
  input.value = q;
  document.title = q ? `${q} – Maktaba` : 'Search – Maktaba';
  const rtl = isRtlText(q);

  const filters = $('#filters');
  const pool = books.filter(b => b.rtl === rtl && b.ftindex);
  const mk = (label, value) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = label;
    b.setAttribute('aria-pressed', String((value || '') === (cid || '')));
    b.addEventListener('click', () => { location.hash = `#/search/${encodeURIComponent(q)}${value ? `?book=${encodeURIComponent(value)}` : ''}`; });
    return b;
  };
  filters.replaceChildren(mk('All books', ''), ...pool.map(b => mk(b.label, b.cid)));
  filters.hidden = !q || pool.length < 2;

  if (!q) {
    $('#results').replaceChildren();
    $('#titles-group').hidden = true;
    $('#results-status').textContent = 'Type what you’re looking for.';
    $('#more').hidden = true;
    input.focus();
    return;
  }

  searchState = { q, cid, rtl, start: 0 };
  const s = searchState;
  runSearch(false);

  // Title matches from every book (covers books without a full-text index, like iFixit).
  try {
    const groups = await suggestAll(q, undefined, 3);
    if (searchState !== s) return;
    const rows = groups.filter(g => !cid || g.book.cid === cid)
      .flatMap(({ book, items }) => items.map(it => pageRow({ cid: book.cid, path: it.path, title: it.value })))
      .slice(0, 8);
    $('#titles').replaceChildren(...rows);
    $('#titles-group').hidden = !rows.length;
  } catch { $('#titles-group').hidden = true; }
}

// ---------- Reader ----------
const frame = $('#frame');
let current = null;
let suppressRoute = false;
let openingHome = null; // cid whose front page was requested, so it is named after the book

function readerURL(cid, path) {
  if (path === '__random') return `/random?content=${encodeURIComponent(cid)}`;
  return `/content/${cid}/${encPath(path)}`;
}

// Hide the article between navigations and reveal it only once our styles apply,
// so Wikipedia's own layout never flashes before the reading layout.
function hideFrame() {
  frame.classList.add('pending');
  $('#reader-loading').classList.add('on');
  watchFrame();
}
function revealFrame() {
  frame.classList.remove('pending');
}

// Poll for the iframe's next document and enhance it at DOMContentLoaded, not at load
// (load waits for every image on the page).
let watchTimer = null;
let readyDoc = null;
function watchFrame() {
  clearInterval(watchTimer);
  const started = Date.now();
  watchTimer = setInterval(() => {
    let doc;
    try { doc = frame.contentDocument; } catch { doc = null; }
    if (doc && doc !== readyDoc && doc.location.href !== 'about:blank' && doc.readyState !== 'loading') {
      clearInterval(watchTimer);
      onFrameReady(doc);
    } else if (Date.now() - started > 15000) {
      clearInterval(watchTimer);
      revealFrame();
    }
  }, 25);
}

function openInFrame(url) {
  hideFrame();
  // replace() keeps the iframe from adding its own history entry on our navigations
  try {
    if (frame.contentWindow && frame.contentWindow.location.href !== 'about:blank') { frame.contentWindow.location.replace(url); return; }
  } catch { /* not ready yet */ }
  frame.src = url;
}

function renderReader(cid, path) {
  const book = byCid.get(cid);
  $('#reader-book').textContent = book?.label || '';
  openingHome = path === '' ? cid : null;
  const want = readerURL(cid, path);
  let have = '';
  try { have = frame.contentWindow.location.pathname; } catch { /* ignore */ }
  if (have !== want) openInFrame(want);
}

function applyScale(doc) {
  const scale = SIZES[store.get('mk.size', 1)] || 1;
  doc.documentElement.style.setProperty('--mk-scale', scale);
  if (!doc.getElementById('mk-reader-css')) doc.documentElement.style.zoom = scale;
  $('#size-label').textContent = `${Math.round(scale * 100)}%`;
}

// Returns once the reading stylesheet (if any) has loaded, so the caller can reveal the page.
function enhance(doc, book) {
  setFrameTheme(doc);
  doc.querySelectorAll('a[href^="http"]').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });
  try { book?.tweak?.(doc); } catch { /* a book's own markup changed; its stylesheet still applies */ }
  if (!book?.css || doc.getElementById('mk-reader-css')) { applyScale(doc); return Promise.resolve(); }
  return new Promise(resolve => {
    const link = doc.createElement('link');
    link.id = 'mk-reader-css';
    link.rel = 'stylesheet';
    link.href = book.css;
    link.onload = link.onerror = () => resolve();
    doc.head.append(link);
    applyScale(doc);
    setTimeout(resolve, 3000); // never leave the page hidden
  });
}

function buildToc(doc) {
  const toc = $('#toc');
  const heads = [...doc.querySelectorAll('h2, h3')].filter(h => h.textContent.trim() && !h.closest('nav, footer, .navbox'));
  toc.replaceChildren();
  if (!heads.length) {
    const p = document.createElement('li');
    p.className = 'toc-empty';
    p.textContent = 'This page has no sections.';
    toc.append(p);
    return;
  }
  heads.forEach((h, i) => {
    if (!h.id) h.id = `mk-h-${i}`;
    const li = document.createElement('li');
    li.className = h.tagName.toLowerCase();
    const a = document.createElement('a');
    a.href = '#';
    a.textContent = h.textContent.replace(/\[edit\]/g, '').trim();
    if (isRtlText(a.textContent)) a.dir = 'rtl';
    a.addEventListener('click', e => {
      e.preventDefault();
      h.closest('details')?.setAttribute('open', '');
      h.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
      if (innerWidth <= 520) toggleSheet(false);
    });
    li.append(a);
    toc.append(li);
  });
}

function addRecent(entry) {
  const list = store.get('mk.recent', []).filter(r => !(r.cid === entry.cid && r.path === entry.path));
  list.unshift({ ...entry, t: Date.now() });
  store.set('mk.recent', list.slice(0, 30));
}

function isSaved(entry) { return store.get('mk.saved', []).some(r => r.cid === entry.cid && r.path === entry.path); }
function updateSaveButton() {
  const on = current ? isSaved(current) : false;
  $('#save').setAttribute('aria-pressed', String(on));
  $('#save').setAttribute('aria-label', on ? 'Remove from saved' : 'Save');
}

function onFrameReady(doc) {
  readyDoc = doc;
  // Any navigation inside the article (link clicks, back/forward) hides it again until enhanced.
  doc.defaultView?.addEventListener('pagehide', hideFrame);
  const loc = doc.location;
  const m = loc.pathname.match(/^\/content\/([^/]+)\/(.*)$/);
  if (!m) { revealFrame(); return; }
  const [, cid, rawPath] = m;
  const path = encPath(rawPath);
  const book = byCid.get(cid);
  const hash = `#/read/${cid}/${path}`;
  if (location.hash !== hash) { suppressRoute = true; history.replaceState(null, '', hash); suppressRoute = false; }
  const isHome = openingHome === cid;
  openingHome = null;
  const title = (isHome && book ? book.label : (doc.title || decodeURIComponent(rawPath).replace(/_/g, ' ') || book?.label || '')).trim()
    .replace(/^Khan Academy \(.*\)$/, 'Khan Academy');
  current = { cid, path, title };
  $('#reader-title').textContent = title;
  $('#reader-title').dir = isRtlText(title) ? 'rtl' : 'auto';
  $('#reader-book').textContent = book?.label || '';
  document.title = `${title} – Maktaba`;
  enhance(doc, book).then(() => {
    revealFrame();
    buildToc(doc);
  });
  addRecent(current);
  updateSaveButton();
}

frame.addEventListener('load', () => {
  $('#reader-loading').classList.remove('on');
  let doc;
  try { doc = frame.contentDocument; } catch { revealFrame(); return; }
  if (doc && doc !== readyDoc && doc.location.href !== 'about:blank') { clearInterval(watchTimer); onFrameReady(doc); }
});

// Theme: follows the device until the toggle is used, then the stored choice wins.
const isDark = () => { const t = store.get('mk.theme', null); return t ? t === 'dark' : darkMQ.matches; };

function applyTheme() {
  const t = store.get('mk.theme', null);
  const root = document.documentElement;
  if (t) root.dataset.theme = t; else delete root.dataset.theme;
  const dark = isDark();
  root.dataset.mkDark = dark ? '1' : '0';
  document.querySelectorAll('[data-theme-toggle]').forEach(b => b.setAttribute('aria-label', dark ? 'Switch to light mode' : 'Switch to dark mode'));
  try { setFrameTheme(frame.contentDocument); } catch { /* no article open */ }
}

// Our variables plus Vector 2022's own night-mode switch.
function setFrameTheme(doc) {
  if (!doc?.documentElement) return;
  const root = doc.documentElement;
  const dark = isDark();
  root.dataset.mkTheme = dark ? 'dark' : 'light';
  root.classList.remove('skin-theme-clientpref-os', 'skin-theme-clientpref-day', 'skin-theme-clientpref-night');
  root.classList.add(dark ? 'skin-theme-clientpref-night' : 'skin-theme-clientpref-day');
}

darkMQ.addEventListener('change', applyTheme);

function toggleSheet(open) {
  const sheet = $('#sheet');
  const show = open ?? sheet.hidden;
  sheet.hidden = !show;
  $('#toc-btn').setAttribute('aria-expanded', String(show));
  if (show) $('#sheet-close').focus();
}

// ---------- Router ----------
function show(view) {
  for (const v of ['home', 'results', 'reader']) $(`#view-${v}`).hidden = v !== view;
  if (view !== 'reader') toggleSheet(false);
}

function route() {
  if (suppressRoute) return;
  const h = location.hash.slice(1) || '/';
  let m;
  if ((m = h.match(/^\/read\/([^/]+)\/?(.*)$/))) {
    show('reader');
    renderReader(m[1], m[2]);
  } else if ((m = h.match(/^\/search\/?([^?]*)(?:\?book=(.*))?$/))) {
    show('results');
    let q = m[1];
    try { q = decodeURIComponent(q); } catch { /* keep raw */ }
    renderResults(q, m[2] ? decodeURIComponent(m[2]) : '');
    scrollTo(0, 0);
  } else {
    show('home');
    document.title = 'Maktaba';
    renderHome();
  }
}

// ---------- Wire up ----------
attachSuggest($('#home-search'), $('#home-q'), $('#home-suggest'));
attachSuggest($('#bar-search'), $('#bar-q'), $('#bar-suggest'));

$('#more').addEventListener('click', () => runSearch(true));
$('#back').addEventListener('click', () => history.length > 1 ? history.back() : (location.hash = '#/'));
$('#find').addEventListener('click', () => { location.hash = '#/search/'; });
$('#toc-btn').addEventListener('click', () => toggleSheet());
$('#sheet-close').addEventListener('click', () => toggleSheet(false));
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#sheet').hidden) toggleSheet(false); });
$('#save').addEventListener('click', () => {
  if (!current) return;
  let list = store.get('mk.saved', []);
  if (isSaved(current)) list = list.filter(r => !(r.cid === current.cid && r.path === current.path));
  else list.unshift({ ...current, t: Date.now() });
  store.set('mk.saved', list);
  updateSaveButton();
});
document.querySelectorAll('[data-size]').forEach(btn => btn.addEventListener('click', () => {
  const i = Math.max(0, Math.min(SIZES.length - 1, store.get('mk.size', 1) + Number(btn.dataset.size)));
  store.set('mk.size', i);
  try { applyScale(frame.contentDocument); } catch { /* ignore */ }
}));
$('#clear-recent').addEventListener('click', () => { store.set('mk.recent', []); renderHome(); });
$('#surprise').addEventListener('click', () => {
  const wiki = books.find(b => b.name === 'wikipedia_en_all') || books[0];
  if (wiki) location.hash = `#/read/${wiki.cid}/__random`;
});
document.querySelectorAll('[data-theme-toggle]').forEach(btn => btn.addEventListener('click', () => {
  store.set('mk.theme', isDark() ? 'light' : 'dark');
  applyTheme();
}));
applyTheme();
addEventListener('hashchange', route);

loadCatalog().then(route).catch(err => {
  show('home');
  const li = document.createElement('li');
  li.className = 'status';
  li.textContent = `Can't reach the library (${err.message}). Check that the Kiwix app is running in Umbrel.`;
  $('#shelf').replaceChildren(li);
});
