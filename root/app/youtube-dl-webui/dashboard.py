"""Dashboard state of the web UI (standard library only).

Reads, incrementally and with bounded memory, what the downloads leave
behind and builds the JSON of ``GET /api/status``:

* ``EventLog`` follows ``/config/.youtube-dl-status/events.jsonl`` (plus the
  rotated ``events.jsonl.1`` once at startup) and keeps the last runs, the
  last finished or failed items, the download times of the last 7 days, the
  manual jobs and per-channel statistics from the ``run_end`` rows (for a
  pass too large for one event, from the ``run_rows`` events before it).
* ``PassLog`` tails the pass log ``/var/log/youtube-dl/youtube-dl.log``
  (which ``tee`` truncates at the start of every pass) with the shared
  ``status_events.LogParser`` while the newest pass has no ``run_end`` yet,
  trusting only lines with that pass's nonce.
* ``Dashboard.status_json()`` combines both with the scheduler state, the free
  disk space and the cookie health into one JSON document plus an ETag.
* ``read_log_since()``/``read_log_tail()`` serve log text for the log panel
  and the plain-text log routes, with the dashboard's own lines removed.

Rules:

* The nonce never leaves the server. The status JSON is built from explicit
  fields (never by copying event dicts), and log text is scrubbed of every
  ``@@ytdl:<hex>`` sentinel, whatever nonce it carries.
* Every string in the status JSON went through ``clean()`` (no ANSI codes,
  control or bidirectional-override characters, at most 300 characters) or,
  for URLs, ``clean_url()`` (dropped instead of cut). The browser still
  renders them only as text.
* Files are opened with ``O_NOFOLLOW | O_NONBLOCK`` and must be regular
  files, so a symlink or a FIFO in their place cannot redirect or block the
  web UI. The status directory is opened like ``status_events`` does.
* Counts from human log lines (skipped, filtered, extraction failures) can be
  inflated by crafted video titles; they are cosmetic, never used for
  decisions. ``new``/``done`` come from the events and are exact.

Manual-job events (written by the web UI's job manager, jobs.py):
``job_queued {ts, run, url, by, force}``, ``job_start {ts, run}``,
``job_end {ts, run, state, rc, title?, error?: {kind, msg}}`` where ``run``
is the job's UUID as 32 hex characters, ``by`` is ``token`` or ``basic`` and
``state`` one of done, partial, failed, cancelled, interrupted, already.
Running and queued jobs come from the job manager's snapshot
(``status_json(jobs=...)``), finished ones from these events.
"""

import collections
import hashlib
import json
import math
import os
import re
import secrets
import stat
import threading
import time
from urllib.parse import urlsplit

import status_events as se

REFRESH_BYTES = 1024 * 1024        # read at most this much per file and poll
INITIAL_BYTES = 16 * 1024 * 1024   # ... except for the first read
HEAD_BYTES = 256                   # start of a file, to notice truncate+regrow
LOG_CHUNK_BYTES = 64 * 1024        # text per /api/log answer
LOG_SCAN_BYTES = 1024 * 1024       # skipping a long line per answer
LOG_LINE_BYTES = LOG_CHUNK_BYTES   # longer log lines are left out
MAX_RUNS = 30
MAX_ITEMS = 300
MAX_JOBS = 50
MAX_CHANNELS = 1000
MAX_RUN_EVENTS = 5000              # start/end events kept of the newest run
MAX_DONE = 20000                   # download times kept (7 days)
MAX_ROWS = 2000                    # run_end channel rows kept per run
MAX_GROUP_ITEMS = 20               # items listed per problem group
SHOWN_RECENT = 10
SHOWN_RUNS = 10
SHOWN_JOBS = 10
DAY = 86400
HISTORY_DAYS = 7
STALE_DAYS = 90
LOW_DISK_BYTES = 20 * 10 ** 9
LOW_DISK_RATIO = 0.05
MAX_COOKIE_BYTES = 1024 * 1024
OK_CODES = (0, 101)                # 101: yt-dlp stopped early on purpose

# YouTube/Google cookies that make up a signed-in session. Only these names
# and their expiry are reported; cookie values are never kept.
LOGIN_COOKIES = frozenset({
    'SID', 'HSID', 'SSID', 'APISID', 'SAPISID', 'LOGIN_INFO',
    '__Secure-1PSID', '__Secure-3PSID', '__Secure-1PAPISID',
    '__Secure-3PAPISID'})
COOKIE_DOMAINS = ('youtube.com', 'google.com')

KIND_SEVERITY = {
    'bot_check': 'error', 'cookies_rotated': 'error', 'disk_full': 'error',
    'rate_limit': 'warning', 'forbidden': 'warning', 'other': 'warning',
    'unavailable': 'info', 'private_members': 'info', 'geo': 'info',
    'members_only': 'info',
}
_SEVERITY_ORDER = {'error': 0, 'warning': 1, 'info': 2}
JOB_STATES = frozenset({'queued', 'running', 'done', 'partial', 'failed',
                        'cancelled', 'interrupted', 'already', 'skipped'})

_JOB_SOURCES = {'token': 'api', 'basic': 'web'}
_HEX_RE = re.compile(r'[0-9a-f]{16,32}\Z')
_JOB_RE = re.compile(r'[0-9a-f]{32}\Z')
_YOUTUBE_ID_RE = re.compile(r'[A-Za-z0-9_-]{11}\Z')


# --------------------------------------------------------------------------
# Small helpers

def clean(value, limit=se.MAX_TEXT):
    """Text safe to show: see status_events.clean_text (no ANSI codes,
    control or bidirectional-override characters, at most ``limit``
    characters); line and paragraph separators become spaces. Non-strings
    become ''."""
    if not isinstance(value, str):
        return ''
    return se.clean_text(value.replace('\u2028', ' ').replace('\u2029', ' '),
                         limit)


def clean_url(value):
    """A cleaned http(s) or other URL, or None. Never cut: a URL longer than
    status_events.MAX_URL is dropped."""
    if not isinstance(value, str) or len(value) > se.MAX_URL:
        return None
    return clean(value, se.MAX_URL) or None


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _int(value):
    return (value if isinstance(value, int) and not isinstance(value, bool)
            else None)


def _count(value):
    value = _int(value)
    return value if value is not None and value >= 0 else 0


def _ts(value):
    value = _num(value)
    return int(value) if value is not None and value > 0 else None


def _hex(value, pattern=_HEX_RE):
    return value if isinstance(value, str) and pattern.match(value) else None


def job_uuid(job_hex):
    h = job_hex
    return f'{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}'


def _open_regular(path, dir_fd=None):
    """Descriptor of a regular file, opened without following a symlink and
    without blocking on a FIFO. Raises OSError otherwise."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                 | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f'{path} is not a regular file')
    except BaseException:
        os.close(fd)
        raise
    return fd


# --------------------------------------------------------------------------
# Incremental line reader

class Read:
    """Result of LineReader.read()."""

    __slots__ = ('lines', 'reset', 'more')

    def __init__(self, lines, reset, more):
        self.lines = lines   # complete lines (str, without the newline)
        self.reset = reset   # the file was truncated or replaced
        self.more = more     # stopped at the byte limit, more is waiting


class LineReader:
    """Complete lines appended to a file since the previous read.

    * Returns only lines ending in a newline: a last line that is still being
      written is read again next time.
    * Keeps the file open between reads. When the path then names another
      file (rotation by rename, or deletion and re-creation), the rest of the
      old file is read first, then the new one from its start (``reset``).
    * Notices truncation (``tee`` empties the pass log at every pass) by the
      size dropping below the offset or by the first bytes changing, which
      also catches a file that was emptied and has grown past the offset
      again. It then starts over (``reset``).
    * Reads at most ``max_bytes`` per call (``more`` says there is more).
    * Skips lines longer than ``max_line`` bytes.
    * With ``status_dir``, the file is opened relative to the status
      directory, which must not be a symlink and must belong to this user
      (status_events.open_status_dir).
    """

    def __init__(self, path, max_line=se.MAX_LINE_BYTES, status_dir=False):
        self.path = path
        self.status_dir = status_dir
        self.max_line = max_line
        self.fd = None
        self.ident = None        # (st_dev, st_ino) of the open file
        self.offset = 0          # start of the first line not returned yet
        self.head = b''
        self.skipping = False    # inside a line longer than max_line
        self.seen = False        # a file was open before

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _stat_path(self):
        """(dev, ino) of what the path names now, or None."""
        try:
            if self.status_dir:
                dir_fd = se.open_status_dir(os.path.dirname(self.path),
                                            create=False)
                try:
                    st = os.stat(os.path.basename(self.path), dir_fd=dir_fd,
                                 follow_symlinks=False)
                finally:
                    os.close(dir_fd)
            else:
                st = os.stat(self.path, follow_symlinks=False)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        return (st.st_dev, st.st_ino)

    def _open(self):
        try:
            if self.status_dir:
                dir_fd = se.open_status_dir(os.path.dirname(self.path),
                                            create=False)
                try:
                    fd = _open_regular(os.path.basename(self.path), dir_fd)
                finally:
                    os.close(dir_fd)
            else:
                fd = _open_regular(self.path)
        except OSError:
            return False
        st = os.fstat(fd)
        self.fd, self.ident = fd, (st.st_dev, st.st_ino)
        self.offset, self.head, self.skipping = 0, b'', False
        return True

    def _truncated(self):
        st = os.fstat(self.fd)
        if st.st_size < self.offset:
            return True
        return bool(self.head) and os.pread(
            self.fd, len(self.head), 0) != self.head

    def _consume(self, lines, max_bytes):
        """Read complete lines from the open file; returns (bytes read,
        reached the end)."""
        pos = self.offset
        buf = b''
        total = 0
        while total < max_bytes:
            chunk = os.pread(self.fd, min(65536, max_bytes - total),
                             pos + len(buf))
            if not chunk:
                break
            total += len(chunk)
            buf += chunk
            start = 0
            while True:
                nl = buf.find(b'\n', start)
                if nl < 0:
                    break
                line = buf[start:nl]
                start = nl + 1
                if self.skipping:
                    self.skipping = False
                elif len(line) <= self.max_line:
                    lines.append(line.decode('utf-8', 'replace'))
            pos += start
            buf = buf[start:]
            if self.skipping or len(buf) > self.max_line:
                # An over-long line: drop what we have and the rest of it.
                pos += len(buf)
                buf = b''
                self.skipping = True
        else:
            self.offset = pos
            return total, False
        self.offset = pos
        return total, True

    def _update_head(self):
        if len(self.head) < HEAD_BYTES and self.offset > len(self.head):
            head = os.pread(self.fd, HEAD_BYTES, 0)
            if head.startswith(self.head):
                self.head = head[:max(len(self.head), self.offset)]

    def read(self, max_bytes=REFRESH_BYTES):
        lines = []
        reset = False
        budget = max_bytes
        if self.fd is not None:
            if self._stat_path() != self.ident:
                # Rotated or deleted: finish the old file first.
                used, at_end = self._consume(lines, budget)
                budget -= used
                if not at_end:
                    return Read(lines, False, True)
                self.close()
                reset = True
            elif self._truncated():
                self.offset, self.head, self.skipping = 0, b'', False
                reset = True
        if self.fd is None:
            if not self._open():
                return Read(lines, reset, False)
            reset = reset or self.seen
            self.seen = True
        _used, at_end = self._consume(lines, max(budget, 0))
        self._update_head()
        return Read(lines, reset, not at_end)


# --------------------------------------------------------------------------
# Event file

def _sum(value):
    value = value if isinstance(value, dict) else {}
    return {key: _count(value.get(key))
            for key in ('new', 'skipped', 'failed', 'filtered', 'members')}


def _row(row):
    """A run_end channel row as a dict (see status_events.CHANNEL_ROW)."""
    if not isinstance(row, list) or not row:
        return None
    row = (row + [None] * len(se.CHANNEL_ROW))[:len(se.CHANNEL_ROW)]
    idx = _int(row[0])
    if idx is None or idx < 1:
        return None
    return {'idx': idx, 'new': _count(row[1]), 'skipped': _count(row[2]),
            'failed': _count(row[3]), 'filtered': _count(row[4]),
            'rc': _int(row[5]), 'secs': _ts(row[6]), 'ts': _ts(row[7]),
            'url': clean_url(row[8]), 'members': _count(row[9])}


def _error(err):
    if not isinstance(err, dict):
        return None
    kind = err.get('kind')
    idx = _int(err.get('idx'))
    return {'idx': idx if idx is not None and idx >= 1 else None,
            'id': clean(err.get('id'), 64) or None,
            'kind': kind if kind in se.KIND_LABELS else 'other',
            'msg': clean(err.get('msg')),
            'title': clean(err.get('title')),
            'url': clean_url(err.get('url')),
            'ie': clean(err.get('ie'), 40)}


def _has_file(info):
    paths = info.get('requested_downloads.:.filepath')
    return isinstance(paths, list) and any(
        isinstance(p, str) and p for p in paths)


def _channel_name(info):
    for key in ('channel', 'uploader', 'playlist_title'):
        name = clean(info.get(key), 120)
        if name:
            return name
    return ''


class EventLog:
    """Bounded state built from the event file."""

    def __init__(self, status_dir):
        self.status_dir = status_dir
        self.reader = LineReader(os.path.join(status_dir, se.EVENTS_NAME),
                                 status_dir=True)
        self.loaded = False
        self.behind = False
        self.runs = collections.OrderedDict()      # run id -> record
        self.newest = None                         # newest run_start's id
        self.run_events = []                       # start/end of newest run
        self.run_names = {}                        # newest run: idx -> name
        self.items = collections.deque(maxlen=MAX_ITEMS)
        self.done = collections.deque()            # (epoch, src, id)
        self.channels = collections.OrderedDict()  # url -> statistics
        self.jobs = collections.OrderedDict()      # job hex -> record

    def newest_run(self):
        return self.runs.get(self.newest) if self.newest else None

    def refresh(self, now=None):
        """Read new events; returns them (parsed, valid ones only)."""
        now = time.time() if now is None else now
        budget = REFRESH_BYTES
        if not self.loaded:
            self.loaded = True
            budget = INITIAL_BYTES
            try:
                for event in se.read_events(self.status_dir,
                                            names=(se.ROTATED_NAME,)):
                    self._apply(event)
            except OSError:
                pass
        result = self.reader.read(budget)
        self.behind = result.more
        new = []
        for line in result.lines:
            event = se.parse_event(line)
            if event is not None:
                self._apply(event)
                new.append(event)
        self._prune(now)
        return new

    def _prune(self, now):
        horizon = now - HISTORY_DAYS * DAY
        while self.done and (self.done[0][0] < horizon
                             or len(self.done) > MAX_DONE):
            self.done.popleft()

    # -- applying events -------------------------------------------------

    def _apply(self, event):
        handler = {
            'run_start': self._run_start, 'run_end': self._run_end,
            'run_rows': self._run_rows,
            'start': self._video, 'end': self._video,
            'job_queued': self._job, 'job_start': self._job,
            'job_end': self._job,
        }.get(event['t'])
        if handler is not None:
            handler(event)

    def _run_start(self, event):
        run = _hex(event.get('run'))
        if run is None:
            return
        self.runs[run] = {
            'id': run, 'ts': _ts(event.get('ts')), 'pid': _int(event.get('pid')),
            'nonce': _hex(event.get('nonce')),
            'channels': _int(event.get('channels')),
            'interval': clean(event.get('interval'), 64),
            'ytdlp': clean(event.get('ytdlp'), 64),
            'ended': False, 'end_ts': None, 'rc': None,
            'next': None, 'partial': False, 'sum': None, 'rows': [],
            'err': []}
        self.runs.move_to_end(run)
        while len(self.runs) > MAX_RUNS:
            self.runs.popitem(last=False)
        self.newest = run
        self.run_events = []
        self.run_names = {}

    def _run_end(self, event):
        run = _hex(event.get('run'))
        if run is None:
            return
        record = self.runs.get(run)
        if record is None:
            # Its run_start was rotated away.
            record = self.runs[run] = {
                'id': run, 'ts': None, 'pid': None, 'nonce': None,
                'channels': None, 'interval': '', 'ytdlp': ''}
            self.runs.move_to_end(run, last=False)
            while len(self.runs) > MAX_RUNS:
                self.runs.popitem(last=False)
        if isinstance(event.get('ch'), list):
            rows = [r for r in map(_row, event['ch'][:MAX_ROWS]) if r]
        else:   # a large pass: the rows came before, in run_rows events
            rows = record.get('rows') or []
        errors = event.get('err') if isinstance(event.get('err'), list) else []
        record.update({
            'ended': True, 'end_ts': _ts(event.get('ts')),
            'rc': _int(event.get('rc')), 'next': _ts(event.get('next')),
            'partial': event.get('partial') is True,
            'sum': _sum(event.get('sum')),
            'rows': rows,
            'err': [e for e in map(_error, errors[:se.MAX_ERRORS]) if e]})
        names = self.run_names if run == self.newest else {}
        self._update_channels(record, names)
        if run == self.newest:
            self.run_events = []
            self.run_names = {}

    def _run_rows(self, event):
        """Channel rows of a large pass, written right before its run_end
        (see status_events.split_run_end)."""
        record = self.runs.get(_hex(event.get('run')))
        rows = event.get('ch')
        if record is None or record['ended'] or not isinstance(rows, list):
            return
        room = MAX_ROWS - len(record['rows'])
        record['rows'].extend(r for r in map(_row, rows[:max(0, room)]) if r)

    def _update_channels(self, record, names):
        for row in record['rows']:
            url = row['url']
            if not url:
                continue
            checked = row['ts'] or record['end_ts']
            stats = self.channels.get(url)
            if stats is None:
                stats = self.channels[url] = {
                    'url': url, 'name': '', 'first_seen': checked,
                    'last_checked': None, 'last_new': None, 'fail_streak': 0,
                    'last': None}
            self.channels.move_to_end(url)
            stats['last_checked'] = checked
            if names.get(row['idx']):
                stats['name'] = names[row['idx']]
            if row['new']:
                stats['last_new'] = checked
            errors = [e for e in record['err'] if e['idx'] == row['idx']]
            # Exit code 1 only because of members-only videos is no failure.
            row_ok = (row['rc'] in OK_CODES
                      or se.only_skipped(row['failed'], errors))
            if row['rc'] is not None:
                stats['fail_streak'] = (0 if row_ok
                                        else stats['fail_streak'] + 1)
            kinds = collections.Counter(e['kind'] for e in errors
                                        if e['kind'] not in se.SKIP_KINDS)
            stats['last'] = {
                'run': record['id'], 'idx': row['idx'], 'new': row['new'],
                'skipped': row['skipped'], 'failed': row['failed'],
                'filtered': row['filtered'], 'members': row['members'],
                'rc': row['rc'], 'ok': row_ok, 'secs': row['secs'],
                'kind': kinds.most_common(1)[0][0] if kinds else None,
                'entry_error': any(e['id'] is None for e in errors)}
        while len(self.channels) > MAX_CHANNELS:
            self.channels.popitem(last=False)

    def _video(self, event):
        src = event.get('src')
        run = _hex(event.get('run'))
        info = event.get('i')
        if (src not in se.SOURCES or run is None or not isinstance(info, dict)
                or not isinstance(info.get('id'), str)):
            return
        if src == 'scheduled' and run == self.newest:
            record = self.newest_run()
            if record is not None and not record['ended']:
                if len(self.run_events) < MAX_RUN_EVENTS:
                    self.run_events.append(event)
                ch = _int(event.get('ch'))
                name = _channel_name(info)
                if event['t'] == 'start' and ch is not None and name:
                    if ch in self.run_names or len(self.run_names) < MAX_ROWS:
                        self.run_names[ch] = name
        job = self.jobs.get(run) if src == 'manual' else None
        if job is not None and event['t'] == 'start':
            job['start'] = info
            if not job['title']:
                # A playlist job is named after the playlist.
                job['title'] = clean(
                    (info.get('playlist_title') if info.get('n_entries')
                     else '') or info.get('title'))
        if event['t'] != 'end':
            return
        done = _has_file(info)
        epoch = _ts(info.get('epoch'))
        if epoch is None:
            record = self.runs.get(run) if src == 'scheduled' else None
            epoch = (record or job or {}).get('ts')
        if job is not None:
            job['items_done' if done else 'items_failed'] += 1
        self.items.append({
            'src': src, 'run': run, 'id': clean(info['id'], 64),
            'title': clean(info.get('title')),
            'channel': clean(info.get('channel') or info.get('uploader'), 120),
            'ts': epoch, 'done': done, 'ch': _int(event.get('ch'))})
        if done and epoch is not None:
            self.done.append((epoch, src, info['id']))

    def _job(self, event):
        run = _hex(event.get('run'), _JOB_RE)
        if run is None:
            return
        job = self.jobs.get(run)
        if job is None:
            job = self.jobs[run] = {
                'id': job_uuid(run), 'url': None, 'by': None, 'title': '',
                'state': 'queued', 'queued': None, 'started': None,
                'ended': None, 'rc': None, 'error': None, 'items_done': 0,
                'items_failed': 0, 'ts': None, 'start': None}
            while len(self.jobs) > MAX_JOBS:
                self.jobs.popitem(last=False)
        ts = _ts(event.get('ts'))
        kind = event['t']
        if kind == 'job_queued':
            job['queued'] = job['ts'] = ts
            job['url'] = clean_url(event.get('url'))
            job['by'] = event.get('by') if event.get('by') in (
                'token', 'basic') else None
        elif kind == 'job_start':
            job['started'] = ts
            if job['state'] == 'queued':
                job['state'] = 'running'
        else:
            job['ended'] = ts
            state = event.get('state')
            job['state'] = state if state in JOB_STATES else 'failed'
            job['rc'] = _int(event.get('rc'))
            if event.get('title') and not job['title']:
                job['title'] = clean(event.get('title'))
            error = event.get('error')
            if isinstance(error, dict):
                error_kind = error.get('kind')
                job['error'] = {
                    'kind': (error_kind if error_kind in se.KIND_LABELS
                             else 'other'),
                    'msg': clean(error.get('msg'))}


# --------------------------------------------------------------------------
# Pass log

class PassLog:
    """Live view of the running (or interrupted) pass from its log."""

    def __init__(self, path):
        self.reader = LineReader(path)
        self.run = None
        self.nonce = None
        self.parser = None
        self.events = []                          # start/end events fed
        self.starts = collections.OrderedDict()   # id -> start event info
        self.behind = False
        self.rotation_ts = None   # last 'cookies rotated' warning seen
        # run id -> all its start/end events from the file, for a run with
        # more than MAX_RUN_EVENTS of them (set by Dashboard).
        self.reload = None

    def detach(self):
        self.run = self.nonce = self.parser = None
        self.events = []
        self.starts.clear()
        self.reader.close()
        self.reader = LineReader(self.reader.path)

    def attach(self, record, events):
        self.detach()
        self.run, self.nonce = record['id'], record['nonce']
        self._new_parser()
        self.add_events(events)

    def _new_parser(self):
        self.parser = se.LogParser(self.nonce or '', run=self.run)

    def add_events(self, events):
        if self.parser is None:
            return
        for event in events:
            if len(self.events) < MAX_RUN_EVENTS:
                self.events.append(event)
            info = event.get('i')
            if (event.get('t') == 'start' and isinstance(info, dict)
                    and isinstance(info.get('id'), str)):
                self.starts[info['id']] = info
                self.starts.move_to_end(info['id'])
                while len(self.starts) > 16:
                    self.starts.popitem(last=False)
        self.parser.add_events(events)

    def _all_events(self):
        if len(self.events) >= MAX_RUN_EVENTS and self.reload is not None:
            try:
                return self.reload(self.run)
            except OSError:
                pass
        return self.events

    def refresh(self, max_bytes, now):
        if self.parser is None:
            return
        result = self.reader.read(max_bytes)
        if result.reset:
            # tee started a new log: the lines read so far belong to
            # another pass. The events stay; the log is parsed again.
            self._new_parser()
            self.parser.add_events(self._all_events())
        self.behind = result.more
        for line in result.lines:
            self.parser.feed(line)
            entry = self.parser.current
            if (entry is not None and 'WARNING' in line
                    and se.classify(line) == 'cookies_rotated'):
                self.rotation_ts = entry.ts_start or int(now)


# --------------------------------------------------------------------------
# Derived values

def stream_weights(start):
    """(format ids, weights) of the formats a merged download fetches one
    after the other, from the parallel lists of the start event; None for a
    single format.

    The lists drop missing values, so a list is only used if it has one
    value per format; otherwise video streams get 90 % and audio-only
    streams 10 % (by vcodec), or the first stream 90 %."""
    if not isinstance(start, dict):
        return None
    ids = start.get('requested_formats.:.format_id')
    if (not isinstance(ids, list) or len(ids) < 2
            or not all(isinstance(i, str) for i in ids)):
        return None
    n = len(ids)
    for key in ('filesize', 'filesize_approx', 'tbr'):
        values = start.get('requested_formats.:.' + key)
        if (isinstance(values, list) and len(values) == n
                and all(_num(v) is not None and v > 0 for v in values)):
            return ids, [float(v) for v in values]
    vcodecs = start.get('requested_formats.:.vcodec')
    if isinstance(vcodecs, list) and len(vcodecs) == n:
        weights = [0.1 if v == 'none' else 0.9 for v in vcodecs]
    else:
        weights = [0.9] + [0.1 / (n - 1)] * (n - 1)
    return ids, weights


def _fraction(value):
    return None if value is None else min(1.0, max(0.0, value))


def video_progress(progress, start=None, phase=''):
    """Progress of the video being downloaded, from its last progress JSON
    and (for merged formats) its start event.

    Returns percent (of the whole video, or None if unknown), the current
    stream (index, count, kind) and the raw numbers for the label."""
    p = progress if isinstance(progress, dict) else {}
    downloaded = _num(p.get('progress.downloaded_bytes'))
    total = _num(p.get('progress.total_bytes'))
    estimate = False
    if not total:
        total = _num(p.get('progress.total_bytes_estimate'))
        estimate = bool(total)
    frag_i = _int(p.get('progress.fragment_index'))
    frag_n = _int(p.get('progress.fragment_count'))
    if p.get('progress.status') == 'finished':
        frac = 1.0
    elif frag_n and frag_i is not None:
        frac = frag_i / frag_n
    elif total and downloaded is not None:
        frac = downloaded / total
    else:
        frac = None
    frac = _fraction(frac)
    vcodec, acodec = p.get('info.vcodec'), p.get('info.acodec')
    kind = None
    if vcodec == 'none':
        kind = 'audio'
    elif isinstance(vcodec, str) and acodec == 'none':
        kind = 'video'
    elif isinstance(vcodec, str) and isinstance(acodec, str):
        kind = 'av'
    weights = stream_weights(start)
    fmt = p.get('info.format_id')
    index = count = None
    whole = frac          # fraction of the whole video
    floor = 0.0           # at least this much is done
    if weights and fmt in weights[0]:
        ids, w = weights
        k = ids.index(fmt)
        index, count = k + 1, len(ids)
        floor = sum(w[:k]) / sum(w)
        whole = (None if frac is None
                 else _fraction((sum(w[:k]) + w[k] * frac) / sum(w)))
    processing = phase.startswith(('processing: ', 'processed: '))
    if processing and p:
        # After the download. A postprocessor before any download progress
        # runs before the download (--convert-subs, SponsorBlock, ...): the
        # video is not done yet, and overall progress never moves back.
        fraction = 1.0
    else:
        fraction = floor if whole is None else whole
    return {
        'phase': 'processing' if processing else (
            'downloading' if p else 'preparing'),
        'postprocessor': clean(phase.split(': ', 1)[1], 40) if processing
        else None,
        'percent': None if processing or whole is None
        else round(whole * 100, 1),
        'stream_percent': None if frac is None else round(frac * 100, 1),
        'stream': {'index': index, 'count': count, 'kind': kind},
        'downloaded': downloaded, 'total': total, 'estimate': estimate,
        'speed': _num(p.get('progress.speed')),
        'eta': _num(p.get('progress.eta')),
        'fragment_index': frag_i, 'fragment_count': frag_n,
        'fraction': fraction,
    }


def youtube_id(item):
    """The YouTube video id of a failed item if an error line named it with
    the youtube extractor (what POST /api/download-now takes), else None."""
    vid = item.get('id')
    if (item.get('ie') == 'youtube' and isinstance(vid, str)
            and _YOUTUBE_ID_RE.match(vid)):
        return vid
    return None


def retry_url(item):
    """The URL 'Download now' would queue for a failed item, or None.

    A YouTube id from an error line becomes a watch URL; otherwise the
    item's http(s) URL (yt-dlp's webpage_url) is used. Both come from
    yt-dlp's output and can be chosen by an uploader, so this is only a
    suggestion: the route must validate it again and needs the user's click
    (CSRF)."""
    vid = youtube_id(item)
    if vid:
        return 'https://www.youtube.com/watch?v=' + vid
    url = item.get('url')
    if isinstance(url, str):
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.scheme in ('http', 'https') and parts.hostname:
            return url
    return None


def disk_usage(path):
    """Free and total bytes of the file system holding ``path``.

    The free space is rounded down to 1/1000 of the total (at least 1 MiB):
    any write on the disk changes the exact value, which would change the
    ETag of every status answer."""
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    if total <= 0:
        return None
    step = max(1024 * 1024, total // 1000)
    return {'free': free // step * step, 'total': total,
            'low': free < LOW_DISK_BYTES or free < total * LOW_DISK_RATIO}


def parse_cookies(data, now):
    """Names and expiry of the login cookies in a Netscape cookies.txt.

    Values are never looked at: each line is split into at most seven
    fields and the seventh (the value) is dropped unread."""
    names = set()
    expiries = []
    count = 0
    for raw in data.splitlines():
        line = raw.decode('utf-8', 'replace')
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        elif line.startswith('#') or not line.strip():
            continue
        fields = line.split('\t', 6)[:6]
        if len(fields) < 6:
            continue
        count += 1
        domain, expires, name = fields[0].lstrip('.').lower(), fields[4], fields[5]
        if name not in LOGIN_COOKIES or not any(
                domain == d or domain.endswith('.' + d) for d in COOKIE_DOMAINS):
            continue
        names.add(name)
        if expires.isdigit() and int(expires) > 0:
            expiries.append(int(expires))
    expires = min(expiries) if expiries else None
    return {'present': True, 'cookies': count, 'login': bool(names),
            'names': sorted(names), 'expires': expires,
            'expired': expires is not None and expires <= now}


class CookieFile:
    """Cookie health of /config/cookies.txt, parsed again when it changes."""

    def __init__(self, path):
        self.path = path
        self.key = None
        self.value = None

    def health(self, now):
        try:
            fd = _open_regular(self.path)
        except FileNotFoundError:
            return {'present': False}
        except OSError:
            return {'present': True, 'readable': False}
        try:
            st = os.fstat(fd)
            key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            if key != self.key:
                data = os.pread(fd, MAX_COOKIE_BYTES, 0)
                self.key, self.value = key, parse_cookies(data, now)
        finally:
            os.close(fd)
        value = dict(self.value)
        value['expired'] = (value['expires'] is not None
                            and value['expires'] <= now)
        return value


def script_alive(proc_dir, pid, since):
    """True if ``pid`` is youtube-dl.sh and was not started after ``since``
    (a PID reused after a container restart is another process)."""
    if not isinstance(pid, int) or pid < 1:
        return False
    base = os.path.join(proc_dir, str(pid))
    try:
        with open(os.path.join(base, 'cmdline'), 'rb') as f:
            args = f.read(8192).split(b'\0')
    except OSError:
        return False
    if not any(os.path.basename(a) == b'youtube-dl.sh' for a in args):
        return False
    if since is None:
        return True
    try:
        with open(os.path.join(base, 'stat'), 'rb') as f:
            data = f.read(4096)
        start_ticks = int(data[data.rindex(b')') + 2:].split()[19])
        with open(os.path.join(proc_dir, 'stat'), 'rb') as f:
            btime = next(int(line.split()[1]) for line in f
                         if line.startswith(b'btime '))
        started = btime + start_ticks / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError, StopIteration):
        return True
    return started <= since + 5


def etag_matches(header, etag):
    """True if an If-None-Match header lists ``etag`` (weak or strong)."""
    if not header:
        return False
    for candidate in header.split(','):
        candidate = candidate.strip()
        if candidate == '*' or candidate.removeprefix('W/') == etag:
            return True
    return False


# --------------------------------------------------------------------------
# Log text for the browser

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]')
_MARKER_RE = re.compile(
    r'^@@ytdl:(?P<nonce>[0-9a-f]*) (?P<kind>channel|pass_end)\b(?: (?P<json>.*))?$')
_WRITING_RE = re.compile(r'''^(?:\d+: )?\[info\] Writing '\{"v":1,"t":"''')
_POSTPROCESS_RE = re.compile(
    r'^(?:\d+: )?\[postprocess\] \S* [a-z]+ @@ytdl:[0-9a-f]*(?: \{.*)?$')
_SENTINEL_SUFFIX_RE = re.compile(r' ?@@ytdl:[0-9a-f]*(?: \{.*)?$')
_SENTINEL_RE = re.compile(r'@@ytdl:[0-9a-f]*')
_LOG_UNSAFE_RE = re.compile(
    '[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e'
    '\u2066-\u2069\ud800-\udfff]')


def scrub_log_line(line, nonce=None):
    """A pass or job log line as shown in the browser, or None to leave it
    out.

    Removes what the dashboard adds to the log: the nonce suffix of the
    progress lines (keeping yt-dlp's human text), the postprocess progress
    lines and the "[info] Writing '{"v":1,...' lines of the events. A
    channel start marker with the current ``nonce`` becomes a readable
    "[dashboard] Channel i of n" line; other markers are left out. Any other
    ``@@ytdl:<hex>`` (e.g. in --verbose's command-line dump) is masked, so no
    nonce ever reaches the browser. Also strips ANSI codes, control and
    bidirectional-override characters, and keeps only the last part of a
    line rewritten with carriage returns.
    """
    line = _ANSI_RE.sub('', line)
    if '\r' in line:
        parts = [part for part in line.split('\r') if part]
        line = parts[-1] if parts else ''
    match = _MARKER_RE.match(line)
    if match:
        if (nonce and match.group('nonce') == nonce
                and match.group('kind') == 'channel'):
            try:
                data = json.loads(match.group('json') or '')
            except ValueError:
                data = None
            if (isinstance(data, dict) and data.get('phase') == 'start'
                    and _int(data.get('index')) is not None):
                text = f'[dashboard] Channel {data["index"]}'
                if _int(data.get('total')) is not None:
                    text += f' of {data["total"]}'
                url = clean_url(data.get('url'))
                return text + (f': {url}' if url else '')
        return None
    if _WRITING_RE.match(line) or _POSTPROCESS_RE.match(line):
        return None
    line = _SENTINEL_SUFFIX_RE.sub('', line)
    line = _SENTINEL_RE.sub('@@ytdl:…', line)
    return _LOG_UNSAFE_RE.sub('', line.replace('\t', '    '))


def _scrub(lines, nonce):
    out = [scrub_log_line(line, nonce) for line in lines]
    text = '\n'.join(line for line in out if line is not None)
    return text + '\n' if text else ''


def _split_lines(data):
    """(lines, bytes consumed): complete lines of ``data``; lines longer
    than LOG_LINE_BYTES are replaced by a notice."""
    end = data.rfind(b'\n')
    if end < 0:
        return [], 0
    lines = []
    for raw in data[:end].split(b'\n'):
        if len(raw) > LOG_LINE_BYTES:
            lines.append(f'[a line of {len(raw) // 1024} KiB was left out]')
        else:
            lines.append(raw.decode('utf-8', 'replace'))
    return lines, end + 1


def _skip_line(fd, pos, size):
    """Position after the next newline from ``pos`` (or how far the scan got)
    and whether a newline was found."""
    scanned = 0
    while scanned < LOG_SCAN_BYTES and pos + scanned < size:
        chunk = os.pread(fd, min(65536, LOG_SCAN_BYTES - scanned),
                         pos + scanned)
        if not chunk:
            break
        nl = chunk.find(b'\n')
        if nl >= 0:
            return pos + scanned + nl + 1, True
        scanned += len(chunk)
    return pos + scanned, False


def read_log_since(path, since=None, nonce=None, limit=LOG_CHUNK_BYTES):
    """The part of a log after byte ``since``: {offset, reset, more, text}.

    * ``since`` None or negative, or beyond the end (the log was truncated):
      ``reset`` is true and the text is the last ``limit`` bytes.
    * Otherwise the text continues at ``since`` with at most ``limit``
      bytes; nothing new gives an empty text.
    * Only complete lines are returned and ``offset`` (pass it as the next
      ``since``) is always at a line start, except while a line longer than
      the scan limit is skipped. A ``since`` inside a line skips to the next
      one, so a cut line can never reveal part of a nonce.
    * ``more`` is true if more text is waiting after ``offset``.
    """
    reset = since is None or since < 0
    try:
        fd = _open_regular(path)
    except OSError:
        return {'offset': 0, 'reset': reset or since != 0, 'more': False,
                'text': ''}
    try:
        size = os.fstat(fd).st_size
        if not reset and since > size:
            reset = True
        start = max(0, size - limit) if reset else since
        lines = []
        if start > 0 and os.pread(fd, 1, start - 1) != b'\n':
            start, found = _skip_line(fd, start, size)
            if not found:
                return {'offset': start, 'reset': reset,
                        'more': start < size, 'text': ''}
        data = os.pread(fd, min(limit, size - start), start) if start < size \
            else b''
        lines, used = _split_lines(data)
        if not used and len(data) == limit:
            # One line longer than the limit: leave it out.
            end, found = _skip_line(fd, start, size)
            if found:
                lines = [f'[a line of {(end - start) // 1024} KiB was left out]']
            used = end - start
        offset = start + used
        return {'offset': offset, 'reset': reset, 'more': offset < size,
                'text': _scrub(lines, nonce)}
    finally:
        os.close(fd)


def read_log_tail(path, limit, nonce=None):
    """The last ``limit`` bytes of a log as scrubbed text (complete lines)."""
    return read_log_since(path, None, nonce, limit)['text']


# --------------------------------------------------------------------------
# Status

class Dashboard:
    """State of the dashboard and the builder of GET /api/status.

    One instance per web UI process (uvicorn runs one worker). ``status()``
    and ``status_json()`` may run in a worker thread; a lock serialises them.
    """

    def __init__(self, status_dir, pass_log, downloads_dir, cookies_file,
                 proc_dir='/proc', interval=None, clock=time.time):
        self.events = EventLog(status_dir)
        self.pass_log = PassLog(pass_log)
        self.pass_log.reload = self._run_events_from_file
        self.downloads_dir = downloads_dir
        self.cookies = CookieFile(cookies_file)
        self.proc_dir = proc_dir
        self.interval = interval
        self.clock = clock
        self.lock = threading.Lock()
        self.boot = secrets.token_hex(4)
        self.rev = 0
        self._digest = None
        self._overall = (None, 0.0)   # (run, highest overall shown so far)

    # -- refresh ---------------------------------------------------------

    def _refresh(self, now):
        new = self.events.refresh(now)
        record = self.events.newest_run()
        if record is None or record['ended']:
            if self.pass_log.parser is not None:
                self.pass_log.detach()
            return
        budget = REFRESH_BYTES
        if self.pass_log.run != record['id']:
            events = self.events.run_events
            if len(events) >= MAX_RUN_EVENTS:     # the list is capped
                try:
                    events = self._run_events_from_file(record['id'])
                except OSError:
                    pass
            self.pass_log.attach(record, events)
            budget = INITIAL_BYTES
        else:
            self.pass_log.add_events([
                e for e in new if e.get('t') in ('start', 'end')
                and e.get('src') == 'scheduled' and e.get('run') == record['id']])
        self.pass_log.refresh(budget, now)

    def _run_events_from_file(self, run):
        return [e for e in se.read_events(self.events.status_dir, run=run)
                if e.get('t') in ('start', 'end')
                and e.get('src') == 'scheduled' and e.get('run') == run]

    def log_nonce(self):
        """Nonce of the newest pass (to recognise its markers in the log);
        stays on the server."""
        with self.lock:
            self._refresh(self.clock())
            record = self.events.newest_run()
            return record['nonce'] if record else None

    def pass_running(self):
        """True while the scheduled pass runs (its run_start has no run_end
        and youtube-dl.sh is alive): 'Run now' then asks first."""
        with self.lock:
            self._refresh(self.clock())
            record = self.events.newest_run()
            return bool(record is not None and not record['ended']
                        and script_alive(self.proc_dir, record['pid'],
                                         record['ts']))

    def known_title(self, video_id):
        """Title of a recently downloaded video (from the events already
        read), or ''."""
        with self.lock:
            for item in reversed(self.events.items):
                if item['id'] == video_id and item['title']:
                    return item['title']
        return ''

    # -- building --------------------------------------------------------

    def status_json(self, jobs=None):
        """(ETag, JSON bytes) of the status.

        The ETag is "<boot>-<rev>": ``boot`` is random per process, ``rev``
        counts changes of the content (everything but ``now``)."""
        with self.lock:
            now = self.clock()
            self._refresh(now)
            body = json.dumps(self._build(now, jobs), sort_keys=True,
                              separators=(',', ':'), allow_nan=False).encode()
            digest = hashlib.blake2b(body, digest_size=16).digest()
            if digest != self._digest:
                self._digest = digest
                self.rev += 1
            etag = f'"{self.boot}-{self.rev}"'
        # The body is a JSON object: add "now" without hashing it.
        return etag, body[:-1] + b',"now":%d}' % int(now)

    def status(self, jobs=None):
        return json.loads(self.status_json(jobs)[1])

    def _interval_text(self, record):
        if self.interval:
            return self.interval
        return record['interval'] if record and record['interval'] else '3h'

    def _build(self, now, jobs):
        events = self.events
        record = events.newest_run()
        interval = self._interval_text(record)
        oneshot = interval == 'false'
        running = interrupted = False
        if record is not None and not record['ended']:
            running = script_alive(self.proc_dir, record['pid'], record['ts'])
            interrupted = not running
        if record is None:
            state = 'nodata'
        elif running:
            state = 'running'
        elif interrupted:
            state = 'interrupted'
        elif oneshot:
            state = 'oneshot'
        elif script_alive(self.proc_dir, record['pid'], record['ts']):
            state = 'sleeping'
        else:
            # The script died or was stopped after its run_end.
            state = 'stopped'
        current = (self._current(record, now, running)
                   if running or interrupted else None)
        errors = []
        if current is not None:
            errors = current.pop('errors')
        last = self._last_run()
        jobs_view = self._jobs(jobs)
        problems, bot_checks = self._problems(
            current, errors, last, jobs_view, now)
        return {
            'v': 1,
            'boot': self.boot,
            'catching_up': events.behind or self.pass_log.behind,
            'scheduler': {
                'state': state, 'oneshot': oneshot,
                'interval': clean(interval, 64),
                'interval_secs': se.parse_interval(interval),
                'next': record['next'] if state == 'sleeping' else None,
                'since': record['ts'] if running else None,
                'ytdlp': record['ytdlp'] if record else '',
            },
            'current': current,
            'last_run': last,
            'runs': self._runs(running),
            'counts': self._counts(now),
            'recent': self._recent(),
            'channels': self._channels(now),
            'problems': problems,
            'pending': self._pending(current, jobs_view),
            'disk': disk_usage(self.downloads_dir),
            'cookies': self._cookies(now, bot_checks),
            'jobs': jobs_view,
        }

    def _run_summary(self, record, running=False):
        result = None
        if record['ended']:
            result = ('ok' if record['rc'] in OK_CODES or se.only_skipped(
                record['sum'].get('failed'), record['err']) else 'failed')
        elif running:
            result = 'running'
        else:
            result = 'interrupted'
        duration = None
        if record['ts'] and record['end_ts']:
            duration = max(0, record['end_ts'] - record['ts'])
        return {'id': record['id'], 'started': record['ts'],
                'ended': record['end_ts'], 'duration': duration,
                'rc': record['rc'], 'partial': record['partial'],
                'sum': record['sum'], 'result': result,
                'channels': record['channels']}

    def _last_run(self):
        """The newest finished run (not the running one)."""
        for record in reversed(self.events.runs.values()):
            if record['ended']:
                return self._run_summary(record)
        return None

    def _runs(self, running):
        out = []
        newest = self.events.newest
        for record in reversed(self.events.runs.values()):
            out.append(self._run_summary(
                record, running and record['id'] == newest))
            if len(out) >= SHOWN_RUNS:
                break
        return out

    def _current(self, record, now, running):
        parser = self.pass_log.parser
        total = (parser.total if parser and parser.total else None) \
            or record['channels']
        entry = parser.current if parser else None
        if entry is not None and entry.index is None:
            entry = None
        totals = {'new': 0, 'skipped': 0, 'failed': 0, 'filtered': 0,
                  'members': 0}
        errors = []
        done_idx = 0
        if parser is not None:
            for index, item in parser.entries.items():
                if index is None:
                    continue
                result = parser.entry_result(item, live=not item.ended)
                for key in totals:
                    totals[key] += result[key]
                ts = item.ts_end or item.ts_start or record['ts']
                for err in result['errors']:
                    errors.append(dict(err, ts=ts, src='scheduled'))
                if item.ended:
                    done_idx = max(done_idx, index)
        channel = video = item_pos = None
        inner = 0.0
        # Between two channels: keep showing the last one until the next
        # channel-start marker.
        shown = entry
        if shown is None and parser is not None and done_idx:
            shown = parser.entries.get(done_idx)
        if shown is not None:
            name = (self.events.run_names.get(shown.index)
                    or clean(shown.playlist, 120))
            channel = {'index': shown.index, 'total': total,
                       'url': clean_url(shown.url), 'name': name}
        if entry is not None:
            video, item_pos, inner = self._video(entry)
            if not channel['name'] and video and video.get('playlist'):
                channel['name'] = video['playlist']
            position = entry.index - 1 + inner
            channels_left = (total - entry.index) if total else None
        else:
            position = done_idx
            channels_left = (total - done_idx) if total else None
        overall = None
        if total:
            # Never backwards within a run: between items or videos the
            # inner fraction falls back for a moment.
            overall = round(100 * min(1.0, position / total), 1)
            run, highest = self._overall
            if run == record['id']:
                overall = max(overall, highest)
            self._overall = (record['id'], overall)
        in_channel = None
        if item_pos and item_pos['count']:
            in_channel = max(0, item_pos['count'] - item_pos['index'])
        return {
            'run': record['id'], 'started': record['ts'],
            'state': 'running' if running else 'interrupted',
            'channels': total, 'channel': channel, 'overall': overall,
            'item': item_pos, 'video': video, 'counts': totals,
            'channels_left': None if channels_left is None
            else max(0, channels_left),
            'in_channel_max': in_channel,
            'errors': errors,
        }

    def _video(self, entry):
        """(video, item position, inner fraction of the channel)."""
        vid = entry.open
        progress = entry.progress if (
            entry.progress and vid and entry.progress.get('info.id') == vid
        ) else None
        start = self.pass_log.starts.get(vid) if vid else None
        known = entry.videos.get(vid) if vid else None
        video = None
        frac = 0.0
        if vid and (progress or start or known):
            phase = entry.phase if entry.phase_id == vid else ''
            info = progress or {}
            pick = (lambda *values: next((v for v in values if v), ''))
            title = pick(info.get('info.title'), (start or {}).get('title'),
                         (known or {}).get('title'))
            channel = pick(info.get('info.channel'), info.get('info.uploader'),
                           (start or {}).get('channel'),
                           (start or {}).get('uploader'),
                           (known or {}).get('channel'))
            prog = video_progress(progress, start, phase)
            frac = prog.pop('fraction') or 0.0
            video = dict(prog, id=clean(vid, 64), title=clean(title),
                         channel=clean(channel, 120),
                         playlist=clean(pick(info.get('info.playlist_title'),
                                             (start or {}).get('playlist_title')),
                                        120))
        auto = count = None
        source = None
        for data, prefix, src in ((progress, 'info.', 'events'),
                                  (start, '', 'events'), (known, '', 'events')):
            if data:
                auto = _int(data.get(prefix + 'playlist_autonumber'))
                count = _int(data.get(prefix + 'n_entries'))
                if auto and count:
                    source = src
                    break
        if not (auto and count) and entry.item:
            auto, count, source = entry.item, entry.n_items, 'log'
        if not (auto and count):
            # A single video (no playlist position): its own progress.
            return video, None, min(1.0, frac)
        auto = min(auto, count)
        inner = min(1.0, (auto - 1 + frac) / count)
        return video, {'index': auto, 'count': count, 'source': source}, inner

    def _counts(self, now):
        """Distinct (source, id) downloaded per 24 h window, newest last.

        ``epoch`` is yt-dlp's extraction time, a little before the end of
        the download; an epoch in the future (clock skew) counts as now."""
        days = [set() for _ in range(HISTORY_DAYS)]
        for epoch, src, vid in self.events.done:
            age = int((now - min(epoch, now)) // DAY)
            if age < HISTORY_DAYS:
                days[age].add((src, vid))
        last = days[0]
        return {'last_24h': {
            'total': len(last),
            'scheduled': sum(1 for src, _ in last if src == 'scheduled'),
            'manual': sum(1 for src, _ in last if src == 'manual')},
            'daily': [len(day) for day in reversed(days)]}

    def _recent(self):
        out = []
        for item in reversed(self.events.items):
            if item['done']:
                out.append({key: item[key] for key in (
                    'src', 'id', 'title', 'channel', 'ts')})
                if len(out) >= SHOWN_RECENT:
                    break
        return out

    def _channels(self, now):
        latest = next((r for r in reversed(self.events.runs.values())
                       if r['ended'] and r['rows']), None)
        if latest is None:
            return []
        out = []
        for row in latest['rows']:
            stats = self.events.channels.get(row['url']) if row['url'] else None
            if stats is None:
                continue
            last = stats['last'] or {}
            rc_bad = (last.get('rc') not in OK_CODES + (None,)
                      and not last.get('ok'))
            if last.get('entry_error') or (
                    rc_bad and not last.get('failed') and not last.get('new')):
                status = 'error'
            elif last.get('failed') and rc_bad:
                # With exit code 0 the failures can only come from log lines
                # a video title forged.
                status = 'failed'
            elif (stats['first_seen'] and now - stats['first_seen'] > STALE_DAYS * DAY
                  and (not stats['last_new']
                       or now - stats['last_new'] > STALE_DAYS * DAY)):
                status = 'stale'
            else:
                status = 'ok'
            out.append({
                'idx': row['idx'], 'url': stats['url'], 'name': stats['name'],
                'last_checked': stats['last_checked'],
                'last_new': stats['last_new'],
                'fail_streak': stats['fail_streak'], 'status': status,
                'last': {key: last.get(key) for key in (
                    'new', 'skipped', 'failed', 'filtered', 'members', 'rc',
                    'kind')}})
        order = {'error': 0, 'failed': 1, 'stale': 2, 'ok': 3}
        out.sort(key=lambda c: (order[c['status']], c['idx']))
        return out

    def _jobs(self, jobs):
        """Manual jobs: ``jobs`` is the job manager's snapshot of running
        and queued jobs (jobs.JobManager.snapshot); finished jobs come from
        the events, without those the snapshot still lists."""
        jobs = jobs or {}
        running = [self._running_job(job) for job in jobs.get('running', ())]
        queued = [self._queued_job(job) for job in jobs.get('queued', ())]
        active = {job['id'] for job in running + queued}
        finished = []
        for job in reversed(self.events.jobs.values()):
            if job['ended'] is None or job['id'] in active:
                continue
            finished.append({
                'id': job['id'], 'state': job['state'], 'url': job['url'],
                'src': _JOB_SOURCES.get(job['by']),
                'title': job['title'], 'queued': job['queued'],
                'started': job['started'], 'ended': job['ended'],
                'rc': job['rc'], 'error': job['error'],
                'items': {'done': job['items_done'],
                          'failed': job['items_failed']}})
            if len(finished) >= SHOWN_JOBS:
                break
        return {'running': running, 'queued': queued, 'finished': finished}

    @staticmethod
    def _queued_job(job):
        return {'id': clean(job.get('id'), 36), 'state': 'queued',
                'url': clean_url(job.get('url')),
                'src': _JOB_SOURCES.get(job.get('by')),
                'force': job.get('force') is True,
                'queued': _ts(job.get('queued')),
                'position': _int(job.get('position'))}

    def _running_job(self, job):
        """A running job: its live progress (from the job's output, nonce
        lines only) with the counts and title from its events."""
        job_id = clean(job.get('id'), 36)
        record = self.events.jobs.get(job_id.replace('-', '')) or {}
        start = record.get('start') or {}
        vid = job.get('open') if isinstance(job.get('open'), str) else None
        progress = job.get('progress')
        if not (isinstance(progress, dict) and vid
                and progress.get('info.id') == vid):
            progress = None
        if start.get('id') != vid:
            start = {}
        video = None
        if vid and (progress or start):
            phase = job.get('phase') if job.get('phase_id') == vid else ''
            info = progress or {}
            pick = (lambda *values: next((v for v in values if v), ''))
            prog = video_progress(progress, start or None,
                                  phase if isinstance(phase, str) else '')
            prog.pop('fraction')
            video = dict(
                prog, id=clean(vid, 64),
                title=clean(pick(info.get('info.title'), start.get('title'))),
                channel=clean(pick(info.get('info.channel'),
                                   info.get('info.uploader'),
                                   start.get('channel'),
                                   start.get('uploader')), 120),
                playlist=clean(pick(info.get('info.playlist_title'),
                                    start.get('playlist_title')), 120))
        item = None
        for data, prefix in ((progress, 'info.'), (start, '')):
            auto = _int((data or {}).get(prefix + 'playlist_autonumber'))
            count = _int((data or {}).get(prefix + 'n_entries'))
            if auto and count:
                item = {'index': min(auto, count), 'count': count,
                        'source': 'events'}
                break
        if item is None and _int(job.get('item')) and _int(job.get('n_items')):
            item = {'index': min(job['item'], job['n_items']),
                    'count': job['n_items'], 'source': 'log'}
        title = record.get('title') or (video or {}).get('playlist') \
            or clean(job.get('playlist')) or (video or {}).get('title') or ''
        return {
            'id': job_id, 'state': 'running', 'url': clean_url(job.get('url')),
            'src': _JOB_SOURCES.get(job.get('by')), 'title': title,
            'force': job.get('force') is True,
            'queued': _ts(job.get('queued')), 'started': _ts(job.get('started')),
            'ended': None, 'rc': None, 'error': None,
            'cancelling': job.get('cancelling') is True,
            'video': video, 'item': item,
            'items': {'done': record.get('items_done', 0),
                      'failed': record.get('items_failed', 0)}}

    def _problems(self, current, errors, last, jobs_view, now):
        items = [dict(e, run=current['run']) for e in errors]
        record = self.events.runs.get(last['id']) if last else None
        if record:
            rows = {row['idx']: row for row in record['rows']}
            for err in record['err']:
                row = rows.get(err['idx']) or {}
                items.append(dict(err, src='scheduled', run=record['id'],
                                  ts=row.get('ts') or record['end_ts']))
        for job in jobs_view.get('finished', []):
            error = job.get('error')
            if job.get('state') in ('failed', 'partial') and error:
                items.append({'idx': None, 'id': None, 'kind': error['kind'],
                              'msg': error['msg'], 'title': job.get('title'),
                              'url': job.get('url'), 'ie': '', 'src': 'manual',
                              'run': job['id'], 'ts': job.get('ended')})
        groups = {}
        seen = set()
        for item in items:
            if item['kind'] in se.SKIP_KINDS:
                continue   # skipped on purpose (members-only), not a problem
            key = (item['kind'], item['src'], item.get('id') or item['msg'])
            if key in seen:
                continue
            seen.add(key)
            groups.setdefault(item['kind'], []).append(item)
        out = []
        for kind, members in groups.items():
            members.sort(key=lambda m: m.get('ts') or 0, reverse=True)
            out.append({
                'kind': kind, 'label': se.KIND_LABELS[kind],
                'hint': se.KIND_HINTS[kind],
                'severity': KIND_SEVERITY.get(kind, 'warning'),
                'count': len(members),
                'last': max((m.get('ts') or 0 for m in members), default=0)
                or None,
                'items': [{
                    'id': m.get('id'), 'title': clean(m.get('title')),
                    'src': m['src'], 'channel': m.get('idx'),
                    'ts': m.get('ts'), 'msg': clean(m.get('msg')),
                    'url': retry_url(m), 'youtube_id': youtube_id(m)}
                    for m in members[:MAX_GROUP_ITEMS]],
            })
        out.sort(key=lambda g: (_SEVERITY_ORDER[g['severity']], -g['count']))
        # Bot checks in the last 24 h, over all known runs (cookie card).
        bot = [i.get('ts') for i in items if i['kind'] == 'bot_check']
        for record in self.events.runs.values():
            if last and record['id'] == last['id']:
                continue
            rows = {row['idx']: row for row in record['rows']}
            for err in record['err']:
                if err['kind'] == 'bot_check':
                    bot.append((rows.get(err['idx']) or {}).get('ts')
                               or record['end_ts'])
        bot = [ts for ts in bot if ts and ts > now - DAY]
        return out, bot

    def _pending(self, current, jobs_view):
        return {
            'queued': len(jobs_view.get('queued', [])),
            'channels_left': current['channels_left'] if current else None,
            'in_channel_max': current['in_channel_max'] if current else None,
        }

    def _cookies(self, now, bot_checks):
        health = self.cookies.health(now)
        rotations = [self.pass_log.rotation_ts] if self.pass_log.rotation_ts \
            else []
        for record in self.events.runs.values():
            rows = {row['idx']: row for row in record['rows']}
            for err in record['err']:
                if err['kind'] == 'cookies_rotated':
                    rotations.append((rows.get(err['idx']) or {}).get('ts')
                                     or record['end_ts'])
        rotations = [ts for ts in rotations if ts]
        health.update({
            'bot_checks_24h': len(bot_checks),
            'last_bot_check': max(bot_checks) if bot_checks else None,
            'last_rotation': max(rotations) if rotations else None})
        return health
