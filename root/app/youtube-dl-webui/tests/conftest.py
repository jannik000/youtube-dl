"""Test fixtures for the web UI.

The application module is named ``youtube-dl-webui.py`` (with hyphens), so it
is loaded by file path. Each test gets a client built against a temporary
config directory, with credentials and feature flags set via environment.
"""

import asyncio
import importlib.util
import os
import re
import signal
import sys
import time

import pytest
from fastapi.testclient import TestClient

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)

USERNAME = 'admin'
PASSWORD = 'correct horse'
API_TOKEN = 'tok-ABCDEF0123456789'

DEFAULT_ARGS_CONF = "--output '/downloads/%(uploader)s/%(title)s.%(ext)s'\n"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        'webui_app', os.path.join(APP_DIR, 'youtube-dl-webui.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _redirect_paths(module, env):
    module.PATHS.app_dir = APP_DIR
    module.PATHS.config_dir = env['config_dir']
    module.PATHS.download_log_dir = env['tmp_dir']
    module.PATHS.youtube_dl_log = os.path.join(env['log_dir'], 'youtube-dl.log')
    module.PATHS.format_file = env['format_file']
    module.PATHS.downloads_dir = env['downloads_dir']
    module.PATHS.proc_dir = env['proc_dir']


@pytest.fixture
def webui_env(tmp_path, monkeypatch):
    """Create a temp config tree and point the module's PATHS at it."""
    config_dir = tmp_path / 'config'
    log_dir = tmp_path / 'logs'
    tmp_dir = tmp_path / 'tmp'
    downloads_dir = tmp_path / 'downloads'
    proc_dir = tmp_path / 'proc'   # no processes unless a test adds some
    for d in (config_dir, log_dir, tmp_dir, downloads_dir, proc_dir):
        d.mkdir()
    (config_dir / 'args.conf').write_text(DEFAULT_ARGS_CONF)
    (config_dir / 'channels.txt').write_text('# channels\n')
    format_file = tmp_path / 'format'
    format_file.write_text('bv*+ba/b')
    (log_dir / 'youtube-dl.log').write_text('hello log\n')

    # Baseline credentials; individual tests override via monkeypatch.setenv.
    monkeypatch.setenv('WEBUI_USERNAME', USERNAME)
    monkeypatch.setenv('WEBUI_PASSWORD', PASSWORD)
    monkeypatch.setenv('WEBUI_API_TOKEN', API_TOKEN)
    for var in ('WEBUI_READONLY', 'WEBUI_ALLOW_UNSAFE_ARGS',
                'WEBUI_ALLOWED_DOMAINS', 'youtubedl_webuipath',
                'youtubedl_interval'):
        monkeypatch.delenv(var, raising=False)

    return {
        'config_dir': str(config_dir),
        'log_dir': str(log_dir),
        'tmp_dir': str(tmp_dir),
        'downloads_dir': str(downloads_dir),
        'proc_dir': str(proc_dir),
        'format_file': str(format_file),
        'monkeypatch': monkeypatch,
    }


@pytest.fixture
def make_client(webui_env):
    """Return a factory: make_client(**env_overrides) -> (TestClient, captured).

    ``captured`` records the argv of the last subprocess the app spawned, so
    tests can assert the download command is built without a shell. The
    client runs the app's lifespan (the job manager's start and shutdown),
    and its event loop lives until the end of the test, so jobs started by
    one request keep running between requests.
    """
    captured = {}
    clients = []

    class _FakeStream:
        async def read(self, n=-1):
            return b''

    class _FakeProc:
        def __init__(self):
            self.stdout = _FakeStream()
            self.returncode = None

        async def wait(self):
            self.returncode = 0
            return 0

        @property
        def pid(self):   # only needed to kill it, and it has finished
            raise AssertionError('a finished process must not be killed')

    async def _fake_exec(*argv, **kwargs):
        captured['argv'] = list(argv)
        return _FakeProc()

    def _factory(setup=None, **env):
        mp = webui_env['monkeypatch']
        for key, value in env.items():
            mp.setenv(key, value)
        module = _load_module()
        _redirect_paths(module, webui_env)
        mp.setattr(module.asyncio, 'create_subprocess_exec', _fake_exec)
        app = module.create_app()
        if setup is not None:
            setup(app)   # before the lifespan starts the job manager
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        client.app_module = module
        client.csrf_token = app.state.config.csrf_token
        return client, captured

    yield _factory
    for client in reversed(clients):
        client.__exit__(None, None, None)


@pytest.fixture
def webui_module(webui_env):
    """The module with PATHS redirected, real subprocesses and no app (for
    tests of module-level functions such as download_bg)."""
    module = _load_module()
    _redirect_paths(module, webui_env)
    return module


class FakeProcess:
    """A yt-dlp stand-in for the job manager. The test feeds its output and
    ends it (through ``client.portal.call``, in the app's event loop), or
    the manager's signals end it (see FakeYtdlp.killpg)."""

    def __init__(self, argv, pid, ignore_term=False):
        self.argv = list(argv)
        self.pid = pid
        self.returncode = None
        self.ignore_term = ignore_term
        self.signals = []
        self.stdout = asyncio.StreamReader()
        self._done = asyncio.get_running_loop().create_future()

    @property
    def nonce(self):
        return re.search(r'@@ytdl:([0-9a-f]+)', ' '.join(self.argv)).group(1)

    @property
    def run(self):
        return re.search(r'"run":"([0-9a-f]+)"', ' '.join(self.argv)).group(1)

    def feed(self, data):
        self.stdout.feed_data(data)

    def exit(self, rc):
        if self.returncode is None:
            self.returncode = rc
            self.stdout.feed_eof()
            self._done.set_result(rc)

    async def wait(self):
        return await asyncio.shield(self._done)


class FakeYtdlp:
    """Spawns FakeProcesses for the job manager and plays os.killpg.

    ``script`` (or the next of ``scripts``) says what a new process does:
    {'output': [bytes...], 'rc': exit code (else it runs until signalled),
    'ignore_term': bool, 'error': exception raised instead of starting}."""

    def __init__(self):
        self.procs = []
        self.kwargs = []
        self.script = {}
        self.scripts = []

    async def spawn(self, *argv, **kwargs):
        script = self.scripts.pop(0) if self.scripts else self.script
        if script.get('error'):
            raise script['error']
        self.kwargs.append(kwargs)
        proc = FakeProcess(argv, 20000 + len(self.procs),
                           script.get('ignore_term', False))
        self.procs.append(proc)
        for chunk in script.get('output', ()):
            proc.feed(chunk)
        if 'rc' in script:
            proc.exit(script['rc'])
        return proc

    def killpg(self, pgid, sig):
        proc = next((p for p in self.procs if p.pid == pgid), None)
        if proc is None or proc.returncode is not None:
            raise ProcessLookupError(pgid)
        proc.signals.append(sig)
        if sig == signal.SIGKILL or (sig == signal.SIGTERM
                                     and not proc.ignore_term):
            proc.exit(-sig)


@pytest.fixture
def job_client(make_client):
    """make_client with the job manager on FakeYtdlp: -> (client, fake)."""
    def _factory(**env):
        fake = FakeYtdlp()

        def setup(app):
            app.state.jobs.spawn = fake.spawn
            app.state.jobs.killpg = fake.killpg

        client, _ = make_client(setup=setup, **env)
        client.jobs = client.app.state.jobs
        return client, fake

    return _factory


def wait_until(predicate, timeout=10.0):
    """Poll ``predicate`` (the app's event loop runs in another thread)."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError('condition not reached in time')
        time.sleep(0.01)


def basic(username=USERNAME, password=PASSWORD):
    return (username, password)


def bearer(token=API_TOKEN):
    return {'Authorization': f'Bearer {token}'}
