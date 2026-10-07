# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is not the youtube-dl source. It is a **Docker image** (published as `jeeaaasustest/youtube-dl`) that wraps [yt-dlp](https://github.com/yt-dlp/yt-dlp) to download YouTube subscriptions or channels on an interval, with an optional FastAPI web UI. Everything under `root/` is copied into `/` of the image (`COPY root/ /`), so `root/app/...` becomes `/app/...` in the container, and so on.

There are no tests, linters or build scripts. Check changes by building and running the image:

```bash
docker build -t youtube-dl-dev .
docker run --rm -it -v "$PWD/tmp-config:/config" -v "$PWD/tmp-downloads:/downloads" \
  -e youtubedl_webui=true -e youtubedl_interval=false -p 8080:8080 youtube-dl-dev
```

`youtubedl_interval=false` makes the container run one download pass and then exit. Settings come from `youtubedl_*` env vars, whose defaults are set in the `Dockerfile` `ENV` block and documented in `README.md`.

## Runtime architecture

1. **`/entrypoint.sh`** runs every script in `/etc/cont-init.d/` in `ls` order, so the numeric prefixes set the order. It then starts supervisord.
   - `05-default-confs`: copies `/config.default/{args.conf,channels.txt}` into `/config` if they are missing. It symlinks `/config/args.conf` to `/etc/yt-dlp.conf` and replaces `$youtubedl_quality` in `/config.default/format`.
   - `10-update-args`: if the user's `/config/args.conf` matches the md5 of an **old default** `args.conf`, it is replaced with the new default. **When you change `root/config.default/args.conf`, add the md5 of the previous default to `old_default_hashes`**. Otherwise existing users with unmodified configs won't get the update.
   - `80-webui`: deletes the webui supervisor conf when `youtubedl_webui` is not `true`.
   - `90-user-permissions`: applies PUID/PGID to user `abc` and chowns `/config`, `/downloads`, `/app` and the log directories.
   - `91-supervisor-credentials`: replaces the `dummy` username/password in `supervisord.conf` with a random string.
2. **supervisord** (`/etc/supervisor/conf.d/*.conf`) runs these programs as `abc`:
   - `youtube-dl` runs `/app/youtube-dl/youtube-dl.sh`, which loops forever and tees its output to `/var/log/youtube-dl/youtube-dl.log`.
   - `youtube-dl-updater` runs `updater.sh`, which pip-upgrades yt-dlp every 3h. While it runs it creates `/tmp/updater-running`, and the main script waits for that file to disappear.
   - `youtube-dl-webui` runs `uvicorn ... youtube-dl-webui:webserver`.
   - `terminate` is not autostarted. It is started when the interval is `false`, and it kills supervisord so the container exits.

### `youtube-dl.sh` (core download loop)
- It builds a `yt-dlp` command string and runs it with `eval`, so quoting matters.
- It adds `--format` (from `/config.default/format`) and `--download-archive /config/archive.txt` **only if** `args.conf` does not already set them. `--cookies` is added when `/config/cookies.txt` exists.
- The URL list comes from `channels.txt`. Subscriptions (`/feed/channels`) and `:ytwatchlater` are appended when the matching env vars are enabled.
- Per-URL args use the `URL | extra args` syntax. The script splits the list into batches: each line containing `|` runs as its own yt-dlp call with those extra args, and the remaining plain URLs run in one batch through `/tmp/urls`.
- It runs the optional `/config/pre-execution.sh` and `/config/post-execution.sh` hooks. When `youtubedl_lockfile` is on, it creates `.youtubedl-running` and `.youtubedl-completed` files in `/downloads`.

### Web UI (`root/app/youtube-dl-webui/`)
- A single FastAPI module (`youtube-dl-webui.py`) with Jinja2 templates. Every template gets `base_path` (from `youtubedl_webuipath`, which is passed to `FastAPI(root_path=...)`), so links must use it.
- The UI edits `/config/args.conf`, `channels.txt` and `archive.txt`, and shows the main log. Manual downloads run yt-dlp as a subprocess with logs saved per download. "Restart" calls `supervisorctl restart youtube-dl`.
- `TemplateResponse(request, name, context)` uses the Starlette 1.0 signature. Keep `request` as the first argument.
- Python dependencies are listed in `root/app/requirements.txt` and installed into the venv at `/home/abc/.venv`.

## CI / release flow (`.github/workflows/`)

- `release-checker.yml` (cron, twice a day) writes the latest yt-dlp tag to `release-versions/latest.txt` and commits it as `Release: v<version>`. Most commits in the history come from this job.
- A push to `master` that changes `release-versions/latest.txt` builds the `latest` and `v<version>` tags. Both pin `yt-dlp[default]==<version>` by running `sed` on the `pip install yt-dlp[default]` line in the `Dockerfile`, **so that line must stay matchable**. The `v<version>` build deletes `updater.sh` and its supervisor conf.
- Any other push builds `unstable` (on master) or a tag named after the branch slug. On master, the build rewrites **line 6** of `updater.sh` to install yt-dlp from git, and adds `git` after `python3-pip` in the Dockerfile. Don't shift the pip line in `updater.sh` or remove `python3-pip` without updating the workflow.
- Images are built for `linux/amd64` and `linux/arm64`. The Dockerfile downloads arch-specific ffmpeg and deno builds.
