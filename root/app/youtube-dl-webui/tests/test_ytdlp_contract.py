"""Contract between status_events.ytdlp_flags() and the installed yt-dlp.

The event flags rely on yt-dlp behaviour that is not a stable API (the
internal progress field '_default_template', dict templates, which print
options imply --quiet). These tests run the real yt-dlp against a local HTTP
server and fail if a yt-dlp update breaks the events. They run in CI and in
the release workflow before an image is published, and need no network: the
only server is a loopback one, and yt-dlp runs with --proxy ''.

With YTDLP_CONTRACT_VERSION set (CI and release.yml set it to the version
under test), a missing yt-dlp or a different installed version fails the run
instead of skipping it, so drift cannot slip through as a skip.
"""

import asyncio
import http.server
import json
import os
import re
import shlex
import subprocess
import sys
import threading

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

import dashboard  # noqa: E402
import jobs  # noqa: E402
import status_events as se  # noqa: E402

REQUIRED_VERSION = os.environ.get('YTDLP_CONTRACT_VERSION', '').strip()
if REQUIRED_VERSION:
    import yt_dlp  # noqa: E402  (required: an ImportError fails the run)
else:
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


def test_installed_version_is_the_one_under_test():
    if not REQUIRED_VERSION:
        pytest.skip('YTDLP_CONTRACT_VERSION not set')
    from yt_dlp.version import __version__
    assert __version__ == REQUIRED_VERSION


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


def test_manual_jobs_with_the_real_ytdlp(server, tmp_path):
    # The job manager's argv (D2: archive, force) and event flags, run by the
    # real yt-dlp: a download, the same URL again (skipped through the
    # archive), and once more forced (without the archive).
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'args.conf').write_text(
        f"-P {shlex.quote(str(tmp_path / 'dl'))}\n-o '%(id)s.%(ext)s'\n"
        "--no-cache-dir\n--proxy ''\n")
    wrapper = tmp_path / 'yt-dlp'
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} '
                       '-m yt_dlp "$@"\n')
    wrapper.chmod(0o755)
    status_dir = str(config / '.youtube-dl-status')
    manager = jobs.JobManager(
        status_dir=status_dir, args_file=str(config / 'args.conf'),
        cookies_file=str(config / 'cookies.txt'),
        archive_file=str(config / 'archive.txt'), format_default='b',
        binary=str(wrapper), legacy_log_dir=str(tmp_path))
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(tmp_path)}

    async def spawn(*argv, **kwargs):
        return await asyncio.create_subprocess_exec(
            *argv, env=env, cwd=str(tmp_path), **kwargs)

    manager.spawn = spawn

    async def run():
        out = []
        for force in (False, False, True):
            ticket = await manager.submit(f'{server}/clip.mp4', by='token',
                                          force=force)
            job = manager.jobs[ticket['id']]
            await asyncio.wait_for(job.task, 120)
            out.append(job)
        return out

    done, skipped, forced = asyncio.run(run())
    logs = [open(manager.log_path(job.id)).read() for job in (done, skipped, forced)]
    assert [(j.state, j.rc) for j in (done, skipped, forced)] == [
        ('done', 0), ('already', 0), ('done', 0)], logs
    assert done.title == 'clip'
    assert (config / 'archive.txt').read_text() == 'generic clip\n'
    assert (tmp_path / 'dl' / 'clip.mp4').read_bytes() == PAYLOAD
    assert 'has already been recorded in the archive' in logs[1]
    events = list(se.read_events(status_dir))
    assert [(e['t'], e['src']) for e in events if e['run'] == done.hex
            and e['t'] in ('start', 'end')] == [('start', 'manual'),
                                                  ('end', 'manual')]
    # What the dashboard makes of it.
    dash = dashboard.Dashboard(status_dir, str(tmp_path / 'none.log'),
                               str(tmp_path), str(config / 'cookies.txt'),
                               proc_dir=str(tmp_path))
    finished = dash.status(jobs=manager.snapshot())['jobs']['finished']
    assert [(j['id'], j['state']) for j in finished] == [
        (forced.id, 'done'), (skipped.id, 'already'), (done.id, 'done')]
    assert finished[2]['items'] == {'done': 1, 'failed': 0}
    assert finished[2]['src'] == 'ios' and finished[2]['title'] == 'clip'
