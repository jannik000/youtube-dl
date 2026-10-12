"""channels.txt as a list of channels for the web UI's channel manager.

Reads the entries the way youtube-dl.sh does (byte order mark, blank and
comment lines, 'URL | options', a trailing ' #comment' on a plain URL) and
changes the file one line at a time: adding appends a line, removing drops
exactly one line. Everything else (comments, blank lines, the order, line
endings) stays as it is. Stdlib only.
"""

import re
import urllib.parse

BOM = '\ufeff'
MAX_URL = 2048
MAX_OPTIONS = 4096

# '@handle' alone means the YouTube channel with that handle.
_HANDLE_RE = re.compile(r'@[\w.\-]{1,100}')
_UNSAFE_RE = re.compile(r'[\x00-\x20\x7f\u0080-\u009f\u2028\u2029'
                        r'\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]')


class ChannelError(ValueError):
    """A refused change; the message is shown to the user."""


def _lines(text):
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith(BOM):
        lines[0] = lines[0][len(BOM):]
    return lines


def _entry(line):
    """(url, options) of a channels.txt line, or None for a line the script
    skips (blank, comment)."""
    stripped = line.strip()
    if not stripped or stripped.startswith('#'):
        return None
    if '|' in stripped:
        url, options = stripped.split('|', 1)
        return url.strip(), options.strip()
    if stripped.startswith((';', ']')):
        return None
    return re.split(r'\s#', stripped, maxsplit=1)[0].strip(), ''


def entries(text):
    """The channels of a channels.txt in file order: [{'line', 'url',
    'options'}], 'line' being the stripped line (what remove() takes)."""
    out = []
    for line in _lines(text):
        entry = _entry(line)
        if entry is not None:
            out.append({'line': line.strip(), 'url': entry[0],
                        'options': entry[1]})
    return out


def comment_lines(text):
    """Number of lines that are neither blank nor a channel."""
    return sum(1 for line in _lines(text)
               if line.strip() and _entry(line) is None)


def _key(url):
    """URL as compared for duplicates: scheme and host lower case, no
    trailing slash, 'www.' and 'm.' ignored."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (parts.hostname or '').lower()
    for prefix in ('www.', 'm.'):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return (parts.scheme.lower(), host, parts.path.rstrip('/'), parts.query)


def normalise_url(url):
    """The URL to store for what the user typed: '@handle' becomes the
    YouTube channel URL. Refuses anything but a plain http(s) URL."""
    url = (url or '').strip()
    if _HANDLE_RE.fullmatch(url):
        url = f'https://www.youtube.com/{url}'
    if not url:
        raise ChannelError('Enter a channel or playlist URL.')
    if len(url) > MAX_URL:
        raise ChannelError('The URL is too long.')
    if _UNSAFE_RE.search(url) or '|' in url or '#' in url:
        raise ChannelError('The URL must not contain spaces, "|", "#" or '
                           'control characters.')
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise ChannelError('This is not a valid URL.') from None
    if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
        raise ChannelError('Only http and https URLs can be added.')
    return url


def add(text, url, options=''):
    """channels.txt with one more line 'url' or 'url | options' at the end.
    ``url`` must come from normalise_url()."""
    options = (options or '').strip()
    if len(options) > MAX_OPTIONS:
        raise ChannelError('The options are too long.')
    if _UNSAFE_RE.search(options.replace(' ', '').replace('\t', '')):
        raise ChannelError('The options must be on one line, without control '
                           'characters.')
    key = _key(url)
    if any(_key(entry['url']) == key for entry in entries(text)):
        raise ChannelError('This channel is already in the list.')
    line = f'{url} | {options}' if options else url
    newline = '\r\n' if '\r\n' in text else '\n'
    if text and not text.endswith(('\n', '\r')):
        text += newline
    return text + line + newline


def remove(text, line):
    """(channels.txt without the first line whose stripped text is
    ``line``, its position among the file's lines)."""
    lines = text.splitlines(keepends=True)
    for pos, raw in enumerate(lines):
        stripped = raw.strip()
        if pos == 0:
            stripped = stripped.lstrip(BOM)
        if stripped == line and _entry(raw.lstrip(BOM)) is not None:
            return ''.join(lines[:pos] + lines[pos + 1:]), pos
    raise ChannelError('This channel is no longer in channels.txt (changed '
                       'meanwhile?). Reload the page.')


def restore(text, line, pos):
    """Put a removed line back at its old position (undo of remove())."""
    newline = '\r\n' if '\r\n' in text else '\n'
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(('\n', '\r')):
        lines[-1] += newline
    pos = max(0, min(pos, len(lines)))
    lines.insert(pos, line + newline)
    return ''.join(lines)
