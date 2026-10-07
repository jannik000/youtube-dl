"""Auth, CSRF, injection and endpoint behaviour of the web UI."""

import os
import uuid

import pytest

from conftest import API_TOKEN, PASSWORD, USERNAME, basic, bearer

SAME_ORIGIN = {'Sec-Fetch-Site': 'same-origin'}


# --- fail closed ------------------------------------------------------------

def test_fail_closed_without_credentials(make_client):
    with pytest.raises(SystemExit):
        make_client(WEBUI_USERNAME='', WEBUI_PASSWORD='', WEBUI_API_TOKEN='')


def test_fail_closed_with_username_but_no_password(make_client):
    # A token is set, so only the incomplete-Basic-credentials rule can fire.
    with pytest.raises(SystemExit):
        make_client(WEBUI_USERNAME='admin', WEBUI_PASSWORD='',
                    WEBUI_API_TOKEN=API_TOKEN)


def test_fail_closed_with_password_but_no_username(make_client):
    with pytest.raises(SystemExit):
        make_client(WEBUI_USERNAME='', WEBUI_PASSWORD='secret',
                    WEBUI_API_TOKEN=API_TOKEN)


def test_token_only_is_allowed_to_start(make_client):
    # API token without basic creds is a valid configuration.
    client, _ = make_client(WEBUI_USERNAME='', WEBUI_PASSWORD='')
    assert client.get('/').status_code == 401  # but browser access needs basic


# --- authentication ---------------------------------------------------------

def test_dashboard_requires_auth(make_client):
    client, _ = make_client()
    r = client.get('/')
    assert r.status_code == 401
    assert 'www-authenticate' in {k.lower() for k in r.headers}


def test_dashboard_wrong_password(make_client):
    client, _ = make_client()
    assert client.get('/', auth=basic(USERNAME, 'wrong')).status_code == 401


def test_dashboard_wrong_username(make_client):
    client, _ = make_client()
    assert client.get('/', auth=basic('root', PASSWORD)).status_code == 401


def test_dashboard_valid_basic(make_client):
    client, _ = make_client()
    assert client.get('/', auth=basic()).status_code == 200


def test_api_token_not_accepted_on_interactive_endpoint(make_client):
    client, _ = make_client()
    # A bearer token must not unlock config editing or logs.
    assert client.get('/edit/args', headers=bearer()).status_code == 401
    assert client.get('/log/youtube-dl', headers=bearer()).status_code == 401


def test_wrong_bearer_token_rejected(make_client):
    client, _ = make_client()
    r = client.post('/api/download', headers=bearer('tok-WRONG'),
                    data={'url': 'https://youtube.com/watch?v=x'})
    assert r.status_code == 401


# --- CSRF -------------------------------------------------------------------

def test_download_form_requires_csrf_token(make_client):
    client, _ = make_client()
    r = client.post('/download', auth=basic(), headers=SAME_ORIGIN,
                    data={'url': 'https://youtube.com/watch?v=x'})
    assert r.status_code == 403


def test_download_form_with_csrf_token(make_client):
    client, captured = make_client()
    r = client.post('/download', auth=basic(), headers=SAME_ORIGIN,
                    data={'url': 'https://youtube.com/watch?v=x',
                          'csrf_token': client.csrf_token},
                    follow_redirects=False)
    assert r.status_code == 303


def test_cross_site_rejected_even_with_token(make_client):
    client, _ = make_client()
    r = client.post('/download', auth=basic(),
                    headers={'Sec-Fetch-Site': 'cross-site'},
                    data={'url': 'https://youtube.com/watch?v=x',
                          'csrf_token': client.csrf_token})
    assert r.status_code == 403


def test_same_site_subdomain_rejected(make_client):
    # A sibling subdomain (youtube.jannikseuss.de vs evil.jannikseuss.de) is
    # 'same-site' but not 'same-origin'; it must be rejected.
    client, _ = make_client()
    r = client.post('/download', auth=basic(),
                    headers={'Sec-Fetch-Site': 'same-site'},
                    data={'url': 'https://youtube.com/watch?v=x',
                          'csrf_token': client.csrf_token})
    assert r.status_code == 403


def test_api_token_is_csrf_exempt(make_client):
    # Browsers never attach Authorization: Bearer cross-site on their own.
    client, _ = make_client()
    r = client.post('/api/download', headers=bearer(),
                    data={'url': 'https://youtube.com/watch?v=x'})
    assert r.status_code == 200
    assert set(r.json()) == {'id'}


def test_restart_requires_post_and_csrf(make_client):
    client, _ = make_client()
    # The old GET endpoint must be gone.
    assert client.get('/restart-youtube-dl', auth=basic()).status_code in (404, 405)
    assert client.post('/restart-youtube-dl', auth=basic(), headers=SAME_ORIGIN,
                       data={}).status_code == 403  # missing token
    r = client.post('/restart-youtube-dl', auth=basic(), headers=SAME_ORIGIN,
                    data={'csrf_token': client.csrf_token},
                    follow_redirects=False)
    assert r.status_code == 303


# --- command / option injection via the URL ---------------------------------

@pytest.mark.parametrize('bad_url', [
    "https://x/';touch /tmp/pwned;'",      # shell metacharacters
    '--exec=id',                            # option injection
    '-o/config/pre-execution.sh',           # option injection (short)
    'file:///etc/passwd',                   # non-http scheme
    'http://user:pass@youtube.com/x',       # credentials in URL
    'http://host /x',                       # whitespace
    'ftp://youtube.com/x',                  # non-http scheme
])
def test_malicious_urls_rejected(make_client, bad_url):
    client, _ = make_client()
    r = client.post('/api/download', headers=bearer(), data={'url': bad_url})
    assert r.status_code in (400, 403), bad_url


def test_download_argv_has_no_shell_and_url_after_dashdash(make_client):
    client, captured = make_client()
    url = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
    r = client.post('/api/download', headers=bearer(), data={'url': url})
    assert r.status_code == 200
    argv = captured['argv']
    assert argv[0] == 'yt-dlp'
    assert '--' in argv and argv[-1] == url
    # The URL is a single argv element, never concatenated into a command.
    assert argv.index('--') == len(argv) - 2


def test_allowed_domains_enforced(make_client):
    client, _ = make_client(WEBUI_ALLOWED_DOMAINS='youtube.com,youtu.be')
    ok = client.post('/api/download', headers=bearer(),
                     data={'url': 'https://music.youtube.com/watch?v=x'})
    assert ok.status_code == 200
    blocked = client.post('/api/download', headers=bearer(),
                          data={'url': 'https://vimeo.com/123'})
    assert blocked.status_code == 403


# --- download id validation (XSS / traversal) -------------------------------

@pytest.mark.parametrize('bad_id', [
    '${alert(document.domain)}',
    '`-alert(1)-`',
    'not-a-uuid',
    '{12345678-1234-5678-1234-567812345678}',      # accepted by uuid.UUID()
    'urn:uuid:12345678-1234-5678-1234-567812345678',
])
def test_bad_download_ids_rejected(make_client, bad_id):
    client, _ = make_client()
    assert client.get(f'/download/{bad_id}', auth=basic()).status_code == 404
    assert client.get(f'/log/download/{bad_id}', auth=basic()).status_code == 404


@pytest.mark.parametrize('bad_id', ['../../etc/passwd', '..', 'a/b', '', None])
def test_validate_id_unit(make_client, bad_id):
    # Traversal strings never reach the route via HTTP (the client normalises
    # them), so check the validator directly.
    client, _ = make_client()
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        client.app_module._validate_id(bad_id)
    client.app_module._validate_id('12345678-1234-5678-1234-567812345678')


# --- config editing / readonly ---------------------------------------------

def test_save_args_rejects_exec(make_client):
    client, _ = make_client()
    r = client.post('/edit/args/save', auth=basic(), headers=SAME_ORIGIN,
                    data={'args_new': ["--exec 'id'\n"],
                          'csrf_token': client.csrf_token})
    assert r.status_code == 400


def test_save_args_accepts_default(make_client):
    client, _ = make_client()
    r = client.post('/edit/args/save', auth=basic(), headers=SAME_ORIGIN,
                    data={'args_new': ["--output '/downloads/%(title)s.%(ext)s'\n"],
                          'csrf_token': client.csrf_token},
                    follow_redirects=False)
    assert r.status_code == 303


def test_save_channels_rejects_exec_in_pipe_args(make_client):
    client, _ = make_client()
    r = client.post('/edit/channels/save', auth=basic(), headers=SAME_ORIGIN,
                    data={'channels_new': ['https://youtube.com/c/x | --exec id\n'],
                          'csrf_token': client.csrf_token})
    assert r.status_code == 400


SAVE_ROUTES = [
    ('/edit/args/save', 'args_new', '--geo-bypass\n'),
    ('/edit/channels/save', 'channels_new', 'https://youtube.com/c/x\n'),
    ('/edit/archive/save', 'archive_new', 'youtube abc\n'),
]
POST_ROUTES = [(route, {field: [value]}) for route, field, value in SAVE_ROUTES] + [
    ('/download', {'url': 'https://youtube.com/watch?v=x'}),
    ('/api/download', {'url': 'https://youtube.com/watch?v=x'}),
    ('/restart-youtube-dl', {}),
]


@pytest.mark.parametrize('route,data', POST_ROUTES)
def test_csrf_enforced_on_every_post_route(make_client, route, data):
    client, _ = make_client()
    missing = client.post(route, auth=basic(), headers=SAME_ORIGIN, data=data)
    assert missing.status_code == 403, 'missing token must be rejected'
    cross = client.post(route, auth=basic(),
                        headers={'Sec-Fetch-Site': 'cross-site'},
                        data={**data, 'csrf_token': client.csrf_token})
    assert cross.status_code == 403, 'cross-site must be rejected'


@pytest.mark.parametrize('route,field,value', SAVE_ROUTES)
def test_readonly_blocks_saving(make_client, route, field, value):
    client, _ = make_client(WEBUI_READONLY='true')
    path = {'args_new': 'args.conf', 'channels_new': 'channels.txt',
            'archive_new': 'archive.txt'}[field]
    target = os.path.join(client.app_module.PATHS.config_dir, path)
    before = open(target).read() if os.path.exists(target) else None
    r = client.post(route, auth=basic(), headers=SAME_ORIGIN,
                    data={field: [value], 'csrf_token': client.csrf_token})
    assert r.status_code == 403
    after = open(target).read() if os.path.exists(target) else None
    assert before == after


def test_unsafe_args_flag_allows_exec(make_client):
    client, _ = make_client(WEBUI_ALLOW_UNSAFE_ARGS='true')
    r = client.post('/edit/args/save', auth=basic(), headers=SAME_ORIGIN,
                    data={'args_new': ["--exec 'id'\n"],
                          'csrf_token': client.csrf_token},
                    follow_redirects=False)
    assert r.status_code == 303


# --- logs & headers ---------------------------------------------------------

@pytest.mark.parametrize('path', [
    '/', '/log/youtube-dl', '/edit/args', '/edit/channels', '/edit/archive',
    f'/log/download/{uuid.uuid4()}', f'/download/{uuid.uuid4()}',
    '/docs', '/redoc', '/openapi.json', '/no-such-route',
])
def test_everything_but_public_assets_requires_auth(make_client, path):
    client, _ = make_client()
    assert client.get(path).status_code == 401


@pytest.mark.parametrize('path', ['/docs', '/redoc', '/openapi.json'])
def test_api_docs_disabled(make_client, path):
    client, _ = make_client()
    assert client.get(path, auth=basic()).status_code == 404


@pytest.mark.parametrize('path', ['/static/app.js', '/favicon.ico'])
def test_public_assets_need_no_auth(make_client, path):
    client, _ = make_client()
    assert client.get(path).status_code == 200


def test_unauthenticated_body_is_not_parsed(make_client):
    # A malformed multipart body: if the body were parsed before auth, the
    # answer would be 400/422 instead of 401.
    client, _ = make_client()
    r = client.post('/edit/args/save', content=b'--x\r\nnot really multipart',
                    headers={'Content-Type': 'multipart/form-data; boundary=x'})
    assert r.status_code == 401


def test_base_path_with_and_without_prefix_stripping(make_client):
    client, _ = make_client(youtubedl_webuipath='/yt')
    for prefix in ('/yt', ''):  # '' = reverse proxy that strips /yt
        assert client.get(f'{prefix}/static/app.js').status_code == 200
        assert client.get(f'{prefix}/', auth=basic()).status_code == 200
        assert client.get(f'{prefix}/edit/args').status_code == 401
    assert 'src="/yt/static/app.js"' in client.get('/yt/', auth=basic()).text


def test_token_on_download_returns_json(make_client):
    client, _ = make_client()
    r = client.post('/download', headers=bearer(),
                    data={'url': 'https://youtube.com/watch?v=x'},
                    follow_redirects=False)
    assert r.status_code == 200 and set(r.json()) == {'id'}


def test_url_whitespace_is_stripped(make_client):
    client, captured = make_client()
    r = client.post('/api/download', headers=bearer(),
                    data={'url': '  https://www.youtube.com/watch?v=x \n'})
    assert r.status_code == 200
    assert captured['argv'][-1] == 'https://www.youtube.com/watch?v=x'


def test_download_log_is_tailed(make_client):
    client, _ = make_client()
    module = client.app_module
    download_id = str(uuid.uuid4())
    path = os.path.join(module.PATHS.download_log_dir, f'download_{download_id}.log')
    with open(path, 'w') as f:
        f.write('x' * (module.LOG_TAIL_BYTES + 1000) + 'END')
    r = client.get(f'/log/download/{download_id}', auth=basic())
    assert r.status_code == 200
    assert len(r.text) == module.LOG_TAIL_BYTES and r.text.endswith('END')


def test_security_headers_present(make_client):
    client, _ = make_client()
    r = client.get('/', auth=basic())
    assert r.headers['X-Frame-Options'] == 'DENY'
    assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
    assert r.headers['X-Content-Type-Options'] == 'nosniff'
    csp = r.headers['Content-Security-Policy']
    # Semantic UI's stylesheet imports Google Fonts and embeds data: fonts.
    assert 'https://fonts.googleapis.com' in csp and 'data:' in csp


def test_security_headers_on_401(make_client):
    client, _ = make_client()
    r = client.get('/')
    assert r.status_code == 401 and r.headers['X-Frame-Options'] == 'DENY'


def test_missing_download_log_returns_empty(make_client):
    client, _ = make_client()
    import uuid
    r = client.get(f'/log/download/{uuid.uuid4()}', auth=basic())
    assert r.status_code == 200 and r.text == ''
