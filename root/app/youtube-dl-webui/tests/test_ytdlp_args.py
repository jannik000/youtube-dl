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
    "--output '/downloads/%(uploader)s/%(title)s.%(ext)s'",
    "--output '/downloads/sub/%(title)s.%(ext)s'",
    "--download-archive '/config/archive.txt'",  # explicitly allowed exception
    "-o '%(title)s.%(ext)s'",                     # relative, no traversal
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
