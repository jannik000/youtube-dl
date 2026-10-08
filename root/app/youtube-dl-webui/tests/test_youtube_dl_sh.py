"""Runs youtube-dl.sh with a stub yt-dlp that records its argv.

The script's absolute paths (/config, /app, /var/log/youtube-dl, ...) are
rewritten into a temporary tree, and it runs under 'bash -c "... | tee log"'
like supervisor runs it. Stubs on PATH replace yt-dlp, sleep and
supervisorctl; python3 is the interpreter running the tests.
"""

import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.abspath(os.path.join(APP_DIR, '..', '..', '..'))
SCRIPT = os.path.join(REPO_ROOT, 'root', 'app', 'youtube-dl', 'youtube-dl.sh')
sys.path.insert(0, APP_DIR)

import status_events as se  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which('bash') is None, reason='needs bash')

FORMAT = 'bv*+ba/b'

STUB_YTDLP = r'''#!/bin/bash
if [ "$1" = --version ]; then echo 2026.08.19; exit 0; fi
n=$(find "$STUB_CALLS" -type f | wc -l)
printf '%s\0' "$@" > "$STUB_CALLS/$(printf '%03d' "$n")"
url="${*: -1}"
echo "[stub] downloading $url"
case "$url" in
  *fail*) echo "ERROR: [generic] fail: Unable to download webpage"; exit 1 ;;
  *break*) exit 101 ;;
esac
exit 0
'''

# Wraps the real helper; the commands in 'fail' write 'output' (into the
# --flags-out file if there is one, else to stdout) and exit with 'code'.
FAKE_HELPER = r'''import sys
sys.path.insert(0, {app_dir!r})
import status_events
if sys.argv[1] in {fail!r}:
    out = [a.split('=', 1)[1] for a in sys.argv if a.startswith('--flags-out=')]
    if out:
        with open(out[0], 'w') as f:
            f.write({output!r})
    else:
        sys.stdout.write({output!r})
    sys.exit({code})
sys.exit(status_events.main(sys.argv[1:]))
'''


def _write(path, text, mode=None):
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
    if mode is not None:
        os.chmod(path, mode)


class Tree:
    def __init__(self, tmp_path, channels, helper=None):
        self.root = str(tmp_path)
        for sub in ('config', 'config.default', 'downloads', 'log', 'bin',
                    'calls', 'app', 'tmp'):
            os.makedirs(os.path.join(self.root, sub))
        self.config = os.path.join(self.root, 'config')
        self.status_dir = os.path.join(self.config, '.youtube-dl-status')
        self.log = os.path.join(self.root, 'log', 'youtube-dl.log')
        _write(os.path.join(self.config, 'args.conf'),
               "--output '/downloads/%(title)s.%(ext)s'\n")
        _write(os.path.join(self.config, 'channels.txt'), channels)
        _write(os.path.join(self.root, 'config.default', 'format'), FORMAT + '\n')

        app = os.path.join(self.root, 'app')
        shutil.copy(os.path.join(APP_DIR, 'ytdlp_args.py'), app)
        if helper is None:
            shutil.copy(os.path.join(APP_DIR, 'status_events.py'), app)
        else:
            _write(os.path.join(app, 'status_events.py'), FAKE_HELPER.format(
                app_dir=APP_DIR, **helper))

        script = open(SCRIPT, encoding='utf-8').read()
        for old, new in (('/config.default/', self.root + '/config.default/'),
                         ('/config/', self.config + '/'),
                         ('/app/youtube-dl-webui/', app + '/'),
                         ('/var/log/youtube-dl/', self.root + '/log/'),
                         ('/downloads/', self.root + '/downloads/'),
                         ('/tmp/updater-running', self.root + '/tmp/updater-running')):
            script = script.replace(old, new)
        self.script = os.path.join(self.root, 'youtube-dl.sh')
        _write(self.script, script, 0o755)

        bin_dir = os.path.join(self.root, 'bin')
        _write(os.path.join(bin_dir, 'yt-dlp'), STUB_YTDLP, 0o755)
        _write(os.path.join(bin_dir, 'python3'),
               f'#!/bin/sh\nexec {sys.executable} "$@"\n', 0o755)
        _write(os.path.join(bin_dir, 'sleep'), '#!/bin/sh\nexit 0\n', 0o755)
        _write(os.path.join(bin_dir, 'supervisorctl'),
               f'#!/bin/sh\necho "$*" >> {self.root}/supervisorctl.calls\n', 0o755)

    def run(self, **env):
        environment = {
            'PATH': f"{self.root}/bin:/usr/bin:/bin", 'HOME': self.root,
            'TMPDIR': os.path.join(self.root, 'tmp'),
            'STUB_CALLS': os.path.join(self.root, 'calls'),
            'youtubedl_webui': 'false', 'youtubedl_interval': '3h',
            'youtubedl_debug': 'false', 'youtubedl_lockfile': 'false',
            'youtubedl_subscriptions': 'false', 'youtubedl_watchlater': 'false',
        }
        environment.update(env)
        subprocess.run(['/bin/bash', '-c', f'/bin/bash {self.script} 2>&1 | tee {self.log}'],
                       env=environment, cwd=self.config, stdout=subprocess.DEVNULL,
                       timeout=120, check=True)
        with open(self.log, encoding='utf-8') as f:
            self.output = f.read()
        calls_dir = os.path.join(self.root, 'calls')
        self.calls = []
        for name in sorted(os.listdir(calls_dir)):
            with open(os.path.join(calls_dir, name), 'rb') as f:
                self.calls.append(f.read().decode().split('\0')[:-1])
        return self

    def events(self):
        return [json.loads(line) for line in open(se.events_path(self.status_dir))]

    def base(self):
        return ['--config-location', self.config + '/args.conf', '--format', FORMAT,
                '--download-archive', self.config + '/archive.txt']


CHANNELS = (
    '\ufeff# channels (with a byte order mark)\n'
    'https://a.example/plain1\n'
    '   https://a.example/plain2   # trailing comment\n'
    ';batch-file comment\n'
    ']another batch-file comment\n'
    'https://a.example/crlf\r\n'
    "https://a.example/args | --playlist-end '-1' --match-filter \"title~='x y'\"\n"
    "https://a.example/bad | --match-filter 'unclosed\n"
    '  # indented comment\n'
)


def test_one_call_per_entry_without_web_ui(tmp_path):
    """Without the web UI the argv is what it was before, except that plain
    URLs no longer share one --batch-file call: each entry runs on its own,
    with '--' before the URL."""
    tree = Tree(tmp_path, CHANNELS).run(youtubedl_subscriptions='true',
                                        youtubedl_watchlater='true')
    base = tree.base()
    assert tree.calls == [
        base + ['--', 'https://a.example/plain1'],
        base + ['--', 'https://a.example/plain2'],
        base + ['--', 'https://a.example/crlf'],
        base + ['--playlist-end', '-1', '--match-filter', "title~='x y'",
                '--', 'https://a.example/args'],
        base + ['--', 'https://www.youtube.com/feed/channels'],
        base + ['--playlist-end', '-1', '--no-playlist-reverse', '--', ':ytwatchlater'],
    ]
    assert ("[channels] skipping 'https://a.example/bad': cannot parse the "
            "arguments after '|'") in tree.output
    assert '@@ytdl' not in tree.output
    assert not os.path.exists(tree.status_dir)
    assert 'waiting 3h..' in tree.output


def test_event_flags_with_web_ui(tmp_path):
    channels = ('https://a.example/one\n'
                "https://a.example/fail | --playlist-end '2'\n"
                'https://a.example/break\n'
                "https://a.example/bad | --match-filter 'unclosed\n")
    tree = Tree(tmp_path, channels).run(youtubedl_webui='true',
                                        youtubedl_interval='1d 3h')
    events = tree.events()
    assert [e['t'] for e in events] == ['run_start', 'run_end']
    run_start, run_end = events
    run, nonce = run_start['run'], run_start['nonce']
    assert run_start['channels'] == 4 and run_start['interval'] == '1d 3h'
    assert run_start['ytdlp'] == '2026.08.19'

    def flags(ch):
        return se.ytdlp_flags('scheduled', run, nonce, se.events_path(tree.status_dir), ch=ch)

    base = tree.base()
    # Order: base, the entry's own args, event flags, '--', URL.
    assert tree.calls == [
        base + flags(1) + ['--', 'https://a.example/one'],
        base + ['--playlist-end', '2'] + flags(2) + ['--', 'https://a.example/fail'],
        base + flags(3) + ['--', 'https://a.example/break'],
    ]
    # Every entry is announced, the unparsable one too.
    markers = [json.loads(line.split(' channel ', 1)[1])
               for line in tree.output.splitlines()
               if line.startswith(f'@@ytdl:{nonce} channel ')]
    assert [(m['phase'], m['index']) for m in markers] == [
        ('start', 1), ('end', 1), ('start', 2), ('end', 2),
        ('start', 3), ('end', 3), ('start', 4), ('end', 4)]
    assert [m['rc'] for m in markers if m['phase'] == 'end'] == [0, 1, 101, 2]
    assert markers[0]['total'] == 4 and markers[0]['url'] == 'https://a.example/one'
    assert f'@@ytdl:{nonce} pass_end' in tree.output.splitlines()
    # run_end: the worst exit code (101 = stopped early on purpose is none),
    # the next pass after the interval, one row per entry.
    assert run_end['run'] == run and run_end['rc'] == 2
    assert run_end['partial'] is False
    assert abs(run_end['next'] - run_end['ts'] - 97200) <= 1
    assert [(row[0], row[5], row[8]) for row in run_end['ch']] == [
        (1, 0, 'https://a.example/one'), (2, 1, 'https://a.example/fail'),
        (3, 101, 'https://a.example/break'), (4, 2, 'https://a.example/bad')]
    assert [(e['idx'], e['id'], e['msg']) for e in run_end['err']] == [
        (2, 'fail', 'Unable to download webpage'),
        (4, None, "cannot parse the arguments after '|'")]
    assert stat.S_IMODE(os.stat(tree.status_dir).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(se.events_path(tree.status_dir)).st_mode) == 0o600
    # The pass is otherwise unchanged.
    assert 'execution took' in tree.output and 'waiting 1d 3h..' in tree.output


def test_worst_exit_code_ignores_101(tmp_path):
    tree = Tree(tmp_path, 'https://a.example/break\nhttps://a.example/ok\n').run(
        youtubedl_webui='true')
    assert tree.events()[-1]['rc'] == 0


@pytest.mark.parametrize('helper', [
    {'fail': ('begin',), 'output': 'garbage\0', 'code': 1},
    # Exit 0 but no valid ids: the script must not use them.
    {'fail': ('begin',), 'output': 'not-hex\0also-not\0', 'code': 0},
    {'fail': ('begin',), 'output': '0123456789abcdef\0bad nonce\0', 'code': 0},
    {'fail': ('begin',), 'output': '', 'code': 0},
])
def test_failing_begin_runs_the_pass_without_events(tmp_path, helper):
    tree = Tree(tmp_path, "https://a.example/one\nhttps://a.example/two | -i\n",
                helper=helper).run(youtubedl_webui='true')
    base = tree.base()
    assert tree.calls == [base + ['--', 'https://a.example/one'],
                          base + ['-i', '--', 'https://a.example/two']]
    assert '@@ytdl' not in tree.output
    assert 'waiting 3h..' in tree.output


def test_failing_channel_start_runs_that_entry_without_flags(tmp_path):
    tree = Tree(tmp_path, "https://a.example/one\n",
                helper={'fail': ('channel-start',), 'output': '--exec\0id\0', 'code': 1}
                ).run(youtubedl_webui='true')
    assert tree.calls == [tree.base() + ['--', 'https://a.example/one']]
    events = tree.events()
    assert [e['t'] for e in events] == ['run_start', 'run_end']
    assert events[-1]['ch'][0][5] == 0      # channel-end still marked it


def test_one_shot_pass_writes_run_end_before_terminating(tmp_path):
    tree = Tree(tmp_path, 'https://a.example/one\n').run(
        youtubedl_webui='true', youtubedl_interval='false')
    run_end = tree.events()[-1]
    assert run_end['t'] == 'run_end' and run_end['next'] is None
    with open(os.path.join(tree.root, 'supervisorctl.calls')) as f:
        assert f.read() == 'start terminate\n'
    lines = tree.output.splitlines()
    nonce = tree.events()[0]['nonce']
    assert lines.index(f'@@ytdl:{nonce} pass_end') < lines.index(
        "youtubedl_interval is set to 'false', container will now exit.")
