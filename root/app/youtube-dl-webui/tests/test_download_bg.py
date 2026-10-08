"""download_bg(): copying yt-dlp's output to the log and reaping the process.

The real tests run a small Python child in place of yt-dlp: a shell wrapper
named as the module's yt-dlp binary execs it with download_bg()'s argv. They
run on asyncio's default loop and on uvloop, the loop uvicorn[standard] picks
in the image.
"""

import asyncio
import contextlib
import os
import shlex
import signal
import sys
import types

import pytest

URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
DOWNLOAD_ID = '0f40d361-c3ce-4241-9b1b-15781a8ac784'
ENDED = '[youtube-dl] Download process ended\n'
# Far beyond asyncio's 64 KiB StreamReader line limit, and more than the pipe
# and reader buffers hold, so a reader that gives up leaves the child blocked.
LONG = 1024 * 1024
LOOPS = ['asyncio', 'uvloop']

# yt-dlp progress without --newline (or a long --print): one huge "line".
LONG_LINE_CHILD = f'''
import sys
out = sys.stdout.buffer
out.write(b'[download] ' + b'x' * {LONG} + b'\\r')
out.write('[download] 100% \\u00fc\\n'.encode())
out.write(('argv: ' + ' '.join(sys.argv[1:]) + '\\n').encode())
out.flush()
'''

SLEEPING_CHILD = '''
import sys, time
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


def _log(webui_env):
    path = os.path.join(webui_env['tmp_dir'], f'download_{DOWNLOAD_ID}.log')
    with open(path, encoding='utf-8', newline='') as f:   # keep '\r'
        return f.read()


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


@pytest.fixture
def spawned(webui_module, monkeypatch):
    """Records the processes download_bg() starts (real subprocesses)."""
    procs = []
    real_exec = asyncio.create_subprocess_exec

    async def capture(*argv, **kwargs):
        proc = await real_exec(*argv, **kwargs)
        procs.append(proc)
        return proc

    monkeypatch.setattr(webui_module.asyncio, 'create_subprocess_exec', capture)
    return procs


async def _leave_nothing_behind(procs):
    # Test cleanup, so a failing run leaves no blocked child behind, and
    # fails instead of hanging (see download_bg() on draining before wait()).
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


async def _wait_for_log(webui_env, text):
    for _ in range(200):
        await asyncio.sleep(0.05)
        with contextlib.suppress(FileNotFoundError):
            if text in _log(webui_env):
                return _log(webui_env)
    pytest.fail(f'{text!r} never reached the log')


@pytest.mark.parametrize('loop', LOOPS)
def test_long_output_line_is_copied_and_process_reaped(
        webui_module, webui_env, spawned, tmp_path, loop):
    webui_module.youtubedl_binary = _fake_ytdlp(tmp_path, LONG_LINE_CHILD)

    async def run():
        try:
            await asyncio.wait_for(
                webui_module.download_bg(URL, DOWNLOAD_ID, []), 30)
            return spawned[0].returncode
        finally:
            await _leave_nothing_behind(spawned)

    returncode = _run(loop, run)
    assert returncode == 0   # the output was drained and the process awaited
    log = _log(webui_env)
    assert 'Error' not in log
    assert ('[download] ' + 'x' * LONG + '\r[download] 100% \u00fc\n') in log
    # The child got download_bg()'s argv, with the URL after '--'.
    assert f' -- {URL}\n' in log
    assert log.endswith(ENDED)


@pytest.mark.parametrize('loop', LOOPS)
def test_cancel_kills_and_reaps_the_process(
        webui_module, webui_env, spawned, tmp_path, loop):
    webui_module.youtubedl_binary = _fake_ytdlp(tmp_path, SLEEPING_CHILD)

    async def run():
        task = asyncio.create_task(
            webui_module.download_bg(URL, DOWNLOAD_ID, []))
        try:
            await _wait_for_log(webui_env, 'started')
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            return spawned[0].returncode, spawned[0].pid
        finally:
            await _leave_nothing_behind(spawned)

    returncode, pid = _run(loop, run)
    assert returncode == -signal.SIGKILL   # killed by download_bg() and reaped
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize('loop', LOOPS)
def test_cancel_kills_children_sharing_the_output(
        webui_module, webui_env, spawned, tmp_path, loop):
    # Killing yt-dlp alone would leave the grandchild running, and it holds
    # the pipe open, so draining it would never end.
    webui_module.youtubedl_binary = _fake_ytdlp(tmp_path, GRANDCHILD_CHILD)
    grandchild = []

    async def run():
        task = asyncio.create_task(
            webui_module.download_bg(URL, DOWNLOAD_ID, []))
        try:
            log = await _wait_for_log(webui_env, '\n')
            grandchild.append(int(log.split()[1]))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            return spawned[0].returncode, _running(grandchild[0])
        finally:
            await _leave_nothing_behind(spawned)
            for pid in grandchild:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)

    returncode, grandchild_running = _run(loop, run)
    assert returncode == -signal.SIGKILL
    assert not grandchild_running


@pytest.mark.parametrize('loop', LOOPS)
def test_cancel_with_unread_output_does_not_hang(
        webui_module, webui_env, spawned, tmp_path, monkeypatch, loop):
    # The log write blocks, so the reader buffers yt-dlp's output until the
    # loop pauses the pipe. Before Python 3.13, asyncio's wait() would then
    # never return unless the cleanup drains the pipe first.
    webui_module.youtubedl_binary = _fake_ytdlp(tmp_path, FLOOD_CHILD)
    writing = []

    class StuckLog:
        async def write(self, text):
            writing.append(True)
            await asyncio.Event().wait()

        async def flush(self):
            pass

        async def close(self):
            pass

    async def stuck_open(*args, **kwargs):
        return StuckLog()

    monkeypatch.setattr(webui_module, 'aiofiles',
                        types.SimpleNamespace(open=stuck_open))

    async def run():
        task = asyncio.create_task(
            webui_module.download_bg(URL, DOWNLOAD_ID, []))
        try:
            for _ in range(200):
                await asyncio.sleep(0.05)
                if writing:
                    break
            else:
                pytest.fail('download_bg() never wrote to the log')
            await asyncio.sleep(0.5)   # the reader fills up and pauses
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            return spawned[0].returncode
        finally:
            await _leave_nothing_behind(spawned)

    assert _run(loop, run) == -signal.SIGKILL


def test_utf8_split_across_reads_is_not_mangled(
        webui_module, webui_env, monkeypatch):
    # U+00FC (2 bytes) starts at offset 65535: a 64 KiB read splits it. The
    # output ends inside a sequence, which the final flush must not drop.
    data = (b'a' * 65535 + '\u00fc'.encode() + b' invalid:\xff\n'
            + b'cut:' + '\u00fc'.encode()[:1])

    class FakeProcess:
        returncode = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(data)
            self.stdout.feed_eof()

        async def wait(self):
            self.returncode = 0
            return 0

        @property
        def pid(self):   # only needed to kill it, and it has finished
            raise AssertionError('a finished process must not be killed')

    async def fake_exec(*argv, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(webui_module.asyncio, 'create_subprocess_exec', fake_exec)
    asyncio.run(webui_module.download_bg(URL, DOWNLOAD_ID, []))
    assert _log(webui_env) == ('a' * 65535 + '\u00fc invalid:\ufffd\n'
                               + 'cut:\ufffd' + ENDED)
