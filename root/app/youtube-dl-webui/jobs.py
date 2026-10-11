"""Manual downloads of the web UI: a bounded queue of yt-dlp jobs.

One ``JobManager`` per web UI process (uvicorn runs a single worker) keeps
the jobs started from the download form or the API token (the iOS
shortcut):

* At most ``parallel`` jobs run at once (``WEBUI_MAX_PARALLEL_DOWNLOADS``,
  1 to 3) and at most ``MAX_QUEUED`` wait; a full queue is refused
  (``QueueFull``, the route answers 429). A URL that is already queued or
  running returns that job instead of a second one (double taps in the share
  sheet). A YouTube video whose id is in archive.txt is answered at once as
  ``already`` without starting yt-dlp, unless the download is forced.
* yt-dlp runs with ``create_subprocess_exec`` (never a shell), stdin from
  /dev/null, in its own session, so cancelling reaches its children too
  (ffmpeg and external downloaders share its stdout). The URL follows
  ``--``. The event flags (status_events.ytdlp_flags) come after every
  argument the user controls.
* The output is copied in chunks through an incremental UTF-8 decoder, never
  line by line: progress without --newline is one endless line, and asyncio's
  line reader fails beyond 64 KiB. The raw log keeps everything; only the
  parser for the live status skips lines longer than ``MAX_PARSE_LINE``. The
  pipe is always drained to its end and yt-dlp awaited (before Python 3.13,
  asyncio's ``wait()`` returns only once the pipe is closed). On an error or
  a cancellation of the copy, yt-dlp's process group is killed first.
* Cancel sends SIGTERM to the process group and SIGKILL ``cancel_grace``
  seconds later. The group id only ever comes from a process this manager
  started. Shutting down the web UI stops running jobs the same way, faster.
* Every step is an event in ``events.jsonl`` (``job_queued``, ``job_start``,
  ``job_end``), which the dashboard reads. After a restart of the web UI,
  queued jobs are validated again and queued again (if not older than a
  day); jobs that were running are ``interrupted`` and can be retried.
* Job logs are ``/config/.youtube-dl-status/jobs/<id>.log`` (directory 0700,
  files 0600, opened without following symlinks); the newest ``MAX_LOGS``
  are kept. Logs of older versions are still read from
  ``/tmp/download_<id>.log``.

The manager runs in the event loop: all its state is changed there, file
work runs in threads. It raises its own exceptions; the routes turn them
into HTTP answers.
"""

import asyncio
import codecs
import collections
import contextlib
import logging
import os
import re
import signal
import stat
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit, urlunsplit

import status_events as se

MAX_QUEUED = 20                # jobs waiting (running ones not counted)
MAX_PARALLEL = 3               # upper limit of WEBUI_MAX_PARALLEL_DOWNLOADS
OUTPUT_CHUNK_BYTES = 64 * 1024
MAX_PARSE_LINE = se.MAX_LINE_BYTES   # longer lines are logged, not parsed
CANCEL_GRACE = 10.0            # cancel: SIGTERM, then SIGKILL after this
SHUTDOWN_GRACE = 2.0           # the same when the web UI stops
KILL_GRACE = 5.0               # after SIGKILL, before the copy is abandoned
LOG_TIMEOUT = 5.0              # for the last lines of a job's log
MAX_LOGS = 50                  # job logs kept
MAX_KNOWN = 200                # finished jobs remembered for retry
REQUEUE_AGE = 86400            # queued jobs older than this are not resumed
ARCHIVE_MAX_BYTES = 64 * 1024 * 1024
COOKIES_MAX_BYTES = 16 * 1024 * 1024
JOBS_DIR = 'jobs'
OK_CODES = (0, 101)            # 101: yt-dlp stopped early on purpose
ENDED = '[youtube-dl] Download process ended\n'

UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z')
_HEX_RE = re.compile(r'[0-9a-f]{32}\Z')
_LOG_NAME_RE = re.compile(
    r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.log\Z')
_COOKIES_NAME_RE = re.compile(
    r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
    r'\.cookies\Z')
YOUTUBE_ID_RE = re.compile(r'[A-Za-z0-9_-]{11}\Z')
_YOUTUBE_HOSTS = frozenset({'youtube.com', 'www.youtube.com', 'm.youtube.com',
                            'music.youtube.com'})
_YOUTU_BE_HOSTS = frozenset({'youtu.be', 'www.youtu.be'})
_YOUTUBE_PATH_RE = re.compile(r'/(?:shorts|live)/([^/]+)/?\Z')
_FORMAT_RE = re.compile(r'(--format |-f )', re.I | re.M)
_ARCHIVE_RE = re.compile(r'^--download-archive ', re.M)

_log = logging.getLogger('uvicorn.error')


class JobError(Exception):
    """Base of the manager's refusals; ``str()`` is the message to show."""


class QueueFull(JobError):
    pass


class UnknownJob(JobError):
    pass


class JobActive(JobError):
    pass


class NotActive(JobError):
    pass


class ShuttingDown(JobError):
    pass


# --------------------------------------------------------------------------
# URLs

def youtube_video_id(url):
    """The id of a single YouTube video (watch, youtu.be, shorts or live
    URL), or None. A URL with a ``list`` parameter is a playlist download
    and has no single id."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
    except ValueError:
        return None
    query = parse_qs(parts.query)
    if 'list' in query:
        return None
    vid = None
    if host in _YOUTU_BE_HOSTS:
        vid = parts.path.strip('/')
    elif host in _YOUTUBE_HOSTS:
        if parts.path.rstrip('/') == '/watch':
            values = query.get('v') or ['']
            vid = values[0] if len(values) == 1 else None
        else:
            match = _YOUTUBE_PATH_RE.match(parts.path)
            vid = match.group(1) if match else None
    return vid if vid and YOUTUBE_ID_RE.match(vid) else None


def url_key(url):
    """The URL in a normalised form, to recognise a duplicate job: one key
    per YouTube video, otherwise scheme and host in lower case, without the
    fragment."""
    vid = youtube_video_id(url)
    if vid:
        return 'youtube:' + vid
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                       parts.path or '/', parts.query, ''))


def watch_url(video_id):
    """The watch URL of a YouTube id ('Download now' of a failed video)."""
    if not isinstance(video_id, str) or not YOUTUBE_ID_RE.match(video_id):
        raise ValueError('not a YouTube video id')
    return 'https://www.youtube.com/watch?v=' + video_id


def format_args(args_conf, format_default):
    """--format unless args.conf chooses one (as the web UI always did)."""
    if _FORMAT_RE.search(args_conf) or not format_default:
        return []
    return ['--format', format_default]


def sets_archive(args_conf):
    """True if args.conf chooses its own archive (like youtube-dl.sh)."""
    return bool(_ARCHIVE_RE.search(args_conf))


# --------------------------------------------------------------------------
# Files

def _open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                 | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f'{path} is not a regular file')
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_text(path, limit=1024 * 1024):
    try:
        fd = _open_regular(path)
    except OSError:
        return ''
    try:
        return os.read(fd, limit).decode('utf-8', 'replace')
    finally:
        os.close(fd)


class ArchiveIndex:
    """YouTube ids in archive.txt, read again when the file changes."""

    def __init__(self, path):
        self.path = path
        self.key = None
        self.ids = frozenset()
        self.lock = threading.Lock()

    def contains(self, video_id):
        with self.lock:
            try:
                fd = _open_regular(self.path)
            except OSError:
                return False
            try:
                st = os.fstat(fd)
                key = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
                if key != self.key:
                    self.key, self.ids = key, None
                    if st.st_size <= ARCHIVE_MAX_BYTES:
                        self.ids = frozenset(self._parse(fd))
            finally:
                os.close(fd)
            return self.ids is not None and video_id in self.ids

    @staticmethod
    def _parse(fd):
        # yt-dlp writes '<extractor> <id>' per line, 'youtube <id>' here.
        for line in se.iter_lines(fd):
            parts = line.split()
            if len(parts) == 2 and parts[0] == 'youtube':
                yield parts[1]


def _open_dir(name, dir_fd=None, create=True):
    """Descriptor of a directory of the web UI's user, mode 0700, opened
    without following a symlink."""
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=dir_fd)
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                 | os.O_CLOEXEC, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise PermissionError(f'{name} belongs to another user')
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_append(name, dir_fd=None):
    fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
                 | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f'{name} is not a regular file')
        if stat.S_IMODE(st.st_mode) != 0o600:
            os.fchmod(fd, 0o600)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class JobLog:
    """A job's log file; writes run in a thread."""

    def __init__(self, fd):
        self.fd = fd

    async def write(self, text):
        if text:
            await asyncio.to_thread(_write_all, self.fd,
                                    text.encode('utf-8', 'replace'))

    async def close(self):
        fd, self.fd = self.fd, None
        if fd is not None:
            await asyncio.to_thread(os.close, fd)


def scan_jobs(status_dir):
    """Job records from the job events of both event files, oldest first:
    {hex: {url, by, force, queued, started, ended, state}}. Never creates
    the status directory."""
    try:
        dir_fd = se.open_status_dir(status_dir, create=False)
    except OSError:
        return {}
    jobs = collections.OrderedDict()
    try:
        for name in (se.ROTATED_NAME, se.EVENTS_NAME):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=dir_fd)
            except OSError:
                continue
            try:
                for line in se.iter_lines(fd, partial=False):
                    if '"job_' not in line:   # cheap pre-filter
                        continue
                    event = se.parse_event(line)
                    if event is not None:
                        _fold_job(jobs, event)
            finally:
                os.close(fd)
    finally:
        os.close(dir_fd)
    return jobs


def _fold_job(jobs, event):
    run = event.get('run')
    if not isinstance(run, str) or not _HEX_RE.match(run):
        return
    record = jobs.get(run)
    if record is None:
        record = jobs[run] = {'url': None, 'by': None, 'force': False,
                              'queued': None, 'started': None, 'ended': None,
                              'state': None}
        while len(jobs) > 4 * MAX_KNOWN:
            jobs.popitem(last=False)
    ts = event.get('ts')
    ts = ts if isinstance(ts, int) and not isinstance(ts, bool) else None
    kind = event.get('t')
    if kind == 'job_queued':
        url = event.get('url')
        record['url'] = url if isinstance(url, str) else None
        record['by'] = event.get('by') if event.get('by') in (
            'token', 'basic') else 'basic'
        record['force'] = event.get('force') is True
        record['queued'] = ts
    elif kind == 'job_start':
        record['started'] = ts or record['queued']
    elif kind == 'job_end':
        record['ended'] = ts or record['started'] or record['queued']
        record['state'] = event.get('state') if isinstance(
            event.get('state'), str) else 'failed'


class LineSplitter:
    """Complete lines of a text stream for the parser; lines longer than
    ``limit`` characters are skipped (the raw log keeps them)."""

    def __init__(self, limit=MAX_PARSE_LINE):
        self.limit = limit
        self.parts = []
        self.size = 0
        self.skipping = False

    def feed(self, text):
        lines = []
        pieces = text.split('\n')
        for piece in pieces[:-1]:
            if not self.skipping and self.size + len(piece) <= self.limit:
                lines.append(''.join(self.parts) + piece)
            self.parts, self.size, self.skipping = [], 0, False
        last = pieces[-1]
        if last and not self.skipping:
            if self.size + len(last) > self.limit:
                self.parts, self.size, self.skipping = [], 0, True
            else:
                self.parts.append(last)
                self.size += len(last)
        return lines

    def flush(self):
        line = '' if self.skipping else ''.join(self.parts)
        self.parts, self.size, self.skipping = [], 0, False
        return [line] if line else []


# --------------------------------------------------------------------------
# Jobs

class Job:
    def __init__(self, job_id, url, by, force, queued):
        self.id = job_id
        self.hex = job_id.replace('-', '')
        self.url = url
        self.key = url_key(url) if url else None
        self.by = by
        self.force = force
        self.state = 'queued'
        self.queued = queued
        self.started = None
        self.ended = None
        self.rc = None
        self.title = ''
        self.error = None
        self.nonce = None
        self.parser = None
        self.proc = None
        self.log = None
        self.ready = False        # its job_queued event was written
        self.starter = None       # task starting yt-dlp
        self.pump = None          # task copying the output
        self.task = None          # task running the job to its end
        self.terminator = None
        self.eof = False          # the output was read to its end
        self.newline = True       # the log ends with a newline
        self.events_ok = False
        self.cookies = None       # the job's private copy of cookies.txt
        self.cancelling = False
        self.end_state = None     # 'cancelled' or 'interrupted' once decided


def _clean(value, limit=se.MAX_TEXT):
    return se.clean_text(value, limit)


class JobManager:
    """The queue and the running jobs (see the module docstring)."""

    def __init__(self, *, status_dir, args_file, cookies_file, archive_file,
                 format_default='', binary='yt-dlp', parallel=1,
                 max_queued=MAX_QUEUED, legacy_log_dir='/tmp',
                 clock=time.time):
        self.status_dir = status_dir
        self.args_file = args_file
        self.cookies_file = cookies_file
        self.archive_file = archive_file
        self.format_default = format_default
        self.binary = binary
        self.parallel = max(1, min(MAX_PARALLEL, int(parallel)))
        self.max_queued = max_queued
        self.legacy_log_dir = legacy_log_dir
        self.clock = clock
        # Replaceable in tests. spawn None: asyncio.create_subprocess_exec.
        self.spawn = None
        self.killpg = os.killpg
        self.open_log = self._open_log
        self.title_for = None     # video id -> known title (runs in a thread)
        self.cancel_grace = CANCEL_GRACE
        self.shutdown_grace = SHUTDOWN_GRACE
        self.kill_grace = KILL_GRACE
        self.log_timeout = LOG_TIMEOUT
        self.archive = ArchiveIndex(archive_file)
        self.jobs = collections.OrderedDict()     # id -> Job, newest last
        self.queued = collections.OrderedDict()   # id -> Job, in order
        self.running = {}                         # id -> Job
        self.closed = False

    # -- paths -------------------------------------------------------------

    @property
    def jobs_dir(self):
        return os.path.join(self.status_dir, JOBS_DIR)

    def log_path(self, job_id):
        """Log file of a job: the jobs directory, or /tmp for jobs of an
        older version. ``job_id`` must be a validated UUID."""
        if not UUID_RE.match(job_id):
            raise ValueError('invalid job id')
        path = os.path.join(self.jobs_dir, job_id + '.log')
        if os.path.lexists(path):
            return path
        return os.path.join(self.legacy_log_dir, f'download_{job_id}.log')

    # -- answers -----------------------------------------------------------

    def position(self, job):
        if job.id not in self.queued:
            return None
        for n, job_id in enumerate(self.queued, start=1):
            if job_id == job.id:
                return n
        return None

    def ticket(self, job, duplicate=False):
        """The answer for the client: {id, state, position, message}."""
        position = self.position(job)
        if job.state == 'queued':
            message = (f'Already in the queue (#{position}).' if duplicate
                       else f'Queued (position {position}).')
        elif job.state == 'running':
            if job.cancelling:
                message = 'Cancelling the download.'
            else:
                message = ('Already downloading.' if duplicate
                           else 'Download started.')
        elif job.state == 'already':
            message = ('Already downloaded (in the download archive). Use '
                       'Re-download on the dashboard to fetch it again.')
        else:
            message = {
                'done': 'Download finished.',
                'partial': 'Download finished with errors.',
                'failed': 'Download failed.',
                'cancelled': 'Download cancelled.',
                'interrupted': 'Download interrupted (the web UI restarted).',
            }.get(job.state, 'Download ended.')
        return {'id': job.id, 'state': job.state, 'position': position,
                'message': message}

    def snapshot(self):
        """Running and queued jobs for the dashboard (call in the event
        loop; the result is plain data the dashboard may use in a thread)."""
        running = []
        for job in self.running.values():
            entry = job.parser.current if job.parser else None
            running.append({
                'id': job.id, 'url': job.url, 'by': job.by,
                'force': job.force, 'queued': job.queued,
                'started': job.started, 'cancelling': job.cancelling,
                'progress': dict(entry.progress) if entry and entry.progress
                else None,
                'phase': entry.phase if entry else '',
                'phase_id': entry.phase_id if entry else None,
                'open': entry.open if entry else None,
                'item': entry.item if entry else None,
                'n_items': entry.n_items if entry else None,
                'playlist': entry.playlist if entry else '',
            })
        queued = [{'id': job.id, 'url': job.url, 'by': job.by,
                   'force': job.force, 'queued': job.queued, 'position': n}
                  for n, job in enumerate(self.queued.values(), start=1)]
        return {'running': running, 'queued': queued}

    # -- events ------------------------------------------------------------

    async def _event(self, event):
        """Append a job event; False (and a log line) if that fails: the
        download itself never depends on it."""
        try:
            await asyncio.to_thread(se.append_event, event, self.status_dir)
            return True
        except (OSError, ValueError) as err:
            _log.warning('[jobs] cannot write the %s event: %s', event['t'],
                         err)
            return False

    def _remember(self, job):
        self.jobs[job.id] = job
        self.jobs.move_to_end(job.id)
        finished = [j for j in self.jobs.values()
                    if j.id not in self.queued and j.id not in self.running]
        for old in finished[:max(0, len(finished) - MAX_KNOWN)]:
            del self.jobs[old.id]

    # -- submitting ----------------------------------------------------------

    def _active(self):
        yield from self.running.values()
        yield from self.queued.values()

    def _duplicate(self, key):
        for job in self._active():
            if job.key == key:
                return job
        return None

    def _now(self):
        return int(self.clock())

    async def submit(self, url, *, by, force=False):
        """Queue a download of a validated URL; returns its ticket.

        Raises QueueFull or ShuttingDown."""
        if self.closed:
            raise ShuttingDown('The web UI is shutting down.')
        key = url_key(url)
        job = self._duplicate(key)
        if job is not None:
            return self.ticket(job, duplicate=True)
        vid = youtube_video_id(url)
        if vid and not force:
            args_conf = await asyncio.to_thread(_read_text, self.args_file)
            if not sets_archive(args_conf) and await asyncio.to_thread(
                    self.archive.contains, vid):
                return await self._already(url, by, vid)
            job = self._duplicate(key)   # another request in the meantime
            if job is not None:
                return self.ticket(job, duplicate=True)
        if self.closed:
            raise ShuttingDown('The web UI is shutting down.')
        if (len(self.running) >= self.parallel or self.queued) and \
                len(self.queued) >= self.max_queued:
            raise QueueFull(f'Queue is full ({self.max_queued} waiting). '
                            'Try again later.')
        job = Job(str(uuid.uuid4()), url, by, bool(force), self._now())
        self.queued[job.id] = job   # holds its place in the queue
        self._remember(job)
        await self._event({'t': 'job_queued', 'ts': job.queued,
                           'run': job.hex, 'url': url, 'by': by,
                           'force': job.force})
        job.ready = True            # job_start may follow now
        await self._dispatch()
        return self.ticket(job)

    async def _already(self, url, by, vid):
        job = Job(str(uuid.uuid4()), url, by, False, self._now())
        if self.title_for is not None:
            with contextlib.suppress(Exception):
                job.title = _clean(await asyncio.to_thread(self.title_for,
                                                           vid))
        job.state = 'already'
        job.ended = job.queued
        self._remember(job)
        await self._event({'t': 'job_queued', 'ts': job.queued,
                           'run': job.hex, 'url': url, 'by': by,
                           'force': False})
        end = {'t': 'job_end', 'ts': job.ended, 'run': job.hex,
               'state': 'already', 'rc': None}
        if job.title:
            end['title'] = job.title
        await self._event(end)
        with contextlib.suppress(OSError):
            log = await self.open_log(job)
            try:
                await log.write(f'[youtube-dl] youtube {vid} is already in '
                                'the download archive; nothing to do.\n'
                                + ENDED)
            finally:
                await log.close()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._prune_logs)
        return self.ticket(job)

    # -- running -------------------------------------------------------------

    async def _dispatch(self):
        """Start queued jobs while there is a free slot."""
        while (not self.closed and self.queued
               and len(self.running) < self.parallel):
            job = next(iter(self.queued.values()))
            if not job.ready:
                break
            del self.queued[job.id]
            self.running[job.id] = job
            job.state = 'running'
            job.started = self._now()
            # Shielded: a cancelled request must not leave a half-started job.
            job.starter = asyncio.ensure_future(self._start(job))
            await asyncio.shield(job.starter)

    def argv(self, job, with_events=True):
        """yt-dlp's argv for a job (reads args.conf; call in a thread)."""
        args_conf = _read_text(self.args_file)
        argv = [self.binary, '--no-playlist-reverse', '--playlist-end', '-1',
                '--config-location', self.args_file,
                *format_args(args_conf, self.format_default)]
        if job.cookies:
            argv += ['--cookies', job.cookies]
        if job.force:
            # Also switches off an archive args.conf chooses.
            argv.append('--no-download-archive')
        elif not sets_archive(args_conf):
            argv += ['--download-archive', self.archive_file]
        if with_events:
            argv += se.ytdlp_flags('manual', job.hex, job.nonce,
                                   se.events_path(self.status_dir))
        return argv + ['--', job.url]

    async def _open_log(self, job):
        """The job's log in the jobs directory; /tmp if that fails."""
        def open_fd():
            try:
                status_fd = se.open_status_dir(self.status_dir)
                try:
                    jobs_fd = _open_dir(JOBS_DIR, status_fd)
                finally:
                    os.close(status_fd)
                try:
                    return _open_append(job.id + '.log', jobs_fd)
                finally:
                    os.close(jobs_fd)
            except OSError as err:
                _log.warning('[jobs] cannot use %s (%s); logging to %s',
                             self.jobs_dir, err, self.legacy_log_dir)
                return _open_append(os.path.join(
                    self.legacy_log_dir, f'download_{job.id}.log'))
        return JobLog(await asyncio.to_thread(open_fd))

    def _copy_cookies(self, job):
        """A private copy of cookies.txt for the job (0600 in the jobs
        directory), or None without cookies.txt. yt-dlp saves its cookie
        jar on exit by truncating and rewriting the file; jobs running next
        to each other and to the pass would otherwise read each other's
        half-written file and save the short jar back, so manual jobs never
        write the shared file (their cookie updates are not kept)."""
        try:
            src = os.open(self.cookies_file, os.O_RDONLY | os.O_NONBLOCK
                          | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        try:
            st = os.fstat(src)
            if not stat.S_ISREG(st.st_mode):
                return None
            if st.st_size > COOKIES_MAX_BYTES:
                raise OSError(f'{self.cookies_file} is too large')
            data = bytearray()
            while len(data) <= COOKIES_MAX_BYTES:
                chunk = os.read(src, 1024 * 1024)
                if not chunk:
                    break
                data += chunk
        finally:
            os.close(src)
        status_fd = se.open_status_dir(self.status_dir)
        try:
            jobs_fd = _open_dir(JOBS_DIR, status_fd)
        finally:
            os.close(status_fd)
        name = job.id + '.cookies'
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=jobs_fd)
            try:
                _write_all(fd, bytes(data))
            finally:
                os.close(fd)
        finally:
            os.close(jobs_fd)
        return os.path.join(self.jobs_dir, name)

    def _remove_cookies(self, job):
        path, job.cookies = job.cookies, None
        if path:
            with contextlib.suppress(OSError):
                os.unlink(path)

    async def _start(self, job):
        try:
            job.nonce = se.new_id()
            job.events_ok = await self._event(
                {'t': 'job_start', 'ts': job.started, 'run': job.hex})
            try:
                job.cookies = await asyncio.to_thread(self._copy_cookies, job)
            except OSError as err:
                _log.warning('[jobs] cannot copy %s for job %s (%s); running '
                             'without cookies', self.cookies_file, job.id, err)
            argv = await asyncio.to_thread(self.argv, job, job.events_ok)
            job.parser = se.LogParser(job.nonce, run=job.hex, unmarked=True)
            job.log = await self.open_log(job)
            spawn = self.spawn or asyncio.create_subprocess_exec
            job.proc = await spawn(
                *argv, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        except Exception as err:
            _log.warning('[jobs] cannot start job %s: %s', job.id, err)
            await self._log_write(job, f'Error: {err}\n')
            job.task = asyncio.ensure_future(
                self._finish(job, f'could not start yt-dlp: {err}'))
            return
        job.pump = asyncio.ensure_future(self._pump(job))
        job.task = asyncio.ensure_future(self._supervise(job))
        if job.cancelling or self.closed:
            job.end_state = job.end_state or 'interrupted'
            job.terminator = asyncio.ensure_future(self._terminate(
                job, self.cancel_grace if job.cancelling
                else self.shutdown_grace))

    async def _pump(self, job):
        """Copy yt-dlp's output to the log and feed the parser, to the end
        of the pipe; then await yt-dlp."""
        proc = job.proc
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        splitter = LineSplitter()
        try:
            while chunk := await proc.stdout.read(OUTPUT_CHUNK_BYTES):
                text = decoder.decode(chunk)
                await job.log.write(text)
                if text:
                    job.newline = text.endswith('\n')
                self._parse(job, splitter.feed(text))
            job.eof = True
            text = decoder.decode(b'', final=True)
            await job.log.write(text)
            if text:
                job.newline = text.endswith('\n')
            self._parse(job, splitter.feed(text) + splitter.flush())
            await proc.wait()
        finally:
            # After an error or a cancellation: kill the group, drain the
            # pipe (a paused reader never sees its end), reap yt-dlp.
            if proc.returncode is None or not job.eof:
                self._signal(job, signal.SIGKILL)
            if not job.eof:
                while await proc.stdout.read(OUTPUT_CHUNK_BYTES):
                    pass
                job.eof = True
            await proc.wait()

    def _parse(self, job, lines):
        for line in lines:
            job.parser.feed(line)

    def _signal(self, job, sig):
        """Signal the job's process group, as long as it can still exist:
        yt-dlp not reaped yet, or something still holds its output open."""
        proc = job.proc
        if proc is None or (proc.returncode is not None and job.eof):
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            self.killpg(proc.pid, sig)

    async def _supervise(self, job):
        await asyncio.wait({job.pump})
        failure = None
        if not job.pump.cancelled() and job.pump.exception() is not None:
            failure = str(job.pump.exception()) or 'error'
            await self._log_write(job, f'Error: {failure}\n')
        await self._finish(job, failure)

    async def _log_write(self, job, text):
        """Write our own lines to a job's log, never waiting for long: the
        log may be the very thing that is stuck."""
        if job.log is None:
            return
        if not job.newline:
            text = '\n' + text
        try:
            await asyncio.wait_for(job.log.write(text), self.log_timeout)
            job.newline = text.endswith('\n')
        except Exception as err:
            _log.warning('[jobs] cannot write the log of job %s: %r', job.id,
                         err)

    async def _terminate(self, job, grace):
        """SIGTERM, SIGKILL after ``grace`` seconds, and if the copy is still
        stuck (e.g. writing the log), cancel it; its cleanup reaps yt-dlp."""
        self._signal(job, signal.SIGTERM)
        for wait, sig in ((grace, signal.SIGKILL), (self.kill_grace, None)):
            done, _ = await asyncio.wait({job.pump}, timeout=wait)
            if done:
                return
            if sig is not None:
                self._signal(job, sig)
        job.pump.cancel()

    def _job_events(self, job):
        try:
            return [e for e in se.read_events(self.status_dir, run=job.hex)
                    if e.get('t') in ('start', 'end')
                    and e.get('src') == 'manual']
        except OSError:
            return []

    def _outcome(self, job, events, failure):
        """(state, title, error) of a job that has ended."""
        parser = job.parser
        result = {'new': 0, 'skipped': 0, 'failed': 0, 'errors': []}
        entry = None
        if parser is not None:
            parser.add_events(events)
            parser.finish()
            entry = parser.entries.get(None)
            if entry is not None:
                result = parser.entry_result(entry)
        rc = job.proc.returncode if job.proc is not None else None
        if job.end_state:
            state = job.end_state
        elif failure is not None:
            state = 'failed'
        elif rc in OK_CODES:
            state = ('already' if not result['new'] and not result['failed']
                     and result['skipped'] else 'done')
            if (state == 'done' and not result['new'] and not result['failed']
                    and result.get('filtered')):
                # e.g. a live stream or premiere under args.conf's default
                # --match-filter '!is_live': nothing was downloaded.
                state = 'failed'
                failure = ('Nothing downloaded: skipped by --match-filter in '
                           'args.conf (a live stream or premiere?).')
        else:
            state = 'partial' if result['new'] else 'failed'
        title = ''
        starts = [e['i'] for e in events if e.get('t') == 'start']
        if starts:
            first = starts[0]
            title = (first.get('playlist_title') if first.get('n_entries')
                     else '') or first.get('title')
        if not title and entry is not None:
            title = entry.playlist or (entry.progress or {}).get('info.title')
        error = None
        if state in ('failed', 'partial'):
            errors = result['errors']
            pick = next((e for e in errors if e['kind'] != 'other'),
                        errors[0] if errors else None)
            if failure is not None:
                error = {'kind': 'other', 'msg': failure}
            elif pick is not None:
                error = {'kind': pick['kind'], 'msg': pick['msg']}
            else:
                error = {'kind': 'other',
                         'msg': f'yt-dlp exited with code {rc}'}
            error['msg'] = _clean(error['msg'])
        return state, _clean(title), error

    async def _finish(self, job, failure=None):
        try:
            events = (await asyncio.to_thread(self._job_events, job)
                      if job.events_ok else [])
            state, title, error = self._outcome(job, events, failure)
        except Exception as err:   # never leave a job running
            _log.exception('[jobs] cannot evaluate job %s', job.id)
            state, title, error = 'failed', '', {'kind': 'other',
                                                 'msg': _clean(str(err))}
        job.rc = job.proc.returncode if job.proc is not None else None
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._remove_cookies, job)
        job.state, job.title, job.error = state, title, error
        job.ended = self._now()
        if job.log is not None:
            if job.rc not in OK_CODES + (None,):
                await self._log_write(
                    job, f'[youtube-dl] yt-dlp exited with code {job.rc}\n')
            await self._log_write(job, ENDED)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(job.log.close(), self.log_timeout)
        event = {'t': 'job_end', 'ts': job.ended, 'run': job.hex,
                 'state': state, 'rc': job.rc}
        if title:
            event['title'] = title
        if error:
            event['error'] = error
        await self._event(event)
        self.running.pop(job.id, None)
        self._remember(job)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._prune_logs)
        await self._dispatch()

    def _prune_logs(self):
        """Keep the newest MAX_LOGS job logs (never those of active jobs)."""
        try:
            status_fd = se.open_status_dir(self.status_dir, create=False)
        except OSError:
            return
        try:
            try:
                jobs_fd = _open_dir(JOBS_DIR, status_fd, create=False)
            except OSError:
                return
            try:
                logs = []
                with os.scandir(jobs_fd) as entries:
                    for entry in entries:
                        # Cookie copies left by a job that was killed.
                        match = _COOKIES_NAME_RE.match(entry.name)
                        if match and match.group(1) not in self.running:
                            with contextlib.suppress(OSError):
                                os.unlink(entry.name, dir_fd=jobs_fd)
                            continue
                        match = _LOG_NAME_RE.match(entry.name)
                        if not match or not entry.is_file(
                                follow_symlinks=False):
                            continue
                        if (match.group(1) in self.running
                                or match.group(1) in self.queued):
                            continue
                        logs.append((entry.stat(follow_symlinks=False)
                                     .st_mtime_ns, entry.name))
                logs.sort()
                for _mtime, name in logs[:max(0, len(logs) - MAX_LOGS)]:
                    with contextlib.suppress(OSError):
                        os.unlink(name, dir_fd=jobs_fd)
            finally:
                os.close(jobs_fd)
        finally:
            os.close(status_fd)

    # -- cancel and retry ------------------------------------------------------

    def get(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            raise UnknownJob('No such download.')
        return job

    async def cancel(self, job_id):
        """Cancel a queued job (it never starts) or stop a running one;
        returns its ticket. Raises UnknownJob or NotActive."""
        job = self.get(job_id)
        if job.id in self.queued:
            del self.queued[job.id]
            job.state = job.end_state = 'cancelled'
            job.ended = self._now()
            self._remember(job)
            await self._event({'t': 'job_end', 'ts': job.ended,
                               'run': job.hex, 'state': 'cancelled',
                               'rc': None})
            return self.ticket(job)
        if job.id not in self.running:
            raise NotActive('The download has already ended.')
        if not job.cancelling:
            job.cancelling = True
            job.end_state = 'cancelled'
            if job.pump is not None:
                job.terminator = asyncio.ensure_future(
                    self._terminate(job, self.cancel_grace))
        return self.ticket(job)

    async def retry(self, job_id, validate, force=None):
        """Queue a finished job's URL again (validated again by
        ``validate``); ``force`` None keeps the job's own choice."""
        job = self.get(job_id)
        if job.id in self.queued or job.id in self.running:
            raise JobActive('The download is still queued or running.')
        if not job.url:
            raise UnknownJob('The URL of this download is not known.')
        url = validate(job.url)
        return await self.submit(url, by='basic',
                                 force=job.force if force is None else force)

    # -- start and shutdown ------------------------------------------------------

    async def start(self, validate):
        """After a restart of the web UI: queue jobs that never started
        again (validated again, if not older than REQUEUE_AGE), and mark
        jobs that were running as interrupted."""
        records = await asyncio.to_thread(scan_jobs, self.status_dir)
        with contextlib.suppress(Exception):   # also stale cookie copies
            await asyncio.to_thread(self._prune_logs)
        now = self._now()
        for run, record in records.items():
            job_id = str(uuid.UUID(run))
            job = Job(job_id, record['url'], record['by'] or 'basic',
                      record['force'], record['queued'])
            job.started = record['started']
            if record['ended'] is not None:
                job.state = record['state']
                job.ended = record['ended']
                self._remember(job)
                continue
            requeue = (record['started'] is None and record['url']
                       and record['queued'] is not None
                       and now - record['queued'] <= REQUEUE_AGE
                       and len(self.queued) < self.max_queued)
            error = None
            if requeue:
                try:
                    job.url = validate(record['url'])
                    job.key = url_key(job.url)
                except Exception as err:
                    requeue = False
                    error = {'kind': 'other', 'msg': _clean(
                        'URL not allowed any more: '
                        + str(getattr(err, 'detail', err)))}
            if requeue and self._duplicate(job.key) is None:
                job.ready = True
                self.queued[job.id] = job
                self._remember(job)
                continue
            job.state = 'failed' if error else 'interrupted'
            job.error = error
            job.ended = now
            self._remember(job)
            event = {'t': 'job_end', 'ts': now, 'run': job.hex,
                     'state': job.state, 'rc': None}
            if error:
                event['error'] = error
            await self._event(event)
        await self._dispatch()

    async def shutdown(self):
        """Stop running jobs (they become interrupted); queued jobs stay
        queued in the event file and are resumed by the next start."""
        self.closed = True
        starting = {job.starter for job in self.running.values()
                    if job.starter is not None and not job.starter.done()}
        if starting:
            await asyncio.wait(starting, timeout=self.shutdown_grace)
        jobs = list(self.running.values())
        for job in jobs:
            if not job.end_state:
                job.end_state = 'interrupted'
            if job.pump is not None and job.terminator is None:
                job.terminator = asyncio.ensure_future(
                    self._terminate(job, self.shutdown_grace))
        tasks = {job.task for job in jobs if job.task is not None}
        if tasks:
            _done, pending = await asyncio.wait(
                tasks, timeout=self.shutdown_grace + self.kill_grace + 1)
            for task in pending:
                task.cancel()
