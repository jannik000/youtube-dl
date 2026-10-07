"""Guard against reintroducing shell execution in the web UI or scripts."""

import os
import re

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))
WEBUI_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _python_sources():
    for base, _dirs, files in os.walk(WEBUI_DIR):
        if os.path.basename(base) == 'tests':
            continue
        for name in files:
            if name.endswith('.py'):
                yield os.path.join(base, name)


def test_no_shell_subprocess_in_python():
    offenders = []
    for path in _python_sources():
        text = open(path).read()
        if 'create_subprocess_shell' in text or re.search(r'shell\s*=\s*True', text):
            offenders.append(path)
        if re.search(r'os\.system\s*\(', text):
            offenders.append(path)
    assert not offenders, f'shell execution found in: {offenders}'


def test_runtime_scripts_are_executable():
    # entrypoint.sh executes these directly and aborts the container if one
    # fails, so a missing +x bit (e.g. on a newly added init script) means
    # the container does not start at all.
    root = os.path.join(REPO_ROOT, 'root')
    scripts = [os.path.join(root, 'entrypoint.sh'),
               os.path.join(root, 'etc', 'supervisor', 'terminate.sh')]
    init_dir = os.path.join(root, 'etc', 'cont-init.d')
    scripts += [os.path.join(init_dir, n) for n in os.listdir(init_dir)]
    app_dir = os.path.join(root, 'app', 'youtube-dl')
    scripts += [os.path.join(app_dir, n) for n in os.listdir(app_dir)
                if n.endswith('.sh')]
    not_executable = [s for s in scripts if not os.access(s, os.X_OK)]
    assert not not_executable, f'missing +x: {not_executable}'


def test_shell_script_has_no_eval():
    script = os.path.join(REPO_ROOT, 'root', 'app', 'youtube-dl', 'youtube-dl.sh')
    if not os.path.exists(script):
        return
    for lineno, line in enumerate(open(script), start=1):
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        assert not re.match(r'eval\b', stripped), f'eval at {script}:{lineno}'
