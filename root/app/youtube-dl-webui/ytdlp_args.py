"""Shared yt-dlp argument handling for the web UI and youtube-dl.sh.

Two jobs:

* ``split`` (CLI) turns a single string of ``yt-dlp`` arguments into a
  NUL-separated token list using the same ``shlex`` rules yt-dlp uses for
  config files. ``youtube-dl.sh`` uses this to parse the per-URL ``| args``
  from channels.txt into a bash array, so it no longer needs ``eval``.

* ``validate_args_conf`` / ``validate_channels`` reject dangerous options
  before the web UI writes args.conf / channels.txt. Rejection is based on
  yt-dlp's *own* option parser, so abbreviations (``--plugin-d``), bundled
  short options and ``--alias`` definitions are resolved the same way yt-dlp
  resolves them at runtime, not matched as raw strings.

This is defense in depth. The real boundary is authentication: only
authenticated admins can write these files, and WEBUI_READONLY disables
writing entirely. See SECURITY-REVIEW.md ("Grenzen der Options-Prüfung").
"""

import os
import shlex
import sys

# Directory downloads and the archive are allowed to live in. Everything else
# that writes to the filesystem outside here is rejected.
DOWNLOADS_DIR = '/downloads'
ARCHIVE_EXACT = '/config/archive.txt'

# Options rejected by canonical name. Each either runs a command, loads code
# or extra config, or reads/writes outside /downloads in a way the output-path
# check below cannot see. Matched after yt-dlp resolves abbreviations/aliases.
DENIED_LONG_OPTIONS = frozenset({
    '--exec', '--exec-before-download',
    '--netrc-cmd', '--netrc-location',
    '--plugin-dirs',
    '--use-postprocessor',
    '--config-locations',
    '--batch-file',
    '--load-info-json',
    '--alias',
    '--ffmpeg-location',
    '--downloader', '--external-downloader',
    '--downloader-args', '--external-downloader-args',
    '--postprocessor-args',
    '--cookies', '--cookies-from-browser',
    '--cache-dir',
    '--enable-file-urls',
    '--update', '--update-to',
})

# Short forms of denied options (optparse allows bundling, e.g. -Ua).
DENIED_SHORT_OPTIONS = frozenset({'a', 'U'})

# Options whose values are filesystem paths and must stay under /downloads.
# Checked on the parsed result so the limit holds however the path was set.
_PATH_OPTION_ATTRS = ('outtmpl', 'paths', 'print_to_file', 'download_archive')


class ArgsError(ValueError):
    """Raised when a line of configuration is rejected."""


def split_shell_args(text):
    """Split a string into tokens exactly like yt-dlp parses a config file."""
    return shlex.split(text, comments=True)


def _get_parser():
    # Imported lazily so the CLI ``split`` path works even if the full option
    # machinery is unavailable; validation needs it and will fail closed.
    from yt_dlp.options import create_parser
    return create_parser()


def _canonical_long_opt(parser, token):
    """Resolve a '--opt[=val]' token to its canonical yt-dlp option name.

    Raises ArgsError for unknown or ambiguous options (fail closed): yt-dlp
    would reject them at runtime too, and an ambiguous prefix must never be
    allowed to slip past the denylist.
    """
    name = token.split('=', 1)[0]
    try:
        return parser._match_long_opt(name)
    except Exception as err:  # AmbiguousOptionError, BadOptionError, API drift
        raise ArgsError(f'option {name!r} is not an unambiguous yt-dlp option '
                        f'({type(err).__name__})')


def _check_denied_tokens(parser, tokens):
    for tok in tokens:
        if tok == '--' or not tok.startswith('-') or tok == '-':
            continue
        if tok.startswith('--'):
            canon = _canonical_long_opt(parser, tok)
            if canon in DENIED_LONG_OPTIONS:
                raise ArgsError(f'option {canon} is not allowed')
        else:
            # Short cluster like -Ua. A leading digit means a negative number.
            cluster = tok[1:]
            if cluster[:1].isdigit():
                continue
            for ch in cluster:
                if ch in DENIED_SHORT_OPTIONS:
                    raise ArgsError(f'option -{ch} is not allowed')


def _path_skeleton(value):
    """Replace %(...)s template fields with 'X' so only literal path parts
    remain. Template fields cannot introduce a leading '/' or a '..' segment,
    so the skeleton is enough to judge where the file is written."""
    out, i = [], 0
    while i < len(value):
        if value[i] == '%' and i + 1 < len(value) and value[i + 1] == '(':
            depth = 0
            j = i + 1
            while j < len(value):
                if value[j] == '(':
                    depth += 1
                elif value[j] == ')':
                    depth -= 1
                    if depth == 0:
                        j += 1
                        # skip the conversion/type chars (e.g. 's', 'd', '>fmt')
                        while j < len(value) and value[j] not in '/\\':
                            j += 1
                        break
                j += 1
            out.append('X')
            i = j
        else:
            out.append(value[i])
            i += 1
    return ''.join(out)


def _ensure_within_downloads(value, allow_archive=False):
    skeleton = _path_skeleton(value)
    if '\x00' in skeleton:
        raise ArgsError('path contains a null byte')
    # Reject parent-dir traversal in any form.
    parts = skeleton.replace('\\', '/').split('/')
    if '..' in parts:
        raise ArgsError(f'path {value!r} must not contain ".."')
    if not os.path.isabs(skeleton):
        # Relative paths resolve under the download process working directory;
        # without a leading '/' and without '..' they cannot reach /config.
        return
    normalized = os.path.normpath(skeleton)
    if allow_archive and normalized == ARCHIVE_EXACT:
        return
    if normalized == DOWNLOADS_DIR or normalized.startswith(DOWNLOADS_DIR + '/'):
        return
    raise ArgsError(f'path {value!r} must be under {DOWNLOADS_DIR}')


def _check_paths(options):
    for templ in (options.outtmpl or {}).values():
        _ensure_within_downloads(templ)
    for directory in (options.paths or {}).values():
        _ensure_within_downloads(directory)
    for entries in (options.print_to_file or {}).values():
        for _template, filename in entries:
            _ensure_within_downloads(filename)
    if options.download_archive:
        _ensure_within_downloads(options.download_archive, allow_archive=True)


def validate_tokens(tokens):
    """Validate an already-split token list. Raises ArgsError if rejected."""
    # Cheap string guard first: these would no longer be shell-expanded after
    # the youtube-dl.sh rewrite, but their presence signals stale, now-inert
    # config that should be corrected rather than silently kept.
    for tok in tokens:
        if '$(' in tok or '`' in tok:
            raise ArgsError('shell command substitution ($(...) or backticks) '
                            'is no longer supported; use yt-dlp output '
                            'templates such as %(upload_date>%Y)s instead')
    parser = _get_parser()
    _check_denied_tokens(parser, tokens)
    try:
        import yt_dlp
        parsed = yt_dlp.parse_options(['--ignore-config', *tokens])
    except ArgsError:
        raise
    except SystemExit as err:
        raise ArgsError(f'yt-dlp rejected the options (exit {err.code})')
    except Exception as err:
        raise ArgsError(f'could not parse options: {err}')
    _check_paths(parsed.options)


def validate_args_conf(text):
    """Validate the full args.conf text. Raises ArgsError if rejected."""
    try:
        tokens = split_shell_args(text)
    except ValueError as err:
        raise ArgsError(f'could not parse args.conf: {err}')
    validate_tokens(tokens)


def validate_channels(text):
    """Validate channels.txt. Each line may be 'URL | extra yt-dlp args'; only
    the args after '|' are validated (the URL is validated on download)."""
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith('#') or '|' not in line:
            continue
        extra = line.split('|', 1)[1]
        try:
            tokens = split_shell_args(extra)
        except ValueError as err:
            raise ArgsError(f'line {lineno}: could not parse arguments: {err}')
        try:
            validate_tokens(tokens)
        except ArgsError as err:
            raise ArgsError(f'line {lineno}: {err}')


def _main(argv):
    if len(argv) >= 2 and argv[1] == 'split':
        # Emit NUL-separated tokens for the shell script to read into an array.
        tokens = split_shell_args(argv[2] if len(argv) > 2 else '')
        sys.stdout.write('\x00'.join(tokens))
        return 0
    sys.stderr.write('usage: ytdlp_args.py split "<args>"\n')
    return 2


if __name__ == '__main__':
    raise SystemExit(_main(sys.argv))
