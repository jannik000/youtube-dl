"""Static checks of the frontend: what the CSP and the XSS rules depend on.

The CSP is default-src 'none' with script-src/style-src 'self' (no
'unsafe-inline'), so templates must not contain inline scripts, style
attributes or <style> blocks, and app.js must never turn server text into
markup.
"""

import os
import re

import pytest

from conftest import basic

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(APP_DIR, 'static')
TEMPLATES = os.path.join(APP_DIR, 'templates')


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _templates():
    return sorted(os.path.join(TEMPLATES, n) for n in os.listdir(TEMPLATES)
                  if n.endswith('.html'))


def _strip_js_comments(source):
    """Source without // and /* */ comments; strings and regex literals are
    kept (a '//' inside them is not a comment)."""
    out = []
    i, n = 0, len(source)
    prev = ''   # last significant character, to tell a regex from a division
    while i < n:
        c = source[i]
        if c in '\'"`':
            j = i + 1
            while j < n and source[j] != c:
                j += 2 if source[j] == '\\' else 1
            out.append(source[i:j + 1])
            i = j + 1
            prev = c
        elif source.startswith('//', i):
            j = source.find('\n', i)
            i = n if j < 0 else j
        elif source.startswith('/*', i):
            j = source.find('*/', i + 2)
            i = n if j < 0 else j + 2
        elif c == '/' and (prev == '' or prev in '(,=:[!&|?{};+-*%<>~^'):
            j = i + 1
            in_class = False
            while j < n and (source[j] != '/' or in_class):
                if source[j] == '\\':
                    j += 1
                elif source[j] == '[':
                    in_class = True
                elif source[j] == ']':
                    in_class = False
                j += 1
            out.append(source[i:j + 1])
            i = j + 1
            prev = '/'
        else:
            out.append(c)
            if not c.isspace():
                prev = c
            i += 1
    return ''.join(out)


def test_comment_stripper():
    src = ("var a = 'x // y'; // innerHTML\n/* outerHTML */ var r = /\\/\\//;"
           " var d = 4 / 2; // eval(\n")
    stripped = _strip_js_comments(src)
    assert "'x // y'" in stripped and '/\\/\\//' in stripped
    assert 'innerHTML' not in stripped and 'outerHTML' not in stripped
    assert 'eval' not in stripped and '4 / 2' in stripped


APP_JS = _strip_js_comments(_read(os.path.join(STATIC, 'app.js')))


@pytest.mark.parametrize('sink', [
    r'\.innerHTML\b', r'\.outerHTML\b', r'\binsertAdjacentHTML\b',
    r'\bdocument\.write(ln)?\b', r'\beval\s*\(', r'\bnew\s+Function\b',
    r'\bcreateContextualFragment\b', r'\bDOMParser\b', r'\bsrcdoc\b',
    r'\.setHTML(Unsafe)?\b', r'\bsetTimeout\s*\(\s*[\'"`]',
    r'\bsetInterval\s*\(\s*[\'"`]', r'\.cssText\b',
    r'setAttribute\s*\(\s*[\'"`](on\w*|style|href|src|xlink:href|action|formaction)[\'"`]',
    r'\blocation\s*=(?!=)', r'\.insertRule\b', r'javascript:',
])
def test_app_js_has_no_html_or_code_sinks(sink):
    match = re.search(sink, APP_JS, re.IGNORECASE)
    assert match is None, f'{sink!r} in app.js: {match.group(0)!r}'


def test_app_js_sets_only_sizes_through_the_cssom():
    props = set(re.findall(r'\.style\.(\w+)\s*=', APP_JS))
    assert props <= {'width', 'height'}
    assert '.style[' not in APP_JS


def test_app_js_reads_parameters_from_data_attributes():
    assert 'document.body' in APP_JS and 'dataset' in APP_JS
    # The script itself contains no server-rendered values.
    assert '{{' not in APP_JS and '{%' not in APP_JS


@pytest.mark.parametrize('path', _templates(), ids=os.path.basename)
def test_templates_have_no_inline_script_style_or_safe(path):
    html = _read(path)
    for match in re.finditer(r'<script\b([^>]*)>(.*?)</script>', html,
                             re.IGNORECASE | re.DOTALL):
        attrs, content = match.groups()
        assert 'src=' in attrs and not content.strip(), 'inline script'
    assert not re.search(r'<style\b', html, re.IGNORECASE), '<style> block'
    assert not re.search(r'\sstyle\s*=', html, re.IGNORECASE), 'style attribute'
    assert not re.search(r'\son[a-z]+\s*=', html, re.IGNORECASE), 'event handler attribute'
    assert '|safe' not in html.replace(' ', '') and 'autoescape false' not in html
    assert 'javascript:' not in html.lower()
    # No external resources (CDN, fonts): the CSP would block them anyway.
    for url in re.findall(r'(?:src|href|action)\s*=\s*"([^"]*)"', html):
        assert not re.match(r'(?i)(https?:)?//', url), url


@pytest.mark.parametrize('path', _templates(), ids=os.path.basename)
def test_every_post_form_has_the_csrf_field(path):
    html = _read(path)
    forms = re.findall(r'<form\b([^>]*)>(.*?)</form>', html,
                       re.IGNORECASE | re.DOTALL)
    for attrs, content in forms:
        if re.search(r'method\s*=\s*"post"', attrs, re.IGNORECASE):
            assert ('<input type="hidden" name="csrf_token" '
                    'value="{{ csrf_token }}">') in content, attrs


def test_app_css_is_self_contained():
    css = _read(os.path.join(STATIC, 'app.css'))
    assert '@import' not in css and '@font-face' not in css
    assert not re.search(r'url\s*\(', css), 'no external or data: URLs'
    assert 'expression(' not in css


def test_served_pages_follow_the_rules(make_client):
    """The rendered pages too (the rules above are on the sources)."""
    client, _ = make_client()
    pages = ['/', '/download/12345678-1234-4678-9234-567812345678',
             '/edit/args', '/edit/channels', '/edit/channels/text',
             '/edit/archive']
    for page in pages:
        r = client.get(page, auth=basic())
        assert r.status_code == 200, page
        csp = r.headers['Content-Security-Policy']
        assert "default-src 'none'" in csp and 'unsafe-inline' not in csp
        assert 'https:' not in csp
        html = r.text
        assert not re.search(r'\sstyle\s*=', html, re.IGNORECASE), page
        assert not re.search(r'<style\b', html, re.IGNORECASE), page
        assert re.search(r'<script src="[^"]*/static/app\.js" defer></script>',
                         html), page
        assert len(re.findall(r'<script\b', html)) == 1, page
        for form in re.findall(r'<form\b[^>]*method="post"[^>]*>(.*?)</form>',
                               html, re.DOTALL):
            assert (f'<input type="hidden" name="csrf_token" '
                    f'value="{client.csrf_token}">') in form, page
