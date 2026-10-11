"""Credentials from files: WEBUI_USERNAME_FILE, WEBUI_PASSWORD_FILE and
WEBUI_API_TOKEN_FILE (Docker/compose secrets)."""

import base64
import os
import subprocess
import sys

import pytest

from conftest import APP_DIR

import webui_security

SECRET_PW = 'pw-from-file-9f3c'
SECRET_TOKEN = 'tok-from-file-77aa'


def _basic(user, pw):
    raw = base64.b64encode(f'{user}:{pw}'.encode()).decode()
    return {'Authorization': f'Basic {raw}'}


@pytest.fixture
def secret(tmp_path):
    def _write(name, content, mode=0o644):
        path = tmp_path / name
        path.write_bytes(content.encode() if isinstance(content, str) else content)
        os.chmod(path, mode)
        return str(path)
    return _write


@pytest.fixture
def clean_env(monkeypatch):
    for name in webui_security.SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name + '_FILE', raising=False)
    return monkeypatch


def test_values_from_files_with_trailing_newline(clean_env, secret):
    clean_env.setenv('WEBUI_USERNAME_FILE', secret('user', 'admin\n'))
    clean_env.setenv('WEBUI_PASSWORD_FILE', secret('pw', SECRET_PW + '\n'))
    clean_env.setenv('WEBUI_API_TOKEN_FILE', secret('tok', SECRET_TOKEN + ' \r\n\n'))
    config = webui_security.Config()
    assert config.startup_error() is None
    assert (config.username, config.password, config.api_token) == (
        'admin', SECRET_PW, SECRET_TOKEN)
    assert config.notes == [
        'WEBUI_USERNAME set from WEBUI_USERNAME_FILE',
        'WEBUI_PASSWORD set from WEBUI_PASSWORD_FILE',
        'WEBUI_API_TOKEN set from WEBUI_API_TOKEN_FILE']


def test_only_trailing_whitespace_is_removed(clean_env, secret):
    clean_env.setenv('WEBUI_API_TOKEN_FILE', secret('tok', ' lead\tinner x\n'))
    assert webui_security.Config().api_token == ' lead\tinner x'


def test_file_mixes_with_plain_env(clean_env, secret):
    clean_env.setenv('WEBUI_USERNAME', 'admin')
    clean_env.setenv('WEBUI_PASSWORD_FILE', secret('pw', SECRET_PW))
    config = webui_security.Config()
    assert config.startup_error() is None and config.has_basic_auth


@pytest.mark.parametrize('name', webui_security.SECRET_NAMES)
def test_value_and_file_both_set_refuses(clean_env, secret, name):
    clean_env.setenv('WEBUI_USERNAME', 'admin')
    clean_env.setenv('WEBUI_PASSWORD', 'x')
    clean_env.setenv('WEBUI_API_TOKEN', 'y')
    clean_env.setenv(name + '_FILE', secret('f', 'other-value'))
    error = webui_security.Config().startup_error()
    assert error == f'{name} and {name}_FILE are both set; set only one of them'


@pytest.mark.parametrize('content,expected', [
    ('', 'which is empty'),
    ('\n \t\r\n', 'which is empty'),
    (b'\xff\xfe', 'is not valid UTF-8'),
    ('x' * (64 * 1024 + 1), 'is larger than 64 KiB'),
])
def test_unusable_file_refuses(clean_env, secret, content, expected):
    clean_env.setenv('WEBUI_API_TOKEN_FILE', secret('tok', content))
    error = webui_security.Config().startup_error()
    assert expected in error and error.startswith('WEBUI_API_TOKEN_FILE')


def test_missing_file_and_directory_refuse(clean_env, tmp_path):
    clean_env.setenv('WEBUI_API_TOKEN_FILE', str(tmp_path / 'nope'))
    assert 'which does not exist' in webui_security.Config().startup_error()
    clean_env.setenv('WEBUI_API_TOKEN_FILE', str(tmp_path))
    assert 'not a regular file' in webui_security.Config().startup_error()


@pytest.mark.skipif(os.geteuid() == 0, reason='root reads any file')
def test_unreadable_file_refuses(clean_env, secret):
    clean_env.setenv('WEBUI_API_TOKEN_FILE', secret('tok', SECRET_TOKEN, 0o000))
    assert 'not readable by the web UI user' in webui_security.Config().startup_error()


def test_unusable_file_never_falls_back_to_no_auth(clean_env, secret, tmp_path):
    # A working token plus a broken password file: refuse, do not start with
    # the token alone (the user asked for a password).
    clean_env.setenv('WEBUI_USERNAME', 'admin')
    clean_env.setenv('WEBUI_PASSWORD_FILE', str(tmp_path / 'missing'))
    clean_env.setenv('WEBUI_API_TOKEN', 'y')
    assert webui_security.Config().startup_error()


def test_no_credentials_message_names_file_variants(clean_env):
    error = webui_security.Config().startup_error()
    assert 'WEBUI_PASSWORD_FILE' in error and 'WEBUI_API_TOKEN_FILE' in error


def _check_cli(env):
    return subprocess.run(
        [sys.executable, '-I', os.path.join(APP_DIR, 'webui_security.py'),
         'check-credentials'], env=env, capture_output=True, text=True)


def test_check_cli_prints_sources_never_values(secret):
    env = {'PATH': os.environ['PATH'], 'WEBUI_USERNAME': 'admin',
           'WEBUI_PASSWORD_FILE': secret('pw', SECRET_PW + '\n'),
           'WEBUI_API_TOKEN_FILE': secret('tok', SECRET_TOKEN)}
    r = _check_cli(env)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        'WEBUI_PASSWORD set from WEBUI_PASSWORD_FILE',
        'WEBUI_API_TOKEN set from WEBUI_API_TOKEN_FILE']
    assert SECRET_PW not in r.stdout + r.stderr
    assert SECRET_TOKEN not in r.stdout + r.stderr


def test_check_cli_fails_closed_without_values(secret):
    env = {'PATH': os.environ['PATH'], 'WEBUI_API_TOKEN': SECRET_TOKEN,
           'WEBUI_API_TOKEN_FILE': secret('tok', SECRET_TOKEN)}
    r = _check_cli(env)
    assert r.returncode == 1
    assert r.stdout.startswith('ERROR: WEBUI_API_TOKEN and WEBUI_API_TOKEN_FILE')
    assert SECRET_TOKEN not in r.stdout + r.stderr


def test_app_from_files_only(make_client, webui_env, secret, capsys):
    mp = webui_env['monkeypatch']
    for name in ('WEBUI_PASSWORD', 'WEBUI_API_TOKEN'):
        mp.delenv(name, raising=False)
    client, _ = make_client(
        WEBUI_USERNAME='admin',
        WEBUI_PASSWORD_FILE=secret('pw', SECRET_PW + '\n'),
        WEBUI_API_TOKEN_FILE=secret('tok', SECRET_TOKEN + '\n'))
    err = capsys.readouterr().err
    assert '[web-ui] WEBUI_PASSWORD set from WEBUI_PASSWORD_FILE' in err
    assert SECRET_PW not in err and SECRET_TOKEN not in err

    assert client.get('/').status_code == 401
    assert client.get('/', headers=_basic('admin', SECRET_PW)).status_code == 200
    assert client.get('/', headers=_basic('admin', SECRET_PW + '\n')).status_code == 401
    r = client.post('/api/download',
                    headers={'Authorization': f'Bearer {SECRET_TOKEN}'},
                    data={'url': 'https://www.youtube.com/watch?v=x'})
    assert r.status_code == 200 and 'id' in r.json()


def test_app_refuses_to_start_with_bad_file(make_client, webui_env, tmp_path):
    with pytest.raises(SystemExit):
        make_client(WEBUI_API_TOKEN_FILE=str(tmp_path / 'missing'))
