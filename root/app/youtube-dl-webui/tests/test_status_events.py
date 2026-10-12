"""Tests for status_events.py: event flags, event file, interval, parser and
the begin/channel/end commands youtube-dl.sh calls.

The fixtures under fixtures/status/ are real output (paths shortened to
/config and /downloads) of youtube-dl.sh with the web UI enabled, running
the pinned yt-dlp with the research's fake YouTube extractor against a
local server: an archived video, a 429 and an unavailable video on the watch
page, a filtered live stream, a bot check, downloads that fail with 429 and
404, a page whose title forges log lines, an unsupported URL and a
channels.txt line whose arguments cannot be parsed. pass_quiet is the same
pass with --quiet --no-progress in args.conf; manual_mixed is one manual
job.
"""

import json
import os
import re
import stat
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import status_events as se  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'fixtures', 'status')
RUN = '0123456789abcdef'
NONCE = 'fedcba9876543210'
EVF = '/config/.youtube-dl-status/events.jsonl'


def _events(status_dir):
    with open(os.path.join(status_dir, se.EVENTS_NAME)) as f:
        return [json.loads(line) for line in f]


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


# -- flags -----------------------------------------------------------------

def _value(flags, option, n=0):
    """The value after the n-th occurrence of option."""
    positions = [i for i, f in enumerate(flags) if f == option]
    return flags[positions[n] + 1]


def test_flags_shape_and_order():
    flags = se.ytdlp_flags('scheduled', RUN, NONCE, EVF, ch=4)
    options = [f for f in flags if f.startswith('--')]
    assert options == ['--newline', '--progress', '--progress-delta', '--color',
                       '--progress-template', '--progress-template',
                       '--print-to-file', '--print-to-file']
    assert _value(flags, '--progress-delta') == '2'
    assert _value(flags, '--color') == 'never'
    download = _value(flags, '--progress-template', 0)
    assert download.startswith(
        'download:[download] %(progress._default_template)s @@ytdl:' + NONCE
        + ' %(.{progress.status,')
    postprocess = _value(flags, '--progress-template', 1)
    assert postprocess == (
        'postprocess:[postprocess] %(progress.postprocessor)s %(progress.status)s '
        '@@ytdl:' + NONCE + ' %(.{progress.status,progress.postprocessor,info.id})j')
    start = _value(flags, '--print-to-file', 0)
    assert start.startswith('video:{"v":1,"t":"start","src":"scheduled","run":"'
                            + RUN + '","ch":4,"i":%(.{id,title,')
    assert start.endswith('})j}')
    end = _value(flags, '--print-to-file', 1)
    assert end == ('after_video:{"v":1,"t":"end","src":"scheduled","run":"' + RUN
                   + '","ch":4,"i":%(.{id,title,channel,epoch,'
                   'requested_downloads.:.filepath})j}')
    # Both events go to the event file.
    assert [flags[i + 2] for i, f in enumerate(flags) if f == '--print-to-file'] \
        == [EVF, EVF]


def test_flags_avoid_the_known_traps():
    flags = se.ytdlp_flags('manual', RUN, NONCE, EVF)
    assert '--print' not in flags            # implies --quiet
    templates = ' '.join(flags)
    assert 'playlist:' not in templates      # keeps every entry in memory
    for field in re.findall(r'%\(([^)]*)\)', templates):
        # No dict nested in the dict (prints a bare NA); no whole info dict,
        # formats list or description (huge lines).
        assert field.count('{') <= 1, field
        for key in field.strip('.{}').split(','):
            assert key.split('.')[-1] not in (
                'info', 'formats', 'requested_formats', 'description'), key
    assert '"ch"' not in templates           # manual jobs have no channel
    assert '"src":"manual"' in templates


@pytest.mark.parametrize('run, nonce', [
    ('0123456789ABCDEF', NONCE),     # upper case
    ('0123456789abcde', NONCE),      # too short
    ('0' * 33, NONCE),               # too long
    ('0123456789abcdeg', NONCE),     # not hex
    ('0123456789abcde"', NONCE),     # would break the JSON template
    (RUN, 'fedcba987654321\n'),
    (RUN, None),
])
def test_flags_reject_bad_ids(run, nonce):
    with pytest.raises(ValueError):
        se.ytdlp_flags('scheduled', run, nonce, EVF)


@pytest.mark.parametrize('kwargs', [
    {'src': 'other'},
    {'ch': 0}, {'ch': -1}, {'ch': True}, {'ch': '1'},
    {'events_file': 'relative/events.jsonl'},
    {'events_file': '/config/%(id)s.jsonl'},
    {'events_file': '/config/a\nb'},
])
def test_flags_reject_bad_arguments(kwargs):
    args = {'src': 'scheduled', 'run': RUN, 'nonce': NONCE, 'events_file': EVF}
    args.update(kwargs)
    with pytest.raises(ValueError):
        se.ytdlp_flags(**args)


def test_job_uuid_hex_is_a_valid_run():
    se.ytdlp_flags('manual', '0123456789abcdef0123456789abcdef', NONCE, EVF)


# -- begin, event file, rotation ---------------------------------------------

def test_begin_prints_ids_and_appends_run_start(tmp_path, capsys):
    status_dir = str(tmp_path / 'status')
    assert se.main(['begin', f'--status-dir={status_dir}', '--channels=3',
                    '--interval=1d 3h', '--pid=42',
                    '--ytdlp=2026.08.19\nsecond line']) == 0
    out = capsys.readouterr().out
    run, nonce, rest = out.split('\0')
    assert rest == ''
    assert re.fullmatch('[0-9a-f]{16}', run) and re.fullmatch('[0-9a-f]{16}', nonce)
    assert run != nonce
    [event] = _events(status_dir)
    assert event['v'] == 1 and event['t'] == 'run_start'
    assert (event['run'], event['nonce'], event['pid'], event['channels'],
            event['interval'], event['ytdlp']) == (run, nonce, 42, 3, '1d 3h',
                                                   '2026.08.19')
    assert abs(event['ts'] - time.time()) < 60
    assert _mode(status_dir) == 0o700
    assert _mode(os.path.join(status_dir, se.EVENTS_NAME)) == 0o600


def test_files_are_private_whatever_the_umask(tmp_path):
    status_dir = str(tmp_path / 'status')
    old = os.umask(0)
    try:
        se.append_event({'t': 'x'}, status_dir)
    finally:
        os.umask(old)
    assert _mode(status_dir) == 0o700
    assert _mode(os.path.join(status_dir, se.EVENTS_NAME)) == 0o600


def test_existing_modes_are_tightened(tmp_path):
    # yt-dlp creates the directory and file with the umask's modes if it
    # writes an event before the helper did.
    status_dir = tmp_path / 'status'
    status_dir.mkdir(mode=0o755)
    os.chmod(status_dir, 0o755)
    events = status_dir / se.EVENTS_NAME
    events.write_text('')
    os.chmod(events, 0o644)
    se.append_event({'t': 'x'}, str(status_dir))
    assert _mode(str(status_dir)) == 0o700
    assert _mode(str(events)) == 0o600


def test_symlinked_events_file_is_refused(tmp_path, capsys):
    status_dir = tmp_path / 'status'
    status_dir.mkdir(mode=0o700)
    victim = tmp_path / 'victim'
    victim.write_text('keep\n')
    os.symlink(victim, status_dir / se.EVENTS_NAME)
    with pytest.raises(OSError):
        se.append_event({'t': 'x'}, str(status_dir))
    assert se.main(['begin', f'--status-dir={status_dir}', '--channels=1',
                    '--pid=1']) == 1
    captured = capsys.readouterr()
    assert captured.out == ''              # the script gets no ids
    assert 'begin failed' in captured.err
    assert victim.read_text() == 'keep\n'


def test_symlinked_status_dir_is_refused(tmp_path):
    real = tmp_path / 'elsewhere'
    real.mkdir(mode=0o700)
    os.symlink(real, tmp_path / 'status')
    with pytest.raises(OSError):
        se.append_event({'t': 'x'}, str(tmp_path / 'status'))
    assert list(real.iterdir()) == []


def test_symlinked_events_file_is_not_read(tmp_path):
    status_dir = tmp_path / 'status'
    status_dir.mkdir(mode=0o700)
    victim = tmp_path / 'victim'
    victim.write_text('{"v":1,"t":"x"}\n')
    os.symlink(victim, status_dir / se.EVENTS_NAME)
    with pytest.raises(OSError):
        list(se.read_events(str(status_dir)))


def test_oversized_event_is_refused(tmp_path):
    with pytest.raises(ValueError):
        se.append_event({'t': 'x', 'pad': 'a' * se.MAX_EVENT_BYTES},
                        str(tmp_path / 'status'))


def _fill(path, size):
    line = b'{"v":1,"t":"old","pad":"' + b'a' * 100 + b'"}\n'
    with open(path, 'wb') as f:
        f.write(line * (size // len(line)) + b'\n' * (size % len(line)))


def test_rotation_above_2_mib(tmp_path):
    status_dir = tmp_path / 'status'
    status_dir.mkdir(mode=0o700)
    events = status_dir / se.EVENTS_NAME
    rotated = status_dir / se.ROTATED_NAME
    rotated.write_text('older\n')
    _fill(events, se.ROTATE_BYTES + 1)
    old = events.read_bytes()
    assert se.main(['begin', f'--status-dir={status_dir}', '--channels=0',
                    '--pid=1']) == 0
    assert rotated.read_bytes() == old           # the previous .1 is replaced
    assert [e['t'] for e in _events(str(status_dir))] == ['run_start']
    assert _mode(str(events)) == 0o600


def test_no_rotation_at_2_mib(tmp_path):
    status_dir = tmp_path / 'status'
    status_dir.mkdir(mode=0o700)
    events = status_dir / se.EVENTS_NAME
    _fill(events, se.ROTATE_BYTES)
    assert se.rotate(str(status_dir)) is False
    assert not (status_dir / se.ROTATED_NAME).exists()
    assert events.stat().st_size == se.ROTATE_BYTES


def test_read_events_skips_invalid_and_unfinished_lines(tmp_path):
    status_dir = tmp_path / 'status'
    status_dir.mkdir(mode=0o700)
    (status_dir / se.ROTATED_NAME).write_text('{"v":1,"t":"a","run":"r1"}\n')
    (status_dir / se.EVENTS_NAME).write_bytes(
        b'{"v":1,"t":"b","run":"r1"}\n'
        b'not json\n'
        b'["v",1]\n'
        b'{"v":2,"t":"future"}\n'
        b'{"t":"no version"}\n'
        b'{"v":1,"t":"c","run":"r2"}\n'
        b'{"v":1,"t":"' + b'x' * (se.MAX_LINE_BYTES + 1) + b'"}\n'
        b'{"v":1,"t":"d","run":"r1"}\n'
        b'{"v":1,"t":"still being writ')
    assert [e['t'] for e in se.read_events(str(status_dir))] == ['a', 'b', 'c', 'd']
    assert [e['t'] for e in se.read_events(str(status_dir), run='r1')] == ['a', 'b', 'd']
    assert list(se.read_events(str(tmp_path / 'missing'))) == []


# -- interval -----------------------------------------------------------------

@pytest.mark.parametrize('text, seconds', [
    ('3h', 10800), ('1d 3h', 97200), ('1.5h', 5400), ('90', 90),
    ('2m  30s', 150), (' 10m ', 600), ('1e2', 100), ('0x10', 16), ('.5m', 30),
    ('0', 0), ('+5', 5), ('0x1p3', 8), ('1e+2s', 100), ('INF', None),
    # GNU sleep rejects these, youtube-dl.sh then sleeps the default 3h.
    ('bogus', 10800), ('', 10800), ('3H', 10800), ('-5', 10800), ('-0', 10800),
    ('1h bogus', 10800), ('1,5h', 10800), ('nan', 10800), ('5x', 10800),
    ('1e', 10800), ('False', 10800), ('3h\r', 10800),
    # No next pass.
    ('false', None), ('inf', None), ('infinityd', None), ('1e400', None),
])
def test_interval(text, seconds):
    assert se.parse_interval(text) == seconds


# -- classification -----------------------------------------------------------

@pytest.mark.parametrize('msg, warnings, kind', [
    ('No video formats found!',
     ['Sign in to confirm you’re not a bot. Use --cookies-from-browser'],
     'bot_check'),
    ("Sign in to confirm you're not a bot. This helps protect our community.",
     [], 'bot_check'),
    ('Unable to download webpage: HTTP Error 429: Too Many Requests', [], 'rate_limit'),
    ("This content isn't available, try again later.", [], 'rate_limit'),
    ('unable to download video data: HTTP Error 403: Forbidden', [], 'forbidden'),
    ('Video unavailable. This video has been removed by the uploader', [], 'unavailable'),
    ('unable to download video data: HTTP Error 404: Not Found', [], 'unavailable'),
    ('Premieres in 3 hours', [], 'unavailable'),
    ('Private video. Sign in if you\'ve been granted access to this video', [],
     'private_members'),
    ('Join this channel to get access to members-only content like this video', [],
     'members_only'),
    ('Sign in to confirm your age. This video may be inappropriate for some users.',
     [], 'private_members'),
    ('The uploader has not made this video available in your country', [], 'geo'),
    ('[Errno 28] No space left on device', [], 'disk_full'),
    ('No video formats found!',
     ['The provided YouTube account cookies are no longer valid. They have '
      'likely been rotated in the browser as a security measure.'],
     'cookies_rotated'),
    ('Requested format is not available', [], 'other'),
    ('', [], 'other'),
])
def test_classify(msg, warnings, kind):
    assert se.classify(msg, warnings) == kind
    assert se.KIND_HINTS[kind] and se.KIND_LABELS[kind]


def test_every_hint_is_one_sentence():
    for hint in se.KIND_HINTS.values():
        assert hint.endswith('.') and hint.count('. ') == 0


def test_clean_text():
    assert se.clean_text('\x1b[0;31mred\x1b[0m') == 'red'
    assert se.clean_text('a\u202egnp.exe\u2066b\u2069') == 'agnp.exeb'
    assert se.clean_text('a\x00b\x07c\nd\te') == 'abc d e'
    assert se.clean_text('x' * 400) == 'x' * 299 + '…'
    assert se.clean_text('\ud800ok') == 'ok'
    assert se.clean_text(None) == ''


# -- parser -------------------------------------------------------------------

def _start(vid, ch=1, run=RUN, **info):
    return {'v': 1, 't': 'start', 'src': 'scheduled', 'run': run, 'ch': ch,
            'i': {'id': vid, **info}}


def _end(vid, path=None, ch=1, run=RUN):
    info = {'id': vid}
    if path:
        info['requested_downloads.:.filepath'] = [path]
    return {'v': 1, 't': 'end', 'src': 'scheduled', 'run': run, 'ch': ch,
            'i': info}


def _marker(phase, index, **data):
    return f'@@ytdl:{NONCE} channel ' + json.dumps(
        {'phase': phase, 'index': index, **data})


def _writing(kind, run=RUN):
    return (f'[info] Writing \'{{"v":1,"t":"{kind}","src":"scheduled",'
            f'"run":"{run}","ch":1,"i":%(.{{id}})j}}\' to: {EVF}')


def test_outcome_comes_from_events_and_errors_attach_to_the_open_video():
    p = se.LogParser(NONCE, run=RUN)
    p.add_events([_start('a', title='A'), _end('a', '/downloads/a.mp4'),
                  _start('b', title='B'), _end('b'),
                  _start('c'), _start('c'), _end('c', '/downloads/c.mp4')])
    for line in [
            _marker('start', 1, total=1, url='https://x/'),
            _writing('start'),
            'ERROR: Preprocessing: Unable to communicate with SponsorBlock API',
            _writing('end'),
            '[download] Downloading item 2 of 3',
            _writing('start'),
            'ERROR: unable to download video data: HTTP Error 404: Not Found',
            _writing('end'),
            _writing('start'), _writing('start'), _writing('end'),
            _marker('end', 1, rc=1, secs=3)]:
        p.feed(line)
    p.finish()
    s = p.summary()
    # An ERROR line alone never fails a downloaded video (SponsorBlock), and
    # a video started twice (-f a,b) counts once.
    assert s['sum'] == {'new': 2, 'skipped': 0, 'failed': 1, 'filtered': 0, 'members': 0}
    assert s['err'] == [{'idx': 1, 'id': 'b', 'kind': 'unavailable',
                         'msg': 'unable to download video data: HTTP Error 404: Not Found',
                         'title': 'B'}]
    assert s['ch'] == [[1, 2, 0, 1, 0, 1, 3, None, 'https://x/', 0]]


@pytest.mark.parametrize('msg', [
    '[youtube] aaaaaaaaaaa: Join this channel to get access to members-only '
    'content like this video, and other exclusive perks.',
    '[youtube] aaaaaaaaaaa: This video is available to this channel\'s members '
    'on level: Tier 1 (or any higher level). Join this channel to get access.',
])
def test_members_only_is_its_own_kind(msg):
    assert se.classify(msg) == 'members_only'
    assert se.classify('[youtube] x: Private video. Sign in if you\'ve been '
                       'granted access to this video') == 'private_members'


def test_only_skipped():
    members = {'kind': 'members_only'}
    assert se.only_skipped(0, [members, members])
    assert not se.only_skipped(1, [members])
    assert not se.only_skipped(0, [])
    assert not se.only_skipped(0, [members, {'kind': 'other'}])


def test_members_only_videos_count_as_members_not_failed():
    p = se.LogParser(NONCE, run=RUN)
    p.add_events([_start('a', title='A'), _end('a', '/downloads/a.mp4')])
    for line in [
            _marker('start', 1, total=1, url='https://x/'),
            '[download] Downloading item 1 of 2',
            _writing('start'), _writing('end'),
            '[download] Downloading item 2 of 2',
            'ERROR: [youtube] bbbbbbbbbbb: Join this channel to get access to '
            'members-only content like this video, and other exclusive perks.',
            _marker('end', 1, rc=1, secs=3)]:
        p.feed(line)
    p.finish()
    s = p.summary()
    assert s['sum'] == {'new': 1, 'skipped': 0, 'failed': 0, 'filtered': 0,
                        'members': 1}
    assert [e['kind'] for e in s['err']] == ['members_only']
    assert s['ch'][0][9] == 1


JOIN = ('WARNING: [youtube] Join this channel to get access to members-only '
        'content like this video, and other exclusive perks.')


@pytest.mark.parametrize('with_event', [True, False])
def test_members_only_warning_without_an_id(with_event):
    # yt-dlp (args.conf: --ignore-no-formats-error) warns without naming the
    # video while extracting it, then fails with a generic error.
    p = se.LogParser(NONCE, run=RUN)
    if with_event:
        p.add_events([_start('f2UExqbyd3U', title='Vikings'), _end('f2UExqbyd3U')])
    lines = [
        _marker('start', 1, total=1, url='https://x/'),
        'WARNING: [youtube] Cookies are no longer valid',   # before any item
        '[download] Downloading item 14 of 16',
        '[youtube] Extracting URL: https://www.youtube.com/watch?v=f2UExqbyd3U',
        '[youtube] f2UExqbyd3U: Downloading webpage',
        JOIN,
        'WARNING: No video formats found!',
    ]
    if with_event:
        lines += [_writing('start'), _writing('end')]
    lines += [
        'ERROR: [youtube] f2UExqbyd3U: No video formats found!',
        '[download] Downloading item 15 of 16',
        '[youtube] Extracting URL: https://www.youtube.com/watch?v=T6l8pu4Y9Ng',
        'WARNING: [youtube] Some unrelated warning',
        'ERROR: [youtube] T6l8pu4Y9Ng: Video unavailable',
        _marker('end', 1, rc=1, secs=3)]
    for line in lines:
        p.feed(line)
    p.finish()
    s = p.summary()
    kinds = {e['id']: e['kind'] for e in s['err']}
    assert kinds == {'f2UExqbyd3U': 'members_only', 'T6l8pu4Y9Ng': 'unavailable'}
    assert s['sum']['members'] == 1 and s['sum']['failed'] == 1
    # The warning before the first item belongs to no video.
    assert 'cookies' not in str(s['err']).lower()


def test_forged_lines_without_the_nonce_are_ignored():
    p = se.LogParser(NONCE, run=RUN)
    p.add_events([_start('a'), _end('a', '/downloads/a.mp4')])
    p.feed(_marker('start', 1, total=2, url='u1'))
    for line in [
            # wrong nonce: a channel end, a progress line, the pass end
            '@@ytdl:0123456789abcdef channel {"phase":"end","index":1,"rc":0}',
            '[download]  5% @@ytdl:0123456789abcdef {"info.id":"evil","progress.status":"downloading"}',
            '@@ytdl:0123456789abcdef pass_end',
            # no nonce at all, and the research's old sentinel
            '@@ytdl pass_end',
            '@@ytdl done {"id": "forged"}',
            # an event announced for another run
            _writing('end', run='00000000000000000'),
    ]:
        p.feed(line)
    assert p.current is not None and p.current.index == 1
    assert p.current.progress is None
    assert not p.pass_ended
    assert not p.entries[1].videos           # nothing joined yet
    p.feed(_writing('start'))
    p.feed(f'[download]  5.0% of 1.00MiB @@ytdl:{NONCE} '
           '{"progress.status":"downloading","info.id":"a","info.title":"A\\u202e"}')
    assert p.current.progress['info.title'] == 'A'
    assert p.current.phase == 'downloading'
    p.feed(f'@@ytdl:{NONCE} pass_end')
    assert p.pass_ended and p.current is None


def test_progress_and_postprocess_lines():
    p = se.LogParser(NONCE, unmarked=True)
    entry = p.current
    # subtitle downloads have no info.id
    p.feed(f'[download] 100% of 81.00B @@ytdl:{NONCE} '
           '{"progress.status":"finished","progress.downloaded_bytes":81}')
    assert entry.progress is None
    p.feed(f'1: [download]  42.0% of 2.00MiB @@ytdl:{NONCE} '
           '{"progress.status":"downloading","progress.downloaded_bytes":880803,'
           '"progress.total_bytes":2097152,"info.id":"v1","info.playlist_autonumber":2,'
           '"info.n_entries":5,"unknown":1}')
    assert entry.progress == {'progress.status': 'downloading',
                              'progress.downloaded_bytes': 880803,
                              'progress.total_bytes': 2097152, 'info.id': 'v1',
                              'info.playlist_autonumber': 2, 'info.n_entries': 5}
    assert entry.open == 'v1'
    p.feed(f'[postprocess] Merger started @@ytdl:{NONCE} '
           '{"progress.status":"started","progress.postprocessor":"Merger","info.id":"v1"}')
    assert entry.phase == 'processing: Merger'
    p.feed('\x1b[0;31mERROR:\x1b[0m [youtube] zz: Video unavailable')
    p.feed('[download] Downloading item 3 of N/A')
    assert (entry.item, entry.n_items, entry.open) == (3, None, None)
    p.finish()
    assert p.summary()['sum']['failed'] == 1


def test_events_arriving_after_their_log_lines_are_joined():
    # The web UI reads the log and the event file at different moments.
    p = se.LogParser(NONCE, run=RUN)
    p.feed(_marker('start', 1, total=1, url='u'))
    p.feed(_writing('start'))
    p.feed('ERROR: unable to download video data: HTTP Error 429: Too Many Requests')
    p.add_events([_start('a')])
    assert p.entries[1].open == 'a'
    p.feed('ERROR: unable to download video data: HTTP Error 429: Too Many Requests')
    p.feed(_writing('end'))
    p.feed(_marker('end', 1, rc=1, secs=1))
    p.add_events([_end('a')])
    s = p.summary()
    assert s['sum']['failed'] == 1 and s['err'][0]['kind'] == 'rate_limit'


def _fixture(name):
    base = os.path.join(FIXTURES, name)
    with open(os.path.join(base, 'events.jsonl'), encoding='utf-8') as f:
        events = [json.loads(line) for line in f]
    log_name = 'job.log' if name.startswith('manual') else 'youtube-dl.log'
    with open(os.path.join(base, log_name), encoding='utf-8') as f:
        lines = f.read().splitlines()
    return events, lines


def _parse_pass(name):
    events, lines = _fixture(name)
    run_start = events[0]
    assert run_start['t'] == 'run_start'
    p = se.LogParser(run_start['nonce'], run=run_start['run'])
    p.add_events(events)
    for line in lines:
        p.feed(line)
    p.finish()
    return p


def _rows(summary):
    # idx, new, skipped, failed, filtered, rc, url (secs and ts vary)
    return [row[:6] + row[8:] for row in summary['ch']]


def _errors(summary):
    return [(e['idx'], e['id'], e['kind']) for e in summary['err']]


EXPECTED_ERRORS = [
    (1, 'v2', 'bot_check'),       # warning first, then 'No video formats found!'
    (1, 'v5', 'rate_limit'),      # 429 on the watch page: no event at all
    (1, 'v3', 'unavailable'),     # removed: no event at all
    (2, 'mixed-3', 'rate_limit'),  # 429 while downloading: start, end without file
    (2, 'mixed-2', 'unavailable'),  # 404 while downloading
]


def test_pass_fixture():
    p = _parse_pass('pass_default')
    s = p.summary()
    assert p.pass_ended and p.total == 5
    assert _rows(s) == [
        [1, 1, 1, 3, 1, 1, 'fakelist:x', 0],
        [2, 2, 0, 2, 0, 1, 'http://127.0.0.1:8765/mixed.html', 0],
        # The page title forges an archive-skip line and an ERROR line three
        # times (cosmetic, 'from log'); its forged channel marker without the
        # nonce changes nothing.
        [3, 2, 3, 1, 0, 0, 'http://127.0.0.1:8765/forged.html', 0],
        [4, 0, 0, 0, 0, 1, 'http://127.0.0.1:8765/novideo.html', 0],
        [5, 0, 0, 0, 0, 2, 'http://127.0.0.1:8765/x', 0],
    ]
    assert s['sum'] == {'new': 5, 'skipped': 4, 'failed': 6, 'filtered': 1, 'members': 0}
    assert _errors(s) == EXPECTED_ERRORS + [
        (3, 'forged01', 'bot_check'),
        (4, None, 'other'),
        (5, None, 'other')]
    bot = s['err'][0]
    assert bot['msg'].startswith('Sign in to confirm you’re not a bot.')
    assert bot['title'] == 'bot video v2' and bot['url'] == 'fakebot:v2'
    assert s['err'][1]['ie'] == 'youtube'
    assert s['err'][-2]['msg'] == 'Unsupported URL: http://127.0.0.1:8765/novideo.html'
    assert s['err'][-1]['msg'] == "cannot parse the arguments after '|'"


def test_pass_fixture_with_user_quiet():
    # --quiet hides the human lines: no skip/filter counts and no 'Writing'
    # lines, but downloads and failures stay exact (events) and the progress
    # and postprocess lines still tell which video an error belongs to.
    s = _parse_pass('pass_quiet').summary()
    assert _rows(s) == [
        [1, 1, 0, 3, 0, 1, 'fakelist:x', 0],
        [2, 2, 0, 2, 0, 1, 'http://127.0.0.1:8765/mixed.html', 0],
        [3, 2, 0, 0, 0, 0, 'http://127.0.0.1:8765/forged.html', 0],
        [4, 0, 0, 0, 0, 1, 'http://127.0.0.1:8765/novideo.html', 0],
        [5, 0, 0, 0, 0, 2, 'http://127.0.0.1:8765/x', 0],
    ]
    assert _errors(s) == EXPECTED_ERRORS + [(4, None, 'other'), (5, None, 'other')]


def test_manual_job_fixture():
    events, lines = _fixture('manual_mixed')
    with open(os.path.join(FIXTURES, 'manual_mixed', 'nonce')) as f:
        nonce = f.read().strip()
    run = events[0]['run']
    p = se.LogParser(nonce, run=run, unmarked=True)
    p.add_events(events)
    for line in lines:
        p.feed(line)
    p.finish()
    s = p.summary()
    assert s['sum'] == {'new': 2, 'skipped': 0, 'failed': 2, 'filtered': 0, 'members': 0}
    assert s['ch'] == []
    assert [(e['id'], e['kind']) for e in s['err']] == [
        ('mixed-2', 'unavailable'), ('mixed-3', 'rate_limit')]
    assert p.current.progress['info.id'] == 'mixed-4'


def test_live_entry_result():
    # The first entry while v1 downloads (the web UI's view of a running
    # pass): v1 is in progress, not failed; v2 logged its error and is.
    events, lines = _fixture('pass_default')
    cut = next(i for i, line in enumerate(lines)
               if '0.1% of' in line and '"info.id": "v1"' in line)
    p = se.LogParser(events[0]['nonce'], run=events[0]['run'])
    starts = [e for e in events
              if e['t'] == 'start' and e['i']['id'] in ('v1', 'v2')]
    p.add_events(starts)
    for line in lines[:cut + 1]:
        p.feed(line)
    entry = p.current
    assert entry.phase == 'downloading' and entry.phase_id == 'v1'
    live = p.entry_result(entry, live=True)
    assert (live['active'], live['new'], live['failed']) == ('v1', 0, 3)
    final = p.entry_result(entry)
    assert (final['active'], final['new'], final['failed']) == (None, 0, 4)
    # v1's end event arrives before its "Writing" line: live counts it.
    p.add_events([e for e in events
                  if e['t'] == 'end' and e['i']['id'] == 'v1'])
    live = p.entry_result(entry, live=True)
    assert (live['active'], live['new'], live['failed']) == (None, 1, 3)
    assert entry.videos['v1']['ended'] is False   # the parser is unchanged


def test_the_nonce_of_a_fixture_is_needed():
    events, lines = _fixture('pass_default')
    p = se.LogParser('0' * 16, run=events[0]['run'])
    p.add_events(events)
    for line in lines:
        p.feed(line)
    p.finish()
    # Without the nonce the channel markers are not trusted, so no log line
    # belongs to an entry: only the events count (no skips, filters or
    # extraction errors, no exit codes).
    s = p.summary()
    assert not p.pass_ended
    assert [row[:6] for row in s['ch']] == [[1, 1, 0, 1, 0, None],
                                             [2, 2, 0, 2, 0, None],
                                             [3, 2, 0, 0, 0, None]]


# -- end ---------------------------------------------------------------------

def _copy_fixture_pass(tmp_path, name='pass_default'):
    events, lines = _fixture(name)
    status_dir = tmp_path / 'status'
    status_dir.mkdir(mode=0o700)
    with open(status_dir / se.EVENTS_NAME, 'w') as f:
        for event in events:
            f.write(json.dumps(event) + '\n')
    log = tmp_path / 'youtube-dl.log'
    log.write_text('\n'.join(lines) + '\n')
    return str(status_dir), str(log), events[0]


def test_end_appends_run_end(tmp_path):
    status_dir, log, run_start = _copy_fixture_pass(tmp_path)
    before = time.time()
    assert se.main(['end', f'--status-dir={status_dir}', f'--run={run_start["run"]}',
                    f'--nonce={run_start["nonce"]}', '--rc=2', '--interval=3h',
                    f'--log={log}']) == 0
    event = _events(status_dir)[-1]
    assert event['t'] == 'run_end' and event['run'] == run_start['run']
    assert event['rc'] == 2 and event['partial'] is False
    assert before - 1 <= event['ts'] <= time.time() + 1
    assert abs(event['next'] - (event['ts'] + 10800)) <= 1
    assert event['sum'] == {'new': 5, 'skipped': 4, 'failed': 6, 'filtered': 1, 'members': 0}
    assert [row[0] for row in event['ch']] == [1, 2, 3, 4, 5]
    assert len(event['err']) == 8
    assert run_start['nonce'] not in json.dumps(event)


def test_end_waits_for_the_pass_end_marker(tmp_path):
    status_dir, log, run_start = _copy_fixture_pass(tmp_path)
    lines = open(log).read().splitlines()
    marker = f'@@ytdl:{run_start["nonce"]} pass_end'
    with open(log, 'w') as f:
        f.write('\n'.join(line for line in lines if line != marker) + '\n')

    def late_tee():
        time.sleep(0.5)
        with open(log, 'a') as f:
            f.write(marker + '\n')

    thread = threading.Thread(target=late_tee)
    thread.start()
    started = time.monotonic()
    assert se.main(['end', f'--status-dir={status_dir}', f'--run={run_start["run"]}',
                    f'--nonce={run_start["nonce"]}', '--rc=0', '--interval=false',
                    f'--log={log}', '--wait=10']) == 0
    thread.join()
    assert 0.4 < time.monotonic() - started < 5
    event = _events(status_dir)[-1]
    assert event['partial'] is False and event['next'] is None


def test_end_without_marker_writes_a_partial_run_end(tmp_path):
    status_dir, log, run_start = _copy_fixture_pass(tmp_path)
    marker = f'@@ytdl:{run_start["nonce"]} pass_end'
    lines = [line for line in open(log).read().splitlines() if line != marker]
    with open(log, 'w') as f:
        f.write('\n'.join(lines) + '\n')
    assert se.main(['end', f'--status-dir={status_dir}', f'--run={run_start["run"]}',
                    f'--nonce={run_start["nonce"]}', '--rc=1', '--interval=1h',
                    f'--log={log}', '--wait=0.2']) == 0
    event = _events(status_dir)[-1]
    assert event['partial'] is True
    assert event['sum']['new'] == 5
    # A missing log still gives a run_end (from the events alone).
    assert se.main(['end', f'--status-dir={status_dir}', f'--run={run_start["run"]}',
                    f'--nonce={run_start["nonce"]}', '--rc=1', '--interval=1h',
                    f'--log={tmp_path}/missing.log', '--wait=0']) == 0
    event = _events(status_dir)[-1]
    assert event['partial'] is True and event['sum']['new'] == 5


def test_run_end_is_cut_to_fit(tmp_path):
    status_dir = str(tmp_path / 'status')
    for i in range(60):
        vid = f'id{i:05d}'
        se.append_event(_start(vid, title='t' * 400,
                               webpage_url='https://x.example/' + 'u' * 1500), status_dir)
        se.append_event(_end(vid), status_dir)
    se.append_event(_start('long', webpage_url='https://x.example/' + 'u' * 3000),
                    status_dir)
    log = tmp_path / 'log'
    log.write_text('\n'.join([_marker('start', 1, total=1, url='u'),
                              _marker('end', 1, rc=1, secs=1),
                              f'@@ytdl:{NONCE} pass_end']) + '\n')
    assert se.main(['end', f'--status-dir={status_dir}', f'--run={RUN}',
                    f'--nonce={NONCE}', '--rc=1', f'--log={log}']) == 0
    event = _events(status_dir)[-1]
    assert event['t'] == 'run_end' and event['sum']['failed'] == 61
    assert 0 < len(event['err']) < se.MAX_ERRORS
    assert len(se.encode_event(event)) <= se.MAX_EVENT_BYTES
    assert len(event['err'][0]['title']) == se.MAX_TEXT


def test_errors_are_capped():
    p = se.LogParser(NONCE, run=RUN)
    p.feed(_marker('start', 1, total=1, url='u'))
    for i in range(200):
        p.feed(f'ERROR: [youtube] id{i:05d}: Video unavailable')
    p.finish()
    s = p.summary()
    assert s['sum']['failed'] == 200
    assert len(s['err']) == se.MAX_ERRORS and s['err_total'] == 200


def test_cut_urls_are_dropped():
    p = se.LogParser(NONCE, run=RUN)
    p.add_events([_start('a', webpage_url='https://x.example/' + 'u' * 3000),
                  _start('b', webpage_url='https://x.example/b')])
    p.finish()
    assert [e.get('url') for e in p.summary()['err']] == [None, 'https://x.example/b']


def test_channel_commands(tmp_path, capsys):
    flags_file = tmp_path / 'flags'
    status_dir = str(tmp_path / 'status')
    assert se.main(['channel-start', f'--status-dir={status_dir}', f'--run={RUN}',
                    f'--nonce={NONCE}', '--index=2', '--total=3',
                    '--url=https://x/"\n\u202e', f'--flags-out={flags_file}']) == 0
    line = capsys.readouterr().out
    assert line.endswith('\n') and line.count('\n') == 1
    prefix = f'@@ytdl:{NONCE} channel '
    assert line.startswith(prefix)
    data = json.loads(line[len(prefix):])
    assert (data['phase'], data['index'], data['total'], data['url']) == \
        ('start', 2, 3, 'https://x/"')
    flags = flags_file.read_text().split('\0')
    assert flags[-1] == ''
    assert flags[:-1] == se.ytdlp_flags('scheduled', RUN, NONCE,
                                        se.events_path(status_dir), ch=2)
    assert se.main(['channel-end', f'--status-dir={status_dir}', f'--run={RUN}',
                    f'--nonce={NONCE}', '--index=2', '--rc=1', '--secs=7',
                    '--error=bad args']) == 0
    data = json.loads(capsys.readouterr().out[len(prefix):])
    assert (data['phase'], data['rc'], data['secs'], data['error']) == ('end', 1, 7, 'bad args')
    # A bad nonce fails (the script then runs the entry without flags).
    assert se.main(['channel-start', f'--status-dir={status_dir}', f'--run={RUN}',
                    '--nonce=x', '--index=1', '--total=1']) == 1
    assert capsys.readouterr().out == ''


def test_started_videos_keep_their_extractor_for_download_now():
    """A video that fails after its start event (a bot check under
    --ignore-no-formats-error, a 403 on the media) still names its
    extractor, so the dashboard can offer 'Download now'."""
    bot, media, other = 'abcdefghijk', 'ABCDEFGHIJK', 'zzzzzzzzzzz'
    p = se.LogParser(NONCE, run=RUN)
    p.add_events([_start(bot), _end(bot),
                  _start(media, webpage_url=se.YOUTUBE_WATCH + media),
                  _end(media),
                  _start(other, webpage_url='https://evil.example/watch?v=' + other),
                  _end(other)])
    for line in [
            _marker('start', 1, total=1, url='https://x/'),
            _writing('start'),
            f'ERROR: [youtube] {bot}: No video formats found!',
            _writing('end'),
            _writing('start'),
            'ERROR: unable to download video data: HTTP Error 403: Forbidden',
            _writing('end'),
            _writing('start'),
            'ERROR: unable to download video data: HTTP Error 403: Forbidden',
            _writing('end'),
            _marker('end', 1, rc=1, secs=3)]:
        p.feed(line)
    p.finish()
    errors = {e['id']: e for e in p.summary()['err']}
    assert errors[bot]['ie'] == 'youtube'
    assert errors[media]['ie'] == 'youtube'
    assert 'ie' not in errors[other]
