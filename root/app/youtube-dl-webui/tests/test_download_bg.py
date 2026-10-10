"""Manual downloads with real processes: copying yt-dlp's output to the job
log and never leaving yt-dlp behind.

These are the guarantees download_bg() gave (it was replaced by the job
manager, jobs.py): output is copied in chunks (a 1 MiB line, a UTF-8
character split across reads and the decoder's final flush), the pipe is
drained and yt-dlp awaited, and stopping a job kills yt-dlp's whole process
group without hanging on unread output.

A small Python child stands in for yt-dlp: a shell wrapper named as the
manager's yt-dlp binary execs it with the job's argv. The tests run on
asyncio's default loop and on uvloop, the loop uvicorn[standard] picks in
the image.
"""

import asyncio
import contextlib
import os
import shlex
import signal
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs  # noqa: E402

URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
ENDED = jobs.ENDED
# Far beyond asyncio's 64 KiB StreamReader line limit and the parser's
# 256 KiB line cap, and more than the pipe and reader buffers hold, so a
# reader that gives up leaves the child blocked.
LONG = 1024 * 1024
LOOPS = ['asyncio', 'uvloop']

# yt-dlp progress without --newline (or a long --print): one huge "line",
# then a progress line with the job's nonce (taken from its own argv).
LONG_LINE_CHILD = f'''
import json, re, sys
nonce = re.search('@@ytdl:([0-9a-f]+)', ' '.join(sys.argv)).group(1)
out = sys.stdout.buffer
out.write(b'[download] ' + b'x' * {LONG} + b'\\r')
out.write('[download] 100% \\u00fc\\n'.encode())
real = json.dumps({{'info.id': 'v1', 'info.title': 'Title \\u00fc',
                    'progress.status': 'finished'}})
out.write(('[download] 100% @@ytdl:' + nonce + ' ' + real + '\\n').encode())
fake = json.dumps({{'info.id': 'forged', 'progress.status': 'downloading'}})
out.write(b'y' * {LONG} + (' @@ytdl:' + nonce + ' ' + fake + '\\n').encode())
out.write(('argv: ' + ' '.join(sys.argv[1:]) + '\\n').encode())
out.flush()
'''

SLEEPING_CHILD = '''
import sys, time
print('started', flush=True)
time.sleep(60)
'''

# Ignores SIGTERM: cancelling must follow up with SIGKILL.
STUBBORN_CHILD = '''
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('started', flush=True)
time.sleep(60)
'''

# Like yt-dlp running ffmpeg: the grandchild inherits the stdout pipe.
GRANDCHILD_CHILD = '''
import subprocess, sys, time
gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print('grandchild', gc.pid, flush=True)
time.sleep(60)
'''

# yt-dlp has exited, but a child it left behind still holds the output.
ORPHAN_CHILD = '''
import subprocess, sys
gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
print('grandchild', gc.pid, flush=True)
'''

FLOOD_CHILD = f'''
import sys, time
sys.stdout.buffer.write(b'y\\n' * {LONG})
sys.stdout.flush()
time.sleep(60)
'''


def _fake_ytdlp(tmp_path, source):
    child = tmp_path / 'child.py'
    child.write_text(source)
    binary = tmp_path / 'fake-yt-dlp'
    binary.write_text('#!/bin/sh\n'
                      f'exec {shlex.quote(sys.executable)} '
                      f'{shlex.quote(str(child))} "$@"\n')
    binary.chmod(0o755)
    return str(binary)


@pytest.fixture
def manager(tmp_path):
    config = tmp_path / 'config'
    config.mkdir()
    (config / 'args.conf').write_text("--output '/downloads/%(title)s.%(ext)s'\n")
    m = jobs.JobManager(
        status_dir=str(config / '.youtube-dl-status'),
        args_file=str(config / 'args.conf'),
        cookies_file=str(config / 'cookies.txt'),
        archive_file=str(config / 'archive.txt'), format_default='bv*+ba/b',
        legacy_log_dir=str(tmp_path))
    m.procs = []

    async def capture(*argv, **kwargs):
        assert kwargs['start_new_session'] is True
        assert kwargs['stdin'] == asyncio.subprocess.DEVNULL
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        m.procs.append(proc)
        return proc

    m.spawn = capture
    return m


def _log(manager, job_id):
    with open(manager.log_path(job_id), encoding='utf-8', newline='') as f:
        return f.read()   # keeps '\r'


def _run(loop, main):
    factory = None
    if loop == 'uvloop':
        factory = pytest.importorskip('uvloop').new_event_loop
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(main())


def _running(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] not in ('Z', 'X')
    except FileNotFoundError:
        return False


async def _leave_nothing_behind(procs):
    # Test cleanup, so a failing run leaves no blocked child behind, and
    # fails instead of hanging (see JobManager._pump on draining first).
    async def reap(proc):
        while await proc.stdout.read(65536):
            pass
        await proc.wait()

    for proc in procs:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await asyncio.wait_for(reap(proc), 10)


async def _wait_for_log(manager, job_id, text):
    for _ in range(200):
        await asyncio.sleep(0.05)
        with contextlib.suppress(FileNotFoundError):
            if text in _log(manager, job_id):
                return _log(manager, job_id)
    pytest.fail(f'{text!r} never reached the log')


async def _ended(manager, job_id, timeout=10):
    job = manager.jobs[job_id]
    await asyncio.wait_for(job.task, timeout)
    return job


@pytest.mark.parametrize('loop', LOOPS)
def test_long_output_line_is_copied_and_process_reaped(manager, tmp_path, loop):
    manager.binary = _fake_ytdlp(tmp_path, LONG_LINE_CHILD)

    async def run():
        try:
            ticket = await manager.submit(URL, by='token')
            assert ticket['state'] == 'running'
            job = await _ended(manager, ticket['id'], 30)
            return job, manager.procs[0].returncode
        finally:
            await _leave_nothing_behind(manager.procs)

    job, returncode = _run(loop, run)
    assert returncode == 0   # the output was drained and the process awaited
    assert job.state == 'done' and job.rc == 0
    log = _log(manager, job.id)
    assert 'Error' not in log
    assert ('[download] ' + 'x' * LONG + '\r[download] 100% ü\n') in log
    # The parser skipped the lines over 256 KiB (the forged progress at the
    # end of the last one) and read the line between them.
    assert job.parser.current.progress['info.id'] == 'v1'
    assert job.parser.current.progress['info.title'] == 'Title ü'
    # The child got the job's argv, with the URL after '--'.
    assert f' -- {URL}\n' in log
    assert log.endswith(ENDED)


@pytest.mark.parametrize('loop', LOOPS)
def test_cancel_kills_and_reaps_the_process(manager, tmp_path, loop):
    manager.binary = _fake_ytdlp(tmp_path, SLEEPING_CHILD)

    async def run():
        try:
            ticket = await manager.submit(URL, by='basic')
            await _wait_for_log(manager, ticket['id'], 'started')
            assert (await manager.cancel(ticket['id']))['state'] == 'running'
            job = await _ended(manager, ticket['id'])
            return job, manager.procs[0].returncode, manager.procs[0].pid
        finally:
            await _leave_nothing_behind(manager.procs)

    job, returncode, pid = _run(loop, run)
    assert returncode == -signal.SIGTERM   # stopped by the manager, reaped
    assert job.state == 'cancelled'
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert _log(manager, job.id).endswith(
        f'[youtube-dl] yt-dlp exited with code {-signal.SIGTERM}\n' + ENDED)


@pytest.mark.parametrize('loop', LOOPS)
def test_cancel_follows_up_with_sigkill(manager, tmp_path, loop):
    manager.binary = _fake_ytdlp(tmp_path, STUBBORN_CHILD)
    manager.cancel_grace = 0.5

    async def run():
        try:
            ticket = await manager.submit(URL, by='basic')
            await _wait_for_log(manager, ticket['id'], 'started')
            await manager.cancel(ticket['id'])
            await asyncio.sleep(0.2)
            alive = manager.procs[0].returncode is None   # ignored SIGTERM
            job = await _ended(manager, ticket['id'])
            return job, alive, manager.procs[0].returncode
        finally:
            await _leave_nothing_behind(manager.procs)

    job, alive_after_term, returncode = _run(loop, run)
    assert alive_after_term
    assert returncode == -signal.SIGKILL
    assert job.state == 'cancelled'


@pytest.mark.parametrize('loop', LOOPS)
def test_cancel_kills_children_sharing_the_output(manager, tmp_path, loop):
    # Stopping yt-dlp alone would leave the grandchild running, and it holds
    # the pipe open, so draining it would never end.
    manager.binary = _fake_ytdlp(tmp_path, GRANDCHILD_CHILD)
    grandchild = []

    async def run():
        try:
            ticket = await manager.submit(URL, by='basic')
            log = await _wait_for_log(manager, ticket['id'], '\n')
            grandchild.append(int(log.split()[1]))
            await manager.cancel(ticket['id'])
            job = await _ended(manager, ticket['id'])
            return job, manager.procs[0].returncode, _running(grandchild[0])
        finally:
            await _leave_nothing_behind(manager.procs)
            for pid in grandchild:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)

    job, returncode, grandchild_running = _run(loop, run)
    assert returncode == -signal.SIGTERM
    assert not grandchild_running
    assert job.state == 'cancelled'


@pytest.mark.parametrize('loop', LOOPS)
def test_cancel_after_ytdlp_exited_kills_what_holds_the_output(
        manager, tmp_path, loop):
    manager.binary = _fake_ytdlp(tmp_path, ORPHAN_CHILD)
    grandchild = []

    async def run():
        try:
            ticket = await manager.submit(URL, by='basic')
            log = await _wait_for_log(manager, ticket['id'], '\n')
            grandchild.append(int(log.split()[1]))
            await asyncio.wait_for(manager.procs[0].wait(), 10)
            await asyncio.sleep(0.2)
            assert manager.jobs[ticket['id']].state == 'running'   # not EOF
            await manager.cancel(ticket['id'])
            job = await _ended(manager, ticket['id'])
            return job, _running(grandchild[0])
        finally:
            await _leave_nothing_behind(manager.procs)
            for pid in grandchild:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)

    job, grandchild_running = _run(loop, run)
    assert not grandchild_running
    assert job.state == 'cancelled' and job.rc == 0


@pytest.mark.parametrize('loop', LOOPS)
def test_log_error_kills_the_process(manager, tmp_path, loop):
    manager.binary = _fake_ytdlp(tmp_path, SLEEPING_CHILD)

    class BrokenLog:
        async def write(self, text):
            raise OSError(28, 'No space left on device')

        async def close(self):
            pass

    async def broken_open(job):
        return BrokenLog()

    manager.open_log = broken_open

    async def run():
        try:
            ticket = await manager.submit(URL, by='basic')
            job = await _ended(manager, ticket['id'])
            return job, manager.procs[0].returncode
        finally:
            await _leave_nothing_behind(manager.procs)

    job, returncode = _run(loop, run)
    assert returncode == -signal.SIGKILL
    assert job.state == 'failed' and 'No space left' in job.error['msg']


@pytest.mark.parametrize('loop', LOOPS)
def test_cancel_with_unread_output_does_not_hang(manager, tmp_path, loop):
    # The log write blocks, so the reader buffers yt-dlp's output until the
    # loop pauses the pipe. Before Python 3.13, asyncio's wait() would then
    # never return unless the cleanup drains the pipe first.
    manager.binary = _fake_ytdlp(tmp_path, FLOOD_CHILD)
    manager.cancel_grace = manager.kill_grace = manager.log_timeout = 0.3
    writing = []

    class StuckLog:
        async def write(self, text):
            writing.append(True)
            await asyncio.Event().wait()

        async def close(self):
            pass

    async def stuck_open(job):
        return StuckLog()

    manager.open_log = stuck_open

    async def run():
        try:
            ticket = await manager.submit(URL, by='basic')
            for _ in range(200):
                await asyncio.sleep(0.05)
                if writing:
                    break
            else:
                pytest.fail('the job never wrote to its log')
            await asyncio.sleep(0.5)   # the reader fills up and pauses
            await manager.cancel(ticket['id'])
            job = await _ended(manager, ticket['id'])
            return job, manager.procs[0].returncode
        finally:
            await _leave_nothing_behind(manager.procs)

    job, returncode = _run(loop, run)
    assert returncode == -signal.SIGTERM
    assert job.state == 'cancelled'


@pytest.mark.parametrize('loop', LOOPS)
def test_shutdown_stops_running_jobs(manager, tmp_path, loop):
    manager.binary = _fake_ytdlp(tmp_path, STUBBORN_CHILD)
    manager.shutdown_grace = 0.3

    async def run():
        try:
            ticket = await manager.submit(URL, by='basic')
            await _wait_for_log(manager, ticket['id'], 'started')
            await manager.shutdown()
            return manager.jobs[ticket['id']], manager.procs[0].returncode
        finally:
            await _leave_nothing_behind(manager.procs)

    job, returncode = _run(loop, run)
    assert returncode == -signal.SIGKILL
    assert job.state == 'interrupted' and not manager.running


def test_utf8_split_across_reads_is_not_mangled(manager):
    # U+00FC (2 bytes) starts at offset 65535: a 64 KiB read splits it. The
    # output ends inside a sequence, which the final flush must not drop.
    data = (b'a' * 65535 + 'ü'.encode() + b' invalid:\xff\n'
            + b'cut:' + 'ü'.encode()[:1])

    class FakeProcess:
        returncode = None
        pid = 4242

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(data)
            self.stdout.feed_eof()

        async def wait(self):
            self.returncode = 0
            return 0

    async def fake_spawn(*argv, **kwargs):
        return FakeProcess()

    def no_kill(pgid, sig):
        raise AssertionError('a finished process must not be killed')

    manager.spawn = fake_spawn
    manager.killpg = no_kill

    async def run():
        ticket = await manager.submit(URL, by='basic')
        return await _ended(manager, ticket['id'])

    job = asyncio.run(run())
    # A line the output left open is closed before the manager's own line.
    assert _log(manager, job.id) == ('a' * 65535 + 'ü invalid:�\n'
                                     + 'cut:�\n' + ENDED)
