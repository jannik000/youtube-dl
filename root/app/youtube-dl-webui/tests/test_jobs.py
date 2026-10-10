"""The manual download queue (jobs.py) and its routes.

yt-dlp is played by conftest.FakeYtdlp: the tests feed its output and end
it, and the manager's process-group signals end it too. The real-process
guarantees (chunked copy, draining, killing the group) are in
test_download_bg.py.
"""

import json
import os
import signal
import stat
import sys
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs  # noqa: E402
import status_events as se  # noqa: E402
from conftest import basic, bearer, wait_until  # noqa: E402

SAME_ORIGIN = {'Sec-Fetch-Site': 'same-origin'}
VID = 'dQw4w9WgXcQ'
WATCH = f'https://www.youtube.com/watch?v={VID}'
TICKET = {'id', 'state', 'position', 'message'}
HTML = 'text/html,application/xhtml+xml,*/*;q=0.8'


def api(client, url, **data):
    return client.post('/api/download', headers=bearer(),
                       data={'url': url, **data})


def browser(client, path, data=None, **kwargs):
    kwargs.setdefault('headers', SAME_ORIGIN)
    return client.post(path, auth=basic(),
                       data={**(data or {}), 'csrf_token': client.csrf_token},
                       **kwargs)


def status(client):
    return client.get('/api/status', auth=basic()).json()


def finish(client, proc, rc=0):
    client.portal.call(proc.exit, rc)


def idle(client):
    return not client.jobs.running and not client.jobs.queued


def events(client):
    path = client.app_module.PATHS.events_file
    with open(path) as f:
        return [json.loads(line) for line in f]


def job_events(client, job_id, kind=None):
    run = job_id.replace('-', '')
    return [e for e in events(client) if e.get('run') == run
            and (kind is None or e['t'] == kind)]


def write_events(webui_env, records):
    status_dir = os.path.join(webui_env['config_dir'], '.youtube-dl-status')
    os.mkdir(status_dir, 0o700)
    with open(os.path.join(status_dir, se.EVENTS_NAME), 'w') as f:
        for record in records:
            f.write(json.dumps({'v': 1, **record}) + '\n')
    return status_dir


# -- URLs ------------------------------------------------------------------------

@pytest.mark.parametrize('url,vid', [
    (WATCH, VID),
    (f'https://youtube.com/watch?v={VID}&t=30', VID),
    (f'https://m.youtube.com/watch/?v={VID}', VID),
    (f'https://music.youtube.com/watch?v={VID}', VID),
    (f'https://youtu.be/{VID}?si=abc', VID),
    (f'https://www.youtube.com/shorts/{VID}', VID),
    (f'https://www.youtube.com/live/{VID}?feature=share', VID),
    (f'https://www.youtube.com/watch?v={VID}&list=PL123', None),   # playlist
    (f'https://youtu.be/{VID}?list=PL123', None),
    (f'https://www.youtube.com/watch?v={VID}&v=aaaaaaaaaaa', None),
    ('https://www.youtube.com/watch?v=short', None),
    ('https://www.youtube.com/@channel/videos', None),
    (f'https://evil.example/watch?v={VID}', None),
    (f'https://www.youtube.com.evil.example/watch?v={VID}', None),
])
def test_youtube_video_id(url, vid):
    assert jobs.youtube_video_id(url) == vid


def test_url_key_normalises():
    assert jobs.url_key(f'https://youtu.be/{VID}') == jobs.url_key(WATCH)
    assert (jobs.url_key('HTTPS://Example.COM/a?b=1#frag')
            == jobs.url_key('https://example.com/a?b=1'))
    assert jobs.url_key('https://example.com') == jobs.url_key(
        'https://example.com/')
    assert jobs.url_key('https://example.com/A') != jobs.url_key(
        'https://example.com/a')


# -- queue -------------------------------------------------------------------------

def test_queue_holds_one_running_and_twenty_waiting(job_client):
    client, fake = job_client()
    first = api(client, 'https://example.com/v0')
    assert first.status_code == 200 and set(first.json()) == TICKET
    assert first.json()['state'] == 'running'
    assert first.json()['position'] is None
    assert first.json()['message'] == 'Download started.'
    for n in range(1, 21):
        answer = api(client, f'https://example.com/v{n}').json()
        assert answer['state'] == 'queued' and answer['position'] == n
        assert answer['message'] == f'Queued (position {n}).'
    full = api(client, 'https://example.com/v21')
    assert full.status_code == 429 and full.headers['retry-after'] == '60'
    assert full.json()['message'] == 'Queue is full (20 waiting). Try again later.'
    # The browser form is refused the same way: JSON for the script, a page
    # for a form sent without JavaScript.
    assert browser(client, '/download', {'url': 'https://example.com/v22'}
                   ).status_code == 429
    page = browser(client, '/download', {'url': 'https://example.com/v22'},
                   headers={**SAME_ORIGIN, 'Accept': HTML})
    assert page.status_code == 429 and page.headers['retry-after'] == '60'
    assert page.headers['content-type'].startswith('text/html')
    assert 'Queue is full (20 waiting)' in page.text
    assert len(fake.procs) == 1
    shown = status(client)
    assert [j['position'] for j in shown['jobs']['queued']] == list(range(1, 21))
    assert shown['pending']['queued'] == 20
    assert shown['jobs']['running'][0]['url'] == 'https://example.com/v0'
    # The next job starts when the running one ends, and a place is free.
    finish(client, fake.procs[0])
    wait_until(lambda: len(fake.procs) == 2)
    assert fake.procs[1].argv[-1] == 'https://example.com/v1'
    assert api(client, 'https://example.com/v21').json()['position'] == 20


def test_a_duplicate_returns_the_active_job(job_client):
    client, fake = job_client()
    first = api(client, 'https://example.com/a').json()
    again = api(client, 'https://EXAMPLE.com/a#comments').json()
    assert again == {'id': first['id'], 'state': 'running', 'position': None,
                     'message': 'Already downloading.'}
    queued = api(client, WATCH).json()
    for variant in (f'https://youtu.be/{VID}', f'https://m.youtube.com/watch?v={VID}&t=5',
                    f'https://www.youtube.com/shorts/{VID}'):
        answer = api(client, variant).json()
        assert answer == {'id': queued['id'], 'state': 'queued', 'position': 1,
                          'message': 'Already in the queue (#1).'}
    # The playlist around the same video is another download.
    other = api(client, f'{WATCH}&list=PL123').json()
    assert other['id'] != queued['id'] and other['position'] == 2
    assert len(fake.procs) == 1
    # Once it has ended, the same URL is a new job.
    finish(client, fake.procs[0])
    wait_until(lambda: len(fake.procs) == 2)
    assert api(client, 'https://example.com/a').json()['id'] != first['id']


@pytest.mark.parametrize('value,expected', [
    ('', 1), ('1', 1), ('2', 2), ('3', 3), ('9', 3), ('0', 1), ('x', 1),
    ('-2', 1)])
def test_parallel_downloads_are_clamped(job_client, value, expected):
    client, fake = job_client(WEBUI_MAX_PARALLEL_DOWNLOADS=value)
    for n in range(4):
        api(client, f'https://example.com/{n}')
    assert len(fake.procs) == expected
    assert len(status(client)['jobs']['running']) == expected


def test_browser_form_redirects_to_the_job_page(job_client):
    client, fake = job_client()
    r = browser(client, '/download', {'url': 'https://example.com/v'},
                follow_redirects=False)
    assert r.status_code == 303
    job_id = r.headers['location'].rsplit('/', 1)[1]
    uuid.UUID(job_id)
    assert client.get(f'/download/{job_id}', auth=basic()).status_code == 200
    [running] = status(client)['jobs']['running']
    assert running['id'] == job_id and running['src'] == 'web'
    [queued] = job_events(client, job_id, 'job_queued')
    assert queued['by'] == 'basic' and queued['url'] == 'https://example.com/v'
    assert queued['force'] is False


# -- argv (D2) ---------------------------------------------------------------------

def _argv(client, fake, url='https://example.com/v', **data):
    assert api(client, url, **data).status_code == 200
    return fake.procs[-1]


def test_job_argv(job_client):
    client, fake = job_client()
    paths = client.app_module.PATHS
    proc = _argv(client, fake)
    argv = proc.argv
    assert argv[:10] == [
        'yt-dlp', '--no-playlist-reverse', '--playlist-end', '-1',
        '--config-location', paths.args_file, '--format', 'bv*+ba/b',
        '--download-archive', paths.archive_file]
    # The event flags follow everything the user controls, then '--' URL.
    assert argv[10:-2] == se.ytdlp_flags('manual', proc.run, proc.nonce,
                                         paths.events_file)
    assert argv[-2:] == ['--', 'https://example.com/v']
    assert argv.index('--') == len(argv) - 2
    assert proc.run == job_events(client, status(client)['jobs']['running'][0]
                                  ['id'], 'job_start')[0]['run']
    assert fake.kwargs[0]['stdin'] == jobs.asyncio.subprocess.DEVNULL
    assert fake.kwargs[0]['start_new_session'] is True
    assert '--cookies' not in argv


def test_job_argv_with_cookies_and_force(job_client):
    client, fake = job_client()
    paths = client.app_module.PATHS
    with open(paths.cookies_file, 'w') as f:
        f.write('# Netscape HTTP Cookie File\n')
    argv = _argv(client, fake, force='1').argv
    i = argv.index('--cookies')
    assert argv[i + 1] == paths.cookies_file and i < argv.index('--newline')
    # Forced: no archive at all, not even one args.conf chooses.
    assert '--no-download-archive' in argv
    assert '--download-archive' not in argv
    [queued] = [e for e in events(client) if e['t'] == 'job_queued']
    assert queued['force'] is True


def test_job_argv_when_args_conf_chooses_format_and_archive(job_client):
    client, fake = job_client()
    paths = client.app_module.PATHS
    with open(paths.args_file, 'w') as f:
        f.write("--format 'best'\n--download-archive /config/mine.txt\n")
    argv = _argv(client, fake).argv
    assert '--format' not in argv and '--download-archive' not in argv
    assert '--no-download-archive' not in argv


# -- archive check (D2) ------------------------------------------------------------

def test_archived_youtube_video_is_answered_at_once(job_client):
    client, fake = job_client()
    paths = client.app_module.PATHS
    with open(paths.archive_file, 'w') as f:
        f.write(f'youtube aaaaaaaaaaa\nyoutube {VID}\nvimeo 123\n')
    for url in (WATCH, f'https://youtu.be/{VID}',
                f'https://www.youtube.com/shorts/{VID}',
                f'https://www.youtube.com/live/{VID}'):
        answer = api(client, url)
        assert answer.status_code == 200
        assert answer.json()['state'] == 'already'
        assert answer.json()['position'] is None
        assert answer.json()['message'].startswith('Already downloaded')
    assert not fake.procs                       # yt-dlp never started
    already = status(client)['jobs']['finished'][0]
    assert already['state'] == 'already' and already['src'] == 'ios'
    log = client.get(f'/log/download/{already["id"]}', auth=basic()).text
    assert 'already in the download archive' in log
    # The archive is read again when it changes.
    with open(paths.archive_file, 'a') as f:
        f.write('youtube bbbbbbbbbbb\n')
    assert api(client, 'https://youtu.be/bbbbbbbbbbb').json()['state'] == 'already'
    # A playlist URL is not answered from the archive.
    assert api(client, f'{WATCH}&list=PL1').json()['state'] == 'running'
    # Re-download: a retry with force runs yt-dlp without the archive.
    r = browser(client, f'/api/download/{already["id"]}/retry', {'force': '1'})
    assert r.status_code == 200 and r.json()['state'] == 'queued'
    finish(client, fake.procs[0])
    wait_until(lambda: len(fake.procs) == 2)
    assert '--no-download-archive' in fake.procs[1].argv
    assert fake.procs[1].argv[-1] == f'https://www.youtube.com/live/{VID}'


def test_archive_check_is_skipped_when_args_conf_has_its_own(job_client):
    client, fake = job_client()
    paths = client.app_module.PATHS
    with open(paths.archive_file, 'w') as f:
        f.write(f'youtube {VID}\n')
    with open(paths.args_file, 'w') as f:
        f.write('--download-archive /config/other.txt\n')
    assert api(client, WATCH).json()['state'] == 'running'


def test_archive_index_refuses_symlinks(tmp_path):
    target = tmp_path / 'real.txt'
    target.write_text(f'youtube {VID}\n')
    link = tmp_path / 'archive.txt'
    link.symlink_to(target)
    assert not jobs.ArchiveIndex(str(link)).contains(VID)
    assert jobs.ArchiveIndex(str(target)).contains(VID)


# -- cancel ----------------------------------------------------------------------------

def test_cancelling_a_queued_job_never_starts_it(job_client):
    client, fake = job_client()
    api(client, 'https://example.com/a')
    queued = api(client, 'https://example.com/b').json()
    r = browser(client, f'/api/download/{queued["id"]}/cancel')
    assert r.status_code == 200
    assert r.json() == {'id': queued['id'], 'state': 'cancelled',
                        'position': None, 'message': 'Download cancelled.'}
    assert browser(client, f'/api/download/{queued["id"]}/cancel').status_code == 409
    finish(client, fake.procs[0])
    wait_until(lambda: idle(client))
    assert len(fake.procs) == 1
    [end] = job_events(client, queued['id'], 'job_end')
    assert end['state'] == 'cancelled'
    assert not job_events(client, queued['id'], 'job_start')
    states = {j['id']: j['state'] for j in status(client)['jobs']['finished']}
    assert states[queued['id']] == 'cancelled'


@pytest.mark.parametrize('ignore_term,signals', [
    (False, [signal.SIGTERM]), (True, [signal.SIGTERM, signal.SIGKILL])])
def test_cancelling_a_running_job(job_client, ignore_term, signals):
    client, fake = job_client()
    client.jobs.cancel_grace = 0.2
    fake.script = {'ignore_term': ignore_term}
    job_id = api(client, 'https://example.com/a').json()['id']
    r = browser(client, f'/api/download/{job_id}/cancel')
    assert r.status_code == 202
    assert r.json() == {'id': job_id, 'state': 'running', 'position': None,
                        'message': 'Cancelling the download.'}
    wait_until(lambda: idle(client))
    assert fake.procs[0].signals == signals
    assert fake.procs[0].pid == 20000   # the group of the process it started
    [end] = job_events(client, job_id, 'job_end')
    assert end['state'] == 'cancelled' and end['rc'] == -signals[-1]
    assert status(client)['jobs']['finished'][0]['state'] == 'cancelled'


def test_cancel_and_retry_of_unknown_jobs(job_client):
    client, _ = job_client()
    unknown = str(uuid.uuid4())
    for action in ('cancel', 'retry'):
        assert browser(client, f'/api/download/{unknown}/{action}').status_code == 404
        assert browser(client, f'/api/download/{unknown.upper()}/{action}'
                       ).status_code == 404


# -- retry -----------------------------------------------------------------------------

def test_retry_validates_the_url_again(job_client):
    client, fake = job_client()
    job_id = api(client, 'https://example.com/a').json()['id']
    retry = f'/api/download/{job_id}/retry'
    assert browser(client, retry).status_code == 409    # still running
    finish(client, fake.procs[0], 1)
    wait_until(lambda: idle(client))
    assert status(client)['jobs']['finished'][0]['state'] == 'failed'
    client.app.state.config.allowed_domains = ('youtube.com',)
    assert browser(client, retry).status_code == 403
    client.app.state.config.allowed_domains = ()
    again = browser(client, retry)
    assert again.status_code == 200 and again.json()['state'] == 'running'
    assert again.json()['id'] != job_id
    assert fake.procs[1].argv[-1] == 'https://example.com/a'
    [queued] = job_events(client, again.json()['id'], 'job_queued')
    assert queued['by'] == 'basic'


# -- download now -----------------------------------------------------------------------

def test_download_now_builds_a_watch_url(job_client):
    client, fake = job_client()
    r = browser(client, '/api/download-now', {'video_id': VID})
    assert r.status_code == 200 and set(r.json()) == TICKET
    assert fake.procs[0].argv[-2:] == ['--', WATCH]
    for bad in ('', 'dQw4w9WgXc', f'{VID}&list=x', '../../etc/x', f'{VID} '):
        assert browser(client, '/api/download-now',
                       {'video_id': bad}).status_code == 400, bad
    client.app.state.config.allowed_domains = ('example.com',)
    assert browser(client, '/api/download-now',
                   {'video_id': 'aaaaaaaaaaa'}).status_code == 403


# -- outcome ---------------------------------------------------------------------------

def _video_events(client, proc, items):
    status_dir = client.app_module.PATHS.status_dir
    for vid, title, done in items:
        se.append_event({'t': 'start', 'src': 'manual', 'run': proc.run,
                         'i': {'id': vid, 'title': title}}, status_dir)
        info = {'id': vid, 'title': title, 'epoch': int(time.time())}
        if done:
            info['requested_downloads.:.filepath'] = [f'/downloads/{vid}.mp4']
        se.append_event({'t': 'end', 'src': 'manual', 'run': proc.run,
                         'i': info}, status_dir)


@pytest.mark.parametrize('rc,items,output,state,error', [
    (0, [('v1', 'First', True)], b'', 'done', None),
    (1, [('v1', 'First', True), ('v2', 'Second', False)],
     b'ERROR: unable to download video data: HTTP Error 429: Too Many Requests\n',
     'partial', 'rate_limit'),
    (1, [], b'ERROR: [generic] Unable to download webpage: HTTP Error 404: '
     b'Not Found\n', 'failed', 'unavailable'),
    (2, [], b'', 'failed', 'other'),
    (0, [], f'[download] {VID}: has already been recorded in the archive\n'
     .encode(), 'already', None),
])
def test_job_outcome(job_client, rc, items, output, state, error):
    client, fake = job_client()
    job_id = api(client, 'https://example.com/v').json()['id']
    proc = fake.procs[0]
    _video_events(client, proc, items)
    client.portal.call(proc.feed, output)
    finish(client, proc, rc)
    wait_until(lambda: idle(client))
    [end] = job_events(client, job_id, 'job_end')
    assert end['state'] == state and end['rc'] == rc
    assert (end.get('error') or {}).get('kind') == error
    if items:
        assert end['title'] == 'First'
    [shown] = status(client)['jobs']['finished']
    assert shown['state'] == state
    assert shown['items'] == {'done': sum(1 for i in items if i[2]),
                              'failed': sum(1 for i in items if not i[2])}
    if state in ('failed', 'partial'):
        [group] = [g for g in status(client)['problems'] if g['kind'] == error]
        assert group['items'][0]['src'] == 'manual'
    log = client.get(f'/log/download/{job_id}', auth=basic()).text
    assert log.endswith('[youtube-dl] Download process ended\n')
    assert ('exited with code' in log) == (rc != 0)


def test_a_job_that_cannot_start_fails_and_the_next_one_runs(job_client):
    client, fake = job_client()
    fake.scripts = [{'error': FileNotFoundError(2, 'No such file', 'yt-dlp')}]
    first = api(client, 'https://example.com/a').json()
    wait_until(lambda: not client.jobs.running)
    [end] = job_events(client, first['id'], 'job_end')
    assert end['state'] == 'failed'
    assert end['error']['msg'].startswith('could not start yt-dlp')
    assert api(client, 'https://example.com/b').json()['state'] == 'running'


# -- status and logs ----------------------------------------------------------------------

def test_running_job_in_status_without_the_nonce(job_client):
    client, fake = job_client()
    job_id = api(client, WATCH).json()['id']
    proc = fake.procs[0]
    progress = {'progress.status': 'downloading',
                'progress.downloaded_bytes': 4200, 'progress.total_bytes': 10000,
                'progress.speed': 1000.0, 'progress.eta': 6, 'info.id': 'vid1',
                'info.title': 'Clip ü', 'info.playlist_title': 'List',
                'info.playlist_autonumber': 2, 'info.n_entries': 5}
    forged = dict(progress, **{'progress.downloaded_bytes': 9999,
                               'info.title': 'Forged'})
    lines = [
        f'[download]  42.0% @@ytdl:{proc.nonce} {json.dumps(progress)}',
        f'[download] Forged @@ytdl:{"0" * 16} {json.dumps(forged)}',
        f'[download] Forged {json.dumps(forged)}',
    ]
    client.portal.call(proc.feed, ('\n'.join(lines) + '\n').encode())
    wait_until(lambda: (status(client)['jobs']['running'][0]['video'] or {})
               .get('percent') == 42.0)
    answer = client.get('/api/status', auth=basic())
    [running] = answer.json()['jobs']['running']
    assert running['id'] == job_id and running['src'] == 'ios'
    assert running['video']['title'] == 'Clip ü'
    assert running['item'] == {'index': 2, 'count': 5, 'source': 'events'}
    assert running['cancelling'] is False and running['force'] is False
    logs = [client.get(f'/log/download/{job_id}', auth=basic()).text,
            client.get(f'/api/log/download/{job_id}?since=0', auth=basic()).text]
    assert '[download]  42.0%' in logs[0]
    for text in [answer.text] + logs:
        assert proc.nonce not in text
    # The job log is private: directory 0700, file 0600.
    path = client.jobs.log_path(job_id)
    assert path == os.path.join(client.app_module.PATHS.status_dir, 'jobs',
                                f'{job_id}.log')
    assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_job_logs_are_pruned_to_the_newest_50(job_client):
    client, fake = job_client()
    jobs_dir = os.path.join(client.app_module.PATHS.status_dir, 'jobs')
    os.makedirs(jobs_dir, 0o700)
    os.chmod(os.path.dirname(jobs_dir), 0o700)
    old = []
    for n in range(55):
        name = os.path.join(jobs_dir, f'{uuid.uuid4()}.log')
        with open(name, 'w') as f:
            f.write('old\n')
        os.utime(name, (1000 + n, 1000 + n))
        old.append(name)
    with open(os.path.join(jobs_dir, 'notes.txt'), 'w') as f:
        f.write('not a job log\n')
    job_id = api(client, 'https://example.com/v').json()['id']
    finish(client, fake.procs[0])
    wait_until(lambda: idle(client))
    left = set(os.listdir(jobs_dir))
    assert f'{job_id}.log' in left and 'notes.txt' in left
    assert len(left) == 51
    assert not any(os.path.basename(p) in left for p in old[:6])
    assert all(os.path.basename(p) in left for p in old[6:])


def test_old_download_logs_are_still_read(make_client):
    client, _ = make_client()
    job_id = str(uuid.uuid4())
    path = os.path.join(client.app_module.PATHS.download_log_dir,
                        f'download_{job_id}.log')
    with open(path, 'w') as f:
        f.write('old log\n')
    assert client.get(f'/log/download/{job_id}', auth=basic()).text == 'old log\n'


# -- restart of the web UI ----------------------------------------------------------------

def test_stopping_the_web_ui_interrupts_running_jobs(job_client, make_client):
    client, fake = job_client()
    running = api(client, 'https://example.com/a').json()['id']
    queued = api(client, 'https://example.com/b').json()['id']
    client.__exit__(None, None, None)            # lifespan shutdown
    assert fake.procs[0].signals == [signal.SIGTERM]
    [end] = job_events(client, running, 'job_end')
    assert end['state'] == 'interrupted'
    assert not job_events(client, queued, 'job_end')   # stays queued
    # The next start resumes the queue.
    again, captured = make_client()
    assert captured['argv'][-1] == 'https://example.com/b'
    finished = {j['id']: j['state'] for j in status(again)['jobs']['finished']}
    assert finished[running] == 'interrupted'


def test_restart_recovery(job_client, webui_env):
    now = int(time.time())
    ids = {name: uuid.uuid4().hex for name in (
        'first', 'second', 'running', 'old', 'denied', 'done')}

    def queued(name, url, ts=now - 60, by='token'):
        return {'t': 'job_queued', 'ts': ts, 'run': ids[name], 'url': url,
                'by': by, 'force': name == 'second'}

    write_events(webui_env, [
        queued('done', 'https://example.com/done'),
        {'t': 'job_start', 'ts': now - 59, 'run': ids['done']},
        {'t': 'job_end', 'ts': now - 50, 'run': ids['done'], 'state': 'done',
         'rc': 0},
        queued('first', 'https://example.com/first'),
        queued('running', 'https://example.com/running'),
        {'t': 'job_start', 'ts': now - 30, 'run': ids['running']},
        queued('second', 'https://example.com/second', by='basic'),
        queued('old', 'https://example.com/old', ts=now - 2 * 86400),
        queued('denied', 'https://vimeo.com/1'),
    ])
    client, fake = job_client(WEBUI_ALLOWED_DOMAINS='example.com')
    # The first queued job runs again, the second waits, forced as before.
    assert [p.argv[-1] for p in fake.procs] == ['https://example.com/first']
    assert fake.procs[0].run == ids['first']
    shown = status(client)
    assert [j['url'] for j in shown['jobs']['queued']] == ['https://example.com/second']
    assert shown['jobs']['queued'][0]['force'] is True
    assert shown['jobs']['running'][0]['src'] == 'ios'
    ends = {e['run']: e for e in events(client) if e['t'] == 'job_end'}
    assert ends[ids['running']]['state'] == 'interrupted'
    assert ends[ids['old']]['state'] == 'interrupted'
    assert ends[ids['denied']]['state'] == 'failed'
    assert 'WEBUI_ALLOWED_DOMAINS' in ends[ids['denied']]['error']['msg']
    finished = {j['id']: j['state'] for j in shown['jobs']['finished']}
    assert finished == {str(uuid.UUID(ids[n])): s for n, s in (
        ('done', 'done'), ('running', 'interrupted'), ('old', 'interrupted'),
        ('denied', 'failed'))}
    # An interrupted job can be retried.
    r = browser(client, f'/api/download/{uuid.UUID(ids["running"])}/retry')
    assert r.status_code == 200 and r.json()['state'] == 'queued'


def test_start_does_not_create_the_status_dir(make_client):
    client, _ = make_client()
    assert not os.path.exists(client.app_module.PATHS.status_dir)


# -- run now --------------------------------------------------------------------------------

def test_run_now_asks_while_a_pass_runs(make_client):
    client, captured = make_client()
    paths = client.app_module.PATHS
    pid = 4321
    os.makedirs(paths.status_dir, 0o700)
    se.append_event({'t': 'run_start', 'ts': int(time.time()), 'run': 'a' * 16,
                     'nonce': 'b' * 16, 'pid': pid, 'channels': 2,
                     'interval': '3h', 'ytdlp': '2026.08.19'}, paths.status_dir)
    os.makedirs(os.path.join(paths.proc_dir, str(pid)))
    with open(os.path.join(paths.proc_dir, str(pid), 'cmdline'), 'wb') as f:
        f.write(b'/bin/bash\0/app/youtube-dl/youtube-dl.sh\0')
    r = browser(client, '/restart-youtube-dl', follow_redirects=False)
    assert r.status_code == 409 and 'confirm=1' in r.json()['detail']
    # Without JavaScript the form gets a page that asks, with a form that
    # confirms (and carries the CSRF token).
    page = browser(client, '/restart-youtube-dl', follow_redirects=False,
                   headers={**SAME_ORIGIN, 'Accept': HTML})
    assert page.status_code == 409
    assert 'name="confirm" value="1"' in page.text
    assert f'name="csrf_token" value="{client.csrf_token}"' in page.text
    assert 'argv' not in captured
    r = browser(client, '/restart-youtube-dl', {'confirm': '1'},
                follow_redirects=False)
    assert r.status_code == 303
    assert captured['argv'] == ['supervisorctl', 'restart', 'youtube-dl']
    # Once the pass has ended, no confirmation is needed.
    captured.clear()
    se.append_event({'t': 'run_end', 'ts': int(time.time()), 'run': 'a' * 16,
                     'rc': 0, 'next': None, 'partial': False, 'sum': {},
                     'ch': [], 'err': []}, paths.status_dir)
    assert browser(client, '/restart-youtube-dl',
                   follow_redirects=False).status_code == 303
    assert captured['argv'] == ['supervisorctl', 'restart', 'youtube-dl']


# -- auth and CSRF of the new routes ----------------------------------------------------------

JOB_POST_ROUTES = ['/api/download/{id}/cancel', '/api/download/{id}/retry',
                   '/api/download-now', '/restart-youtube-dl']


@pytest.mark.parametrize('route', JOB_POST_ROUTES)
def test_job_routes_need_basic_auth_and_csrf(job_client, route):
    client, fake = job_client()
    job_id = api(client, 'https://example.com/a').json()['id']
    path = route.format(id=job_id)
    data = {'video_id': VID, 'confirm': '1'}
    assert client.post(path, auth=basic(), headers=SAME_ORIGIN,
                       data=data).status_code == 403
    assert client.post(path, auth=basic(), headers={'Sec-Fetch-Site': 'cross-site'},
                       data={**data, 'csrf_token': client.csrf_token}
                       ).status_code == 403
    assert client.post(path, headers=bearer(),
                       data={**data, 'csrf_token': client.csrf_token}
                       ).status_code == 401
    assert client.post(path, data=data).status_code == 401
    # Nothing happened.
    assert len(fake.procs) == 1 and fake.procs[0].signals == []
    assert client.jobs.running
