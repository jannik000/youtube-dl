// Dashboard of the youtube-dl web UI (vanilla JavaScript, no framework).
//
// Served by a route as /static/app.js so the CSP can stay script-src 'self'.
// Rules this file keeps (tests/test_frontend_static.py checks them):
// * Parameters come from data attributes on <body>, never from script
//   source rendered by the server.
// * Text from the server (titles, channel names, URLs, log lines, error
//   messages) reaches the page only through textContent and DOM properties.
//   No markup is ever parsed from strings; icons are cloned from the
//   <template id="icons"> in layout.html.
// * Styles are set only through the CSSOM (bar widths and heights).
// * Every POST carries the CSRF token; the browser adds Sec-Fetch-Site.
(function () {
  'use strict';

  var body = document.body;
  document.documentElement.classList.remove('no-js');

  var cfg = body.dataset;
  var BASE = cfg.basePath || '';
  var CSRF = cfg.csrfToken || '';
  var PAGE = cfg.page || 'dashboard';
  var JOB_ID = cfg.downloadId || '';
  var PARALLEL = parseInt(cfg.parallel, 10) || 1;
  var MAX_QUEUED = parseInt(cfg.maxQueued, 10) || 20;

  var FAST = 2000;
  var SLOW = 10000;
  var MAX_BACKOFF = 30000;
  var MAX_LOG_LINES = 5000;
  var UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  var VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;

  var KIND_LABELS = {
    bot_check: 'Bot check', rate_limit: 'Rate-limited', forbidden: 'Forbidden',
    unavailable: 'Unavailable', private_members: 'Private or members-only',
    geo: 'Blocked in this country', disk_full: 'Disk full',
    cookies_rotated: 'Cookies expired', other: 'Other error'
  };

  function $(id) { return document.getElementById(id); }

  // ---- DOM helpers --------------------------------------------------------

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function add(parent) {
    for (var i = 1; i < arguments.length; i++) {
      var kid = arguments[i];
      if (kid === null || kid === undefined || kid === false) { continue; }
      if (Array.isArray(kid)) {
        kid.forEach(function (k) { add(parent, k); });
      } else if (typeof kid === 'string' || typeof kid === 'number') {
        parent.appendChild(document.createTextNode(String(kid)));
      } else {
        parent.appendChild(kid);
      }
    }
    return parent;
  }

  function fill(parent) {
    while (parent.firstChild) { parent.removeChild(parent.firstChild); }
    var args = Array.prototype.slice.call(arguments, 1);
    add.apply(null, [parent].concat(args));
    return parent;
  }

  function bdi(text, cls) { return el('bdi', cls, text); }

  var iconStore = $('icons');
  function icon(name, cls) {
    if (!iconStore || !iconStore.content) { return null; }
    var src = iconStore.content.querySelector('[data-icon="' + name + '"]');
    if (!src) { return null; }
    var node = src.cloneNode(true);
    node.removeAttribute('data-icon');
    if (cls) { cls.split(' ').forEach(function (c) { node.classList.add(c); }); }
    return node;
  }

  function chip(text, tone, iconName, medium) {
    var node = el('span', 'chip' + (medium ? ' chip-m' : '') + ' tone-' + (tone || 'idle'));
    add(node, iconName ? icon(iconName, 'ic-s') : null, text);
    return node;
  }

  function link(text, href, cls, key) {
    var a = el('a', cls, text);
    a.href = href;
    if (key) { a.dataset.key = key; }   // keeps the focus across re-renders
    return a;
  }

  function button(text, cls, action, data) {
    var b = el('button', 'btn ' + (cls || ''), text);
    b.type = 'button';
    b.dataset.action = action;
    Object.keys(data || {}).forEach(function (k) { b.dataset[k] = data[k]; });
    b.dataset.key = action + ':' + Object.keys(data || {}).map(function (k) { return data[k]; }).join(':');
    return b;
  }

  // ---- Formatting -----------------------------------------------------------

  var skew = 0;   // server clock minus browser clock, seconds
  function nowS() { return Date.now() / 1000 + skew; }

  function makeFormat(options, fallback) {
    try { return new Intl.DateTimeFormat(undefined, options); } catch (e) {
      return new Intl.DateTimeFormat(undefined, fallback);
    }
  }
  var fmtHM = makeFormat({ hour: '2-digit', minute: '2-digit' }, {});
  var fmtClock = makeFormat({ hour: '2-digit', minute: '2-digit', second: '2-digit' }, {});
  var fmtDay = makeFormat({ month: 'short', day: 'numeric' }, {});
  var fmtFull = makeFormat({ dateStyle: 'full', timeStyle: 'medium' },
    { year: 'numeric', month: 'long', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' });
  var rtf = null;
  try { rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }); } catch (e) { rtf = null; }
  var nf1 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  var nf2 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
  var nf0 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

  function isNum(v) { return typeof v === 'number' && isFinite(v); }
  function date(ts) { return new Date(ts * 1000); }
  function hm(ts) { return isNum(ts) ? fmtHM.format(date(ts)) : '–'; }
  function full(ts) { return isNum(ts) ? fmtFull.format(date(ts)) : ''; }

  function capital(text) { return text ? text.charAt(0).toUpperCase() + text.slice(1) : text; }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() &&
      a.getDate() === b.getDate();
  }

  // "14:20", "Yesterday 23:20" or "Oct 6 08:20" in the browser's locale.
  function dayTime(ts) {
    if (!isNum(ts)) { return '–'; }
    var d = date(ts);
    var today = date(nowS());
    if (sameDay(d, today)) { return hm(ts); }
    var yesterday = new Date(today.getTime() - 86400000);
    if (sameDay(d, yesterday) && rtf) { return capital(rtf.format(-1, 'day')) + ' ' + hm(ts); }
    return fmtDay.format(d) + ' ' + hm(ts);
  }

  function ago(ts) {
    if (!isNum(ts)) { return '–'; }
    var diff = ts - nowS();
    var abs = Math.abs(diff);
    if (!rtf) { return dayTime(ts); }
    if (abs < 45) { return rtf.format(0, 'second'); }
    if (abs < 3600) { return rtf.format(Math.round(diff / 60), 'minute'); }
    if (abs < 86400) { return rtf.format(Math.round(diff / 3600), 'hour'); }
    return rtf.format(Math.round(diff / 86400), 'day');
  }

  function duration(secs) {
    if (!isNum(secs)) { return '–'; }
    secs = Math.max(0, Math.round(secs));
    if (secs < 60) { return secs + ' s'; }
    var m = Math.floor(secs / 60);
    if (m < 60) {
      var s = secs % 60;
      return m + ' min' + (m < 10 && s ? ' ' + (s < 10 ? '0' : '') + s + ' s' : '');
    }
    var h = Math.floor(m / 60);
    if (h < 48) { return h + ' h' + (m % 60 ? ' ' + (m % 60) + ' min' : ''); }
    return Math.round(h / 24) + ' days';
  }

  function clock(secs) {
    if (!isNum(secs)) { return ''; }
    secs = Math.max(0, Math.round(secs));
    var h = Math.floor(secs / 3600);
    var m = Math.floor((secs % 3600) / 60);
    var s = secs % 60;
    var mm = (h && m < 10 ? '0' : '') + m;
    return (h ? h + ':' : '') + mm + ':' + (s < 10 ? '0' : '') + s;
  }

  function bytes(n) {
    if (!isNum(n)) { return '–'; }
    var units = ['B', 'kB', 'MB', 'GB', 'TB', 'PB'];
    var i = 0;
    while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
    var fmt = i === 0 ? nf0 : (n >= 100 ? nf0 : (n >= 10 ? nf1 : nf2));
    return fmt.format(n) + ' ' + units[i];
  }

  function pct(p) { return isNum(p) ? nf0.format(Math.floor(p)) + ' %' : ''; }

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  function kindLabel(kind) { return KIND_LABELS[kind] || 'Error'; }

  function timeNode(ts, text, cls) {
    var node = el('span', cls, text);
    node.title = full(ts);
    return node;
  }

  // ---- Progress bars --------------------------------------------------------

  // A bar with a label row and a meta line. percent null means
  // indeterminate (striped).
  function bar(opts) {
    var wrap = el('div', 'bar-wrap');
    add(wrap, add(el('div', 'bar-label'), el('span', null, opts.label), el('span', null, opts.right || '')));
    var track = el('div', 'bar' + (opts.large ? ' bar-l' : '') + (isNum(opts.percent) ? '' : ' is-indeterminate'));
    track.setAttribute('role', 'progressbar');
    track.setAttribute('aria-label', opts.aria || opts.label);
    if (isNum(opts.percent)) {
      track.setAttribute('aria-valuemin', '0');
      track.setAttribute('aria-valuemax', '100');
      track.setAttribute('aria-valuenow', String(Math.round(opts.percent)));
    }
    track.setAttribute('aria-valuetext', opts.valuetext || opts.label);
    var fillNode = el('div', 'bar-fill');
    fillNode.style.width = (isNum(opts.percent) ? Math.max(0, Math.min(100, opts.percent)) : 0) + '%';
    add(wrap, add(track, fillNode));
    if (opts.meta) { add(wrap, el('div', 'bar-meta', opts.meta)); }
    return wrap;
  }

  // The current-video bar: phase label, the whole video's percent (merged
  // formats are weighted by the API) and the current stream's numbers.
  function videoBar(v, aria, large) {
    if (!v) {
      return bar({ label: 'Checking for new videos…', percent: null, aria: aria, large: large });
    }
    var label;
    var meta = [];
    var percent = v.percent;
    var s = v.stream || {};
    if (v.phase === 'processing') {
      label = 'Processing' + (v.postprocessor ? ': ' + v.postprocessor : '') + '…';
      percent = null;
      if (isNum(v.downloaded)) { meta.push('Downloaded ' + bytes(v.total || v.downloaded)); }
      meta.push('post-processing can take a moment');
    } else if (v.phase === 'preparing') {
      label = 'Preparing…';
      percent = null;
    } else {
      if (s.count > 1) {
        label = s.kind === 'audio' ? 'Audio stream' : (s.kind === 'video' ? 'Video stream' : 'Stream');
        label += ' ' + s.index + ' of ' + s.count;
      } else if (v.fragment_count) {
        label = 'Segment ' + (v.fragment_index || 0) + ' of ' + v.fragment_count;
      } else {
        label = 'Downloading';
      }
      if (!isNum(percent)) {
        label = 'Size unknown';
        if (isNum(v.downloaded)) { meta.push(bytes(v.downloaded) + ' so far'); }
      } else if (isNum(v.downloaded) && isNum(v.total)) {
        meta.push(bytes(v.downloaded) + ' of ' + (v.estimate ? '~' : '') + bytes(v.total));
      }
      if (isNum(v.speed)) { meta.push(bytes(v.speed) + '/s'); }
      if (isNum(v.eta)) { meta.push(clock(v.eta) + ' left'); }
      if (s.count > 1 && s.index < s.count) {
        meta.push(s.kind === 'video' ? 'then audio and merge' : 'then the next stream');
      }
    }
    var valuetext = isNum(percent)
      ? Math.round(percent) + ' percent' + (isNum(v.eta) ? ', ' + Math.round(v.eta) + ' seconds left' : '')
      : label;
    return bar({ label: label, right: pct(percent), percent: percent, meta: meta.join(' · '),
      aria: aria, valuetext: valuetext, large: large });
  }

  // ---- Toast, banners, announcements ----------------------------------------

  var toastTimer = null;
  function toast(text, tone, href, linkText) {
    var box = $('toast');
    if (!box) { return; }
    box.className = 'toast tone-' + (tone || 'ok');
    $('toast-text').textContent = text;
    var a = $('toast-link');
    if (href) {
      a.href = href;
      a.textContent = linkText || 'Details';
      a.hidden = false;
    } else {
      a.hidden = true;
    }
    box.hidden = false;
    clearTimeout(toastTimer);
    if (tone !== 'err') {
      toastTimer = setTimeout(function () { box.hidden = true; }, 8000);
    }
  }

  function announce(text) {
    var region = $('announce');
    if (region) { region.textContent = text; }
  }

  // ---- Requests -------------------------------------------------------------

  function post(path, fields) {
    var data = new URLSearchParams();
    Object.keys(fields || {}).forEach(function (k) {
      if (fields[k] !== undefined && fields[k] !== null) { data.set(k, fields[k]); }
    });
    data.set('csrf_token', CSRF);
    return fetch(BASE + path, {
      method: 'POST', body: data, credentials: 'same-origin', redirect: 'manual',
      headers: { Accept: 'application/json' }
    }).then(function (r) {
      if (r.type === 'opaqueredirect') { return { ok: true, status: 303, data: null }; }
      return r.json().catch(function () { return null; }).then(function (d) {
        return { ok: r.ok, status: r.status, data: d };
      });
    }).then(function (res) {
      if (res.status === 401) { session.expired = true; renderBanners(); }
      setTimeout(refreshNow, 0);
      return res;
    });
  }

  function failText(res) {
    var d = res.data || {};
    var text = d.message || d.detail;
    if (typeof text !== 'string') { text = ''; }
    if (res.status === 403 && /csrf|cross-origin/i.test(text)) {
      return 'The page is out of date (the web UI restarted). Reload and try again.';
    }
    if (res.status === 401) { return 'Session expired. Reload to sign in again.'; }
    if (res.status === 429) { return text || 'Queue is full (' + MAX_QUEUED + ' waiting). Try again later.'; }
    return text || 'Request failed (' + res.status + ').';
  }

  function networkFail() { toast('The server could not be reached. Try again.', 'err'); }

  function jobHref(id) { return BASE + '/download/' + id; }

  function ticketToast(res) {
    if (!res.ok) { toast(failText(res), 'err'); return null; }
    var t = res.data || {};
    var id = typeof t.id === 'string' && UUID_RE.test(t.id) ? t.id : null;
    var tone = t.state === 'already' ? 'idle' : 'ok';
    toast(typeof t.message === 'string' ? t.message : 'Queued.', tone,
      id && PAGE !== 'job' ? jobHref(id) : null, 'Details');
    return id;
  }

  // ---- Status polling -------------------------------------------------------

  var session = { expired: false, restarted: false, lost: false };
  var poller = { etag: null, timer: null, fails: 0, busy: false, inFlight: false };
  var status = null;
  var boot = null;

  function cadence(s) {
    if (!s) { return SLOW; }
    var sc = s.scheduler || {};
    var jobs = s.jobs || {};
    if (sc.state === 'running') { return FAST; }
    if ((jobs.running || []).length || (jobs.queued || []).length) { return FAST; }
    if (isNum(sc.next) && sc.next - nowS() < 15) { return FAST; }
    return SLOW;
  }

  function schedulePoll(ms) {
    clearTimeout(poller.timer);
    if (document.hidden || session.expired) { return; }
    poller.timer = setTimeout(poll, ms);
  }

  function refreshNow() {
    if (!poller.inFlight) { schedulePoll(0); }
  }

  function poll() {
    if (document.hidden || session.expired) { return; }
    poller.inFlight = true;
    var headers = { Accept: 'application/json' };
    if (poller.etag) { headers['If-None-Match'] = poller.etag; }
    fetch(BASE + '/api/status', { headers: headers, cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 304) { return 'same'; }
        if (r.status === 401) { return 'expired'; }
        if (!r.ok) { throw new Error('status ' + r.status); }
        poller.etag = r.headers.get('ETag');
        return r.json();
      })
      .then(function (data) {
        poller.inFlight = false;
        if (data === 'expired') {
          session.expired = true;
          renderBanners();
          return;
        }
        poller.fails = 0;
        session.lost = false;
        if (data !== 'same') { apply(data); }
        render();
        schedulePoll(cadence(status));
      })
      .catch(function () {
        poller.inFlight = false;
        poller.fails += 1;
        if (poller.fails >= 3) { session.lost = true; }
        renderBanners();
        schedulePoll(Math.min(MAX_BACKOFF, FAST * Math.pow(2, poller.fails)));
      });
  }

  function apply(data) {
    if (!data || typeof data !== 'object') { return; }
    if (isNum(data.now)) { skew = data.now - Date.now() / 1000; }
    if (boot && data.boot && data.boot !== boot) { session.restarted = true; }
    if (!boot) { boot = data.boot; }
    announceChanges(status, data);
    status = data;
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      clearTimeout(poller.timer);
      clearTimeout(logState.timer);
    } else {
      schedulePoll(0);
      scheduleLog(0);
    }
  });

  // Announce state changes only (not every progress update).
  function announceChanges(prev, next) {
    if (!prev || !next) { return; }
    var a = (prev.scheduler || {}).state;
    var b = (next.scheduler || {}).state;
    if (a !== 'running' && b === 'running') { announce('Scheduled run started.'); }
    if (a === 'running' && b !== 'running') { announce('Scheduled run finished.'); }
    var active = {};
    ((prev.jobs || {}).running || []).concat((prev.jobs || {}).queued || []).forEach(function (j) { active[j.id] = true; });
    // One announcement for all jobs that ended since the last update
    // (each announce() replaces the previous one), failures first.
    var failed = [];
    var other = [];
    ((next.jobs || {}).finished || []).forEach(function (j) {
      if (active[j.id]) {
        var text = 'Download ' + (STATE_TEXT[j.state] || 'ended').toLowerCase() + ': ' + (j.title || j.url || '') + '.';
        (j.state === 'failed' || j.state === 'partial' ? failed : other).push(text);
      }
    });
    if (failed.length || other.length) { announce(failed.concat(other).join(' ')); }
  }

  // ---- Rendering ------------------------------------------------------------

  var lastSig = {};
  var openState = {};   // open/closed <details> by data-key

  // Re-render a section only when its data (or the minute) changed, and
  // keep the focus on the same control.
  function section(name, node, data, draw) {
    if (!node) { return; }
    var sig = JSON.stringify(data) + '|' + Math.floor(nowS() / 60);
    if (lastSig[name] === sig) { return; }
    // Replacing the node under a finger or mouse between pointerdown and
    // pointerup swallows the click: wait until the press is over.
    if (pressed && node.contains(pressed)) { return; }
    lastSig[name] = sig;
    var focused = document.activeElement;
    var key = focused && node.contains(focused) && focused.dataset ? focused.dataset.key : null;
    draw(node, data);
    if (key) {
      var again = node.querySelector('[data-key="' + cssEscape(key) + '"]');
      if (again) { again.focus(); }
    }
  }

  var pressed = null;
  var pressGen = 0;
  document.addEventListener('pointerdown', function (e) { pressed = e.target; pressGen += 1; }, true);
  function released() {
    var gen = pressGen;
    // After the click that follows pointerup.
    setTimeout(function () {
      if (gen === pressGen && pressed) { pressed = null; render(); }
    }, 350);
  }
  document.addEventListener('pointerup', released, true);
  document.addEventListener('pointercancel', released, true);

  // Running jobs without their live progress: the parts of a section that
  // only change with it are patched by updateProgress() instead.
  function steadyJob(job) {
    if (!job) { return job; }
    var copy = {};
    Object.keys(job).forEach(function (k) {
      if (k !== 'video' && k !== 'item') { copy[k] = job[k]; }
    });
    var v = job.video;
    // Names change per video, not per progress update.
    copy.video = v ? { id: v.id, title: v.title, channel: v.channel, playlist: v.playlist } : null;
    return copy;
  }

  function steadyJobs(jobs) {
    jobs = jobs || {};
    return { running: (jobs.running || []).map(steadyJob), queued: jobs.queued, finished: jobs.finished };
  }

  // Fill the progress boxes of running jobs (bars and labels only, nothing
  // to click) when their progress changed.
  var progressSig = {};
  function updateProgress(root, jobs, draw) {
    if (!root) { return; }
    (jobs || []).forEach(function (job) {
      var box = root.querySelector('[data-progress="' + cssEscape(job.id) + '"]');
      if (!box) { return; }
      var sig = JSON.stringify([job.video, job.item]);
      if (box.dataset.sig === sig) { return; }
      box.dataset.sig = sig;
      fill(box, draw(job));
    });
  }

  function cssEscape(text) {
    return window.CSS && CSS.escape ? CSS.escape(text) : String(text).replace(/["\\]/g, '\\$&');
  }

  document.addEventListener('toggle', function (e) {
    var d = e.target;
    if (d && d.dataset && d.dataset.key) { openState[d.dataset.key] = d.open; }
  }, true);

  function render() {
    renderBanners();
    if (!status) { return; }
    var s = status;
    section('pill', $('pill'), [s.scheduler, s.current && s.current.channel, problemCount(s)], renderPill);
    section('cards', $('cards'), [s.scheduler, s.current, s.last_run, s.counts, s.pending, s.disk, s.cookies],
      renderCards);
    section('sched', $('sched'), [s.scheduler, s.current, s.last_run], renderSched);
    section('manual', $('manual'), [steadyJobs(s.jobs), s.scheduler.state], renderManual);
    updateProgress($('manual'), (s.jobs || {}).running, manualProgress);
    section('attention', $('attention'), s.problems, renderAttention);
    section('recent', $('recent-list'), s.recent, renderRecent);
    section('channels', $('channels-rows'), s.channels, renderChannels);
    section('runs', $('runs-rows'), s.runs, renderRuns);
    var job = findJob(s, JOB_ID);
    section('job', $('job'), [steadyJob(job), s.scheduler.state], renderJob);
    updateProgress($('job'), job ? [job] : [], jobProgress);
    section('log-sources', $('log-source'), logSourceList(s.jobs), renderLogSources);
    var version = $('footer-version');
    if (version) {
      version.textContent = 'youtube-dl' + (s.scheduler.ytdlp ? ' · yt-dlp ' + s.scheduler.ytdlp : '');
    }
    var updated = $('footer-updated');
    if (updated) {
      updated.textContent = 'Updated ' + fmtClock.format(new Date()) + ' · refreshes every ' +
        (cadence(s) === FAST ? '2 s while running' : '10 s while idle');
    }
  }

  function problemCount(s) {
    return (s.problems || []).filter(function (g) { return g.severity === 'error'; })
      .reduce(function (n, g) { return n + g.count; }, 0);
  }

  // Header status pill
  function renderPill(node) {
    var s = status;
    var sc = s.scheduler;
    var tone = 'idle';
    var ic = 'moon';
    var text;
    var problems = problemCount(s);
    if (sc.state === 'running') {
      var ch = s.current && s.current.channel;
      if (problems) {
        tone = 'err'; ic = 'alert'; text = 'Running · ' + plural(problems, 'problem', 'problems');
      } else {
        tone = 'run'; ic = 'dot';
        text = 'Running' + (ch ? ' · channel ' + ch.index + ' of ' + ch.total : '');
      }
    } else if (sc.state === 'sleeping') {
      text = isNum(sc.next) ? (sc.next <= nowS() ? 'Starting…' : 'Sleeping · next ' + hm(sc.next)) : 'Sleeping';
    } else if (sc.state === 'interrupted') {
      tone = 'warn'; ic = 'alert'; text = 'Interrupted';
    } else if (sc.state === 'stopped') {
      tone = 'warn'; ic = 'alert'; text = 'Downloader stopped';
    } else if (sc.state === 'oneshot') {
      text = 'Finished · one-shot mode';
    } else {
      text = 'Waiting for the first run';
    }
    if (problems && sc.state !== 'running') { tone = 'err'; ic = 'alert'; }
    node.className = 'pill needs-js tone-' + tone;
    fill(node, icon(ic, ic === 'dot' ? 'ic-dot' : 'ic-m'), el('span', null, text));
    node.title = text;
  }

  function renderBanners() {
    var node = $('banners');
    if (!node) { return; }
    var list = [];
    if (session.expired) {
      list.push({ tone: 'err', text: 'Session expired. Reload to sign in again.', reload: true });
    } else if (session.restarted) {
      list.push({ tone: 'run', text: 'The web UI was restarted.', reload: true });
    }
    if (session.lost && !session.expired) {
      list.push({ tone: 'warn', text: 'Connection lost, retrying…' });
    }
    var s = status;
    if (s) {
      var c = s.cookies || {};
      if (c.bot_checks_24h > 0 && PAGE === 'dashboard') {
        list.push({ tone: 'warn', text: 'YouTube bot check seen ' + c.bot_checks_24h +
          '× in the last 24 h. Downloads may fail until /config/cookies.txt is refreshed.', href: '#attention', link: 'Show problems' });
      }
      if (s.disk && s.disk.low) {
        list.push({ tone: 'warn', text: 'Disk space is low: ' + bytes(s.disk.free) + ' free under /downloads.' });
      }
      if (s.scheduler.oneshot && s.scheduler.state === 'running') {
        list.push({ tone: 'run', text: 'One-shot mode: the container exits after this run.' });
      }
      if (s.scheduler.state === 'nodata' && PAGE === 'dashboard') {
        list.push({ tone: 'run', text: 'Status appears after the first run with the dashboard enabled. ' +
          'Counters start now; earlier downloads are not counted.' });
      }
      if (s.catching_up) {
        list.push({ tone: 'idle', text: 'Reading the history; numbers may still change.' });
      }
    }
    var sig = JSON.stringify(list);
    if (lastSig.banners === sig) { return; }
    lastSig.banners = sig;
    fill(node, list.map(function (b) {
      var row = el('div', 'banner tone-' + b.tone);
      add(row, icon(b.tone === 'run' || b.tone === 'idle' ? 'info' : 'alert', 'ic-l'), el('span', 'banner-text', b.text));
      if (b.reload) {
        var r = el('button', 'linkish', 'Reload');
        r.type = 'button';
        r.dataset.action = 'reload';
        add(row, r);
      }
      if (b.href) { add(row, link(b.link, b.href)); }
      return row;
    }));
  }

  // Status cards
  function card(o) {
    var a = el('a', 'card' + (o.border ? ' is-' + o.border : ''));
    a.href = o.href || '#now';
    var head = add(el('div', 'card-head'), el('span', 'section-label', o.label));
    if (o.tag) {
      var tag = el('span', 'tag', o.tag);
      if (o.tagTitle) { tag.title = o.tagTitle; }
      add(head, tag);
    }
    var valueRow = el('div', 'card-value-row');
    if (o.dot) { add(valueRow, add(el('span', 'fg-' + o.dot), icon('dot', 'ic-dot'))); }
    var value = el('span', 'card-value' + (o.valueTone ? ' fg-' + o.valueTone : '') + (o.valueMuted ? ' is-muted' : ''), o.value);
    if (o.valueTitle) { value.title = o.valueTitle; }
    add(valueRow, value);
    add(a, head, valueRow, el('span', 'card-sub' + (o.subTone ? ' fg-' + o.subTone : ''), o.sub || ''));
    if (isNum(o.bar)) {
      var f = el('div', 'card-bar-fill' + (o.barWarn ? ' is-warn' : ''));
      f.style.width = Math.max(0, Math.min(100, o.bar)) + '%';
      add(a, add(el('div', 'card-bar'), f));
    }
    if (o.mini) {
      var mini = el('div', 'mini');
      var max = Math.max.apply(null, o.mini.concat([1]));
      o.mini.forEach(function (v, i) {
        var b = el('div', 'mini-bar' + (i === o.mini.length - 1 ? ' is-today' : ''));
        b.style.height = Math.round(4 + (v / max) * 18) + 'px';
        b.title = plural(v, 'video', 'videos');
        add(mini, b);
      });
      add(a, mini);
    }
    return a;
  }

  function schedulerCard(s) {
    var sc = s.scheduler;
    var every = sc.interval && sc.interval !== 'false' ? ' · every ' + sc.interval : '';
    if (sc.state === 'running') {
      return { label: 'Scheduler', value: 'Running', dot: 'run',
        sub: 'since ' + hm(sc.since) + ' · ' + duration(nowS() - sc.since) + every };
    }
    if (sc.state === 'sleeping') {
      var sub = 'no next run known';
      if (isNum(sc.next)) {
        sub = sc.next <= nowS() ? 'starting…' : 'next run ' + hm(sc.next) + ' · in ' + duration(sc.next - nowS());
      }
      return { label: 'Scheduler', value: 'Sleeping', dot: 'idle', sub: sub };
    }
    if (sc.state === 'interrupted') {
      return { label: 'Scheduler', value: 'Interrupted', dot: 'warn', border: 'warn',
        sub: 'the run from ' + hm(s.current && s.current.started) + ' was stopped' };
    }
    if (sc.state === 'stopped') {
      return { label: 'Scheduler', value: 'Stopped', dot: 'warn', border: 'warn',
        sub: 'the downloader is not running' };
    }
    if (sc.state === 'oneshot') {
      return { label: 'Scheduler', value: 'Finished', dot: 'idle', sub: 'one-shot mode: runs once' };
    }
    return { label: 'Scheduler', value: '–', sub: 'waiting for the first run' };
  }

  function lastRunCard(s) {
    var r = s.last_run;
    if (!r) { return { label: 'Last run', value: '–', sub: 'no run recorded yet', href: '#runs' }; }
    var sum = r.sum || {};
    var sub = duration(r.duration);
    var o = { label: 'Last run', value: dayTime(r.started), valueTitle: full(r.started), href: '#runs' };
    if (r.result === 'ok') {
      sub += ' · ✓ OK · ' + (sum.new || 0) + ' new';
    } else {
      sub += ' · ✗ ' + (sum.failed || 0) + ' failed · exit code ' + r.rc;
      o.subTone = 'err';
    }
    o.sub = sub;
    return o;
  }

  function runCountsCard(s) {
    var c;
    var o = { label: 'This run', tag: 'from log',
      tagTitle: 'Skipped and filtered counts come from the log.', href: '#now' };
    if (s.current) {
      c = s.current.counts;
    } else if (s.last_run && s.last_run.sum) {
      c = s.last_run.sum;
      o.label = 'Last run result';
      o.valueMuted = true;
      o.href = '#runs';
    } else {
      return { label: 'This run', value: '–', sub: 'no run yet' };
    }
    o.value = (c.new || 0) + ' new';
    o.sub = (c.skipped || 0) + ' skipped · ' + (c.failed || 0) + ' failed · ' + (c.filtered || 0) + ' filtered';
    return o;
  }

  function pendingCard(s) {
    var p = s.pending || {};
    var o = { label: 'Pending', value: (p.queued || 0) + ' queued', href: '#manual' };
    var parts = [];
    if (s.current) {
      if (isNum(p.channels_left)) { parts.push(plural(p.channels_left, 'channel', 'channels') + ' left'); }
      if (isNum(p.in_channel_max) && p.in_channel_max > 0) { parts.push('≤ ' + p.in_channel_max + ' more in this channel'); }
      o.tag = 'estimate ⓘ';
      o.tagTitle = 'New videos in channels not checked yet are unknown until yt-dlp reaches them.';
    } else if (s.scheduler.state === 'sleeping' && isNum(s.scheduler.next)) {
      parts.push('next check ' + hm(s.scheduler.next));
    }
    o.sub = parts.join(' · ');
    return o;
  }

  function diskCard(s) {
    var d = s.disk;
    if (!d || !d.total) { return { label: 'Disk', value: '–', sub: 'not available', href: '#recent' }; }
    var used = 100 * (d.total - d.free) / d.total;
    return { label: 'Disk', value: bytes(d.free), href: '#recent',
      sub: (d.low ? '⚠ low · ' : '') + 'free of ' + bytes(d.total) + ' · ' + nf0.format(used) + ' % used',
      subTone: d.low ? 'warn' : null, bar: used, barWarn: d.low, border: d.low ? 'warn' : null };
  }

  function cookiesCard(s) {
    var c = s.cookies || {};
    var o = { label: 'Cookies', href: '#attention' };
    var now = nowS();
    if (c.bot_checks_24h > 0) {
      o.value = 'Bot check'; o.valueTone = 'err'; o.border = 'err';
      o.sub = c.bot_checks_24h + '× in 24 h · refresh cookies.txt';
    } else if (!c.present) {
      o.value = 'None'; o.valueTone = 'idle'; o.sub = 'no cookies.txt in /config';
    } else if (c.readable === false) {
      o.value = 'Unreadable'; o.valueTone = 'warn'; o.sub = 'cookies.txt cannot be read';
    } else if (isNum(c.last_rotation) && now - c.last_rotation < 86400) {
      o.value = '⚠ Rotated'; o.valueTone = 'warn'; o.border = 'warn';
      o.sub = 'YouTube rejected them ' + ago(c.last_rotation);
    } else if (c.expired) {
      o.value = 'Expired'; o.valueTone = 'err'; o.border = 'err'; o.sub = 'login cookies expired ' + ago(c.expires);
    } else if (!c.login) {
      o.value = 'No login'; o.valueTone = 'warn'; o.sub = 'cookies.txt has no YouTube login';
    } else {
      o.value = 'OK'; o.valueTone = 'ok';
      o.sub = isNum(c.expires) ? 'login cookies expire in ' + duration(c.expires - now) : 'session cookies';
    }
    return o;
  }

  function renderCards(node) {
    var s = status;
    var daily = (s.counts && s.counts.daily) || [];
    var last = (s.counts && s.counts.last_24h) || {};
    fill(node, [
      schedulerCard(s), lastRunCard(s),
      { label: 'Last 24 h', value: String(last.total || 0), href: '#recent',
        sub: (last.scheduled || 0) + ' scheduled · ' + (last.manual || 0) + ' manual',
        mini: daily.length ? daily : null },
      runCountsCard(s), pendingCard(s), diskCard(s), cookiesCard(s)
    ].map(card));
  }

  // Counter chips of a run
  function countChips(c) {
    c = c || {};
    return add(el('div', 'chips'),
      chip((c.new || 0) + ' new', 'ok', 'check', true),
      chip((c.skipped || 0) + ' skipped', 'idle', 'skip', true),
      chip((c.failed || 0) + ' failed', c.failed ? 'err' : 'idle', 'x', true),
      chip((c.filtered || 0) + ' filtered', 'idle', 'filter', true));
  }

  var STATE_CHIP = {
    running: ['Running', 'run', 'dot'], sleeping: ['Sleeping', 'idle', 'moon'],
    interrupted: ['Interrupted', 'warn', 'alert'], oneshot: ['Finished', 'idle', 'moon'],
    stopped: ['Stopped', 'warn', 'alert'], nodata: ['No data yet', 'idle', 'moon']
  };

  function runNowButtons() {
    var run = el('button', 'btn btn-primary', 'Run now');
    run.type = 'button';
    run.dataset.action = 'run-now';
    run.dataset.key = 'sched-run-now';
    var log = el('button', 'btn', 'Open last run log');
    log.type = 'button';
    log.dataset.action = 'open-log';
    log.dataset.key = 'sched-open-log';
    return add(el('div', 'row'), run, log);
  }

  function lastRunLine(r) {
    if (!r) { return null; }
    var sum = r.sum || {};
    return el('div', 'muted small', 'Last run ' + dayTime(r.started) + ' · ' + duration(r.duration) +
      ' · ' + (sum.new || 0) + ' new · ' + (sum.skipped || 0) + ' skipped · ' + (sum.failed || 0) + ' failed');
  }

  function renderSched(node) {
    var s = status;
    var sc = s.scheduler;
    var cur = s.current;
    var chipInfo = STATE_CHIP[sc.state] || STATE_CHIP.nodata;
    var head = add(el('div', 'row'), el('span', 'panel-title', 'Scheduled run'),
      chip(chipInfo[0], chipInfo[1], chipInfo[2], true));
    var parts = [];
    if (sc.state === 'running' && cur) {
      add(head, el('span', 'muted small', 'started ' + hm(cur.started) + ' · ' + duration(nowS() - cur.started)));
      var ch = cur.channel;
      var overallLabel = 'Overall' + (ch ? ' · channel ' + ch.index + ' of ' + ch.total
        : (isNum(cur.channels) ? ' · ' + plural(cur.channels, 'channel', 'channels') : ''));
      var overall = bar({ label: overallLabel, right: pct(cur.overall), percent: cur.overall,
        aria: 'Overall progress', large: true,
        valuetext: isNum(cur.overall) ? Math.round(cur.overall) + ' percent' : overallLabel });
      if (ch) {
        var line = add(el('div', 'small'), add(el('span', 'strong'), bdi(ch.name || ch.url || 'Channel ' + ch.index)));
        if (cur.item) {
          add(line, el('span', 'muted', ' · video ' + cur.item.index + ' of ' + cur.item.count + ' in this channel'));
        }
        add(overall, line);
      }
      parts.push(overall);
      var v = cur.video;
      var box = el('div', 'video-box');
      if (v) {
        add(box, add(el('div', 'video-head'),
          add(el('div', 'thumb'), icon('play', 'ic-m')),
          add(el('div', 'grow'),
            add(el('div', 'video-title clamp-2'), bdi(v.title || v.id || 'Video')),
            v.channel ? add(el('div', 'muted small'), bdi(v.channel)) : null)));
      }
      add(box, videoBar(v, 'Current video'));
      parts.push(box);
      parts.push(countChips(cur.counts));
    } else if (sc.state === 'interrupted') {
      parts.push(add(el('div', 'idle-line'), icon('alert'),
        el('div', null, 'The run started ' + dayTime(cur && cur.started) +
          ' was stopped (restart or container stop).')));
      if (cur) { parts.push(countChips(cur.counts)); }
      parts.push(runNowButtons());
    } else {
      var text;
      if (sc.state === 'sleeping' && isNum(sc.next)) {
        text = sc.next <= nowS() ? 'Nothing running. The next run is starting…'
          : 'Nothing running. Next scheduled run at ' + hm(sc.next) + ' (in ' + duration(sc.next - nowS()) + ').';
      } else if (sc.state === 'stopped') {
        text = 'The downloader is not running (stopped, or restarting). Run now starts a new run.';
      } else if (sc.state === 'oneshot') {
        text = 'Runs once, then the container exits.';
      } else if (sc.state === 'nodata') {
        text = 'No run recorded yet. The dashboard starts counting with the next run.';
      } else {
        text = 'Nothing running.';
      }
      parts.push(add(el('div', 'idle-line'), icon('moon'),
        add(el('div'), el('div', 'strong', text), lastRunLine(s.last_run))));
      parts.push(runNowButtons());
      if (s.last_run && s.last_run.sum) { parts.push(countChips(s.last_run.sum)); }
    }
    fill(node, head, parts);
  }

  // Manual downloads
  var SRC = { api: 'API', web: 'Web' };
  var STATE_TEXT = {
    done: 'Done', partial: 'Done with errors', failed: 'Failed', cancelled: 'Cancelled',
    interrupted: 'Interrupted', already: 'Already downloaded', running: 'Running', queued: 'Queued'
  };
  var STATE_TONE = {
    done: ['ok', 'check'], partial: ['warn', 'alert'], failed: ['err', 'x'], cancelled: ['idle', 'ban'],
    interrupted: ['warn', 'alert'], already: ['idle', 'archive'], running: ['run', 'dot'], queued: ['idle', 'clock']
  };

  function stateChip(job, medium) {
    var t = STATE_TONE[job.state] || ['idle', null];
    var text = STATE_TEXT[job.state] || job.state;
    if (job.state === 'failed' && job.error && job.error.kind) { text += ' · ' + kindLabel(job.error.kind).toLowerCase(); }
    if (job.state === 'running' && job.cancelling) { text = 'Stopping…'; }
    return chip(text, t[0], t[1], medium);
  }

  function srcChip(job) { return job.src ? chip(SRC[job.src] || job.src, 'idle') : null; }

  function jobName(job) { return job.title || job.url || 'Download'; }

  function retryAction(job) {
    if (job.state === 'already' || job.state === 'done') {
      return { text: 'Re-download', action: 'redownload' };
    }
    if (job.state === 'failed' || job.state === 'interrupted' || job.state === 'cancelled' || job.state === 'partial') {
      return { text: 'Retry', action: 'retry' };
    }
    return null;
  }

  function itemBar(job) {
    var item = job.item;
    if (!item || !(item.count > 1)) { return null; }
    var v = job.video || {};
    var frac = isNum(v.percent) ? v.percent / 100 : 0;
    var p = 100 * Math.min(1, (item.index - 1 + frac) / item.count);
    return bar({ label: 'Item ' + item.index + ' of ' + item.count, right: pct(p), percent: p,
      aria: 'Playlist progress', valuetext: 'item ' + item.index + ' of ' + item.count });
  }

  function cancelButton(job, cls) {
    var b = button(job.cancelling ? 'Stopping…' : 'Cancel', cls || 'btn-danger-outline btn-s', 'cancel', { id: job.id });
    if (job.cancelling) { b.disabled = true; }
    return b;
  }

  function progressBox(job, draw) {
    var box = el('div', 'job-progress');
    box.dataset.progress = job.id;
    // The live job (status), not the copy without progress the section got.
    var live = findJob(status, job.id) || job;
    box.dataset.sig = JSON.stringify([live.video, live.item]);
    return add(box, draw(live));
  }

  function manualProgress(job) {
    return [itemBar(job), videoBar(job.video, 'Manual download')];
  }

  function jobProgress(job) {
    var v = job.video;
    var now = null;
    if (v && v.title && job.title && v.title !== job.title) {
      now = add(el('div', 'small'), el('span', 'muted', 'Now: '), bdi(v.title));
    }
    return [now, itemBar(job), videoBar(v, 'Current video', true)];
  }

  function renderManual(node) {
    var s = status;
    var jobs = s.jobs || {};
    var running = jobs.running || [];
    var queued = jobs.queued || [];
    var finished = jobs.finished || [];
    var parts = [add(el('div', 'panel-head'), el('span', 'panel-title', 'Manual downloads'),
      el('span', 'muted small', PARALLEL + ' at a time · up to ' + MAX_QUEUED + ' waiting'))];
    running.forEach(function (job) {
      var title = link('', jobHref(job.id), 'job-title clamp-2', 'title:' + job.id);
      add(title, bdi(jobName(job)));
      parts.push(add(el('div', 'job-box'),
        add(el('div', 'row'), srcChip(job), title),
        progressBox(job, manualProgress),
        add(el('div', 'row'), cancelButton(job), link('Log', jobHref(job.id), 'btn btn-s', 'log:' + job.id))));
    });
    if (running.length && s.scheduler.state === 'running') {
      parts.push(add(el('div', 'info-line'), icon('info'),
        el('span', null, 'Running alongside the scheduled pass (more requests to YouTube).')));
    }
    if (queued.length) {
      parts.push(add(el('div', 'list'), queued.map(function (job) {
        var x = button('✕', 'btn-icon btn-s', 'cancel', { id: job.id });
        x.setAttribute('aria-label', 'Remove from queue');
        x.title = 'Remove from queue';
        return add(el('div', 'list-row'),
          el('span', 'qpos', '#' + job.position),
          add(el('span', 'grow mono'), bdi(job.url || '')),
          srcChip(job),
          job.force ? chip('re-download', 'idle') : null,
          timeNode(job.queued, 'added ' + ago(job.queued), 'muted small'),
          x);
      })));
    } else if (running.length) {
      parts.push(el('div', 'empty', 'Queue is empty.'));
    }
    if (finished.length) {
      parts.push(el('div', 'section-label', 'Finished'));
      parts.push(add(el('div', 'list'), finished.map(function (job) {
        var meta = [SRC[job.src] || null, ago(job.ended)];
        if (job.state === 'interrupted') { meta.push('web UI restarted'); }
        if (job.error && job.error.msg && job.state !== 'failed') { meta.push(job.error.msg); }
        var title = add(el('span', 'grow small clamp-1' + (job.title ? '' : ' mono')), bdi(jobName(job)));
        if (job.title && job.url) { title.title = job.url; }
        var act = retryAction(job);
        return add(el('div', 'list-row'),
          stateChip(job),
          title,
          timeNode(job.ended, meta.filter(Boolean).join(' · '), 'muted small'),
          act ? button(act.text, 'btn-s', act.action, { id: job.id }) : null,
          link('Log', jobHref(job.id), 'btn-link', 'log:' + job.id));
      })));
    }
    if (!running.length && !queued.length && !finished.length) {
      parts.push(el('div', 'empty-plain', 'No manual downloads yet. Share a link from your iPad or paste one above.'));
    }
    fill(node, parts);
  }

  // Needs attention
  var SEVERITY_TONE = { error: 'err', warning: 'warn', info: 'idle' };

  function renderAttention(node, groups) {
    var list = $('attention-groups');
    node.hidden = !groups || !groups.length;
    if (!groups || !groups.length) { fill(list); return; }
    fill(list, groups.map(function (g) {
      var d = el('details', 'group');
      var key = 'group:' + g.kind;
      d.dataset.key = key;
      d.open = key in openState ? openState[key] : g.severity === 'error';
      var summary = add(el('summary'),
        chip((g.label || kindLabel(g.kind)) + ' ×' + g.count, SEVERITY_TONE[g.severity] || 'warn', 'alert', true),
        g.last ? timeNode(g.last, 'last ' + dayTime(g.last), 'muted small') : null,
        el('span', 'group-hint', g.hint || ''));
      var items = el('div', 'group-items');
      (g.items || []).forEach(function (it) {
        var where = it.src === 'manual' ? 'manual' : (isNum(it.channel) ? 'channel ' + it.channel : 'scheduled');
        var row = add(el('div', 'list-row group-item'),
          el('span', 'mono', it.id || '—'),
          add(el('span', 'grow'), bdi(it.title || '(title unknown)')),
          timeNode(it.ts, where + ' · ' + dayTime(it.ts), 'muted small'));
        if (it.youtube_id && VIDEO_ID_RE.test(it.youtube_id)) {
          var b = button('Download now', 'btn-s', 'download-now', { video: it.youtube_id });
          if (it.url) { b.title = it.url; }
          add(row, b);
        }
        if (it.msg) { add(row, el('div', 'group-msg', it.msg)); }
        add(items, row);
      });
      return add(d, summary, items);
    }));
  }

  function renderRecent(node, recent) {
    if (!recent || !recent.length) {
      fill(node, el('div', 'empty', 'Nothing downloaded since the dashboard was enabled. ' +
        'Earlier downloads are in archive.txt but have no timestamps.'));
      return;
    }
    fill(node, recent.map(function (r) {
      var manual = r.src === 'manual';
      return add(el('div', 'recent-row'),
        add(el('div', 'thumb thumb-s'), icon('play', 'ic-s')),
        add(el('div', 'recent-main'),
          add(el('span', 'recent-title'), bdi(r.title || r.id || 'Video')),
          r.channel ? add(el('span', 'muted small'), bdi(r.channel)) : null,
          timeNode(r.ts, ago(r.ts), 'muted small')),
        chip(manual ? 'Manual' : 'Scheduled', manual ? 'run' : 'idle'));
    }));
  }

  function td(text, label, cls) {
    var cell = el('td', cls);
    cell.dataset.label = label;
    if (text !== null && text !== undefined) {
      add(cell, typeof text === 'object' ? text : String(text));
    }
    return cell;
  }

  var CHANNEL_STATUS = {
    ok: ['✓ OK', 'ok'], stale: ['⚠ No new videos in 90 days', 'warn'],
    failed: ['✗ ', 'err'], error: ['✗ ', 'err']
  };

  function renderChannels(node, channels) {
    channels = channels || [];
    var empty = $('channels-empty');
    if (empty) { empty.hidden = channels.length > 0; }
    var bad = channels.filter(function (c) { return c.status !== 'ok'; }).length;
    var summary = $('channels-summary');
    if (summary) {
      summary.textContent = channels.length
        ? '· ' + channels.length + ' · ' + (bad ? bad + (bad === 1 ? ' needs' : ' need') + ' attention' : 'all OK')
        : '';
    }
    fill(node, channels.map(function (c) {
      var st = CHANNEL_STATUS[c.status] || CHANNEL_STATUS.error;
      var text = st[0];
      if (c.status === 'failed' || c.status === 'error') {
        text += c.last && c.last.kind ? kindLabel(c.last.kind) : (c.status === 'failed' ? 'Failed' : 'Error');
      }
      var last = c.last || {};
      var lastRun = c.fail_streak > 1 ? 'failed ' + c.fail_streak + ' runs in a row'
        : (last.new || 0) + ' new' + (last.failed ? ' · ' + last.failed + ' failed' : '');
      var name = add(el('span'), bdi(c.name || c.url || 'Channel ' + c.idx));
      if (c.url) { name.title = c.url; }
      return add(el('tr'),
        td(name, 'Channel', c.name ? null : 'mono'),
        td(timeNode(c.last_checked, dayTime(c.last_checked)), 'Last checked'),
        td(c.last_new ? timeNode(c.last_new, ago(c.last_new)) : '–', 'Last new', 'col-last-new'),
        td(lastRun, 'Last run'),
        td(text, 'Status', 'status fg-' + st[1]));
    }));
  }

  var RUN_RESULT = {
    ok: ['✓ OK', 'ok'], failed: ['✗ exit code ', 'err'], running: ['● running', 'run'],
    interrupted: ['⚠ interrupted', 'warn']
  };

  function renderRuns(node, runs) {
    runs = runs || [];
    var empty = $('runs-empty');
    if (empty) { empty.hidden = runs.length > 0; }
    fill(node, runs.map(function (r) {
      var res = RUN_RESULT[r.result] || ['–', 'idle'];
      var text = res[0] + (r.result === 'failed' ? r.rc : '');
      var sum = r.sum;
      return add(el('tr'),
        td(timeNode(r.started, dayTime(r.started)), 'Start'),
        td(r.result === 'running' ? duration(nowS() - r.started) : duration(r.duration), 'Duration'),
        td(sum ? sum.new : '–', 'New'),
        td(sum ? sum.skipped : '–', 'Skipped'),
        td(sum ? sum.failed : '–', 'Failed'),
        td(text, 'Result', 'status fg-' + res[1]));
    }));
  }

  // Job page
  function findJob(s, id) {
    if (!id || !s || !s.jobs) { return null; }
    var all = (s.jobs.running || []).concat(s.jobs.queued || [], s.jobs.finished || []);
    for (var i = 0; i < all.length; i++) {
      if (all[i].id === id) { return all[i]; }
    }
    return null;
  }

  var JOB_MESSAGE = {
    done: 'Download finished.', partial: 'Finished, but some items failed.',
    failed: 'The download failed.', cancelled: 'The download was cancelled.',
    interrupted: 'The web UI restarted while this download ran.',
    already: 'Already downloaded: the video is in the download archive.'
  };

  function renderJob(node, data) {
    var job = data[0];
    if (!job) {
      fill(node, el('p', 'empty-plain', 'This download is not in the recent list any more ' +
        '(the dashboard keeps the last 10 finished downloads). Its log is below.'));
      return;
    }
    var times = ['queued ' + hm(job.queued)];
    if (isNum(job.started)) { times.push('started ' + hm(job.started)); }
    if (isNum(job.ended)) { times.push('ended ' + hm(job.ended)); }
    if (isNum(job.started)) { times.push(duration((job.ended || nowS()) - job.started)); }
    var v = job.video;
    var sub = [];
    if (v && v.channel) { sub.push(v.channel); }
    if (v && v.playlist && v.playlist !== job.title) { sub.push(v.playlist); }
    var parts = [
      add(el('div', 'row'), stateChip(job, true), srcChip(job),
        job.force ? chip('re-download', 'idle') : null,
        timeNode(job.queued, times.join(' · '), 'muted small')),
      add(el('div', 'job-main'),
        add(el('div', 'thumb thumb-l'), icon('play', 'ic-l')),
        add(el('div', 'grow'),
          add(el('div', 'job-heading'), bdi(job.title || (v && v.title) || 'Download')),
          sub.length ? add(el('div', 'muted'), bdi(sub.join(' · '))) : null,
          job.url ? add(el('div', 'mono muted'), bdi(job.url)) : null))
    ];
    if (job.state === 'running') {
      parts.push(progressBox(job, jobProgress));
    } else if (job.state === 'queued') {
      parts.push(el('p', null, 'Waiting in the queue: position ' + job.position + '.'));
    } else {
      parts.push(el('p', null, JOB_MESSAGE[job.state] || 'The download ended.'));
      if (job.error && job.error.msg) {
        parts.push(add(el('div', 'callout tone-' + (job.state === 'partial' ? 'warn' : 'err')),
          add(el('div', 'stack'), el('strong', null, kindLabel(job.error.kind)), el('div', 'mono', job.error.msg))));
      }
    }
    var items = job.items || {};
    if (items.done || items.failed) {
      parts.push(el('div', 'counts-line muted', plural(items.done || 0, 'video', 'videos') + ' downloaded · ' +
        (items.failed || 0) + ' failed'));
    }
    var actions = el('div', 'row');
    if (job.state === 'running' || job.state === 'queued') {
      add(actions, cancelButton(job, 'btn-danger-outline'));
    }
    var act = retryAction(job);
    if (act) { add(actions, button(act.text, 'btn-primary', act.action, { id: job.id })); }
    if (job.state === 'partial') {   // 'done' already got Re-download
      add(actions, button('Re-download', '', 'redownload', { id: job.id }));
    }
    add(actions, link('Jump to log', '#log', 'btn', 'jump-log'),
      link('Back to dashboard', BASE + '/', 'btn', 'back'));
    parts.push(actions);
    fill(node, parts);
    var log = $('log-title');
    if (log) { log.textContent = 'Log · this download'; }
  }

  // ---- Actions ----------------------------------------------------------------

  var dialog = $('confirm-dialog');

  function confirmRestart() {
    if (dialog && typeof dialog.showModal === 'function') {
      if (!dialog.open) { dialog.showModal(); }
      var cancel = $('confirm-cancel');
      if (cancel) { cancel.focus(); }
      return;
    }
    // Browsers without <dialog>: a plain confirmation.
    if (window.confirm('Abort the running pass and start over?')) { restart(true); }
  }

  function restart(confirmed) {
    return post('/restart-youtube-dl', confirmed ? { confirm: '1' } : {}).then(function (res) {
      if (res.status === 409) { confirmRestart(); return; }
      if (!res.ok) { toast(failText(res), 'err'); return; }
      toast(confirmed ? 'Pass restarted.' : 'Scheduled run started.', 'ok');
      announce('Scheduled run started.');
    }, networkFail);
  }

  function runNow() {
    if (status && status.scheduler && status.scheduler.state === 'running') {
      confirmRestart();
    } else {
      restart(false);
    }
  }

  if (dialog) {
    $('confirm-cancel').addEventListener('click', function () { dialog.close(); });
    $('confirm-ok').addEventListener('click', function () {
      dialog.close();
      restart(true);
    });
  }

  function withBusy(btn, promise) {
    if (btn) { btn.disabled = true; }
    return promise.then(function (v) {
      if (btn && btn.isConnected) { btn.disabled = false; }
      return v;
    }, function (e) {
      if (btn && btn.isConnected) { btn.disabled = false; }
      networkFail();
      return e;
    });
  }

  document.addEventListener('click', function (e) {
    var target = e.target;
    // Close open menus when clicking elsewhere.
    document.querySelectorAll('details.menu[open]').forEach(function (d) {
      if (!d.contains(target)) { d.open = false; }
    });
    var btn = target && target.closest ? target.closest('[data-action]') : null;
    if (!btn) {
      var anchor = target && target.closest ? target.closest('a[href^="#"]') : null;
      if (anchor) { openTarget(anchor.hash); }
      return;
    }
    var id = btn.dataset.id;
    var action = btn.dataset.action;
    if (id && !UUID_RE.test(id)) { return; }
    if (action === 'reload') {
      window.location.reload();
    } else if (action === 'run-now') {
      runNow();
    } else if (action === 'open-log') {
      openLog('run');
    } else if (action === 'cancel') {
      withBusy(btn, post('/api/download/' + id + '/cancel').then(function (res) {
        if (!res.ok) { toast(failText(res), 'err'); return; }
        toast((res.data && res.data.message) || 'Cancelled.', 'idle');
      }));
    } else if (action === 'retry' || action === 'redownload') {
      var fields = action === 'redownload' ? { force: '1' } : {};
      withBusy(btn, post('/api/download/' + id + '/retry', fields).then(function (res) {
        var newId = ticketToast(res);
        if (newId && PAGE === 'job') { window.location.assign(jobHref(newId)); }
      }));
    } else if (action === 'download-now') {
      var video = btn.dataset.video;
      if (!VIDEO_ID_RE.test(video || '')) { return; }
      withBusy(btn, post('/api/download-now', { video_id: video }).then(ticketToast));
    }
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      document.querySelectorAll('details.menu[open]').forEach(function (d) {
        d.open = false;
        var s = d.querySelector('summary');
        if (s) { s.focus(); }
      });
    }
  });

  // Links to a closed <details> (cards, "Show problems") open it.
  function openTarget(hash) {
    if (!hash || hash.length < 2) { return; }
    var node = document.getElementById(hash.slice(1));
    if (node && node.tagName === 'DETAILS') { node.open = true; }
  }
  window.addEventListener('hashchange', function () { openTarget(window.location.hash); });

  // The download form: queued through the JSON API; without JavaScript it
  // posts to /download and the server redirects to the job page.
  var dlForm = $('dl-form');
  var dlUrl = $('dl-url');
  var dlHint = $('dl-hint');
  if (dlUrl && dlHint) {
    dlUrl.addEventListener('input', function () {
      dlHint.hidden = !/\/@|\/channel\/|\/c\/|\/user\/|[?&]list=|\/playlist/.test(dlUrl.value);
    });
  }
  if (dlForm) {
    dlForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var url = dlUrl.value.trim();
      if (!url) { dlUrl.focus(); return; }
      var force = dlForm.elements.force;
      var submit = dlForm.querySelector('button[type="submit"]');
      withBusy(submit, post('/api/download', { url: url, force: force && force.checked ? '1' : '' })
        .then(function (res) {
          if (ticketToast(res)) {
            dlUrl.value = '';
            dlHint.hidden = true;
            if (force) { force.checked = false; }
            var opts = dlForm.querySelector('details');
            if (opts) { opts.open = false; }
          }
        }));
    });
  }

  var runForm = $('run-form');
  if (runForm) {
    runForm.addEventListener('submit', function (e) {
      e.preventDefault();
      runNow();
    });
  }

  var toastClose = $('toast-close');
  if (toastClose) {
    toastClose.addEventListener('click', function () { $('toast').hidden = true; });
  }

  // ---- Log panel ----------------------------------------------------------------

  var logDetails = $('log');
  var logBody = $('log-body');
  var logSelect = $('log-source');
  var logFollow = $('log-follow');
  var logNote = $('log-note');
  var logState = {
    source: JOB_ID ? 'job:' + JOB_ID : 'run', offset: -1, gen: 0, timer: null, count: 0, busy: false,
    inFlight: null
  };

  function logPath(source) {
    if (source === 'run') { return '/log/youtube-dl'; }
    return '/log/download/' + source.slice(4);
  }

  function lineClass(line) {
    if (/^ERROR[:\s]/.test(line)) { return 'log-line is-err'; }
    if (/^WARNING[:\s]/.test(line)) { return 'log-line is-warn'; }
    if (/has already been recorded in the archive/.test(line)) { return 'log-line is-skip'; }
    if (/^\[dashboard\] /.test(line)) { return 'log-line is-mark'; }
    return 'log-line';
  }

  function appendLog(text, reset) {
    if (reset) {
      fill(logBody);
      logState.count = 0;
    }
    var lines = text ? text.split('\n') : [];
    if (lines.length && lines[lines.length - 1] === '') { lines.pop(); }
    if (lines.length) {
      var frag = document.createDocumentFragment();
      lines.forEach(function (line) { frag.appendChild(el('div', lineClass(line), line)); });
      logBody.appendChild(frag);
      logState.count += lines.length;
      while (logState.count > MAX_LOG_LINES && logBody.firstChild) {
        logBody.removeChild(logBody.firstChild);
        logState.count -= 1;
      }
      if (logFollow && logFollow.checked) { logBody.scrollTop = logBody.scrollHeight; }
    }
    if (logNote) {
      logNote.hidden = logState.count > 0;
      logNote.textContent = 'The log is empty.';
    }
  }

  function scheduleLog(ms) {
    if (!logBody) { return; }
    clearTimeout(logState.timer);
    // A request of this source is under way: its answer schedules the
    // next one (a second chain would append the same lines twice).
    if (logState.inFlight === logState.gen) { return; }
    if (!logDetails || !logDetails.open || document.hidden || session.expired) { return; }
    logState.timer = setTimeout(function () { pollLog(0); }, ms);
  }

  function pollLog(round) {
    var gen = logState.gen;
    var source = logState.source;
    logState.inFlight = gen;
    var url = BASE + '/api' + logPath(source) + '?since=' + logState.offset;
    fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { session.expired = true; renderBanners(); throw new Error('401'); }
        if (!r.ok) { throw new Error('log ' + r.status); }
        return r.json();
      })
      .then(function (d) {
        if (gen !== logState.gen) { return; }
        logState.inFlight = null;
        appendLog(typeof d.text === 'string' ? d.text : '', d.reset === true);
        if (isNum(d.offset)) { logState.offset = d.offset; }
        if (d.more && round < 8) {
          pollLog(round + 1);
        } else {
          scheduleLog(cadence(status));
        }
      })
      .catch(function () {
        if (gen === logState.gen) { logState.inFlight = null; scheduleLog(SLOW); }
      });
  }

  function setLogSource(source) {
    logState.source = source;
    logState.offset = -1;
    logState.gen += 1;
    if (logBody) { fill(logBody); logState.count = 0; }
    var dl = $('log-download');
    if (dl) {
      dl.href = BASE + logPath(source);
      dl.download = source === 'run' ? 'youtube-dl.log' : 'download-' + source.slice(4) + '.log';
    }
    var title = $('log-title');
    if (title) {
      title.textContent = source === 'run' ? 'Log · current run'
        : (source === 'job:' + JOB_ID ? 'Log · this download' : 'Log · manual download');
    }
    if (logSelect && logSelect.value !== source) { logSelect.value = source; }
    scheduleLog(0);
  }

  function openLog(source) {
    if (!logDetails) { return; }
    logDetails.open = true;
    setLogSource(source);
    logDetails.scrollIntoView({ block: 'start' });
  }

  // The options only (no progress), so the <select> is rebuilt only when
  // jobs come or go.
  function logSourceList(jobs) {
    var list = [{ value: 'run', text: 'Current run' }];
    var seen = {};
    if (JOB_ID) {
      list.unshift({ value: 'job:' + JOB_ID, text: 'This download' });
      seen[JOB_ID] = true;
    }
    jobs = jobs || {};
    (jobs.running || []).concat(jobs.finished || []).forEach(function (j) {
      if (seen[j.id] || !UUID_RE.test(j.id)) { return; }
      seen[j.id] = true;
      var name = jobName(j);
      list.push({ value: 'job:' + j.id, text: 'Job: ' + (name.length > 48 ? name.slice(0, 47) + '…' : name) });
    });
    return list;
  }

  function renderLogSources(node, list) {
    list = list.slice();
    if (!list.some(function (o) { return o.value === logState.source; })) {
      list.push({ value: logState.source, text: 'Job: earlier download' });
    }
    fill(node, list.map(function (o) {
      var opt = el('option', null, o.text);
      opt.value = o.value;
      return opt;
    }));
    node.value = logState.source;
  }

  if (logDetails && logBody) {
    logDetails.addEventListener('toggle', function () {
      if (logDetails.open) { scheduleLog(0); } else { clearTimeout(logState.timer); }
    });
    if (logSelect) {
      logSelect.addEventListener('change', function () {
        var v = logSelect.value;
        if (v === 'run' || (v.indexOf('job:') === 0 && UUID_RE.test(v.slice(4)))) { setLogSource(v); }
      });
    }
    document.querySelectorAll('[data-filter]').forEach(function (b) {
      b.addEventListener('click', function () {
        var f = b.dataset.filter;
        document.querySelectorAll('[data-filter]').forEach(function (o) {
          o.setAttribute('aria-pressed', o === b ? 'true' : 'false');
        });
        logBody.classList.toggle('hide-skip', f === 'noskip');
        logBody.classList.toggle('only-problems', f === 'errors');
      });
    });
    if (logFollow) {
      logFollow.addEventListener('change', function () {
        if (logFollow.checked) { logBody.scrollTop = logBody.scrollHeight; }
      });
    }
    var copy = $('log-copy');
    if (copy) {
      copy.addEventListener('click', function () {
        var lines = [];
        logBody.querySelectorAll('.log-line').forEach(function (line) {
          if (line.offsetParent !== null || !logDetails.open) { lines.push(line.textContent); }
        });
        var text = lines.join('\n');
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () { toast('Log copied.', 'ok'); },
            function () { toast('Copying is not allowed here; select the text instead.', 'err'); });
        } else {
          var range = document.createRange();
          range.selectNodeContents(logBody);
          var sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
          toast('Log selected; copy it with the system menu.', 'idle');
        }
      });
    }
    setLogSource(logState.source);
  }

  // ---- Start ------------------------------------------------------------------

  openTarget(window.location.hash);
  poll();
})();
