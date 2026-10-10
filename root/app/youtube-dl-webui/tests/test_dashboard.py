"""Tests for dashboard.py and the read APIs: GET /api/status and
GET /api/log/...?since=.

The pass fixtures under fixtures/status/ (see test_status_events.py) are a
real pass of youtube-dl.sh with the web UI enabled: 5 entries, run id
1998e886921669d3, nonce 593e0c5364f0d919, pid 1056.
"""

import json
import os
import stat
import sys
import time
import types
import uuid
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dashboard  # noqa: E402
import status_events as se  # noqa: E402
from conftest import basic, bearer  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'fixtures', 'status')
PASS_RUN = '1998e886921669d3'
PASS_NONCE = '593e0c5364f0d919'
PASS_PID = 1056
PASS_TS = 1791494421
SCRIPT_CMDLINE = b'/bin/bash\0/app/youtube-dl/youtube-dl.sh\0'
NOW = 1791500000


def _fixture(name):
    with open(os.path.join(FIXTURES, name, 'events.jsonl')) as f:
        events = [json.loads(line) for line in f]
    log_name = 'job.log' if name == 'manual_mixed' else 'youtube-dl.log'
    with open(os.path.join(FIXTURES, name, log_name)) as f:
        lines = f.read().split('\n')
    return events, lines


class Env:
    """A status directory, pass log, /downloads, fake /proc and cookies."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.status_dir = str(tmp_path / 'status')
        os.mkdir(self.status_dir, 0o700)
        self.events_file = os.path.join(self.status_dir, se.EVENTS_NAME)
        self.log = str(tmp_path / 'youtube-dl.log')
        self.downloads = str(tmp_path / 'downloads')
        os.mkdir(self.downloads)
        self.proc = str(tmp_path / 'proc')
        os.mkdir(self.proc)
        self.cookies = str(tmp_path / 'cookies.txt')
        self.now = NOW

    def events(self, events, name=se.EVENTS_NAME, mode='a'):
        with open(os.path.join(self.status_dir, name), mode) as f:
            for event in events:
                f.write((event if isinstance(event, str)
                         else json.dumps(event)) + '\n')

    def set_log(self, lines):
        with open(self.log, 'w') as f:
            f.write('\n'.join(lines) + '\n')

    def process(self, pid, cmdline=SCRIPT_CMDLINE, started=None):
        base = os.path.join(self.proc, str(pid))
        os.makedirs(base, exist_ok=True)
        with open(os.path.join(base, 'cmdline'), 'wb') as f:
            f.write(cmdline)
        if started is not None:
            btime = 1000
            ticks = int((started - btime) * os.sysconf('SC_CLK_TCK'))
            fields = ['S'] + ['0'] * 18 + [str(ticks)] + ['0'] * 10
            with open(os.path.join(base, 'stat'), 'w') as f:
                f.write(f'{pid} (youtube-dl.sh) ' + ' '.join(fields) + '\n')
            with open(os.path.join(self.proc, 'stat'), 'w') as f:
                f.write(f'cpu 1 2 3\nbtime {btime}\nprocesses 5\n')

    def dash(self, interval='3h'):
        return dashboard.Dashboard(
            self.status_dir, self.log, self.downloads, self.cookies,
            proc_dir=self.proc, interval=interval, clock=lambda: self.now)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def run_start(run='a' * 16, ts=NOW - 100, pid=4242, nonce='b' * 16,
              channels=3, interval='3h'):
    return {'v': 1, 't': 'run_start', 'ts': ts, 'run': run, 'nonce': nonce,
            'pid': pid, 'channels': channels, 'interval': interval,
            'ytdlp': '2026.08.19'}


def run_end(run='a' * 16, ts=NOW - 50, rc=0, nxt=NOW + 3600, rows=(),
            err=(), total=None):
    return {'v': 1, 't': 'run_end', 'ts': ts, 'run': run, 'rc': rc,
            'next': nxt, 'partial': False,
            'sum': total or {'new': 0, 'skipped': 0, 'failed': 0,
                             'filtered': 0},
            'ch': list(rows), 'err': list(err)}


def end(vid, epoch, src='scheduled', run='a' * 16, done=True, title='T',
        ch=None):
    info = {'id': vid, 'title': title, 'channel': 'C', 'epoch': epoch}
    if done:
        info['requested_downloads.:.filepath'] = [f'/downloads/{vid}.mp4']
    event = {'v': 1, 't': 'end', 'src': src, 'run': run, 'i': info}
    if ch is not None:
        event['ch'] = ch
    return event


# -- clean -------------------------------------------------------------------

def test_clean_strips_bidi_ansi_and_controls():
    raw = ('\x1b[31mred\x1b[0m \u202egnp.exe\u202c \u2066x\u2069 a\x00b\x07c'
           '\x9b d\u200f e\u2028f\tg\nh')
    assert dashboard.clean(raw) == 'red gnp.exe x abc d e f g h'
    assert dashboard.clean('x' * 400) == 'x' * 299 + '…'
    assert dashboard.clean(None) == '' and dashboard.clean(5) == ''


def test_clean_url_drops_instead_of_cutting():
    assert dashboard.clean_url('https://a/\u202eb') == 'https://a/b'
    assert dashboard.clean_url('https://a/' + 'x' * 2100) is None
    assert dashboard.clean_url('') is None and dashboard.clean_url(1) is None


# -- LineReader --------------------------------------------------------------

def _append(path, data, mode='ab'):
    with open(path, mode) as f:
        f.write(data)


def test_line_reader_returns_only_complete_lines(tmp_path):
    path = str(tmp_path / 'log')
    reader = dashboard.LineReader(path)
    assert reader.read().lines == []            # no file yet
    _append(path, b'one\ntw')
    first = reader.read()
    assert first.lines == ['one'] and not first.reset and not first.more
    assert reader.read().lines == []
    _append(path, b'o\nthree\n')
    assert reader.read().lines == ['two', 'three']
    assert reader.read().lines == []


def test_line_reader_starts_over_after_truncation(tmp_path):
    path = str(tmp_path / 'log')
    _append(path, b'a long first line\nsecond\n')
    reader = dashboard.LineReader(path)
    assert reader.read().lines == ['a long first line', 'second']
    _append(path, b'new\n', mode='wb')        # tee truncates the log
    result = reader.read()
    assert result.reset and result.lines == ['new']


def test_line_reader_notices_truncate_and_regrow(tmp_path):
    # Emptied and grown past the old offset between two reads: the size
    # alone does not show it, the first bytes do.
    path = str(tmp_path / 'log')
    _append(path, b'2026-10-08 10:00:00 - starting execution\nold\n')
    reader = dashboard.LineReader(path)
    reader.read()
    _append(path, b'2026-10-08 13:00:00 - starting execution\nnew line 1\n'
            b'new line 2\n', mode='wb')
    result = reader.read()
    assert result.reset
    assert result.lines == ['2026-10-08 13:00:00 - starting execution',
                            'new line 1', 'new line 2']


def test_line_reader_finishes_a_rotated_file_first(tmp_path):
    path = str(tmp_path / 'events.jsonl')
    _append(path, b'1\n2\n')
    reader = dashboard.LineReader(path)
    assert reader.read().lines == ['1', '2']
    _append(path, b'3\n')                       # written before the rotation
    os.replace(path, path + '.1')
    _append(path, b'4\n')
    result = reader.read()
    assert result.lines == ['3', '4'] and result.reset
    _append(path, b'5\n')
    assert reader.read().lines == ['5']


def test_line_reader_byte_cap(tmp_path):
    path = str(tmp_path / 'log')
    _append(path, b''.join(b'%04d\n' % i for i in range(1000)))   # 5000 B
    reader = dashboard.LineReader(path)
    seen = []
    reads = 0
    while True:
        result = reader.read(max_bytes=1024)
        seen += result.lines
        reads += 1
        if not result.more:
            break
    assert seen == ['%04d' % i for i in range(1000)] and reads >= 5


def test_line_reader_skips_overlong_lines(tmp_path):
    path = str(tmp_path / 'log')
    _append(path, b'ok1\n' + b'x' * 300 + b'\nok2\n' + b'y' * 250)
    reader = dashboard.LineReader(path, max_line=100)
    assert reader.read().lines == ['ok1', 'ok2']
    _append(path, b'y' * 100 + b'\nok3\n')      # end of the long line
    assert reader.read().lines == ['ok3']


def test_line_reader_refuses_symlinks_and_fifos(tmp_path):
    target = tmp_path / 'secret'
    target.write_text('secret\n')
    link = tmp_path / 'log'
    link.symlink_to(target)
    assert dashboard.LineReader(str(link)).read().lines == []
    fifo = tmp_path / 'fifo'
    os.mkfifo(fifo)
    assert dashboard.LineReader(str(fifo)).read().lines == []   # no hang


def test_events_in_a_symlinked_status_dir_are_not_read(env, tmp_path):
    env.events([run_start()])
    linked = str(tmp_path / 'linked')
    os.symlink(env.status_dir, linked)
    dash = dashboard.Dashboard(linked, env.log, env.downloads, env.cookies,
                               proc_dir=env.proc, clock=lambda: NOW)
    assert dash.status()['scheduler']['state'] == 'nodata'


# -- scheduler state -----------------------------------------------------------

def test_state_nodata(env):
    status = env.dash().status()
    assert status['scheduler']['state'] == 'nodata'
    assert status['current'] is None and status['last_run'] is None
    assert status['runs'] == [] and status['channels'] == []


def test_state_running_needs_the_script_process(env):
    env.events([run_start(pid=4242)])
    env.process(4242)
    status = env.dash().status()
    assert status['scheduler']['state'] == 'running'
    assert status['scheduler']['since'] == NOW - 100
    assert status['current']['state'] == 'running'
    assert status['runs'][0]['result'] == 'running'


@pytest.mark.parametrize('cmdline', [None, b'/usr/bin/python3\0x.py\0',
                                     b'/bin/bash\0youtube-dl.sh.bak\0'])
def test_state_interrupted_without_the_script(env, cmdline):
    env.events([run_start(pid=4242)])
    if cmdline is not None:
        env.process(4242, cmdline)
    status = env.dash().status()
    assert status['scheduler']['state'] == 'interrupted'
    assert status['current']['state'] == 'interrupted'
    assert status['runs'][0]['result'] == 'interrupted'


def test_a_reused_pid_is_not_the_pass(env):
    # After a container restart the same pid may belong to a new script
    # that has not written its run_start yet.
    env.events([run_start(pid=4242, ts=NOW - 100)])
    env.process(4242, started=NOW - 10)
    assert env.dash().status()['scheduler']['state'] == 'interrupted'
    env.process(4242, started=NOW - 101)
    assert env.dash().status()['scheduler']['state'] == 'running'


def test_state_sleeping_and_oneshot(env):
    env.events([run_start(), run_end(nxt=NOW + 3600)])
    env.process(4242)   # the script sleeps; it is still alive
    status = env.dash().status()
    assert status['scheduler']['state'] == 'sleeping'
    assert status['scheduler']['next'] == NOW + 3600
    assert status['scheduler']['interval_secs'] == 10800
    assert status['current'] is None
    assert status['last_run']['result'] == 'ok'
    oneshot = env.dash(interval='false').status()['scheduler']
    assert oneshot['state'] == 'oneshot' and oneshot['oneshot']
    assert oneshot['next'] is None and oneshot['interval_secs'] is None


def test_running_in_oneshot_mode(env):
    env.events([run_start(interval='false')])
    env.process(4242)
    scheduler = env.dash(interval='false').status()['scheduler']
    assert scheduler['state'] == 'running' and scheduler['oneshot']


def test_superseded_run_is_interrupted(env):
    env.events([run_start(run='1' * 16, ts=NOW - 900, pid=10),
                run_start(run='2' * 16, ts=NOW - 100, pid=4242),
                run_end(run='2' * 16, ts=NOW - 50)])
    env.process(10)   # even if its pid was reused
    runs = env.dash().status()['runs']
    assert [(r['id'], r['result']) for r in runs] == [
        ('2' * 16, 'ok'), ('1' * 16, 'interrupted')]
    assert runs[0]['duration'] == 50


# -- counts --------------------------------------------------------------------

def test_last_24h_boundary_and_dedupe(env):
    env.events([
        end('old', NOW - 86401),                  # just outside
        end('in', NOW - 86399),
        end('in', NOW - 3600),                    # same video again: once
        end('in', NOW - 60, src='manual', run='c' * 32),   # other source
        end('fail', NOW - 60, done=False),         # failed: not counted
        end('future', NOW + 30),                   # clock skew: counts now
        end('week', NOW - 6 * 86400 - 10),
        end('ancient', NOW - 8 * 86400),
    ])
    counts = env.dash().status()['counts']
    assert counts['last_24h'] == {'total': 3, 'scheduled': 2, 'manual': 1}
    assert counts['daily'] == [1, 0, 0, 0, 0, 1, 3]


def test_recent_lists_downloads_newest_first(env):
    env.events([end(f'v{i}', NOW - 100 + i, title=f'Title {i}')
                for i in range(15)] + [end('bad', NOW, done=False)])
    recent = env.dash().status()['recent']
    assert [r['id'] for r in recent] == [f'v{i}' for i in range(14, 4, -1)]
    assert recent[0] == {'src': 'scheduled', 'id': 'v14', 'title': 'Title 14',
                         'channel': 'C', 'ts': NOW - 86}


# -- live pass -----------------------------------------------------------------

def _cut_pass(env, until_line, until_event):
    """The fixture pass as it looked in the middle: the log up to the first
    line containing ``until_line``, the events up to ``until_event``."""
    events, lines = _fixture('pass_default')
    cut = next(i for i, line in enumerate(lines) if until_line in line)
    stop = next(i for i, e in enumerate(events) if until_event(e))
    env.events(events[:stop + 1])
    env.set_log(lines[:cut + 1])
    env.process(PASS_PID)
    env.now = PASS_TS + 20
    return events, lines


def test_live_pass_progress_counts_and_pending(env):
    _cut_pass(env, '0.1% of  832.66KiB at  Unknown B/s ETA Unknown @@ytdl:'
              + PASS_NONCE + ' {"progress.status": "downloading", '
              '"progress.downloaded_bytes": 1024, "progress.total_bytes": '
              '852639, "info.id": "mixed-1"',
              lambda e: e['t'] == 'start' and e['i']['id'] == 'mixed-1')
    status = env.dash().status()
    current = status['current']
    assert status['scheduler']['state'] == 'running'
    assert current['channel'] == {'index': 2, 'total': 5,
                                  'url': 'http://127.0.0.1:8765/mixed.html',
                                  'name': 'Mixed Page'}
    assert current['item'] == {'index': 4, 'count': 4, 'source': 'events'}
    video = current['video']
    assert video['id'] == 'mixed-1' and video['title'] == 'Mixed Page (1)'
    assert video['phase'] == 'downloading'
    assert (video['downloaded'], video['total']) == (1024, 852639)
    assert video['percent'] == round(100 * 1024 / 852639, 1)
    # Channel 2 of 5, item 4 of 4 at 0.12 %.
    assert current['overall'] == round(
        100 * (1 + (3 + 1024 / 852639) / 4) / 5, 1)
    # Entry 1 is over: v1 new, v6 archived, v4 filtered, v2/v3/v5 failed.
    # Entry 2: mixed-4 new, mixed-3 and mixed-2 failed, mixed-1 running.
    assert current['counts'] == {'new': 2, 'skipped': 1, 'failed': 5,
                                 'filtered': 1}
    assert status['pending'] == {'queued': 0, 'channels_left': 3,
                                 'in_channel_max': 0}
    assert current['channels_left'] == 3 and current['in_channel_max'] == 0
    kinds = {g['kind']: g['count'] for g in status['problems']}
    assert kinds == {'bot_check': 1, 'rate_limit': 2, 'unavailable': 2}
    assert status['problems'][0]['kind'] == 'bot_check'   # most severe first


def test_live_video_with_an_error_counts_as_failed(env):
    # The bot check: v2 started and logged its ERROR, the next item has not
    # started yet. v2 is no longer in progress.
    _cut_pass(env, 'ERROR: [youtube] v2: No video formats found!',
              lambda e: e['t'] == 'start' and e['i']['id'] == 'v2')
    current = env.dash().status()['current']
    assert current['counts'] == {'new': 0, 'skipped': 1, 'failed': 3,
                                 'filtered': 1}
    assert current['item'] == {'index': 5, 'count': 6, 'source': 'events'}


def test_live_pass_between_items_and_processing(env):
    # While the first channel's items are checked: no video, item from log.
    _cut_pass(env, '[youtube] Extracting URL: fakeunavail:v3',
              lambda e: e['t'] == 'run_start')
    current = env.dash().status()['current']
    assert current['video'] is None
    assert current['item'] == {'index': 4, 'count': 6, 'source': 'log'}
    assert current['overall'] == round(100 * (3 / 6) / 5, 1)
    assert current['in_channel_max'] == 2


def test_live_pass_postprocessing(env, tmp_path):
    _cut_pass(env, '[postprocess] Metadata started @@ytdl:' + PASS_NONCE
              + ' {"progress.status": "started", "progress.postprocessor": '
              '"Metadata", "info.id": "v1"}',
              lambda e: e['t'] == 'start' and e['i']['id'] == 'v1')
    video = env.dash().status()['current']['video']
    assert video['id'] == 'v1' and video['phase'] == 'processing'
    assert video['postprocessor'] == 'Metadata' and video['percent'] is None


def test_live_pass_follows_new_lines_and_events(env):
    events, lines = _cut_pass(env, '[download] Downloading item 1 of 4',
                              lambda e: e['t'] == 'run_start')
    dash = env.dash()
    assert dash.status()['current']['channel']['index'] == 2
    # The rest of the pass arrives.
    env.events(events[1:])
    env.set_log(lines)
    status = dash.status()
    assert status['current']['channel'] is None      # all entries ended
    assert status['current']['overall'] == 100.0
    assert status['current']['counts'] == {'new': 5, 'skipped': 4,
                                           'failed': 6, 'filtered': 1}


def test_a_new_pass_log_is_parsed_again(env):
    _cut_pass(env, '[download] Downloading item 1 of 4',
              lambda e: e['t'] == 'run_start')
    dash = env.dash()
    assert dash.status()['current']['counts']['skipped'] == 1
    # tee truncated the log for the next pass, which has no run_start yet.
    env.set_log(['', '2026-10-08 21:30:00 - starting execution',
                 '[download] v9: x has already been recorded in the archive'])
    status = dash.status()
    assert status['current']['counts']['skipped'] == 0
    assert status['current']['channel'] is None


def _finished_pass(env):
    """The whole fixture pass plus the run_end that 'end' writes."""
    events, lines = _fixture('pass_default')
    env.events(events)
    env.set_log(lines)
    # 'end' stamps run_end with the clock: use the fixture's time, or the
    # 24 h counters would depend on the age of the fixture.
    clock = types.SimpleNamespace(
        time=lambda: max(e['ts'] for e in events if 'ts' in e) + 5,
        monotonic=time.monotonic, sleep=time.sleep)
    with mock.patch.object(se, 'time', clock):
        assert se.main(['end', f'--status-dir={env.status_dir}',
                        f'--run={PASS_RUN}', f'--nonce={PASS_NONCE}',
                        '--rc=1', '--interval=3h', f'--log={env.log}',
                        '--wait=0']) == 0
    with open(env.events_file) as f:
        return json.loads(f.read().splitlines()[-1])


def test_finished_pass(env):
    event = _finished_pass(env)
    env.process(PASS_PID)
    env.now = event['ts'] + 60
    status = env.dash().status()
    assert status['scheduler']['state'] == 'sleeping'
    assert status['scheduler']['next'] == event['next']
    assert status['current'] is None
    last = status['last_run']
    assert last['result'] == 'failed' and last['rc'] == 1
    assert last['sum'] == {'new': 5, 'skipped': 4, 'failed': 6, 'filtered': 1}
    channels = {c['idx']: c for c in status['channels']}
    assert [c['idx'] for c in status['channels']] == [4, 5, 1, 2, 3]
    assert channels[4]['status'] == 'error'       # unsupported URL
    assert channels[5]['status'] == 'error'       # unparsable '| args'
    assert channels[1]['status'] == 'failed'
    assert channels[1]['name'] == 'Fake Channel'
    assert channels[1]['last'] == {'new': 1, 'skipped': 1, 'failed': 3,
                                   'filtered': 1, 'rc': 1, 'kind': 'bot_check'}
    assert channels[1]['fail_streak'] == 1 and channels[1]['last_new']
    assert channels[3]['status'] == 'ok' and channels[3]['fail_streak'] == 0
    groups = {g['kind']: g for g in status['problems']}
    assert set(groups) == {'bot_check', 'rate_limit', 'unavailable', 'other'}
    # forged01 comes from a forged title (cosmetic): 'Download now' builds a
    # watch URL from a YouTube id only when the line names the extractor.
    bot = {i['id']: i for i in groups['bot_check']['items']}
    assert bot['forged01']['url'] is None   # not an 11-character id
    assert bot['v2']['url'] is None          # webpage_url is not http(s)
    rate = {i['id']: i for i in groups['rate_limit']['items']}
    assert rate['mixed-3']['url'] == 'http://127.0.0.1:8765/mixed.html'
    assert status['cookies']['bot_checks_24h'] == 2


def test_problems_from_the_last_run_and_failed_jobs(env):
    job = 'd' * 32
    err = [{'idx': 1, 'id': 'dQw4w9WgXcQ', 'ie': 'youtube', 'kind': 'bot_check',
            'msg': 'Sign in to confirm you’re not a bot'},
           {'idx': 1, 'id': 'dQw4w9WgXcQ', 'ie': 'youtube', 'kind': 'bot_check',
            'msg': 'duplicate'},
           {'idx': 2, 'id': None, 'kind': 'nonsense', 'msg': 'x\x1b[31m'}]
    env.events([
        run_start(), run_end(err=err, rows=[[1, 0, 0, 1, 0, 1, 3, NOW - 60,
                                             'https://www.youtube.com/@a']]),
        {'v': 1, 't': 'job_queued', 'ts': NOW - 30, 'run': job,
         'url': 'https://youtu.be/abc', 'by': 'token'},
        {'v': 1, 't': 'job_start', 'ts': NOW - 29, 'run': job},
        {'v': 1, 't': 'job_end', 'ts': NOW - 20, 'run': job, 'state': 'failed',
         'rc': 1, 'error': {'kind': 'rate_limit', 'msg': 'HTTP Error 429'}},
    ])
    status = env.dash().status()
    groups = {g['kind']: g for g in status['problems']}
    assert [g['kind'] for g in status['problems']] == [
        'bot_check', 'other', 'rate_limit']
    bot = groups['bot_check']
    assert bot['count'] == 1 and bot['severity'] == 'error'
    assert bot['hint'] == se.KIND_HINTS['bot_check']
    assert bot['items'][0]['url'] == 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
    assert bot['items'][0]['ts'] == NOW - 60 and bot['items'][0]['channel'] == 1
    assert groups['other']['items'][0]['msg'] == 'x'
    manual = groups['rate_limit']['items'][0]
    assert manual['src'] == 'manual' and manual['url'] == 'https://youtu.be/abc'
    finished = status['jobs']['finished']
    assert finished[0]['id'] == str(uuid.UUID(job))
    assert finished[0]['state'] == 'failed' and finished[0]['src'] == 'ios'


def test_running_and_queued_jobs_from_the_snapshot(env):
    running, queued, ended = 'e' * 32, 'f' * 32, '1' * 32
    start = {'id': 'v2', 'title': 'Part 2', 'channel': 'Chan',
             'playlist_title': 'Lectures', 'playlist_autonumber': 2,
             'n_entries': 40,
             'requested_formats.:.format_id': ['137', '140'],
             'requested_formats.:.filesize': [900, 100]}
    env.events([
        {'v': 1, 't': 'job_queued', 'ts': NOW - 60, 'run': running,
         'url': 'https://www.youtube.com/playlist?list=PL1', 'by': 'token'},
        {'v': 1, 't': 'job_start', 'ts': NOW - 59, 'run': running},
        {'v': 1, 't': 'start', 'src': 'manual', 'run': running,
         'i': dict(start, id='v1', title='Part 1', playlist_autonumber=1)},
        end('v1', NOW - 50, src='manual', run=running),
        {'v': 1, 't': 'start', 'src': 'manual', 'run': running, 'i': start},
        # Ended in the events, still running in the snapshot (a race).
        {'v': 1, 't': 'job_queued', 'ts': NOW - 5, 'run': ended,
         'url': 'https://example.com/x', 'by': 'basic'},
        {'v': 1, 't': 'job_end', 'ts': NOW - 1, 'run': ended, 'state': 'done',
         'rc': 0},
    ])
    progress = {'progress.status': 'downloading', 'info.id': 'v2',
                'progress.downloaded_bytes': 50, 'progress.total_bytes': 100,
                'info.format_id': '140', 'info.vcodec': 'none',
                'info.acodec': 'mp4a', 'info.title': 'Part 2'}
    snapshot = {
        'running': [
            {'id': str(uuid.UUID(running)), 'url': 'https://www.youtube.com/'
             'playlist?list=PL1', 'by': 'token', 'force': False,
             'queued': NOW - 60, 'started': NOW - 59, 'cancelling': True,
             'progress': progress, 'phase': 'downloading', 'phase_id': 'v2',
             'open': 'v2', 'item': 7, 'n_items': 9, 'playlist': ''},
            {'id': str(uuid.UUID(ended)), 'url': 'https://example.com/x',
             'by': 'basic', 'force': False, 'queued': NOW - 5,
             'started': NOW - 4, 'cancelling': False, 'progress': None,
             'phase': '', 'phase_id': None, 'open': None, 'item': None,
             'n_items': None, 'playlist': ''}],
        'queued': [{'id': str(uuid.UUID(queued)), 'url': 'https://e.com/\x1b[1m',
                    'by': 'basic', 'force': True, 'queued': NOW, 'position': 1}],
    }
    status = env.dash().status(jobs=snapshot)
    jobs = status['jobs']
    first = jobs['running'][0]
    assert first['title'] == 'Lectures' and first['src'] == 'ios'
    assert first['cancelling'] is True and first['state'] == 'running'
    assert first['items'] == {'done': 1, 'failed': 0}
    assert first['item'] == {'index': 2, 'count': 40, 'source': 'events'}
    video = first['video']
    assert video['title'] == 'Part 2' and video['channel'] == 'Chan'
    # The audio stream (100 of 1000 bytes) is half done: 90 % + 5 %.
    assert video['percent'] == 95.0 and video['stream']['kind'] == 'audio'
    assert jobs['queued'] == [{
        'id': str(uuid.UUID(queued)), 'state': 'queued',
        'url': 'https://e.com/', 'src': 'web', 'force': True, 'queued': NOW,
        'position': 1}]
    assert status['pending']['queued'] == 1
    # A job still in the snapshot is not listed as finished as well.
    assert jobs['finished'] == []


# -- channels and runs -----------------------------------------------------------

def test_channel_history(env):
    url_a, url_b, url_c = ('https://www.youtube.com/@a', 'https://www.youtube.com/@b',
                           'https://www.youtube.com/@c')
    day = 86400
    runs = []
    for n, (rc_a, new_b) in enumerate([(1, 1), (1, 0), (1, 0)]):
        run = f'{n + 1:016x}'
        ts = NOW - 100 * day + n * 50 * day
        runs += [run_start(run=run, ts=ts),
                 {'v': 1, 't': 'start', 'src': 'scheduled', 'run': run, 'ch': 2,
                  'i': {'id': f'x{n}', 'channel': 'Channel B'}},
                 run_end(run=run, ts=ts + 10, rc=rc_a, rows=[
                     [1, 0, 0, 0, 0, rc_a, 1, ts + 1, url_a],
                     [2, new_b, 3, 0, 0, 0, 1, ts + 2, url_b],
                     [3, 0, 0, 0, 0, 0, 1, ts + 3, url_c]],
                     err=[{'idx': 1, 'id': 'PLx', 'kind': 'unavailable',
                           'msg': 'This playlist does not exist'}])]
    env.events(runs)
    channels = env.dash().status()['channels']
    by_url = {c['url']: c for c in channels}
    assert by_url[url_a]['fail_streak'] == 3
    assert by_url[url_a]['status'] == 'error'   # nothing downloaded, rc 1
    assert by_url[url_a]['last']['kind'] == 'unavailable'
    assert by_url[url_b]['name'] == 'Channel B'
    assert by_url[url_b]['last_new'] == NOW - 100 * day + 2
    assert by_url[url_b]['status'] == 'stale'   # nothing new for 100 days
    assert by_url[url_c]['status'] == 'stale'   # never anything new
    assert by_url[url_b]['last_checked'] == NOW + 2
    assert [c['url'] for c in channels] == [url_a, url_b, url_c]


def test_runs_are_bounded(env):
    events = []
    for n in range(40):
        run = f'{n + 1:016x}'
        events += [run_start(run=run, ts=NOW - 1000 + n),
                   run_end(run=run, ts=NOW - 990 + n)]
    env.events(events)
    dash = env.dash()
    status = dash.status()
    assert len(status['runs']) == 10 and len(dash.events.runs) == 30
    assert status['runs'][0]['id'] == f'{40:016x}'


# -- event file handling ----------------------------------------------------------

def test_rotated_file_is_read_at_startup(env):
    env.events([end('a', NOW - 10)], name=se.ROTATED_NAME)
    env.events([end('b', NOW - 5)])
    dash = env.dash()
    assert dash.status()['counts']['last_24h']['total'] == 2
    # Rotation while the web UI runs: nothing is counted twice.
    env.events([end('c', NOW - 4)])
    os.replace(env.events_file, os.path.join(env.status_dir, se.ROTATED_NAME))
    env.events([end('d', NOW - 3)])
    assert dash.status()['counts']['last_24h']['total'] == 4


def test_invalid_lines_are_ignored(env):
    env.events([
        'not json', '[1, 2]', '{"v": 2, "t": "run_start"}',
        {'v': 1, 't': 'run_start', 'run': 'NOT-HEX', 'ts': NOW},
        {'v': 1, 't': 'end', 'src': 'evil', 'run': 'a' * 16, 'i': {'id': 'x'}},
        {'v': 1, 't': 'mystery'},
        '{"v": 1, "t": "run_start", "run": "%s", "ts": NaN, "pid": "1",'
        ' "channels": Infinity}' % ('e' * 16),
    ])
    status = env.dash().status()
    assert status['runs'][0]['started'] is None
    assert status['runs'][0]['channels'] is None
    assert status['counts']['last_24h']['total'] == 0


# -- derived values ------------------------------------------------------------------

START = {'requested_formats.:.format_id': ['137', '140'],
         'requested_formats.:.vcodec': ['avc1', 'none'],
         'requested_formats.:.filesize': [400_000_000, 8_000_000],
         'requested_formats.:.tbr': [4000.0, 128.0]}


def _progress(**fields):
    p = {'progress.status': 'downloading', 'info.id': 'v'}
    p.update({('info.' if k in ('format_id', 'vcodec', 'acodec') else
               'progress.') + k: v for k, v in fields.items()})
    return p


def test_merged_formats_are_weighted_by_size():
    video = dashboard.video_progress(_progress(
        downloaded_bytes=200_000_000, total_bytes=400_000_000,
        format_id='137', vcodec='avc1', acodec='none'), START)
    assert video['percent'] == round(100 * 200 / 408, 1)
    assert video['stream'] == {'index': 1, 'count': 2, 'kind': 'video'}
    assert video['stream_percent'] == 50.0
    audio = dashboard.video_progress(_progress(
        downloaded_bytes=4_000_000, total_bytes=8_000_000, format_id='140',
        vcodec='none', acodec='mp4a'), START)
    assert audio['percent'] == round(100 * 404 / 408, 1)
    assert audio['stream'] == {'index': 2, 'count': 2, 'kind': 'audio'}


def test_misaligned_lists_fall_back_to_90_10():
    # filesize dropped the audio's None and there is no usable tbr.
    start = dict(START, **{'requested_formats.:.filesize': [400_000_000],
                           'requested_formats.:.tbr': [4000.0]})
    audio = dashboard.video_progress(_progress(
        downloaded_bytes=4, total_bytes=8, format_id='140'), start)
    assert audio['percent'] == 95.0
    start['requested_formats.:.vcodec'] = ['avc1']   # also misaligned
    video = dashboard.video_progress(_progress(
        downloaded_bytes=1, total_bytes=2, format_id='137'), start)
    assert video['percent'] == 45.0
    assert dashboard.stream_weights({'requested_formats.:.format_id': ['18']}) \
        is None


def test_tbr_is_used_without_sizes():
    start = dict(START)
    del start['requested_formats.:.filesize']
    assert dashboard.stream_weights(start) == (['137', '140'], [4000.0, 128.0])
    # A size of 0 is no size either.
    start['requested_formats.:.filesize'] = [0, 8_000_000]
    assert dashboard.stream_weights(start) == (['137', '140'], [4000.0, 128.0])


def test_video_progress_variants():
    fragments = dashboard.video_progress(_progress(
        fragment_index=3, fragment_count=6, downloaded_bytes=10))
    assert fragments['percent'] == 50.0 and fragments['fragment_count'] == 6
    unknown = dashboard.video_progress(_progress(downloaded_bytes=12_400_000))
    assert unknown['percent'] is None and unknown['downloaded'] == 12_400_000
    estimate = dashboard.video_progress(_progress(
        downloaded_bytes=1, total_bytes_estimate=4))
    assert estimate['percent'] == 25.0 and estimate['estimate']
    finished = dashboard.video_progress(_progress(status='finished'))
    assert finished['percent'] == 100.0
    processing = dashboard.video_progress(
        _progress(status='finished'), None, 'processing: EmbedThumbnail')
    assert processing['phase'] == 'processing'
    assert processing['postprocessor'] == 'EmbedThumbnail'
    assert processing['percent'] is None and processing['fraction'] == 1.0
    preparing = dashboard.video_progress(None)
    assert preparing['phase'] == 'preparing' and preparing['percent'] is None
    nan = dashboard.video_progress(_progress(
        downloaded_bytes=float('nan'), total_bytes=float('inf')))
    assert nan['percent'] is None and nan['downloaded'] is None


@pytest.mark.parametrize('item,url', [
    ({'id': 'dQw4w9WgXcQ', 'ie': 'youtube'},
     'https://www.youtube.com/watch?v=dQw4w9WgXcQ'),
    ({'id': 'dQw4w9WgXcQ', 'ie': 'youtube:tab'}, None),
    ({'id': 'dQw4w9WgX', 'ie': 'youtube'}, None),
    ({'id': 'dQw4w9WgXc&', 'ie': 'youtube'}, None),
    ({'id': 'v', 'url': 'https://www.youtube.com/watch?v=x'},
     'https://www.youtube.com/watch?v=x'),
    ({'id': 'v', 'url': 'javascript:alert(1)'}, None),
    ({'id': 'v', 'url': 'fakebot:v2'}, None),
    ({'id': None, 'url': None}, None),
])
def test_retry_url(item, url):
    assert dashboard.retry_url(item) == url


def test_disk_usage(env, tmp_path, monkeypatch):
    disk = env.dash().status()['disk']
    assert disk['total'] > 0 and 0 <= disk['free'] <= disk['total']
    assert dashboard.disk_usage(str(tmp_path / 'missing')) is None

    class Fs:
        f_frsize = 4096
        f_blocks = 2_000_000_000 // 4096          # 2 GB
        f_bavail = 1_500_000_123 // 4096
    monkeypatch.setattr(dashboard.os, 'statvfs', lambda path: Fs)
    disk = dashboard.disk_usage('/downloads')
    step = Fs.f_blocks * 4096 // 1000
    free = Fs.f_bavail * 4096
    assert disk['free'] % step == 0 and free - step < disk['free'] <= free
    assert disk['low'] is True                    # below 20 GB
    Fs.f_blocks, Fs.f_bavail = 10 ** 12 // 4096, 4 * 10 ** 10 // 4096
    assert dashboard.disk_usage('/downloads')['low'] is True   # 4 % free
    Fs.f_bavail = 6 * 10 ** 10 // 4096
    assert dashboard.disk_usage('/downloads')['low'] is False


def test_etag_matches():
    assert dashboard.etag_matches('"a-1"', '"a-1"')
    assert dashboard.etag_matches('"x", W/"a-1"', '"a-1"')
    assert dashboard.etag_matches('*', '"a-1"')
    assert not dashboard.etag_matches('"a-2"', '"a-1"')
    assert not dashboard.etag_matches(None, '"a-1"')


# -- cookies -----------------------------------------------------------------------

COOKIE_VALUE = 'VALUE-must-never-leave-the-parser'


def test_cookie_health_reports_names_and_expiry_only(env):
    with open(env.cookies, 'w') as f:
        f.write('# Netscape HTTP Cookie File\n\n'
                f'.youtube.com\tTRUE\t/\tTRUE\t{NOW + 9000}\tSID\t{COOKIE_VALUE}\n'
                f'#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t{NOW + 5000}\t'
                f'__Secure-3PSID\t{COOKIE_VALUE}\twith\ttabs\n'
                f'.google.com\tTRUE\t/\tTRUE\t0\tHSID\t{COOKIE_VALUE}\n'
                f'.youtube.com\tTRUE\t/\tTRUE\t{NOW + 10}\tPREF\t{COOKIE_VALUE}\n'
                f'.evil.com\tTRUE\t/\tTRUE\t{NOW + 1}\tSAPISID\t{COOKIE_VALUE}\n'
                'broken line\n')
    os.chmod(env.cookies, 0o600)
    cookies = env.dash().status()['cookies']
    assert cookies == {
        'present': True, 'cookies': 5, 'login': True,
        'names': ['HSID', 'SID', '__Secure-3PSID'], 'expires': NOW + 5000,
        'expired': False, 'bot_checks_24h': 0, 'last_bot_check': None,
        'last_rotation': None}


def test_cookie_health_without_a_file(env, tmp_path):
    assert env.dash().status()['cookies']['present'] is False
    os.symlink(tmp_path / 'elsewhere', env.cookies)
    assert env.dash().status()['cookies']['readable'] is False


def test_cookies_expired_and_reparsed_on_change(env):
    with open(env.cookies, 'w') as f:
        f.write(f'.youtube.com\tTRUE\t/\tTRUE\t{NOW - 1}\tSID\tx\n')
    dash = env.dash()
    assert dash.status()['cookies']['expired'] is True
    with open(env.cookies, 'w') as f:
        f.write(f'.youtube.com\tTRUE\t/\tTRUE\t{NOW + 99}\tLOGIN_INFO\tyz\n')
    cookies = dash.status()['cookies']
    assert cookies['names'] == ['LOGIN_INFO'] and cookies['expired'] is False


# -- log text ------------------------------------------------------------------------

@pytest.mark.parametrize('line,nonce,shown', [
    ('[download]  61.2% of  405.88MiB at 6.08MiB/s ETA 00:26 @@ytdl:'
     + PASS_NONCE + ' {"progress.status": "downloading", "info.id": "x"}',
     PASS_NONCE, '[download]  61.2% of  405.88MiB at 6.08MiB/s ETA 00:26'),
    ('[postprocess] EmbedThumbnail started @@ytdl:' + PASS_NONCE
     + ' {"progress.status": "started"}', PASS_NONCE, None),
    ('[info] Writing \'{"v":1,"t":"end","src":"scheduled","run":"' + PASS_RUN
     + '","i":%(.{id})j}\' to: /config/.youtube-dl-status/events.jsonl',
     PASS_NONCE, None),
    ('@@ytdl:' + PASS_NONCE + ' channel {"phase":"start","index":2,"total":5,'
     '"url":"https://www.youtube.com/@a","ts":1}', PASS_NONCE,
     '[dashboard] Channel 2 of 5: https://www.youtube.com/@a'),
    ('@@ytdl:' + PASS_NONCE + ' channel {"phase":"end","index":2,"rc":0}',
     PASS_NONCE, None),
    ('@@ytdl:' + PASS_NONCE + ' pass_end', PASS_NONCE, None),
    # A marker with another nonce (forged by a title, or an old pass).
    ('@@ytdl:0123456789abcdef channel {"phase":"start","index":1}',
     PASS_NONCE, None),
    ('@@ytdl:' + PASS_NONCE + ' channel {"phase":"start","index":2}', None,
     None),
    # --verbose dumps the command line with the progress template.
    ("[debug] Command-line config: ['--progress-template', 'download:"
     "[download] %(progress._default_template)s @@ytdl:" + PASS_NONCE
     + " %(.{progress.status})j']", PASS_NONCE,
     "[debug] Command-line config: ['--progress-template', 'download:"
     "[download] %(progress._default_template)s @@ytdl:… %(.{progress.status})j']"),
    ('ERROR: [youtube] x: \x1b[0;31mbad\x1b[0m \u202etxt.exe\x07', None,
     'ERROR: [youtube] x: bad txt.exe'),
    ('[download]  10%\r[download]  20%\r', None, '[download]  20%'),
    ('a\tb', None, 'a    b'),
    ('[download] Destination: /downloads/NA/Evil @@ytdl：0123 (2).mp4', None,
     '[download] Destination: /downloads/NA/Evil @@ytdl：0123 (2).mp4'),
])
def test_scrub_log_line(line, nonce, shown):
    assert dashboard.scrub_log_line(line, nonce) == shown


def _lines(n, width=99):
    return ''.join(f'{i:0{width - 1}d}\n' for i in range(n))


def test_log_since_contract(tmp_path):
    path = str(tmp_path / 'log')
    missing = dashboard.read_log_since(path, None)
    assert missing == {'offset': 0, 'reset': True, 'more': False, 'text': ''}
    assert dashboard.read_log_since(path, 0)['reset'] is False
    _append(path, b'one\ntwo\npart')
    first = dashboard.read_log_since(path, -1)
    assert first == {'offset': 8, 'reset': True, 'more': True,
                     'text': 'one\ntwo\n'}
    again = dashboard.read_log_since(path, first['offset'])
    assert again == {'offset': 8, 'reset': False, 'more': True, 'text': ''}
    _append(path, b'ial\nthree\n')
    nxt = dashboard.read_log_since(path, 8)
    assert nxt == {'offset': 22, 'reset': False, 'more': False,
                   'text': 'partial\nthree\n'}
    assert dashboard.read_log_since(path, 22)['text'] == ''
    # Truncated by the next pass: the old offset is past the end.
    _append(path, b'new\n', mode='wb')
    reset = dashboard.read_log_since(path, 22)
    assert reset == {'offset': 4, 'reset': True, 'more': False, 'text': 'new\n'}


def test_log_since_reads_64_kib_at_a_time(tmp_path):
    path = str(tmp_path / 'log')
    _append(path, _lines(2000).encode())                # 200 000 bytes
    seen, since, answers = [], 0, 0
    while True:
        answer = dashboard.read_log_since(path, since)
        assert len(answer['text'].encode()) <= dashboard.LOG_CHUNK_BYTES
        assert not answer['reset']
        seen += answer['text'].splitlines()
        since = answer['offset']
        answers += 1
        if not answer['more']:
            break
    assert seen == [f'{i:098d}' for i in range(2000)] and answers == 4
    tail = dashboard.read_log_since(path, None)
    lines = tail['text'].splitlines()
    assert tail['reset'] and lines[-1] == f'{1999:098d}'
    assert all(len(line) == 98 for line in lines)    # no cut first line


def test_log_since_inside_a_line_skips_to_the_next(tmp_path):
    path = str(tmp_path / 'log')
    _append(path, b'[download] x @@ytdl:' + PASS_NONCE.encode() + b' {}\nnext\n')
    answer = dashboard.read_log_since(path, 25)   # in the middle of the nonce
    assert answer['text'] == 'next\n' and PASS_NONCE[5:] not in answer['text']


def test_log_since_leaves_out_overlong_lines(tmp_path):
    path = str(tmp_path / 'log')
    _append(path, b'before\n' + b'j' * 100_000 + b'\nafter\n')
    first = dashboard.read_log_since(path, 0)
    assert first['text'] == 'before\n'
    second = dashboard.read_log_since(path, first['offset'])
    assert second['text'] == '[a line of 97 KiB was left out]\n'
    third = dashboard.read_log_since(path, second['offset'])
    assert third['text'] == 'after\n' and not third['more']


def test_log_tail_leaves_out_overlong_lines(tmp_path):
    path = str(tmp_path / 'log')
    _append(path, b'before\n' + b'j' * 100_000 + b'\nafter\n')
    assert dashboard.read_log_tail(path, 256 * 1024) == (
        'before\n[a line of 97 KiB was left out]\nafter\n')


def test_log_tail_of_the_fixture_has_no_nonce(tmp_path):
    _events, lines = _fixture('pass_default')
    path = str(tmp_path / 'log')
    _append(path, '\n'.join(lines).encode())
    text = dashboard.read_log_tail(path, 256 * 1024, PASS_NONCE)
    assert PASS_NONCE not in text and '@@ytdl:5' not in text
    # Forged markers (another nonce) are gone too.
    assert not any(line.startswith('@@ytdl:') for line in text.splitlines())
    assert "[info] Writing '" not in text
    assert '[dashboard] Channel 2 of 5: http://127.0.0.1:8765/mixed.html' in text
    assert ('[download] 100% of  832.66KiB in 00:00:02 at 382.65KiB/s\n'
            in text)


# -- HTTP API --------------------------------------------------------------------------

def _place_fixture_pass(client):
    module = client.app_module
    events, lines = _fixture('pass_default')
    status_dir = module.PATHS.status_dir
    os.makedirs(status_dir, mode=0o700, exist_ok=True)
    with open(module.PATHS.events_file, 'w') as f:
        for event in events:
            f.write(json.dumps(event) + '\n')
    with open(module.PATHS.youtube_dl_log, 'w') as f:
        f.write('\n'.join(lines))
    pid_dir = os.path.join(module.PATHS.proc_dir, str(PASS_PID))
    os.makedirs(pid_dir, exist_ok=True)
    with open(os.path.join(pid_dir, 'cmdline'), 'wb') as f:
        f.write(SCRIPT_CMDLINE)


READ_APIS = ['/api/status', '/api/log/youtube-dl', '/api/log/youtube-dl?since=0',
             f'/api/log/download/{uuid.uuid4()}',
             f'/api/log/download/{uuid.uuid4()}?since=5']


@pytest.mark.parametrize('path', READ_APIS)
def test_read_apis_need_basic_auth(make_client, path):
    client, _ = make_client()
    assert client.get(path, auth=basic()).status_code == 200
    assert client.get(path, headers=bearer()).status_code == 401
    anonymous = client.get(path)
    assert anonymous.status_code == 401
    assert anonymous.headers['www-authenticate'].startswith('Basic ')
    assert client.get(path, auth=basic('admin', 'wrong')).status_code == 401


def test_status_api_etag_and_no_store(make_client):
    client, _ = make_client()
    first = client.get('/api/status', auth=basic())
    assert first.status_code == 200
    assert first.headers['content-type'] == 'application/json'
    assert first.headers['cache-control'] == 'no-store'
    etag = first.headers['etag']
    boot = first.json()['boot']
    assert etag.startswith(f'"{boot}-') and first.json()['now'] > 0
    same = client.get('/api/status', auth=basic(),
                      headers={'If-None-Match': etag})
    assert same.status_code == 304 and same.content == b''
    assert same.headers['etag'] == etag
    assert same.headers['cache-control'] == 'no-store'
    assert same.headers['x-frame-options'] == 'DENY'
    # Something changes: a new revision.
    _place_fixture_pass(client)
    changed = client.get('/api/status', auth=basic(),
                         headers={'If-None-Match': etag})
    assert changed.status_code == 200 and changed.headers['etag'] != etag
    assert changed.json()['scheduler']['state'] == 'running'
    # Another process has another boot id.
    other, _ = make_client()
    assert other.get('/api/status', auth=basic(),
                     headers={'If-None-Match': etag}).status_code == 200


def test_no_response_reveals_the_nonce(make_client):
    client, _ = make_client()
    _place_fixture_pass(client)
    module = client.app_module
    responses = [client.get(path, auth=basic()) for path in (
        '/api/status', '/api/log/youtube-dl', '/api/log/youtube-dl?since=0',
        '/log/youtube-dl')]
    status = responses[0].json()
    assert status['scheduler']['state'] == 'running'
    assert status['current']['counts']['new'] == 5
    for response in responses:
        assert response.status_code == 200
        assert PASS_NONCE not in response.text
    log = responses[1].json()
    assert log['reset'] and '[dashboard] Channel 3 of 5' in log['text']
    # The events file holds the nonce; it is never served.
    with open(module.PATHS.events_file) as f:
        assert PASS_NONCE in f.read()
    # A sleeping pass (run_end written) is no different.
    assert se.main(['end', f'--status-dir={module.PATHS.status_dir}',
                    f'--run={PASS_RUN}', f'--nonce={PASS_NONCE}', '--rc=1',
                    '--interval=3h', f'--log={module.PATHS.youtube_dl_log}',
                    '--wait=0']) == 0
    sleeping = client.get('/api/status', auth=basic())
    assert sleeping.json()['scheduler']['state'] == 'sleeping'
    assert PASS_NONCE not in sleeping.text


def test_log_api_since(make_client):
    client, _ = make_client()
    module = client.app_module
    with open(module.PATHS.youtube_dl_log, 'w') as f:
        f.write('one\ntwo\n')
    first = client.get('/api/log/youtube-dl', auth=basic()).json()
    assert first == {'offset': 8, 'reset': True, 'more': False,
                     'text': 'one\ntwo\n'}
    with open(module.PATHS.youtube_dl_log, 'a') as f:
        f.write('three\n')
    answer = client.get('/api/log/youtube-dl?since=8', auth=basic())
    assert answer.headers['cache-control'] == 'no-store'
    assert answer.json() == {'offset': 14, 'reset': False, 'more': False,
                             'text': 'three\n'}
    huge = client.get(f'/api/log/youtube-dl?since={10 ** 30}', auth=basic())
    assert huge.json()['reset'] is True
    assert client.get('/api/log/youtube-dl?since=x',
                      auth=basic()).status_code == 422


def test_download_log_api(make_client):
    client, _ = make_client()
    module = client.app_module
    download_id = str(uuid.uuid4())
    path = os.path.join(module.PATHS.download_log_dir,
                        f'download_{download_id}.log')
    with open(path, 'w') as f:
        f.write('[download] 5% @@ytdl:0123456789abcdef {"info.id": "x"}\n')
    answer = client.get(f'/api/log/download/{download_id}?since=0',
                        auth=basic()).json()
    assert answer['text'] == '[download] 5%\n'
    for bad in ('not-a-uuid', download_id.upper(), '..'):
        assert client.get(f'/api/log/download/{bad}',
                          auth=basic()).status_code == 404


def test_status_dir_files_stay_private(make_client):
    # The web UI only reads: it never creates or loosens the status files.
    client, _ = make_client()
    module = client.app_module
    client.get('/api/status', auth=basic())
    assert not os.path.exists(module.PATHS.status_dir)
    _place_fixture_pass(client)
    os.chmod(module.PATHS.events_file, 0o600)
    client.get('/api/status', auth=basic())
    assert stat.S_IMODE(os.stat(module.PATHS.events_file).st_mode) == 0o600
