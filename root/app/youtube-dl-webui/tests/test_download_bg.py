"""download_bg(): copying yt-dlp's output to the log and reaping the process.

The real tests run a small Python child in place of yt-dlp: a shell wrapper
named as the module's yt-dlp binary execs it with download_bg()'s argv.
"""

import asyncio
import contextlib
import os
import shlex
import signal
import sys

import pytest

URL = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
DOWNLOAD_ID = '0f40d361-c3ce-4241-9b1b-15781a8ac784'
ENDED = '[youtube-dl] Download process ended\n'
# Far beyond asyncio's 64 KiB StreamReader line limit, and more than the pipe
# and reader buffers hold, so a reader that gives up leaves the child blocked.
LONG = 1024 * 1024

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
    # Test cleanup, so a failing run does not leave a blocked child behind.
    for proc in procs:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


def test_long_output_line_is_copied_and_process_reaped(
        webui_module, webui_env, spawned, tmp_path):
    webui_module.youtubedl_binary = _fake_ytdlp(tmp_path, LONG_LINE_CHILD)

    async def run():
        try:
            await asyncio.wait_for(
                webui_module.download_bg(URL, DOWNLOAD_ID, []), 30)
            return spawned[0].returncode
        finally:
            await _leave_nothing_behind(spawned)

    returncode = asyncio.run(run())
    assert returncode == 0   # the output was drained and the process awaited
    log = _log(webui_env)
    assert 'Error' not in log
    assert ('[download] ' + 'x' * LONG + '\r[download] 100% \u00fc\n') in log
    # The child got download_bg()'s argv, with the URL after '--'.
    assert f' -- {URL}\n' in log
    assert log.endswith(ENDED)


def test_cancel_kills_and_reaps_the_process(
        webui_module, webui_env, spawned, tmp_path):
    webui_module.youtubedl_binary = _fake_ytdlp(tmp_path, SLEEPING_CHILD)

    async def run():
        task = asyncio.create_task(
            webui_module.download_bg(URL, DOWNLOAD_ID, []))
        try:
            for _ in range(200):   # until the child's output reached the log
                await asyncio.sleep(0.05)
                if spawned and 'started' in _log(webui_env):
                    break
            else:
                pytest.fail('the child never wrote to the log')
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            return spawned[0].returncode, spawned[0].pid
        finally:
            await _leave_nothing_behind(spawned)

    returncode, pid = asyncio.run(run())
    assert returncode == -signal.SIGKILL   # killed by download_bg() and reaped
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_utf8_split_across_reads_is_not_mangled(
        webui_module, webui_env, monkeypatch):
    # U+00FC (2 bytes) starts at offset 65535: a 64 KiB read splits it.
    data = b'a' * 65535 + '\u00fc'.encode() + b' invalid:\xff\n'

    class FakeProcess:
        returncode = None

        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(data)
            self.stdout.feed_eof()

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            raise AssertionError('a finished process must not be killed')

    async def fake_exec(*argv, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(webui_module.asyncio, 'create_subprocess_exec', fake_exec)
    asyncio.run(webui_module.download_bg(URL, DOWNLOAD_ID, []))
    assert _log(webui_env) == ('a' * 65535 + '\u00fc invalid:\ufffd\n' + ENDED)
