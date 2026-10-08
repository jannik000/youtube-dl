# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is not the youtube-dl source. It is a **Docker image** (published as `ghcr.io/jannik000/youtube-dl`) that wraps [yt-dlp](https://github.com/yt-dlp/yt-dlp) to download YouTube subscriptions or channels on an interval, with an optional FastAPI web UI. It is a security-hardened fork of `Jeeaaasus/youtube-dl` (MIT); `SECURITY-REVIEW.md` lists the findings and fixes. Everything under `root/` is copied into `/` of the image (`COPY root/ /`), so `root/app/...` becomes `/app/...` in the container, and so on.

## Commands

```bash
# Web UI + validator tests (pinned deps plus the yt-dlp version the images use)
pip install -r root/app/requirements.txt "yt-dlp[default]==$(cat release-versions/latest.txt)" pytest httpx
pytest -q root/app/youtube-dl-webui/tests
pytest -q root/app/youtube-dl-webui/tests/test_webui.py::test_api_token_is_csrf_exempt   # single test

# Shell lint (CI runs exactly this)
shellcheck --severity=warning root/entrypoint.sh root/app/youtube-dl/*.sh root/etc/cont-init.d/* \
  root/etc/supervisor/terminate.sh tests/e2e/run.sh

# Dependency audit
pip-audit -r root/app/requirements.txt --strict

# End-to-end: builds the image and tests running containers (needs a Docker daemon that can build)
bash tests/e2e/run.sh
#   E2E_SKIP_BUILD=1 E2E_IMAGE=<tag>  -> test an existing image
#   E2E_BUILD_ARGS='--build-arg YTDLP_VERSION=2026.08.19' -> extra docker build args
```

`youtubedl_interval=false` makes the container run one download pass and then exit. Settings come from `youtubedl_*` and `WEBUI_*` env vars; defaults are in the `Dockerfile` and documented in `README.md`.

## Runtime architecture

1. **`/entrypoint.sh`** sets a system-only `PATH` for all root processes, runs every script in `/etc/cont-init.d/` in glob order (numeric prefixes set the order) and **aborts the container if one fails**. It then starts supervisord. Every file in `cont-init.d` must be executable (a unit test checks this).
   - `05-default-confs`: copies `/config.default/{args.conf,channels.txt}` into `/config` if missing, symlinks `/config/args.conf` to `/etc/yt-dlp.conf`, substitutes `$youtubedl_quality` in `/config.default/format`.
   - `07-updater`: removes the updater's supervisor conf unless `youtubedl_autoupdate=true`.
   - `10-update-args`: replaces `/config/args.conf` with the new default if it matches the md5 of an **old default**. **When you change `root/config.default/args.conf`, add the md5 of the previous default to `old_default_hashes`.**
   - `80-webui`: removes the webui conf unless `youtubedl_webui=true` **and** credentials are set (fail closed, logged).
   - `90-user-permissions`: refuses `PUID`/`PGID` 0, applies them to user `abc`, chowns `/config`, `/downloads`, `/var/log` and abc's HOME `/home/abc`. `/app` and the venv `/opt/venv` stay root-owned (root-owned parents, so abc cannot replace them); the venv is chowned only when the updater is enabled. Test changes here with a `PUID` other than 911.
   - `91-supervisor-credentials`: random socket username/password.
2. **supervisord** (root) runs `youtube-dl` (`youtube-dl.sh`, tees to `/var/log/youtube-dl/youtube-dl.log`), the optional `youtube-dl-updater` and `youtube-dl-webui` as `abc`, and the non-autostarted `terminate` as root. abc's programs get `PATH` (with `/opt/venv/bin`) and `HOME` via `environment=`. Supervisor looks up a bare `command=` in the program's `environment=` PATH if set, otherwise in supervisord's own (system-only) PATH; all commands use absolute paths (`/bin/bash`, `/opt/venv/bin/uvicorn`). Never give a root-run program an `environment=` PATH containing abc-writable directories.
   - `youtube-dl` has `stopasgroup`/`killasgroup`: stop and restart (web UI *Restart*, `POST /restart-youtube-dl`) send SIGTERM to the whole process group (`bash -c`, `youtube-dl.sh`, yt-dlp, `tee`). Without them only `bash -c` died and the old pass kept running next to the new one. The SIGKILL to the group follows only if `bash -c` still runs after `stopwaitsecs`, so a member that ignores SIGTERM (a user pre/post script with `trap '' TERM`) survives; an unexpected death of `bash -c` (no supervisord stop) does not reach the group at all.
3. All yt-dlp processes run with the WORKDIR `/config` as current directory, so relative output paths land in `/config`.

Env flags are compared as `[ "$var" = true ]` — never execute them (`if $var`).

### `youtube-dl.sh` (core download loop)
- Builds the yt-dlp command as a **bash array**; there is **no `eval`** (a test enforces this).
- Adds `--format` and `--download-archive /config/archive.txt` only if `args.conf` doesn't set them; `--cookies` when `/config/cookies.txt` exists.
- `channels.txt` lines with `URL | extra args` run individually; the args are split by `ytdlp_args.py split` (shlex, NUL-terminated tokens) and passed before `-- URL`. A line whose args cannot be parsed is skipped. Plain URLs run together via a temp `--batch-file`.
- The interval is split into words for `sleep` (`1d 3h`); an invalid value falls back to 3h so passes never run back to back.
- `youtubedl_interval=false`: the script only runs `supervisorctl start terminate` (`terminate.sh` sends SIGQUIT to supervisord, which stops every program and exits 0, so the container exits 0) and then `sleep infinity`. Never stop programs from inside the script (`supervisorctl stop all`): it is in the process group supervisor stops as a whole, so it would kill itself before `terminate` starts and the container would never exit. Never let it exit there either: `autorestart` would begin a second pass before the shutdown.

### Web UI (`root/app/youtube-dl-webui/`)
- `youtube-dl-webui.py`: `create_app()` factory; `webserver` is created lazily via module `__getattr__` (uvicorn resolves it on startup). Paths live in the `PATHS` object so tests can redirect them.
- An HTTP middleware authenticates from the headers **before** routes and body parsing run; only `PUBLIC_PATHS` (`/favicon.ico`, `/static/app.js`) are open, and the API token is accepted only on `TOKEN_ROUTES` (`POST /download`, `POST /api/download`, both answer JSON for token clients). Route dependencies authenticate again and drive CSRF. FastAPI's `/docs`/`/openapi.json` are disabled.
- `webui_security.py`: env-based `Config` (fail closed via `startup_error()`), Basic/Bearer auth with `compare_digest`, CSRF token + `Sec-Fetch-Site` check (Bearer requests exempt), URL validation (stripped, http/https only, optional `WEBUI_ALLOWED_DOMAINS`), security headers/CSP.
- `ytdlp_args.py`: shared by the UI (validation on save) and `youtube-dl.sh` (splitting). Validation walks tokens with **yt-dlp's own parser**, matching denied options by Option object (covers aliases like `--ppa`), then checks parsed values and resolves output paths like `YoutubeDL.get_output_path()` (relative to `/config`/`--paths home`). It is defense in depth, not a sandbox.
- Downloads run via `create_subprocess_exec` with `--` before the URL. Never use `create_subprocess_shell`/`shell=True` (a test greps for it).
- `download_bg()` copies yt-dlp's output in 64 KiB chunks through an incremental UTF-8 decoder, never line by line (asyncio's 64 KiB line limit; progress without `--newline` is one endless line), drains it to EOF and awaits the process. yt-dlp starts in its own session; on an error or cancellation `download_bg()` kills that process group (ffmpeg and external downloaders share yt-dlp's stdout), drains the pipe, then awaits yt-dlp (before Python 3.13, asyncio's `wait()` returns only once the pipe is closed).
- Templates get `base_path`, `csrf_token`, `readonly`. Every POST form needs `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">`. No inline scripts: CSP is `script-src 'self'`; JS lives in `static/app.js` (served by a route, not a StaticFiles mount, so it works behind prefix-stripping proxies) and reads parameters from `data-*` attributes.
- `TemplateResponse(request, name, context)` uses the Starlette 1.0 signature.
- `root/app/requirements.txt` is fully pinned (direct + transitive); Dependabot updates it.

## CI / release flow (`.github/workflows/`)

All workflows use the built-in `GITHUB_TOKEN`, minimal per-job `permissions`, checkouts without persisted credentials where nothing is pushed, and actions pinned by commit SHA.
- `ci.yml` (every push and PR): `test` (pytest against the yt-dlp version in `release-versions/latest.txt`, pip-audit), `shellcheck`, `e2e` (`tests/e2e/run.sh`, amd64), `build-arm64` (build only). `publish-unstable` builds `:unstable` (`AUTOUPDATE=true`, updater rewritten to install yt-dlp from git, `git` added after `python3-pip` in the Dockerfile) **only on master and only after all four passed**. No images for other branches or PRs.
- `release.yml` (cron twice a day + manual, master only): job `verify` (read-only, no stored credentials) fetches the latest yt-dlp tag, validates its format and decides to build if the version changed, `:v<version>` is missing in GHCR, or on a manual run; it then runs pytest and the e2e suite against exactly that version. Job `publish` (write access, never runs yt-dlp on the runner) builds and pushes `:latest` and `:v<version>` (`YTDLP_VERSION=<version>`, `AUTOUPDATE=false`) and only afterwards commits `release-versions/latest.txt` (`Release: v<version>`). Keep untrusted/new code out of jobs that hold write tokens.
- The repo is a GitHub fork: scheduled workflows stay disabled until enabled in the Actions tab, and GitHub disables them after 60 days without repository activity. The first `:latest` therefore comes from a manual *Release Images* run on master. In `publish`, outputs of `verify` are re-validated and passed only via `env`; QEMU runs with `cache-image: false`; the version commit is rebased onto the current master.
- yt-dlp is pinned via the `YTDLP_VERSION` build arg, not by editing the Dockerfile. Images are built for `linux/amd64` and `linux/arm64`. ffmpeg (yt-dlp/FFmpeg-Builds, `linux64`/`linuxarm64`) and deno are downloaded with retries, verified against the checksums published in the same release and installed root-owned.
