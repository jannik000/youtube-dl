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
    assert set(r.json()) == {'id', 'state', 'position', 'message'}


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
    'https://[x',                           # urlsplit raises ValueError ...
    'https://[::1',
    'https://]x/',
    'http://example.com＃@evil.com/',   # ... NFKC netloc check
    'http://a℀b.com/',
])
def test_malicious_urls_rejected(make_client, bad_url):
    client, _ = make_client()
    r = client.post('/api/download', headers=bearer(), data={'url': bad_url})
    assert r.status_code == 400, bad_url


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
    # a valid UUID followed by more: only a full match may pass
    '12345678-1234-5678-1234-567812345678x',
    '12345678-1234-5678-1234-567812345678%22%3E%3Cx%3E',
    '12345678-1234-5678-1234-567812345678%0A',
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
JOB_ID = '12345678-1234-4678-9234-567812345678'
POST_ROUTES = [(route, {field: [value]}) for route, field, value in SAVE_ROUTES] + [
    ('/download', {'url': 'https://youtube.com/watch?v=x'}),
    ('/api/download', {'url': 'https://youtube.com/watch?v=x'}),
    ('/restart-youtube-dl', {'confirm': '1'}),
    (f'/api/download/{JOB_ID}/cancel', {}),
    (f'/api/download/{JOB_ID}/retry', {}),
    ('/api/download-now', {'video_id': 'dQw4w9WgXcQ'}),
    ('/edit/channels/add', {'url': 'https://www.youtube.com/@x'}),
    ('/edit/channels/remove', {'line': 'https://www.youtube.com/@x'}),
    ('/edit/channels/undo', {}),
]


def test_every_post_route_is_in_the_csrf_test(make_client):
    client, _ = make_client()
    posts = {route.path for route in client.app.routes
             if 'POST' in getattr(route, 'methods', ())}
    tested = {route.replace(JOB_ID, '{download_id}') for route, _ in POST_ROUTES}
    assert posts == tested


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
    '/edit/channels/text',
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


@pytest.mark.parametrize('path,media', [
    ('/static/app.js', 'text/javascript'), ('/static/app.css', 'text/css'),
    ('/favicon.ico', 'image/png')])
def test_public_assets_need_no_auth(make_client, path, media):
    client, _ = make_client()
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers['content-type'].split(';')[0] == media


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
        assert client.get(f'{prefix}/static/app.css').status_code == 200
        assert client.get(f'{prefix}/', auth=basic()).status_code == 200
        assert client.get(f'{prefix}/edit/args').status_code == 401
    html = client.get('/yt/', auth=basic()).text
    assert 'src="/yt/static/app.js"' in html
    assert 'href="/yt/static/app.css"' in html
    assert 'data-base-path="/yt"' in html


def test_token_on_download_returns_json(make_client):
    client, _ = make_client()
    r = client.post('/download', headers=bearer(),
                    data={'url': 'https://youtube.com/watch?v=x'},
                    follow_redirects=False)
    assert r.status_code == 200
    assert set(r.json()) == {'id', 'state', 'position', 'message'}


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
    line = 'y' * 99 + '\n'
    with open(path, 'w') as f:
        f.write('x' * 1000 + '\n')
        f.write(line * (module.LOG_TAIL_BYTES // 100))
        f.write('END\npartial')
    r = client.get(f'/log/download/{download_id}', auth=basic())
    assert r.status_code == 200
    # Complete lines only: the line cut by the tail limit and the unfinished
    # last line are left out.
    assert len(r.text) <= module.LOG_TAIL_BYTES and r.text.endswith('y\nEND\n')
    assert set(r.text.splitlines()[:-1]) == {line.strip()}


def test_security_headers_present(make_client):
    client, _ = make_client()
    r = client.get('/', auth=basic())
    assert r.headers['X-Frame-Options'] == 'DENY'
    assert r.headers['X-Content-Type-Options'] == 'nosniff'
    csp = r.headers['Content-Security-Policy']
    directives = {}
    for part in csp.split(';'):
        name, *values = part.split()
        directives[name] = values
    # Nothing external and nothing inline: the stylesheet and the script are
    # served by the app, the pages use system fonts.
    assert directives == {
        'default-src': ["'none'"], 'script-src': ["'self'"],
        'style-src': ["'self'"], 'img-src': ["'self'"],
        'connect-src': ["'self'"], 'form-action': ["'self'"],
        'frame-ancestors': ["'none'"], 'base-uri': ["'none'"],
        'object-src': ["'none'"]}
    assert 'https:' not in csp and 'http:' not in csp
    assert 'unsafe' not in csp and 'data:' not in csp


def test_unexpected_error_is_500_with_security_headers(make_client):
    client, _ = make_client()

    def boom():
        raise RuntimeError('boom')
    client.app.add_api_route('/boom', boom)
    r = client.get('/boom', auth=basic())
    assert r.status_code == 500 and r.text == 'Internal Server Error'
    assert r.headers['X-Frame-Options'] == 'DENY'


@pytest.mark.parametrize('webuipath', ['/', '/yt/'])
def test_trailing_slash_in_base_path(make_client, webuipath):
    client, _ = make_client(youtubedl_webuipath=webuipath)
    prefix = webuipath.rstrip('/')
    html = client.get(f'{prefix}/', auth=basic()).text
    assert f'src="{prefix}/static/app.js"' in html
    assert 'href="//' not in html and 'action="//' not in html


def test_security_headers_on_401(make_client):
    client, _ = make_client()
    r = client.get('/')
    assert r.status_code == 401 and r.headers['X-Frame-Options'] == 'DENY'


def test_missing_download_log_returns_empty(make_client):
    client, _ = make_client()
    import uuid
    r = client.get(f'/log/download/{uuid.uuid4()}', auth=basic())
    assert r.status_code == 200 and r.text == ''


# --- pages for browsers without JavaScript ------------------------------------

HTML = 'text/html,application/xhtml+xml,*/*;q=0.8'


def test_refused_args_show_the_editor_again(make_client):
    client, _ = make_client()
    before = open(os.path.join(client.app_module.PATHS.config_dir,
                               'args.conf')).read()
    r = client.post('/edit/args/save', auth=basic(),
                    headers={**SAME_ORIGIN, 'Accept': HTML},
                    data={'args_new': ["--exec 'id' <b>x</b>\n"],
                          'csrf_token': client.csrf_token})
    assert r.status_code == 400
    assert r.headers['content-type'].startswith('text/html')
    assert 'Not saved' in r.text and '--exec' in r.text
    # The refused text is kept in the editor, escaped.
    assert '&lt;b&gt;x&lt;/b&gt;' in r.text and '<b>x</b>' not in r.text
    assert open(os.path.join(client.app_module.PATHS.config_dir,
                             'args.conf')).read() == before


@pytest.mark.parametrize('page,field,name', [
    ('/edit/args', 'args_new', 'args.conf'),
    ('/edit/channels/text', 'channels_new', 'channels.txt'),
    ('/edit/archive', 'archive_new', 'archive.txt')])
def test_edit_pages_show_the_file(make_client, page, field, name):
    client, _ = make_client()
    path = os.path.join(client.app_module.PATHS.config_dir, name)
    with open(path, 'w') as f:
        f.write('\nfirst line after a blank one <script>\n')
    r = client.get(page, auth=basic())
    assert r.status_code == 200
    assert f'name="{field}"' in r.text
    assert f'name="csrf_token" value="{client.csrf_token}"' in r.text
    # One newline after <textarea> is dropped by the HTML parser, so a file
    # that starts with a blank line keeps it.
    assert '>\n\nfirst line after a blank one &lt;script&gt;' in r.text


def test_browser_errors_are_pages_and_api_errors_json(make_client):
    client, _ = make_client()
    page = client.get('/no-such-page', auth=basic(), headers={'Accept': HTML})
    assert page.status_code == 404
    assert page.headers['content-type'].startswith('text/html')
    assert 'Not found' in page.text
    assert client.get('/no-such-page', auth=basic()).json() == {
        'detail': 'Not Found'}
    api = client.get('/api/log/download/x', auth=basic(),
                     headers={'Accept': HTML})
    assert api.status_code == 404 and api.json() == {'detail': 'not found'}
    bad = client.post('/download', auth=basic(),
                      headers={**SAME_ORIGIN, 'Accept': HTML},
                      data={'url': 'file:///etc/passwd',
                            'csrf_token': client.csrf_token})
    assert bad.status_code == 400 and 'only http(s) URLs' in bad.text
    # Authentication still answers with the Basic challenge, not a page.
    r = client.get('/', headers={'Accept': HTML})
    assert r.status_code == 401 and 'www-authenticate' in r.headers


def test_page_carries_settings_in_data_attributes(make_client):
    client, _ = make_client(WEBUI_MAX_PARALLEL_DOWNLOADS='2',
                            WEBUI_READONLY='true')
    html = client.get('/', auth=basic()).text
    assert f'data-csrf-token="{client.csrf_token}"' in html
    assert 'data-page="dashboard"' in html and 'data-parallel="2"' in html
    assert 'data-max-queued="20"' in html and 'data-readonly="true"' in html
    assert 'read-only' in html
    job = str(uuid.uuid4())
    html = client.get(f'/download/{job}', auth=basic()).text
    assert 'data-page="job"' in html and f'data-download-id="{job}"' in html
    assert '<details id="log" class="panel fold needs-js" open>' in html
