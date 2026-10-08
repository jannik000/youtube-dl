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
# Working directory of every yt-dlp process (the image's WORKDIR); relative
# paths resolve against it.
CONFIG_DIR = '/config'

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
    '--write-pages',  # dumps every fetched page into the cwd (/config)
})

# Short forms of denied options (optparse allows bundling, e.g. -Ua).
DENIED_SHORT_OPTIONS = frozenset({'a', 'U'})


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


def _iter_options(parser, tokens):
    """Yield the optparse Option objects referenced by ``tokens``.

    Follows optparse's rules for which tokens are option *values*, so a value
    that happens to start with '-' (``--playlist-end -1``) is not mistaken for
    an option, and the rest of a short cluster after a value-taking option
    (``-o/downloads/a.mp4``) is treated as its value, not as more options.
    """
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        i += 1
        if tok == '--':
            return  # everything after is positional
        if tok.startswith('--'):
            name, has_eq, _value = tok.partition('=')
            opt = parser._long_opt[_canonical_long_opt(parser, name)]
            yield opt
            if opt.takes_value():
                # '--opt=v' supplies the first value inline.
                i += opt.nargs - 1 if has_eq else opt.nargs
        elif tok.startswith('-') and tok != '-':
            j = 1
            while j < len(tok):
                opt = parser._short_opt.get('-' + tok[j])
                if opt is None:
                    raise ArgsError(f'option -{tok[j]} is not a yt-dlp option')
                yield opt
                j += 1
                if opt.takes_value():
                    # Rest of the cluster (if any) is the first value.
                    i += opt.nargs if j >= len(tok) else opt.nargs - 1
                    break
        # anything else is a positional argument (URL) and is ignored here


def _denied_option_ids(parser):
    """Identity set of denied Option objects. Comparing objects instead of
    names also covers every alias of an option (e.g. --ppa for
    --postprocessor-args), whatever spelling the user chose."""
    ids = set()
    for name in DENIED_LONG_OPTIONS:
        opt = parser._long_opt.get(name)
        if opt is not None:
            ids.add(id(opt))
    for ch in DENIED_SHORT_OPTIONS:
        opt = parser._short_opt.get('-' + ch)
        if opt is not None:
            ids.add(id(opt))
    return ids


def _check_denied_tokens(parser, tokens):
    denied = _denied_option_ids(parser)
    for opt in _iter_options(parser, tokens):
        if id(opt) in denied:
            raise ArgsError(f'option {opt.get_opt_string()} is not allowed')


def _check_values(options):
    """Second, name-independent layer: reject dangerous *effects* in the
    parsed options, however they were requested."""
    def get(attr, default=None):
        return getattr(options, attr, default)

    checks = (
        ('--exec', bool(get('exec_cmd'))),
        ('--exec-before-download', bool(get('exec_before_dl_cmd'))),
        ('--netrc-cmd', get('netrc_cmd') is not None),
        ('--netrc-location', get('netrc_location') is not None),
        ('--plugin-dirs', any(d != 'default' for d in (get('plugin_dirs') or []))),
        ('--config-locations', bool(get('config_locations'))),
        ('--batch-file', get('batchfile') is not None),
        ('--load-info-json', get('load_info_filename') is not None),
        ('--use-postprocessor', bool(get('add_postprocessors'))),
        ('--ffmpeg-location', get('ffmpeg_location') is not None),
        ('--downloader', bool(get('external_downloader'))),
        ('--downloader-args', bool(get('external_downloader_args'))),
        ('--postprocessor-args', bool(get('postprocessor_args'))),
        ('--cookies', get('cookiefile') is not None),
        ('--cookies-from-browser', get('cookiesfrombrowser') is not None),
        ('--cache-dir', get('cachedir') not in (None, False)),
        ('--enable-file-urls', bool(get('enable_file_urls'))),
        ('--update', bool(get('update_self'))),
        ('--write-pages', bool(get('write_pages'))),
    )
    for name, is_set in checks:
        if is_set:
            raise ArgsError(f'option {name} is not allowed')


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


def _resolve(value, base, strip=False):
    """Resolve a path option the way yt-dlp does, relative to ``base``.

    yt-dlp expands '~' and environment variables in path values (and in
    output templates, before field substitution) and joins relative paths
    onto the working directory or the --paths home. Expansion depends on the
    runtime environment, so values using it are rejected outright. Only the
    --paths values are stripped by yt-dlp (``strip=True``); output templates,
    --print-to-file targets and the archive name keep leading whitespace,
    which makes them relative.
    """
    if strip:
        value = value.strip()
    if value.startswith('~') or '$' in value:
        raise ArgsError(f'path {value!r} must not use "~" or "$" expansion')
    skeleton = _path_skeleton(value)
    if '\x00' in skeleton:
        raise ArgsError('path contains a null byte')
    if not os.path.isabs(skeleton):
        skeleton = os.path.join(base, skeleton)
    return os.path.normpath(skeleton)


def _require_under_downloads(value, resolved, allow_archive=False):
    if allow_archive and resolved == ARCHIVE_EXACT:
        return
    if resolved == DOWNLOADS_DIR or resolved.startswith(DOWNLOADS_DIR + '/'):
        return
    raise ArgsError(f'path {value!r} resolves to {resolved!r}, which is not '
                    f'under {DOWNLOADS_DIR}')


def _check_paths(options):
    """Every file yt-dlp writes must end up under /downloads.

    Both download processes run with the container WORKDIR as current
    directory, so a relative path is resolved against CONFIG_DIR, where
    pre-/post-execution.sh live. The --paths 'home' directory (itself resolved
    against CONFIG_DIR) is the base for the other path types, output templates
    and --print-to-file targets, exactly as in YoutubeDL.get_output_path().
    """
    # Empty values are skipped: yt-dlp writes nothing for an empty template
    # (e.g. --embed-thumbnail sets 'pl_thumbnail' to '') and an empty --paths
    # value adds nothing to the base.
    paths = dict(options.paths or {})
    home_value = paths.pop('home', None)
    base = CONFIG_DIR
    if home_value is not None and home_value.strip():
        base = _resolve(home_value, CONFIG_DIR, strip=True)
        _require_under_downloads(home_value, base)
    # yt-dlp writes to join(home, paths[type], filename), e.g. temp files
    # under the 'temp' directory. A template that stays inside /downloads
    # relative to home may still climb out relative to a shallower type
    # directory, so every template is checked against every base.
    bases = [base]
    for directory in paths.values():
        if directory.strip():
            resolved = _resolve(directory, base, strip=True)
            _require_under_downloads(directory, resolved)
            bases.append(resolved)
    targets = [t for t in (options.outtmpl or {}).values() if t]
    for entries in (options.print_to_file or {}).values():
        targets += [filename for _template, filename in entries if filename]
    for target in targets:
        for target_base in bases:
            _require_under_downloads(target, _resolve(target, target_base))
    if options.download_archive:
        # The archive is opened relative to the working directory, not home.
        archive = options.download_archive
        _require_under_downloads(archive, _resolve(archive, CONFIG_DIR),
                                 allow_archive=True)


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
    _check_values(parsed.options)
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
        # Emit NUL-terminated tokens for the shell script to read into an
        # array. Terminating (not just separating) keeps an empty last token.
        try:
            tokens = split_shell_args(argv[2] if len(argv) > 2 else '')
        except ValueError as err:
            sys.stderr.write(f'cannot parse arguments: {err}\n')
            return 1
        sys.stdout.write(''.join(token + '\x00' for token in tokens))
        return 0
    sys.stderr.write('usage: ytdlp_args.py split "<args>"\n')
    return 2


if __name__ == '__main__':
    raise SystemExit(_main(sys.argv))
