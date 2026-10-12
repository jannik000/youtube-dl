"""The channel manager: channels.txt as a list (channels_file.py and the
/edit/channels routes)."""

import json
import os
import time

import pytest

from conftest import basic, bearer

import channels_file as cf

SAME_ORIGIN = {'Sec-Fetch-Site': 'same-origin'}

FILE = ('\ufeff# my channels\n'
        '\n'
        'https://www.youtube.com/@alpha\n'
        '  https://www.youtube.com/@beta #old comment\n'
        '; batch-file comment\n'
        ']another one\n'
        'https://www.youtube.com/@gamma | --playlist-end 5\n')


# -- channels_file -------------------------------------------------------------

def test_entries_are_read_like_the_script_reads_them():
    assert cf.entries(FILE) == [
        {'line': 'https://www.youtube.com/@alpha',
         'url': 'https://www.youtube.com/@alpha', 'options': ''},
        {'line': 'https://www.youtube.com/@beta #old comment',
         'url': 'https://www.youtube.com/@beta', 'options': ''},
        {'line': 'https://www.youtube.com/@gamma | --playlist-end 5',
         'url': 'https://www.youtube.com/@gamma', 'options': '--playlist-end 5'},
    ]
    assert cf.comment_lines(FILE) == 3


@pytest.mark.parametrize('typed,stored', [
    ('@delta', 'https://www.youtube.com/@delta'),
    ('  https://www.youtube.com/@delta/videos\n', 'https://www.youtube.com/@delta/videos'),
    ('https://www.youtube.com/playlist?list=PLx', 'https://www.youtube.com/playlist?list=PLx'),
])
def test_normalise_url(typed, stored):
    assert cf.normalise_url(typed) == stored


@pytest.mark.parametrize('bad', [
    '', '   ', 'ftp://x/y', 'javascript:alert(1)', 'www.youtube.com/@x',
    'https://x/a b', 'https://x/a|--exec id', 'https://x/#frag',
    'https://x/\u202ey', 'https://x/' + 'a' * 2048,
])
def test_normalise_url_refuses(bad):
    with pytest.raises(cf.ChannelError):
        cf.normalise_url(bad)


def test_add_appends_one_line_and_keeps_the_rest():
    text = cf.add(FILE, 'https://www.youtube.com/@delta', ' --max-downloads 3 ')
    assert text == FILE + 'https://www.youtube.com/@delta | --max-downloads 3\n'
    # A file without a final newline gets one first; CRLF files stay CRLF.
    assert cf.add('a\r\nhttps://x/1', 'https://x/2') == 'a\r\nhttps://x/1\r\nhttps://x/2\r\n'
    assert cf.add('', 'https://x/2') == 'https://x/2\n'


@pytest.mark.parametrize('dup', [
    'https://www.youtube.com/@alpha', 'https://youtube.com/@alpha/',
    'HTTPS://m.YouTube.com/@alpha', 'https://www.youtube.com/@gamma',
    'https://www.youtube.com/@beta'])
def test_add_refuses_duplicates(dup):
    with pytest.raises(cf.ChannelError, match='already'):
        cf.add(FILE, dup)


def test_add_refuses_multiline_options():
    with pytest.raises(cf.ChannelError):
        cf.add(FILE, 'https://x/1', '--playlist-end 1\nhttps://evil/')


def test_remove_and_restore():
    text, pos = cf.remove(FILE, 'https://www.youtube.com/@beta #old comment')
    assert 'beta' not in text and pos == 3
    assert text.count('\n') == FILE.count('\n') - 1
    assert cf.restore(text, 'https://www.youtube.com/@beta #old comment', pos) \
        == FILE.replace('  https://www.youtube.com/@beta',
                        'https://www.youtube.com/@beta')
    # Comment lines and unknown lines are never removed.
    for line in ('# my channels', '; batch-file comment', 'https://x/none'):
        with pytest.raises(cf.ChannelError):
            cf.remove(FILE, line)
    # Only the first of two equal lines goes.
    twice = 'https://x/1\nhttps://x/1\n'
    assert cf.remove(twice, 'https://x/1') == ('https://x/1\n', 0)


# -- routes --------------------------------------------------------------------

def _file(client):
    return os.path.join(client.app_module.PATHS.config_dir, 'channels.txt')


def _write(client, text):
    with open(_file(client), 'w') as f:
        f.write(text)


def _read(client):
    with open(_file(client)) as f:
        return f.read()


def _post(client, route, **data):
    return client.post(route, auth=basic(), headers=SAME_ORIGIN,
                       data={**data, 'csrf_token': client.csrf_token},
                       follow_redirects=False)


def test_page_lists_channels_with_names_and_status(make_client):
    client, _ = make_client()
    _write(client, FILE + 'https://x/<script>\n')
    status_dir = client.app_module.PATHS.status_dir
    os.makedirs(status_dir, mode=0o700, exist_ok=True)
    now = int(time.time())
    run = 'a' * 16
    events = [
        {'v': 1, 't': 'run_start', 'ts': now - 100, 'run': run, 'nonce': 'b' * 16,
         'pid': 1, 'channels': 1, 'interval': '3h', 'ytdlp': '2026.08.19'},
        {'v': 1, 't': 'start', 'src': 'scheduled', 'run': run, 'ch': 1,
         'i': {'id': 'x', 'channel': 'Alpha Channel'}},
        {'v': 1, 't': 'run_end', 'ts': now - 50, 'run': run, 'rc': 0,
         'next': now + 3600, 'partial': False,
         'sum': {'new': 1, 'skipped': 0, 'failed': 0, 'filtered': 0, 'members': 0},
         'ch': [[1, 1, 0, 0, 0, 0, 3, now - 60, 'https://www.youtube.com/@alpha', 0]],
         'err': []}]
    with open(os.path.join(status_dir, 'events.jsonl'), 'w') as f:
        f.write(''.join(json.dumps(e) + '\n' for e in events))
    r = client.get('/edit/channels', auth=basic())
    assert r.status_code == 200
    html = r.text
    assert 'Alpha Channel' in html and 'new video today' in html
    assert '@beta' in html and 'Not checked yet' in html
    assert '| --playlist-end 5' in html
    assert '3 comment lines in channels.txt are kept' in html
    assert '&lt;script&gt;' in html and '<script>' not in html.split('</header>')[-1]
    assert html.count('action="/edit/channels/remove"') == 4
    assert f'name="csrf_token" value="{client.csrf_token}"' in html
    assert 'href="/edit/channels/text"' in html


def test_add_a_channel(make_client):
    client, _ = make_client()
    _write(client, FILE)
    r = _post(client, '/edit/channels/add', url='@delta', options='--playlist-end 3')
    assert r.status_code == 303 and r.headers['location'] == '/edit/channels?added=1'
    assert _read(client) == FILE + 'https://www.youtube.com/@delta | --playlist-end 3\n'
    assert 'Added.' in client.get('/edit/channels?added=1', auth=basic()).text


@pytest.mark.parametrize('url,options,message', [
    ('https://www.youtube.com/@alpha', '', 'already in the list'),
    ('ftp://x/y', '', 'Only http and https'),
    ('https://www.youtube.com/@x', "--exec 'touch /config/PWNED'", 'Options refused'),
    ('https://www.youtube.com/@x', '--playlist-end "1', 'Options refused'),
])
def test_refused_add_keeps_the_file_and_the_input(make_client, url, options, message):
    client, _ = make_client()
    _write(client, FILE)
    r = _post(client, '/edit/channels/add', url=url, options=options)
    assert r.status_code == 400 and message in r.text
    assert 'Not saved' in r.text
    assert _read(client) == FILE


def test_add_respects_allowed_domains(make_client):
    client, _ = make_client(WEBUI_ALLOWED_DOMAINS='youtube.com')
    _write(client, FILE)
    r = _post(client, '/edit/channels/add', url='https://vimeo.com/user1')
    assert r.status_code == 400 and 'WEBUI_ALLOWED_DOMAINS' in r.text
    assert _post(client, '/edit/channels/add', url='@ok').status_code == 303


def test_unsafe_args_flag_allows_any_options(make_client):
    client, _ = make_client(WEBUI_ALLOW_UNSAFE_ARGS='true')
    _write(client, '')
    r = _post(client, '/edit/channels/add', url='https://x/1', options="--exec 'id'")
    assert r.status_code == 303 and _read(client) == "https://x/1 | --exec 'id'\n"


def test_remove_and_undo(make_client):
    client, _ = make_client()
    _write(client, FILE)
    r = _post(client, '/edit/channels/remove',
              line='https://www.youtube.com/@beta #old comment')
    assert r.status_code == 303 and r.headers['location'] == '/edit/channels?removed=1'
    assert '@beta' not in _read(client)
    page = client.get('/edit/channels?removed=1', auth=basic()).text
    assert 'Removed' in page and 'action="/edit/channels/undo"' in page
    # Without the query the notice is gone.
    assert 'Undo' not in client.get('/edit/channels', auth=basic()).text
    assert _post(client, '/edit/channels/undo').status_code == 303
    assert _read(client) == FILE.replace('  https://www.youtube.com/@beta',
                                         'https://www.youtube.com/@beta')
    # A second undo changes nothing.
    assert _post(client, '/edit/channels/undo').status_code == 303
    assert _read(client).count('@beta') == 1


def test_undo_is_dropped_by_the_next_change(make_client):
    client, _ = make_client()
    _write(client, FILE)
    _post(client, '/edit/channels/remove', line='https://www.youtube.com/@alpha')
    _post(client, '/edit/channels/add', url='@delta')
    _post(client, '/edit/channels/undo')
    assert '@alpha' not in _read(client)


def test_remove_of_a_line_that_is_gone(make_client):
    client, _ = make_client()
    _write(client, FILE)
    r = _post(client, '/edit/channels/remove', line='# my channels')
    assert r.status_code == 409 and 'no longer in channels.txt' in r.text
    assert _read(client) == FILE


def test_readonly(make_client):
    client, _ = make_client(WEBUI_READONLY='true')
    _write(client, FILE)
    page = client.get('/edit/channels', auth=basic()).text
    assert 'read-only' in page and '/edit/channels/remove' not in page
    assert '/edit/channels/add' not in page
    for route, data in [('/edit/channels/add', {'url': '@x'}),
                        ('/edit/channels/remove',
                         {'line': 'https://www.youtube.com/@alpha'}),
                        ('/edit/channels/undo', {})]:
        assert _post(client, route, **data).status_code == 403
    assert _read(client) == FILE


@pytest.mark.parametrize('route', ['/edit/channels/add', '/edit/channels/remove',
                                   '/edit/channels/undo'])
def test_api_token_cannot_change_channels(make_client, route):
    client, _ = make_client()
    _write(client, FILE)
    r = client.post(route, headers=bearer(), data={'url': '@x', 'line': 'x'})
    assert r.status_code == 401
    assert _read(client) == FILE


def test_text_editor_is_still_there(make_client):
    client, _ = make_client()
    _write(client, FILE)
    page = client.get('/edit/channels/text', auth=basic()).text
    assert 'name="channels_new"' in page and 'href="/edit/channels"' in page
