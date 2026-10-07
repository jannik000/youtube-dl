# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is not the youtube-dl source. It is a **Docker image** (published as `ghcr.io/jannik000/youtube-dl`) that wraps [yt-dlp](https://github.com/yt-dlp/yt-dlp) to download YouTube subscriptions or channels on an interval, with an optional FastAPI web UI. It is a security-hardened fork of `Jeeaaasus/youtube-dl` (MIT); `SECURITY-REVIEW.md` lists the findings and fixes. Everything under `root/` is copied into `/` of the image (`COPY root/ /`), so `root/app/...` becomes `/app/...` in the container, and so on.

## Commands

```bash
# Web UI + validator tests (needs the pinned deps plus yt-dlp, pytest, httpx)
pip install -r root/app/requirements.txt 'yt-dlp[default]' pytest httpx
pytest -q root/app/youtube-dl-webui/tests
pytest -q root/app/youtube-dl-webui/tests/test_webui.py::test_api_token_is_csrf_exempt   # single test

# Shell lint (CI runs exactly this)
shellcheck --severity=warning root/entrypoint.sh root/app/youtube-dl/*.sh root/etc/cont-init.d/* root/etc/supervisor/terminate.sh

# Dependency audit
pip-audit -r root/app/requirements.txt --strict

# Build and run the image
docker build -t youtube-dl-dev .
docker run --rm -it -v "$PWD/tmp-config:/config" -v "$PWD/tmp-downloads:/downloads" \
  -e youtubedl_webui=true -e WEBUI_USERNAME=dev -e WEBUI_PASSWORD=dev-password \
  -e youtubedl_interval=false -p 8080:8080 youtube-dl-dev
```

`youtubedl_interval=false` makes the container run one download pass and then exit. Settings come from `youtubedl_*` and `WEBUI_*` env vars; defaults are in the `Dockerfile` and documented in `README.md`.

## Runtime architecture

1. **`/entrypoint.sh`** runs every script in `/etc/cont-init.d/` in glob order (numeric prefixes set the order) and **aborts the container if one fails**. It then starts supervisord.
   - `05-default-confs`: copies `/config.default/{args.conf,channels.txt}` into `/config` if missing, symlinks `/config/args.conf` to `/etc/yt-dlp.conf`, substitutes `$youtubedl_quality` in `/config.default/format`.
   - `07-updater`: removes the updater's supervisor conf unless `youtubedl_autoupdate=true`.
   - `10-update-args`: replaces `/config/args.conf` with the new default if it matches the md5 of an **old default**. **When you change `root/config.default/args.conf`, add the md5 of the previous default to `old_default_hashes`.**
   - `80-webui`: removes the webui conf unless `youtubedl_webui=true` **and** credentials are set (fail closed, logged).
   - `90-user-permissions`: refuses `PUID`/`PGID` 0, applies them to user `abc`, chowns `/config`, `/downloads`, `/var/log`. `/home/abc` (HOME, created 0700) always goes to `abc`, but `/app` and `/home/abc/.venv` stay root-owned and read-only; the venv is chowned only when the updater is enabled. Test any change here with a `PUID` other than 911.
   - `91-supervisor-credentials`: random socket username/password.
2. **supervisord** runs as `abc`: `youtube-dl` (`youtube-dl.sh`, tees to `/var/log/youtube-dl/youtube-dl.log`), optional `youtube-dl-updater`, optional `youtube-dl-webui` (`uvicorn ... youtube-dl-webui:webserver`), and the non-autostarted `terminate`.

Env flags are compared as `[ "$var" = true ]` — never execute them (`if $var`).

### `youtube-dl.sh` (core download loop)
- Builds the yt-dlp command as a **bash array**; there is **no `eval`** (a test enforces this).
- Adds `--format` and `--download-archive /config/archive.txt` only if `args.conf` doesn't set them; `--cookies` when `/config/cookies.txt` exists.
- `channels.txt` lines with `URL | extra args` run individually; the args are split by `ytdlp_args.py split` (shlex, NUL-separated) and passed before `-- URL`. Plain URLs run together via a temp `--batch-file`. Shell syntax in these args is never interpreted.

### Web UI (`root/app/youtube-dl-webui/`)
- `youtube-dl-webui.py`: `create_app()` factory; `webserver` is created lazily via module `__getattr__` (uvicorn resolves it on startup). Paths live in the `PATHS` object so tests can redirect them.
- `webui_security.py`: env-based `Config` (fail closed via `startup_error()`), Basic auth (browser), Bearer token (only `POST /download` and `POST /api/download`), CSRF token + `Sec-Fetch-Site` check (Bearer requests exempt), URL validation (`WEBUI_ALLOWED_DOMAINS`), security headers/CSP.
- `ytdlp_args.py`: shared by the UI (validation on save) and `youtube-dl.sh` (splitting). Validation walks tokens with **yt-dlp's own parser** (resolving abbreviations and aliases by Option object), then checks parsed values and output paths. It is defense in depth, not a sandbox.
- Downloads run via `create_subprocess_exec` with `--` before the URL. Never use `create_subprocess_shell`/`shell=True` (a test greps for it).
- Templates get `base_path`, `csrf_token`, `readonly`. Every POST form needs `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`. No inline scripts: CSP is `script-src 'self'`; JS lives in `static/app.js` and reads parameters from `data-*` attributes.
- `TemplateResponse(request, name, context)` uses the Starlette 1.0 signature.
- `root/app/requirements.txt` is fully pinned (direct + transitive); Dependabot updates it.

## CI / release flow (`.github/workflows/`)

All workflows use the built-in `GITHUB_TOKEN`, minimal `permissions`, and actions pinned by commit SHA.
- `ci.yml`: pytest, pip-audit, shellcheck on every push and PR.
- `release.yml` (cron twice a day + manual): fetches the latest yt-dlp tag, validates its format, writes `release-versions/latest.txt`, commits `Release: v<version>`, and builds `:latest` and `:v<version>` with build args `YTDLP_VERSION=<version>`, `AUTOUPDATE=false`.
- `unstable.yml`: on code pushes, builds `:unstable` (master) or a branch-slug tag with `AUTOUPDATE=true`. On master it rewrites the `pip ... yt-dlp[default]` line of `updater.sh` (matched by pattern) to install from git and adds `git` after `python3-pip` in the Dockerfile.
- yt-dlp is pinned via the `YTDLP_VERSION` build arg, not by editing the Dockerfile. Images are built for `linux/amd64` and `linux/arm64`.
