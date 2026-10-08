"""Contract between status_events.ytdlp_flags() and the installed yt-dlp.

The event flags rely on yt-dlp behaviour that is not a stable API (the
internal progress field '_default_template', dict templates, which print
options imply --quiet). These tests run the real yt-dlp against a local HTTP
server and fail if a yt-dlp update breaks the events. They run in CI and in
the release workflow before an image is published.
"""

import http.server
import json
import os
import re
import subprocess
import sys
import threading

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import status_events as se  # noqa: E402

yt_dlp = pytest.importorskip('yt_dlp')

PAYLOAD = bytes(range(256)) * 768        # 192 KiB, served as video/mp4


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self._send(body=False)

    def do_GET(self):
        self._send(body=True)

    def _send(self, body):
        if self.path.split('?')[0] != '/clip.mp4':
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Content-Length', str(len(PAYLOAD)))
        self.end_headers()
        if body:
            self.wfile.write(PAYLOAD)


@pytest.fixture(scope='module')
def server():
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}'
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope='module')
def download(server, tmp_path_factory):
    """One real download with the scheduled-pass flags."""
    work = tmp_path_factory.mktemp('contract')
    status_dir = str(work / 'status')
    os.close(se.open_status_dir(status_dir))
    run, nonce = se.new_id(), se.new_id()
    flags = se.ytdlp_flags('scheduled', run, nonce, se.events_path(status_dir), ch=1)
    argv = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-cache-dir',
            '--proxy', '', '-P', str(work / 'dl'), '-o', '%(id)s.%(ext)s',
            *flags, '--', f'{server}/clip.mp4']
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(work)}
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          cwd=str(work), env=env, timeout=120)
    output = proc.stdout.decode('utf-8', 'replace')
    with open(se.events_path(status_dir), encoding='utf-8') as f:
        event_lines = f.read().splitlines()
    return {'rc': proc.returncode, 'output': output, 'lines': output.splitlines(),
            'event_lines': event_lines, 'run': run, 'nonce': nonce,
            'dl': work / 'dl'}


def test_download_succeeds(download):
    assert download['rc'] == 0, download['output']
    assert (download['dl'] / 'clip.mp4').read_bytes() == PAYLOAD


def test_progress_lines_carry_nonce_and_json(download):
    sentinel = '@@ytdl:' + download['nonce'] + ' '
    progress = [line for line in download['lines']
                if line.startswith('[download] ') and sentinel in line]
    assert progress, download['output']
    for line in progress:
        human, _, data = line.partition(sentinel)
        human = human[len('[download] '):].strip()
        # The internal '_default_template' field prints NA if it goes away.
        assert human and human != 'NA' and '%' in human, line
        values = json.loads(data)
        assert values['info.id'] == 'clip'
        assert values['progress.status'] in ('downloading', 'finished')
    assert json.loads(progress[-1].partition(sentinel)[2])['progress.status'] == 'finished'


def test_events_are_written_without_bare_na(download):
    events = [se.parse_event(line) for line in download['event_lines']]
    assert None not in events, download['event_lines']
    assert [(e['t'], e['src'], e['run'], e['ch']) for e in events] == [
        ('start', 'scheduled', download['run'], 1),
        ('end', 'scheduled', download['run'], 1)]
    start, end = events
    assert start['i']['id'] == 'clip' and start['i']['title'] == 'clip'
    assert isinstance(start['i']['epoch'], int)
    for event in events:
        assert 'NA' not in event['i'].values()
    [path] = end['i']['requested_downloads.:.filepath']
    assert os.path.samefile(path, download['dl'] / 'clip.mp4')


def test_writing_lines_and_parser(download):
    # The parser joins every event to the 'Writing' line yt-dlp logs for it.
    writing = [line for line in download['lines']
               if se._WRITING_RE.match(line)
               and se._WRITING_RE.match(line).group('run') == download['run']]
    assert len(writing) == 2
    parser = se.LogParser(download['nonce'], run=download['run'], unmarked=True)
    parser.add_events(se.parse_event(line) for line in download['event_lines'])
    for line in download['lines']:
        parser.feed(line)
    entry = parser.current
    assert entry.owed == 0 and not entry.pending
    assert entry.progress['info.id'] == 'clip'
    parser.finish()
    assert parser.summary()['sum'] == {'new': 1, 'skipped': 0, 'failed': 0,
                                       'filtered': 0}


def _options(*argv):
    return yt_dlp.parse_options([*argv, 'https://example.invalid/v']).ydl_opts


FLAGS = se.ytdlp_flags('scheduled', '0' * 16, '1' * 16,
                       '/config/.youtube-dl-status/events.jsonl', ch=1)


def test_flags_imply_neither_quiet_nor_simulate():
    opts = _options('--ignore-config', *FLAGS)
    assert not opts.get('quiet') and not opts.get('simulate')
    assert opts['forceprint'] == {}
    assert set(opts['print_to_file']) == {'video', 'after_video'}


def test_flags_win_over_args_conf(tmp_path):
    conf = tmp_path / 'args.conf'
    conf.write_text("--progress-delta 10\n--no-progress\n--color always\n"
                    "--progress-template 'download:USER %(progress._percent_str)s'\n")
    opts = _options('--config-location', str(conf), *FLAGS)
    assert opts['progress_delta'] == 2.0
    assert opts['noprogress'] is False
    assert opts['progress_with_newline'] is True
    assert opts['color'] == {'stdout': 'never', 'stderr': 'never'}
    assert opts['progress_template']['download'].startswith(
        '[download] %(progress._default_template)s @@ytdl:')
    # The user's --quiet stays theirs (human lines go, events stay).
    conf.write_text('--quiet\n')
    assert _options('--config-location', str(conf), *FLAGS)['quiet'] is True


def test_default_args_conf_with_flags():
    conf = os.path.join(APP_DIR, '..', '..', 'config.default', 'args.conf')
    opts = _options('--config-location', conf, *FLAGS)
    assert not opts.get('quiet') and not opts.get('simulate')
    assert opts['progress_delta'] == 2.0


def test_nested_dict_template_is_still_a_trap():
    # Documents why status_events uses parallel lists: a dict inside the
    # dict template prints a bare NA (invalid JSON).
    info = {'id': 'x', 'requested_formats': [{'format_id': 'a', 'tbr': 1}]}
    with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
        nested = ydl.evaluate_outtmpl('%(.{id,requested_formats.:.{format_id}})j', info)
        flat = ydl.evaluate_outtmpl('%(.{id,requested_formats.:.format_id})j', info)
    assert nested == 'NA'
    assert json.loads(flat) == {'id': 'x', 'requested_formats.:.format_id': ['a']}
    assert re.fullmatch(r'\{.*\}', flat)
