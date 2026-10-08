"""Unit tests for the argument splitter and validator."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ytdlp_args  # noqa: E402


def test_split_matches_quoting():
    assert ytdlp_args.split_shell_args("--output '/a b/c' --geo-bypass") == \
        ['--output', '/a b/c', '--geo-bypass']


def test_split_strips_comments():
    assert ytdlp_args.split_shell_args('--geo-bypass # a comment') == ['--geo-bypass']


def test_split_cli_terminates_every_token(capsys):
    # youtube-dl.sh reads NUL-terminated tokens; an empty last token must
    # survive (e.g. --output-na-placeholder '').
    assert ytdlp_args._main(['x', 'split', "--output-na-placeholder ''"]) == 0
    assert capsys.readouterr().out == '--output-na-placeholder\x00\x00'


def test_split_cli_fails_on_unparsable_input(capsys):
    # The script skips the line instead of running the URL without its args.
    assert ytdlp_args._main(['x', 'split', "--match-filter 'unclosed"]) == 1
    assert capsys.readouterr().out == ''


def test_default_args_conf_is_accepted():
    ytdlp_args.validate_args_conf(
        "--output '/downloads/%(uploader)s/%(title)s.%(ext)s'\n"
        "--playlist-end '16'\n--match-filter '!is_live'\n"
        "--merge-output-format 'mp4'\n--sponsorblock-mark 'all'\n")


@pytest.mark.parametrize('content', [
    "--exec 'id'",
    "--exec=id",
    "--exec-before-download 'id'",
    "--netrc-cmd 'id'",
    "--plugin-dirs /tmp",
    "--use-postprocessor Exec",
    "--config-locations /etc/x",
    "--batch-file /etc/x",
    "--load-info-json /tmp/x.json",
    "--ffmpeg-location /tmp",
    "--downloader ffmpeg",
    "--downloader-args 'ffmpeg:-y'",
    "--cookies /etc/shadow",
    "--cookies-from-browser chrome",
    "--enable-file-urls",
    "--update-to nightly",
])
def test_dangerous_options_rejected(content):
    with pytest.raises(ytdlp_args.ArgsError):
        ytdlp_args.validate_args_conf(content)


@pytest.mark.parametrize('content', [
    "--plugin-d /tmp",         # abbreviation of --plugin-dirs
    "--config-loc /etc/x",     # abbreviation of --config-locations
    "--exe id",                # ambiguous (--exec/--exec-before-download)
    "--al foo",                # ambiguous prefix -> rejected (fail closed)
])
def test_abbreviations_and_ambiguous_rejected(content):
    with pytest.raises(ytdlp_args.ArgsError):
        ytdlp_args.validate_args_conf(content)


@pytest.mark.parametrize('content', [
    "--ppa 'ffmpeg:-y'",                   # alias of --postprocessor-args
    "--external-downloader ffmpeg",        # alias of --downloader
    "--external-downloader-args 'x:-y'",   # alias of --downloader-args
    "-ao /tmp/x",                          # -a (batch-file) in a short cluster
    "-vU",                                 # -U (update) bundled after a flag
    "-o/config/pre-execution.sh",          # attached short value, bad path
])
def test_option_aliases_and_clusters_rejected(content):
    with pytest.raises(ytdlp_args.ArgsError):
        ytdlp_args.validate_args_conf(content)


@pytest.mark.parametrize('content', [
    "-o/downloads/a.mp4",                  # 'a' inside an attached value
    "-o '/downloads/Ua/%(title)s.%(ext)s'",  # 'U'/'a' inside a value
    "--playlist-end -1",                   # value starting with '-'
    "--output-na-placeholder '-na-'",      # value starting with '-'
    "-i -o '/downloads/%(title)s.%(ext)s'",
])
def test_values_are_not_mistaken_for_options(content):
    ytdlp_args.validate_args_conf(content)


def test_default_args_conf_from_repo_is_accepted():
    path = os.path.join(os.path.dirname(__file__), '..', '..', '..',
                        'config.default', 'args.conf')
    with open(path) as f:
        ytdlp_args.validate_args_conf(f.read())


def test_alias_defining_exec_is_rejected():
    # Even unused, an alias is rejected outright.
    with pytest.raises(ytdlp_args.ArgsError):
        ytdlp_args.validate_args_conf("--alias --pwn '--exec {0}'")


@pytest.mark.parametrize('content', [
    "--output '/config/pre-execution.sh'",
    "--output '/etc/cron.d/x'",
    "-o /tmp/x/%(title)s.%(ext)s",
    "--paths '/config'",
    "--print-to-file title '/config/post-execution.sh'",
    "--output '/downloads/../config/%(title)s'",
])
def test_output_outside_downloads_rejected(content):
    with pytest.raises(ytdlp_args.ArgsError):
        ytdlp_args.validate_args_conf(content)


@pytest.mark.parametrize('content', [
    # yt-dlp runs with cwd=/config, so relative paths land next to
    # pre-/post-execution.sh, which youtube-dl.sh executes.
    "-o '%(title)s.%(ext)s'",
    "-o pre-execution.sh",
    "--print-to-file 'touch /tmp/x' post-execution.sh",
    "--print-to-file title 'sub/%(id)s.sh'",
    "-P temp:tmp",                              # temp dir relative to cwd
    "-P tmp",                                   # home relative to cwd
    "--download-archive other.txt",             # /config/other.txt
    "-o '~/x.%(ext)s'",                         # expanded by yt-dlp
    "-o '$HOME/x.%(ext)s'",                     # expanded by yt-dlp
    "-P '/downloads' -o '../config/x'",         # escapes home
    "-P '/downloads/${X}'",
    # yt-dlp does not strip these: a leading space makes them relative
    "-o ' /downloads/%(title)s.%(ext)s'",
    "--print-to-file title ' /downloads/loot'",
    "--download-archive ' /downloads/a.txt'",
    "--write-pages",                            # dumps pages into /config
    # inside /downloads relative to home, but outside relative to the
    # shallower temp/thumbnail directory yt-dlp also writes into
    "-P /downloads/sub -P temp:/downloads -o '../config/x'",
    "-P /downloads/a/b -P thumbnail:/downloads -o 'thumbnail:../config/x'",
])
def test_paths_resolving_outside_downloads_rejected(content):
    with pytest.raises(ytdlp_args.ArgsError):
        ytdlp_args.validate_args_conf(content)


@pytest.mark.parametrize('content', [
    "--output '/downloads/%(uploader)s/%(title)s.%(ext)s'",
    "--output '/downloads/sub/%(title)s.%(ext)s'",
    "--download-archive '/config/archive.txt'",  # explicitly allowed exception
    "--download-archive archive.txt",            # same file, relative to cwd
    "-P /downloads -o '%(title)s.%(ext)s'",       # relative to the home path
    "-P /downloads -P temp:tmp",                  # temp under home
    "-P /downloads --print-to-file title 'titles.txt'",
    "-P ' /downloads' -o '%(title)s.%(ext)s'",    # yt-dlp strips --paths
])
def test_safe_output_paths_accepted(content):
    ytdlp_args.validate_args_conf(content)


def test_command_substitution_rejected():
    with pytest.raises(ytdlp_args.ArgsError):
        ytdlp_args.validate_args_conf('--output "/downloads/$(id)/%(title)s"')
    with pytest.raises(ytdlp_args.ArgsError):
        ytdlp_args.validate_args_conf('--output "/downloads/`id`/%(title)s"')


def test_channels_pipe_args_validated():
    # Plain URLs and safe per-URL args are fine.
    ytdlp_args.validate_channels(
        '# comment\n'
        'https://www.youtube.com/channel/UCxxx\n'
        "https://www.youtube.com/channel/UCyyy | --playlist-end '-1'\n")
    with pytest.raises(ytdlp_args.ArgsError):
        ytdlp_args.validate_channels(
            'https://www.youtube.com/channel/UCxxx | --exec id\n')


def test_channels_reports_line_number():
    with pytest.raises(ytdlp_args.ArgsError) as exc:
        ytdlp_args.validate_channels(
            'https://ok/\n'
            '# a comment\n'
            'https://bad/ | --plugin-dirs /tmp\n')
    assert 'line 3' in str(exc.value)
