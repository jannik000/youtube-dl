"""Status events for the web UI's dashboard (standard library only).

youtube-dl.sh runs this file as ``python3 -I status_events.py <command>``
(like ``ytdlp_args.py split``); the web UI imports it. It is the one place
that defines the event schema, the yt-dlp flags producing the events and the
parser reading them back.

How the events are produced:

* yt-dlp appends a ``start`` and an ``end`` event per video to
  ``events.jsonl`` with ``--print-to-file`` (stages ``video`` and
  ``after_video``). Unlike ``--print``, ``--print-to-file`` does not imply
  ``--quiet``, so the human log keeps its archive, filter and error lines.
  An ``end`` with a file path means the video was downloaded; a ``start``
  without such an ``end`` means it failed.
* Live progress is a JSON suffix on yt-dlp's normal progress lines, after the
  sentinel ``@@ytdl:<nonce>``. A video title can inject whole lines into the
  log, so only lines carrying the nonce of the current pass or job are
  trusted. The nonce is random per pass/job and never sent to a browser.
* youtube-dl.sh brackets every channels.txt entry with ``channel`` marker
  lines (same sentinel, printed by ``channel-start``/``channel-end``) and
  calls ``begin`` and ``end`` around the pass; ``end`` summarises the pass
  log into a ``run_end`` event.

Every producer fails open: if a command fails, the script runs yt-dlp without
the event flags, exactly as it does with the web UI disabled.

Only the unprivileged user writes here, never root: ``/config/.youtube-dl-
status/`` (mode 0700) holds ``events.jsonl`` (0600, JSON Lines, ``"v": 1``,
unknown or invalid lines are ignored by readers) and one rotated
``events.jsonl.1``. Files are opened with ``O_NOFOLLOW`` relative to a
directory descriptor opened with ``O_NOFOLLOW``; our own events are appended
with a single ``write()`` on an ``O_APPEND`` descriptor.

Do not change the yt-dlp flags without the contract test
(tests/test_ytdlp_contract.py). Verified traps: ``--print`` implies quiet
(and simulate for ``video:`` alone); the ``playlist:`` print stage keeps every
entry in memory; a dict nested in a dict template (``.{a,b.:.{c}}``) turns the
whole field into a bare ``NA``; ``%(x)j`` of a single missing value prints
``NA``.
"""

import argparse
import collections
import json
import math
import os
import re
import secrets
import stat
import sys
import time

SCHEMA = 1
STATUS_DIR = '/config/.youtube-dl-status'
EVENTS_NAME = 'events.jsonl'
ROTATED_NAME = EVENTS_NAME + '.1'
ROTATE_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 32 * 1024    # our own events; run_end drops errors to fit
MAX_LINE_BYTES = 256 * 1024    # longer lines in events.jsonl or a log are skipped
MAX_ERRORS = 50                # classified errors kept per run_end
MAX_TEXT = 300                 # characters kept of a message or title
MAX_URL = 2048
DEFAULT_INTERVAL = 3 * 3600    # youtube-dl.sh sleeps 3h if the interval is invalid
END_WAIT = 5.0                 # seconds 'end' waits for tee to write pass_end
SENTINEL = '@@ytdl:'
SOURCES = ('scheduled', 'manual')

_ID_RE = re.compile(r'[0-9a-f]{16,32}\Z')

# Fields of the progress JSON (prefix progress./info.) and of the events.
PROGRESS_FIELDS = (
    'progress.status', 'progress.downloaded_bytes', 'progress.total_bytes',
    'progress.total_bytes_estimate', 'progress.speed', 'progress.eta',
    'progress.fragment_index', 'progress.fragment_count', 'info.id',
    'info.title', 'info.channel', 'info.uploader', 'info.format_id',
    'info.vcodec', 'info.acodec', 'info.playlist_title',
    'info.playlist_autonumber', 'info.n_entries')
POSTPROCESS_FIELDS = ('progress.status', 'progress.postprocessor', 'info.id')
# Parallel lists (requested_formats.:.x), never a dict nested in the dict.
START_FIELDS = (
    'id', 'title', 'channel', 'uploader', 'webpage_url', 'duration', 'epoch',
    'live_status', 'playlist_title', 'playlist_autonumber', 'n_entries',
    'requested_formats.:.format_id', 'requested_formats.:.vcodec',
    'requested_formats.:.filesize', 'requested_formats.:.filesize_approx',
    'requested_formats.:.tbr')
END_FIELDS = ('id', 'title', 'channel', 'epoch',
              'requested_downloads.:.filepath')

# yt-dlp's webpage_url of a YouTube video, followed by its id.
YOUTUBE_WATCH = 'https://www.youtube.com/watch?v='

# Layout of the per-channel rows in run_end.ch.
CHANNEL_ROW = ('idx', 'new', 'skipped', 'failed', 'filtered', 'rc', 'secs',
               'ts', 'url', 'members')


def new_id():
    """A random run id or nonce (16 hex characters)."""
    return secrets.token_hex(8)


def check_id(value, what='id'):
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f'{what} must be 16-32 lowercase hex characters')
    return value


def _dict_template(fields):
    # '%(.{a,b})j': one line of ASCII JSON, newlines escaped, missing keys
    # left out.
    return '%(.{' + ','.join(fields) + '})j'


def ytdlp_flags(src, run, nonce, events_file, ch=None):
    """The yt-dlp arguments producing the events of one pass entry or job.

    They go after the user's arguments (args.conf, channels.txt '| args') and
    before '--': command-line options win over args.conf and later ones over
    earlier ones, so a user's --quiet, --no-progress, --progress-delta or
    --color cannot hide the progress lines.
    """
    if src not in SOURCES:
        raise ValueError(f'src must be one of {SOURCES}')
    check_id(run, 'run')
    check_id(nonce, 'nonce')
    if ch is not None and (isinstance(ch, bool) or not isinstance(ch, int)
                           or ch < 1):
        raise ValueError('ch must be a positive integer')
    # The FILE of --print-to-file is an output template: no '%' allowed.
    if (not isinstance(events_file, str) or not os.path.isabs(events_file)
            or any(c in events_file for c in '%\n\r\0')):
        raise ValueError('events_file must be an absolute path without "%"')

    def event_template(kind, fields):
        head = '{"v":%d,"t":"%s","src":"%s","run":"%s"' % (SCHEMA, kind, src, run)
        if ch is not None:
            head += ',"ch":%d' % ch
        return head + ',"i":' + _dict_template(fields) + '}'

    sentinel = SENTINEL + nonce
    return [
        '--newline', '--progress', '--progress-delta', '2', '--color', 'never',
        '--progress-template',
        'download:[download] %(progress._default_template)s ' + sentinel
        + ' ' + _dict_template(PROGRESS_FIELDS),
        '--progress-template',
        'postprocess:[postprocess] %(progress.postprocessor)s '
        '%(progress.status)s ' + sentinel + ' '
        + _dict_template(POSTPROCESS_FIELDS),
        '--print-to-file', 'video:' + event_template('start', START_FIELDS),
        events_file,
        '--print-to-file', 'after_video:' + event_template('end', END_FIELDS),
        events_file,
    ]


def events_path(status_dir=STATUS_DIR):
    return os.path.join(status_dir, EVENTS_NAME)


# --------------------------------------------------------------------------
# Text clean-up

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]')
# C0/C1 controls, bidirectional overrides/isolates and marks, lone surrogates.
_UNSAFE_RE = re.compile(
    '[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e'
    '\u2066-\u2069\ud800-\udfff]')

# The sentinel and whatever hex follows it: a nonce must never be stored or
# shown, even inside an error message that merely contains it.
_SENTINEL_TEXT_RE = re.compile(r'@@ytdl:[0-9a-fA-F]*')


def clean_text(value, limit=MAX_TEXT):
    """Make a string safe to store and show as text: no ANSI codes, control
    or bidirectional-override characters, tabs and newlines as spaces, any
    ``@@ytdl:<hex>`` masked, at most ``limit`` characters. Non-strings
    become ''."""
    if not isinstance(value, str):
        return ''
    value = _ANSI_RE.sub('', value)
    value = _SENTINEL_TEXT_RE.sub('@@ytdl:\u2026', value)
    value = value.replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')
    value = _UNSAFE_RE.sub('', value).strip()
    if len(value) > limit:
        value = value[:limit - 1] + '…'
    return value


# --------------------------------------------------------------------------
# Interval (same semantics as youtube-dl.sh)

# One GNU sleep operand: a C strtod() number (decimal, hex or infinity) and
# at most one unit character.
_SLEEP_OPERAND_RE = re.compile(r'''
    [ \t\n\v\f\r]*
    (?P<num>[+-]?(?:
        0[xX](?:[0-9a-fA-F]+\.?[0-9a-fA-F]*|\.[0-9a-fA-F]+)(?:[pP][+-]?\d+)?
      | (?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?
      | (?i:inf(?:inity)?)
    ))
    (?P<unit>[smhd]?)\Z''', re.X)
_UNIT_SECONDS = {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}


def parse_interval(text):
    """Seconds youtube-dl.sh sleeps after a pass with youtubedl_interval=text.

    None means there is no next pass: 'false' (one pass, then the container
    exits) or an infinite sleep. Like the script, the words are split on
    spaces, tabs and newlines and handed to GNU sleep, which adds them up and
    rejects the whole list if one word is invalid; the script then sleeps the
    default 3h instead.
    """
    if text == 'false':
        return None
    words = [w for w in re.split('[ \t\n]+', text or '') if w]
    if not words:
        return float(DEFAULT_INTERVAL)
    total = 0.0
    for word in words:
        match = _SLEEP_OPERAND_RE.match(word)
        if not match:
            return float(DEFAULT_INTERVAL)
        num = match.group('num')
        if num.startswith('-'):   # GNU sleep rejects even '-0'
            return float(DEFAULT_INTERVAL)
        try:
            value = (float.fromhex(num) if 'x' in num.lower()
                     else float(num))
        except (OverflowError, ValueError):
            value = math.inf
        total += value * _UNIT_SECONDS[match.group('unit')]
    return None if math.isinf(total) else total


# --------------------------------------------------------------------------
# Error classification

KIND_LABELS = {
    'bot_check': 'Bot check',
    'rate_limit': 'Rate-limited',
    'forbidden': 'Forbidden',
    'unavailable': 'Unavailable',
    'members_only': 'Members-only',
    'private_members': 'Private or age-restricted',
    'geo': 'Blocked in this country',
    'disk_full': 'Disk full',
    'cookies_rotated': 'Cookies expired',
    'other': 'Other error',
}
KIND_HINTS = {
    'bot_check': 'YouTube wants a signed-in session: export fresh cookies to '
                 '/config/cookies.txt from a private browser window.',
    'rate_limit': 'YouTube is limiting requests from this server: wait about '
                  'an hour and keep the interval at 3h or more.',
    'forbidden': 'The server refused the download (HTTP 403); this usually '
                 'passes after a while or with a newer yt-dlp.',
    'unavailable': 'The video was removed or is not out yet; nothing to fix '
                   'unless the URL is wrong.',
    'members_only': 'Only members of the channel can watch it, so it is '
                    'skipped rather than failed; cookies of a member account '
                    'in /config/cookies.txt would download it.',
    'private_members': 'The video is private or age-restricted and needs '
                       'cookies from an account that may watch it.',
    'geo': "The video is blocked in this server's country and cannot be "
           'downloaded from here.',
    'disk_full': 'The disk is full: free space under /downloads, then retry.',
    'cookies_rotated': 'YouTube no longer accepts /config/cookies.txt: export '
                       'it again from a private browser window and close that '
                       'window afterwards.',
    'other': 'See the log for the full message.',
}
# Most specific cause first: rotated cookies or a bot check explain the
# 'No video formats found' or 403 that follows them.
_KIND_RULES = tuple((kind, re.compile(pattern, re.I)) for kind, pattern in (
    ('cookies_rotated', r'cookies are no longer valid|cookies? (?:have|has) '
                        r'(?:likely )?been rotated'),
    ('disk_full', r'No space left on device|\[Errno 28\]|Disk quota exceeded'),
    ('bot_check', r'not a bot\b'),
    ('rate_limit', r'HTTP Error 429|Too Many Requests|rate[- ]limit|'
                   r'try again later'),
    ('members_only', r'members[- ]only|Join this channel|channel.s members|'
                     r'available to (?:this )?channel members'),
    ('private_members', r'Private video|This video is private|'
                        r'confirm your age|age[- ]restricted|'
                        r'inappropriate for some users'),
    ('geo', r'available in your country|geo[- ]?restrict|'
            r'blocked it in your country'),
    ('forbidden', r'HTTP Error 403|\bForbidden\b'),
    ('unavailable', r'Video unavailable|has been removed|no longer available|'
                    r'does not exist|HTTP Error 404|\bNot Found\b|'
                    r'has been terminated|This video is not available|'
                    r'Premieres in|live event will begin'),
))


# Kinds that are not failures: yt-dlp reports them as errors (and exits 1),
# but nothing is wrong and nothing can be fixed from here. They are counted
# as 'members' instead of 'failed' and keep a run or a job 'ok'.
SKIP_KINDS = frozenset({'members_only'})


def only_skipped(failed, errors):
    """True if a non-zero exit code is explained by skipped videos alone:
    nothing failed and every error is of a SKIP_KINDS kind."""
    errors = [e for e in errors or () if isinstance(e, dict)]
    return (not failed and bool(errors)
            and all(e.get('kind') in SKIP_KINDS for e in errors))


def classify_detail(msg, warnings=()):
    """(kind, text): the kind of a failure and the message that shows it.

    ``warnings`` are the WARNING lines seen for the same video; the bot
    check, for example, is only a warning, followed by the error 'No video
    formats found!'.
    """
    texts = [t for t in (msg, *warnings) if isinstance(t, str) and t]
    for kind, pattern in _KIND_RULES:
        for text in texts:
            if pattern.search(text):
                return kind, text
    return 'other', (msg if isinstance(msg, str) else '')


def classify(msg, warnings=()):
    return classify_detail(msg, warnings)[0]


# --------------------------------------------------------------------------
# Status directory and event file

def open_status_dir(status_dir=STATUS_DIR, create=True):
    """Descriptor of the status directory, created with mode 0700.

    Opened with O_NOFOLLOW: a symlink in its place is refused, not followed.
    """
    if create:
        try:
            os.mkdir(status_dir, 0o700)
        except FileExistsError:
            pass
    fd = os.open(status_dir,
                 os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise PermissionError(f'{status_dir} belongs to another user')
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def encode_event(event):
    """One JSON line (bytes, with the newline) of an event dict."""
    record = {'v': SCHEMA, **event}
    return (json.dumps(record, separators=(',', ':'), allow_nan=False)
            + '\n').encode('ascii')


def append_event(event, status_dir=STATUS_DIR):
    """Append one event with a single write() on an O_APPEND descriptor.

    yt-dlp appends to the same file (one write per line as well), so lines
    from concurrent writers never interleave.
    """
    data = encode_event(event)
    if len(data) > MAX_EVENT_BYTES:
        raise ValueError(f'event is {len(data)} bytes (max {MAX_EVENT_BYTES})')
    dir_fd = open_status_dir(status_dir)
    try:
        fd = os.open(EVENTS_NAME,
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                     | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f'{EVENTS_NAME} is not a regular file')
        # yt-dlp creates the file with the umask's mode if it gets there
        # first (e.g. right after a rotation).
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
        written = os.write(fd, data)
        if written != len(data):
            raise OSError(f'short write to {EVENTS_NAME}')
    finally:
        os.close(fd)


def rotate(status_dir=STATUS_DIR, limit=ROTATE_BYTES):
    """Move events.jsonl to events.jsonl.1 (replacing it) once it is larger
    than ``limit``. Returns True if it rotated."""
    dir_fd = open_status_dir(status_dir)
    try:
        try:
            st = os.stat(EVENTS_NAME, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(st.st_mode) or st.st_size <= limit:
            return False
        os.replace(EVENTS_NAME, ROTATED_NAME,
                   src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return True
    finally:
        os.close(dir_fd)


def iter_lines(fd, max_line=MAX_LINE_BYTES, partial=True):
    """Lines (str, without the newline) read from a binary descriptor.

    Lines longer than ``max_line`` bytes are skipped. A last line without a
    newline (possibly still being written) is only returned if ``partial``.
    """
    buf = b''
    skipping = False
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buf += chunk
        while True:
            nl = buf.find(b'\n')
            if nl < 0:
                if len(buf) > max_line:
                    buf, skipping = b'', True
                break
            line, buf = buf[:nl], buf[nl + 1:]
            if skipping:
                skipping = False
                continue
            if len(line) <= max_line:
                yield line.decode('utf-8', 'replace')
    if partial and buf and not skipping:
        yield buf.decode('utf-8', 'replace')


def parse_event(line):
    """The event dict of one events.jsonl line, or None if it is invalid."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if (not isinstance(event, dict) or event.get('v') != SCHEMA
            or not isinstance(event.get('t'), str)):
        return None
    return event


def read_events(status_dir=STATUS_DIR, run=None,
                names=(ROTATED_NAME, EVENTS_NAME)):
    """Events from the rotated and the current file, oldest first, skipping
    invalid lines and a last line that is still being written. With ``run``,
    only that run's events."""
    try:
        dir_fd = open_status_dir(status_dir, create=False)
    except FileNotFoundError:
        return
    needle = None if run is None else str(run)   # cheap pre-filter
    try:
        for name in names:
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=dir_fd)
            except FileNotFoundError:
                continue
            try:
                for line in iter_lines(fd, partial=False):
                    if needle is not None and needle not in line:
                        continue
                    event = parse_event(line)
                    if event is not None and (run is None
                                              or event.get('run') == run):
                        yield event
            finally:
                os.close(fd)
    finally:
        os.close(dir_fd)


# --------------------------------------------------------------------------
# Log parser

_NONCE_LINE_RE = re.compile(
    r'^(?:.*? )?@@ytdl:(?P<nonce>[0-9a-f]{16,32}) '
    r'(?:(?P<kind>channel|pass_end)(?: (?P<json>\{.*\}))?|(?P<pjson>\{.*\}))$')
_CONCURRENT_PREFIX_RE = re.compile(r'^\d+: ')
_WRITING_RE = re.compile(
    r'''^\[info\] Writing '\{"v":1,"t":"(?P<t>start|end)","src":"[a-z]+",'''
    r'''"run":"(?P<run>[0-9a-f]{16,32})"''')
_SKIP_ARCHIVE_RE = re.compile(
    r'^\[download\] (?P<id>[^\s:]+): (?:(?P<title>.*) )?'
    r'has already been recorded in the archive$')
_SKIP_FILTER_RE = re.compile(
    r'^\[download\] (?P<what>.*) does not pass filter \((?P<filter>.*)\), '
    r'skipping \.\.$')
_MESSAGE_RE = re.compile(
    r'^(?P<lvl>ERROR|WARNING): (?:\[(?P<ie>[^\]]+)\] (?P<id>[^\s:]+): )?'
    r'(?P<msg>.*)$')
_ITEM_RE = re.compile(
    r'^\[download\] Downloading item (?P<i>\d+) of (?P<n>\d+|N/A)$')
_PLAYLIST_START_RE = re.compile(r'^\[download\] Downloading playlist: (?P<t>.*)$')
_EXTRACTING_RE = re.compile(r'^\[[^\]]+\] Extracting URL: ')
_PLAYLIST_END_RE = re.compile(
    r'^\[download\] Finished downloading playlist: (?P<t>.*)$')


def _int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _keep_last(items, item, limit=5):
    items.append(item)
    del items[:-limit]


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _new_video(vid):
    return {'id': vid, 'started': False, 'ended': False, 'done': False}


def _update_video(video, event):
    """Record a start or end event in the video's dict."""
    info = event['i']
    for key in ('title', 'channel', 'uploader'):
        if isinstance(info.get(key), str) and not video.get(key):
            video[key] = clean_text(info[key])
    url = info.get('webpage_url')
    if isinstance(url, str) and len(url) <= MAX_URL:   # never cut a URL
        video['url'] = clean_text(url, MAX_URL)
    for key in ('epoch', 'playlist_autonumber', 'n_entries', 'duration'):
        if _num(info.get(key)) is not None:
            video[key] = info[key]
    if event['t'] == 'start':
        video['started'] = True
    else:
        video['ended'] = True
        paths = info.get('requested_downloads.:.filepath')
        if isinstance(paths, list) and any(
                isinstance(p, str) and p for p in paths):
            video['done'] = True


class Entry:
    """What the log and the events say about one channels.txt entry (one
    yt-dlp process). The section of a manual job has index None."""

    def __init__(self, index):
        self.index = index
        self.total = None
        self.url = ''
        self.ts_start = None
        self.ts_end = None
        self.rc = None
        self.secs = None
        self.error = ''          # set by youtube-dl.sh, e.g. unparsable args
        self.ended = False
        self.skipped = 0         # from log lines
        self.filtered = 0        # from log lines
        self.videos = collections.OrderedDict()   # id -> dict (from events)
        # id -> [(explicit, msg, ie)]; explicit: the line named the id
        self.errors = collections.OrderedDict()
        self.warnings = collections.defaultdict(list)   # id -> [msg]
        self.entry_errors = []   # ERROR lines with no video to belong to
        # WARNING lines that name no video, seen before the video opened,
        # e.g. 'WARNING: [youtube] Join this channel ...' while extracting:
        # they go to the next video that starts or names itself in an ERROR.
        self.loose = []
        self.open = None         # id of the video being processed
        self.pending = collections.deque()   # events not matched to the log yet
        self.owed = 0            # 'Writing' lines seen before their event
        self.item = None         # 'Downloading item i of n'
        self.n_items = None
        self.playlist = ''
        self.progress = None     # last progress JSON of this entry
        self.phase = ''
        self.phase_id = None     # id of the video the phase belongs to

    def video(self, vid):
        video = self.videos.get(vid)
        if video is None:
            video = self.videos[vid] = _new_video(vid)
        return video


class LogParser:
    """Parses the merged yt-dlp output of a pass (or of one manual job),
    together with the start/end events of the same run.

    Feed log lines with ``feed()`` and events with ``add_events()``, in any
    order. A video's outcome comes only from the events (exact): an ``end``
    with a file path is done, a ``start`` without one failed. The log adds
    what only exists as human lines: archive and filter skips, extraction
    errors and the reasons of failures. yt-dlp logs "[info] Writing '<our
    template>'" right before it writes an event, so these lines tell where in
    the log each event happened; without them (the user's --quiet) the
    progress lines tell which video is open.

    Only nonce lines with ``nonce`` are trusted. Human lines can be forged by
    video titles, so the counts taken from them (skipped, filtered,
    extraction failures) are cosmetic.

    ``unmarked=True`` is for manual jobs: the whole output is one entry.
    For a pass, lines outside the channel markers are ignored.
    """

    def __init__(self, nonce, run=None, unmarked=False):
        self.nonce = nonce
        self.run = run
        self.unmarked = unmarked
        self.entries = collections.OrderedDict()
        self.current = self._entry(None) if unmarked else None
        self.total = None
        self.pass_ended = False

    def _entry(self, index):
        entry = self.entries.get(index)
        if entry is None:
            entry = self.entries[index] = Entry(index)
        return entry

    # -- events ------------------------------------------------------------

    def add_events(self, events):
        for event in events:
            if not isinstance(event, dict) or event.get('t') not in ('start', 'end'):
                continue
            if self.run is not None and event.get('run') != self.run:
                continue
            info = event.get('i')
            if not isinstance(info, dict) or not isinstance(info.get('id'), str):
                continue
            ch = None if self.unmarked else _int(event.get('ch'))
            entry = self._entry(ch)
            entry.pending.append(event)
            if entry.ended:
                self._flush(entry)
            while entry.owed and entry.pending:
                entry.owed -= 1
                self._apply(entry, entry.pending.popleft())

    def _apply(self, entry, event):
        vid = event['i']['id']
        _update_video(entry.video(vid), event)
        entry.open = vid if event['t'] == 'start' else None
        if entry.open:
            self._adopt_loose(entry, vid)

    def _flush(self, entry):
        while entry.pending:
            self._apply(entry, entry.pending.popleft())
        entry.owed = 0
        entry.open = None

    # -- log lines ---------------------------------------------------------

    def feed(self, line):
        line = _ANSI_RE.sub('', line.rstrip('\r\n'))
        if SENTINEL in line:
            match = _NONCE_LINE_RE.match(line)
            if match:
                if match.group('nonce') == self.nonce:
                    self._nonce_line(match)
                return   # a sentinel with another nonce is never trusted
        entry = self.current
        if entry is None:
            return
        line = _CONCURRENT_PREFIX_RE.sub('', line, count=1)
        match = _WRITING_RE.match(line)
        if match:
            if self.run is not None and match.group('run') == self.run:
                if entry.pending:
                    self._apply(entry, entry.pending.popleft())
                else:
                    entry.owed += 1
            return
        match = _MESSAGE_RE.match(line)
        if match:
            self._message(entry, match)
            return
        if _SKIP_ARCHIVE_RE.match(line):
            entry.skipped += 1
            return
        if _SKIP_FILTER_RE.match(line):
            entry.filtered += 1
            return
        match = _ITEM_RE.match(line)
        if match:
            entry.item = int(match.group('i'))
            entry.n_items = None if match.group('n') == 'N/A' else int(match.group('n'))
            entry.open = None
            entry.phase = ''
            entry.phase_id = None
            entry.loose = []
            return
        if _EXTRACTING_RE.match(line):
            entry.loose = []
            return
        match = _PLAYLIST_START_RE.match(line)
        if match:
            entry.playlist = clean_text(match.group('t'))
            return
        if _PLAYLIST_END_RE.match(line):
            entry.open = None

    def _message(self, entry, match):
        msg = clean_text(match.group('msg'))
        vid = match.group('id')
        if match.group('lvl') == 'WARNING':
            target = vid or entry.open
            if target:
                _keep_last(entry.warnings[target], msg)
            else:
                _keep_last(entry.loose, msg)
            return
        if vid:
            # '[ie] id: ...' names the video (or playlist) itself.
            self._adopt_loose(entry, vid)
            _keep_last(entry.errors.setdefault(vid, []),
                       (True, msg, match.group('ie')))
        elif entry.open:
            # e.g. 'unable to download video data: HTTP Error 404'
            _keep_last(entry.errors.setdefault(entry.open, []), (False, msg, None))
        elif (msg not in entry.entry_errors
              and len(entry.entry_errors) < MAX_ERRORS):
            entry.entry_errors.append(msg)

    @staticmethod
    def _adopt_loose(entry, vid):
        for msg in entry.loose:
            _keep_last(entry.warnings[vid], msg)
        entry.loose = []

    def _nonce_line(self, match):
        kind = match.group('kind')
        if kind == 'pass_end':
            if self.current is not None and not self.unmarked:
                self._flush(self.current)
                self.current = None
            self.pass_ended = True
            return
        try:
            data = json.loads(match.group('json') or match.group('pjson') or '')
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        if kind == 'channel':
            if not self.unmarked:
                self._channel_marker(data)
            return
        entry = self.current
        if entry is None:
            return
        vid = data.get('info.id')
        if not isinstance(vid, str):
            return   # subtitle downloads have no id
        # With the user's --quiet there are no 'Writing' lines; these lines
        # still name the video being processed. (A playlist's own id, e.g.
        # from Concat, is no video: its errors count for the whole entry.)
        entry.open = vid
        entry.phase_id = vid
        self._adopt_loose(entry, vid)
        if 'progress.postprocessor' in data:
            name = clean_text(data.get('progress.postprocessor'), 40)
            if data.get('progress.status') == 'finished':
                entry.phase = 'processed: ' + name
            else:
                entry.phase = 'processing: ' + name
            return
        entry.progress = {
            key: (clean_text(value) if isinstance(value, str) else value)
            for key, value in data.items()
            if key in PROGRESS_FIELDS
            and (isinstance(value, str) or _num(value) is not None)}
        entry.phase = 'downloading'

    def _channel_marker(self, data):
        index = _int(data.get('index'))
        if index is None or index < 1:
            return
        if data.get('phase') == 'start':
            if self.current is not None:
                self._flush(self.current)
            entry = self.current = self._entry(index)
            entry.total = _int(data.get('total'))
            entry.url = clean_text(data.get('url'), MAX_URL)
            entry.ts_start = _num(data.get('ts'))
            if entry.total is not None:
                self.total = entry.total
        elif data.get('phase') == 'end':
            entry = self._entry(index)
            entry.rc = _int(data.get('rc'))
            entry.secs = _num(data.get('secs'))
            entry.ts_end = _num(data.get('ts'))
            entry.error = clean_text(data.get('error'))
            entry.ended = True
            self._flush(entry)
            if self.current is entry:
                self.current = None

    def finish(self):
        """End of input: events not matched to a log line are applied now."""
        for entry in self.entries.values():
            self._flush(entry)

    # -- results -----------------------------------------------------------

    def entry_result(self, entry, live=False):
        """Counts and classified errors of one entry.

        ``live`` is for an entry yt-dlp may still be working on (the web UI's
        view of a running pass): events not matched to their log line yet
        count as well, and the video started last, without an end event or
        an error, is in progress (``'active'``) instead of failed.
        """
        videos = entry.videos
        active = None
        if live:
            videos = collections.OrderedDict(
                (vid, dict(video)) for vid, video in entry.videos.items())
            for event in entry.pending:
                vid = event['i']['id']
                if vid not in videos:
                    videos[vid] = _new_video(vid)
                _update_video(videos[vid], event)
            for vid, video in reversed(videos.items()):
                if video['started'] and not video['ended']:
                    if not entry.errors.get(vid):
                        active = vid
                    break
        new = failed = members = 0
        errors = []
        seen = set(entry.entry_errors)
        for vid, video in videos.items():
            if video['done']:
                new += 1
                continue
            if vid == active:
                continue
            msgs = [m for _explicit, m, _ie in entry.errors.get(vid, ())]
            kind, text = classify_detail(msgs[-1] if msgs else '',
                                         entry.warnings.get(vid, ()))
            if kind in SKIP_KINDS:
                members += 1
            else:
                failed += 1
            error = {'idx': entry.index, 'id': clean_text(vid, 64),
                     'kind': kind, 'msg': clean_text(text)}
            for key in ('title', 'url'):
                if video.get(key):
                    error[key] = video[key]
            # The extractor, for 'Download now': from an explicit
            # 'ERROR: [ie] id:' line, else from yt-dlp's own watch URL.
            ies = [ie for explicit, _m, ie in entry.errors.get(vid, ())
                   if explicit and ie]
            if ies:
                error['ie'] = clean_text(ies[-1], 40)
            elif video.get('url') == YOUTUBE_WATCH + vid:
                error['ie'] = 'youtube'
            errors.append(error)
        for vid, msgs in entry.errors.items():
            if vid in videos:
                continue
            explicit = [(m, ie) for is_explicit, m, ie in msgs if is_explicit]
            if not explicit:
                # Attached to an id that is no video (a playlist's own id).
                for _explicit, msg, _ie in msgs:
                    if msg not in seen:
                        seen.add(msg)
                        errors.append({'idx': entry.index, 'id': None,
                                       'kind': classify(msg), 'msg': msg})
                continue
            # Failed before yt-dlp started the video (e.g. unavailable, or a
            # 429 on the watch page): only the human line knows about it.
            msg, ie = explicit[-1]
            kind, text = classify_detail(msg, entry.warnings.get(vid, ()))
            if kind in SKIP_KINDS:
                members += 1
            else:
                failed += 1
            error = {'idx': entry.index, 'id': clean_text(vid, 64),
                     'kind': kind, 'msg': clean_text(text)}
            if ie:
                error['ie'] = clean_text(ie, 40)
            errors.append(error)
        for msg in entry.entry_errors:
            errors.append({'idx': entry.index, 'id': None,
                           'kind': classify(msg), 'msg': msg})
        if entry.error:
            errors.append({'idx': entry.index, 'id': None, 'kind': 'other',
                           'msg': entry.error})
        return {'new': new, 'skipped': entry.skipped, 'failed': failed,
                'filtered': entry.filtered, 'members': members,
                'errors': errors, 'active': active}

    def summary(self):
        """{'sum': totals, 'ch': CHANNEL_ROW rows, 'err': errors} for run_end
        (or for a manual job with ``unmarked``)."""
        totals = {'new': 0, 'skipped': 0, 'failed': 0, 'filtered': 0,
                  'members': 0}
        rows, errors = [], []
        for index, entry in self.entries.items():
            if (index is None) != self.unmarked:
                continue
            result = self.entry_result(entry)
            for key in totals:
                totals[key] += result[key]
            errors.extend(result['errors'])
            if index is not None:
                rows.append([index, result['new'], result['skipped'],
                             result['failed'], result['filtered'], entry.rc,
                             entry.secs, entry.ts_end, entry.url,
                             result['members']])
        rows.sort(key=lambda row: row[0])
        return {'sum': totals, 'ch': rows, 'err': errors[:MAX_ERRORS],
                'err_total': len(errors)}


# --------------------------------------------------------------------------
# Commands for youtube-dl.sh

def _open_log(path):
    return os.open(path, os.O_RDONLY | os.O_CLOEXEC)


def _log_has_marker(path, marker, tail=65536):
    try:
        fd = _open_log(path)
    except OSError:
        return False
    try:
        size = os.fstat(fd).st_size
        os.lseek(fd, max(0, size - tail), os.SEEK_SET)
        data = b''
        while len(data) < tail:
            chunk = os.read(fd, tail - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    return ('\n' + marker + '\n').encode() in b'\n' + data


def wait_for_marker(path, marker, timeout):
    """True once the log contains the marker line (tee writes the script's
    output with a delay), False after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        if _log_has_marker(path, marker):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def summarize_pass(log_path, status_dir, run, nonce):
    parser = LogParser(nonce, run=run)
    parser.add_events(e for e in read_events(status_dir, run=run)
                      if e.get('t') in ('start', 'end'))
    try:
        fd = _open_log(log_path)
    except OSError:
        fd = None
    if fd is not None:
        try:
            for line in iter_lines(fd):
                parser.feed(line)
        finally:
            os.close(fd)
    parser.finish()
    return parser.summary()


def fit_run_end(event):
    """Drop errors from the end until the event fits MAX_EVENT_BYTES."""
    while len(encode_event(event)) > MAX_EVENT_BYTES and event['err']:
        event['err'].pop()
    return event


def split_run_end(event):
    """The events that record a pass's end: run_end alone if its channel
    rows fit, else 'run_rows' events carrying the rows (each one under
    MAX_EVENT_BYTES) followed by a run_end without 'ch'. The size of
    run_end then no longer depends on the number of entries."""
    if len(encode_event(dict(event, err=[]))) <= MAX_EVENT_BYTES:
        return [fit_run_end(event)]
    rows = event.pop('ch')
    head = {'t': 'run_rows', 'ts': event['ts'], 'run': event['run'], 'ch': []}
    base = len(encode_event(head))
    chunks, chunk, size = [], [], base
    for row in rows:
        row_size = len(json.dumps(row, separators=(',', ':'),
                                  allow_nan=False)) + 1
        if base + row_size > MAX_EVENT_BYTES:
            row = row[:-1] + [None]          # never happens with MAX_URL
            row_size = len(json.dumps(row, separators=(',', ':'))) + 1
        if chunk and size + row_size > MAX_EVENT_BYTES:
            chunks.append(chunk)
            chunk, size = [], base
        chunk.append(row)
        size += row_size
    if chunk:
        chunks.append(chunk)
    event['rows'] = len(rows)
    return [dict(head, ch=c) for c in chunks] + [fit_run_end(event)]


def _cmd_begin(args):
    rotate(args.status_dir)
    run, nonce = new_id(), new_id()
    version = (args.ytdlp or '').splitlines()
    append_event({
        't': 'run_start', 'ts': int(time.time()), 'run': run, 'nonce': nonce,
        'pid': args.pid, 'channels': args.channels,
        'interval': clean_text(args.interval, 64),
        'ytdlp': clean_text(version[0] if version else '', 64),
    }, args.status_dir)
    sys.stdout.write(f'{run}\0{nonce}\0')
    return 0


def _marker(nonce, data):
    return f'{SENTINEL}{nonce} channel {json.dumps(data, separators=(",", ":"))}'


def _cmd_channel_start(args):
    check_id(args.run, 'run')
    check_id(args.nonce, 'nonce')
    if args.flags_out:
        flags = ytdlp_flags('scheduled', args.run, args.nonce,
                            events_path(args.status_dir), ch=args.index)
        with open(args.flags_out, 'w', encoding='utf-8') as f:
            f.write(''.join(flag + '\0' for flag in flags))
    print(_marker(args.nonce, {
        'phase': 'start', 'index': args.index, 'total': args.total,
        'url': clean_text(args.url, MAX_URL), 'ts': int(time.time())}), flush=True)
    return 0


def _cmd_channel_end(args):
    check_id(args.nonce, 'nonce')
    data = {'phase': 'end', 'index': args.index, 'rc': args.rc,
            'secs': args.secs, 'ts': int(time.time())}
    if args.error:
        data['error'] = clean_text(args.error)
    print(_marker(args.nonce, data), flush=True)
    return 0


def _cmd_end(args):
    check_id(args.run, 'run')
    check_id(args.nonce, 'nonce')
    found = wait_for_marker(args.log, f'{SENTINEL}{args.nonce} pass_end',
                            args.wait)
    summary = summarize_pass(args.log, args.status_dir, args.run, args.nonce)
    now = time.time()
    seconds = parse_interval(args.interval)
    event = {
        't': 'run_end', 'ts': int(now), 'run': args.run, 'rc': args.rc,
        'next': None if seconds is None else int(now + seconds),
        'partial': not found, 'sum': summary['sum'], 'ch': summary['ch'],
        'err': summary['err'],
    }
    for record in split_run_end(event):
        append_event(record, args.status_dir)
    return 0


def _parser():
    parser = argparse.ArgumentParser(
        prog='status_events.py', allow_abbrev=False,
        description='Status events for the web UI dashboard (used by '
                    'youtube-dl.sh).')
    sub = parser.add_subparsers(dest='command', required=True)

    def command(name, func, help_text, ids=True):
        p = sub.add_parser(name, help=help_text, allow_abbrev=False)
        p.set_defaults(func=func)
        p.add_argument('--status-dir', default=STATUS_DIR)
        if ids:
            p.add_argument('--run', required=True)
            p.add_argument('--nonce', required=True)
        return p

    p = command('begin', _cmd_begin, 'start a pass: rotate the event file, '
                'append run_start, print "run\\0nonce\\0"', ids=False)
    p.add_argument('--channels', type=int, required=True)
    p.add_argument('--interval', default='')
    p.add_argument('--pid', type=int, required=True)
    p.add_argument('--ytdlp', default='')

    p = command('channel-start', _cmd_channel_start, 'print the start marker '
                'of an entry and write its yt-dlp flags (NUL-terminated)')
    p.add_argument('--index', type=int, required=True)
    p.add_argument('--total', type=int, required=True)
    p.add_argument('--url', default='')
    p.add_argument('--flags-out')

    p = command('channel-end', _cmd_channel_end, 'print the end marker of an '
                'entry')
    p.add_argument('--index', type=int, required=True)
    p.add_argument('--rc', type=int, required=True)
    p.add_argument('--secs', type=int, default=0)
    p.add_argument('--error', default='')

    p = command('end', _cmd_end, 'summarise the pass log into run_end')
    p.add_argument('--rc', type=int, required=True)
    p.add_argument('--interval', default='')
    p.add_argument('--log', required=True)
    p.add_argument('--wait', type=float, default=END_WAIT)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError) as err:
        sys.stderr.write(f'[status] {args.command} failed, no dashboard events '
                         f'for this step: {err}\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
