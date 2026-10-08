"""Test fixtures for the web UI.

The application module is named ``youtube-dl-webui.py`` (with hyphens), so it
is loaded by file path. Each test gets a client built against a temporary
config directory, with credentials and feature flags set via environment.
"""

import asyncio
import importlib.util
import os
import sys

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


@pytest.fixture
def webui_env(tmp_path, monkeypatch):
    """Create a temp config tree and point the module's PATHS at it."""
    config_dir = tmp_path / 'config'
    log_dir = tmp_path / 'logs'
    tmp_dir = tmp_path / 'tmp'
    for d in (config_dir, log_dir, tmp_dir):
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
                'WEBUI_ALLOWED_DOMAINS', 'youtubedl_webuipath'):
        monkeypatch.delenv(var, raising=False)

    return {
        'config_dir': str(config_dir),
        'log_dir': str(log_dir),
        'tmp_dir': str(tmp_dir),
        'format_file': str(format_file),
        'monkeypatch': monkeypatch,
    }


@pytest.fixture
def make_client(webui_env):
    """Return a factory: make_client(**env_overrides) -> (TestClient, captured).

    ``captured`` records the argv of the last subprocess the app spawned, so
    tests can assert the download command is built without a shell.
    """
    captured = {}

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

    def _factory(**env):
        mp = webui_env['monkeypatch']
        for key, value in env.items():
            mp.setenv(key, value)
        module = _load_module()
        _redirect_paths(module, webui_env)
        mp.setattr(module.asyncio, 'create_subprocess_exec', _fake_exec)
        app = module.create_app()
        client = TestClient(app)
        client.app_module = module
        client.csrf_token = app.state.config.csrf_token
        return client, captured

    return _factory


@pytest.fixture
def webui_module(webui_env):
    """The module with PATHS redirected, real subprocesses and no app (for
    tests of module-level functions such as download_bg)."""
    module = _load_module()
    _redirect_paths(module, webui_env)
    return module


def basic(username=USERNAME, password=PASSWORD):
    return (username, password)


def bearer(token=API_TOKEN):
    return {'Authorization': f'Bearer {token}'}
