# youtube-dl

**Automated yt-dlp Docker image for downloading YouTube subscriptions**

This is a security-hardened fork of
[Jeeaaasus/youtube-dl](https://github.com/Jeeaaasus/youtube-dl) (MIT). It adds
authentication, CSRF protection and input validation to the web UI, removes
shell injection in the download scripts, and publishes images to the GitHub
Container Registry. See [SECURITY-REVIEW.md](SECURITY-REVIEW.md) for the full
list of findings and fixes.

Images: `ghcr.io/jannik000/youtube-dl`.  
yt-dlp documentation [here](https://github.com/yt-dlp/yt-dlp).

> **Upgrading from upstream?** The web UI now **requires credentials** and will
> not start without them (`WEBUI_USERNAME`/`WEBUI_PASSWORD` and/or
> `WEBUI_API_TOKEN`). The self-updater is now **opt-in**
> (`youtubedl_autoupdate=true`). Shell command substitution `$(...)` inside
> `--output` in `args.conf` is no longer evaluated — use yt-dlp output
> templates such as `%(upload_date>%Y)s` instead.
>
> **First images (repository owner):** `:latest` and `:v<VERSION>` are built by
> the *Release Images* workflow, not by merging. GitHub disables scheduled
> workflows in forks, so after the first merge open the Actions tab, enable
> *Release Images* and run it once (*Run workflow* on `master`). Before
> switching a container, check that the image exists:
> `docker manifest inspect ghcr.io/jannik000/youtube-dl:latest`.
>
> **Upgrading to the dashboard release?** The web UI is a new [dashboard](#dashboard)
> (Semantic UI and every CDN request are gone). Each `channels.txt` line now
> runs as its own yt-dlp process, so `--break-on-existing` and `--max-downloads`
> count per line ([details](#one-yt-dlp-per-line)). Manual downloads wait in a
> queue and now use `cookies.txt` and the download archive like scheduled
> passes ([details](#manual-downloads)). While the web UI is enabled, the
> container log gets extra progress and marker lines.

# Features
* **Easy Usage with Minimal Setup**
    * Quality options with env parameter
    * Included format selection argument
    * Included set of starter arguments
* **Webui Interface**
    * Live [dashboard](#dashboard): status cards, progress of the current pass and of manual downloads, per-channel table, recent runs, failures that need attention, cookie health
    * Manual downloading through a queue (browser form or iOS Shortcut), with cancel, retry and re-download
    * Manage configuration files
    * Trigger interval downloads (*Run now* stops the running pass, including a download in progress, and starts a new one; it asks first while a pass runs)
    * View logs (live tail with filters)
* **Automatic Updates**
    * Automated image building on every yt-dlp release
    * Optional self updating container (opt-in)
* **Automatic Downloads**
    * Interval options with env parameter
    * Channel URLs from file
* **PUID/PGID**
* **yt-dlp Options**
    * SponsorBlock
    * Format
    * Quality
    * High fps videos
    * Download archive
    * Output
    * Subtitles
    * Thumbnails
    * Geo bypass
    * Proxy support
    * Metadata
    * Etc

# Quick Start

A [docker-compose example](#docker-compose) is further down; the commands below
are the equivalent `docker run` form.

"I want to download all my subscriptions and my watch later playlist in 4k and also enable the webui to manage youtube-dl"
```
docker run -d \
    --name youtube-dl \
    -v youtube-dl_data:/config \
    -v <PATH>:/downloads \
    -e youtubedl_subscriptions=true \
    -e youtubedl_watchlater=true \
    -e youtubedl_quality=2160 \
    -e youtubedl_webui=true \
    -e WEBUI_USERNAME=<choose-a-username> \
    -e WEBUI_PASSWORD=<choose-a-strong-password> \
    -e WEBUI_API_TOKEN=<a-long-random-token> \
    -p 8080:8080 \
    ghcr.io/jannik000/youtube-dl
```
The web UI needs credentials — see [Web UI authentication](#web-ui-authentication).
Then add your cookies as explained in the [Configure youtube-dl](#configure-youtube-dl) section below.

<br>

"I want to download only certain channels"
```
docker run -d \
    --name youtube-dl \
    -v youtube-dl_data:/config \
    -v <PATH>:/downloads \
    ghcr.io/jannik000/youtube-dl
```
Then configure the channels as explained in the [Configure youtube-dl](#configure-youtube-dl) section below.

**Explanation**
* `-v youtube-dl_data:/config`  
  This makes a Docker volume where your config files are saved, named: `youtube-dl_data`.

* `-v <PATH>:/downloads`  
  This makes a bind mount where the videos are downloaded.  
  This is where on your Docker host you want youtube-dl to download videos.  
  Replace `<PATH>`, example: `-v /media/youtube-dl:/downloads`

# Env Parameters
`-e <Parameter>=<Value>`

| Parameter | Value (Default) | What it does
| :---: | :---: | :--- |
| `TZ` | `Europe/London` | Specify TimeZone for the log timestamps to be correct.
| `PUID` | (`911`) | If you need to specify UserID for file permission reasons.
| `PGID` | (`911`) | If you need to specify GroupID for file permission reasons.
| `UMASK` | (`022`) | If you need to specify umask for file permission reasons.
| `youtubedl_debug` | `true` (`false`) | Used to enable verbose mode.
| `youtubedl_lockfile` | `true` (`false`) | Used to enable youtubedl-running, youtubedl-completed files in downloads directory. Useful for external scripts.
| `youtubedl_webui` | `true` (`false`) | Used to enable the web UI: the [dashboard](#dashboard), manual downloads, configuration editing and logs. While it is enabled, the downloader also writes the dashboard's status events (see [Dashboard](#dashboard)).
| `youtubedl_webuipath` | `/yt` (empty) | Set if you wish to change the path the webui is served from e.g. if you want to put the ui behind a path based reverse proxy. Must start with `/`; works whether or not the proxy strips the prefix.
| `youtubedl_webuiport` | (`8080`) | If you need to change the webui port.
| `youtubedl_subscriptions` | `true` (`false`) | If you want to download all your subscriptions. Authentication is required.
| `youtubedl_watchlater` | `true` (`false`) | If you want to download your Watch Later playlist. Authentication is required.
| `youtubedl_interval` | `1h` (`3h`) `12h` `3d` `false` | If you want to change the default download interval.<br>This can be any value compatible with [gnu sleep](https://github.com/tldr-pages/tldr/blob/main/pages/linux/sleep.md) or if set to false, the container will shutoff after executing. A low interval value risks you being ip-banned by YouTube.<br>1 hour, (3 hours), 12 hours, 3 days, false.
| `youtubedl_quality` | `720` (`1080`) `1440` `2160` | If you want to change the default download resolution.<br>720p, (1080p), 1440p, 4k.
| `youtubedl_autoupdate` | `true` (`false`) | If `true`, the container upgrades yt-dlp from PyPI every few hours while running. Off by default; the recommended way to update is to pull a new image. See [Updating](#updating).

### Web UI variables

These only apply when `youtubedl_webui=true`.

| Parameter | Value (Default) | What it does
| :---: | :---: | :--- |
| `WEBUI_USERNAME` | (none) | Username for the browser login (HTTP Basic). Required together with `WEBUI_PASSWORD` to use the browser UI.
| `WEBUI_PASSWORD` | (none) | Password for the browser login. Use a long, unique value.
| `WEBUI_API_TOKEN` | (none) | Bearer token for `POST /api/download` and `POST /download` (e.g. the iOS shortcut); with the token both answer JSON `{"id", "state", "position", "message"}` (see [iOS Shortcut](#ios-shortcut)). A long random string. Does **not** grant access to the dashboard, config editing or logs.
| `WEBUI_MAX_PARALLEL_DOWNLOADS` | `2` (`1`) | How many [manual downloads](#manual-downloads) run at the same time, 1 to 3 (larger numbers count as 3, anything else as 1). Further ones wait in a queue of at most 20. One manual download may already run next to the scheduled pass; more parallel downloads mean more requests to YouTube.
| `WEBUI_READONLY` | `true` (`false`) | If `true`, the web UI cannot edit `args.conf`, `channels.txt` or `archive.txt`; they are shown read-only.
| `WEBUI_ALLOWED_DOMAINS` | (none) | Comma-separated allowlist of domains accepted by the download endpoints, e.g. `youtube.com,youtu.be`. Subdomains are included. Unset means any `http(s)` host.
| `WEBUI_ALLOW_UNSAFE_ARGS` | `true` (`false`) | If `true`, disables validation of saved `args.conf`/`channels.txt`. Only enable if you understand that options such as `--exec` then allow arbitrary command execution.

The web UI must have either `WEBUI_USERNAME`+`WEBUI_PASSWORD`, or
`WEBUI_API_TOKEN`, or both. If it is enabled without any credentials it
refuses to start (fail closed) and logs the reason.

# Image Tags
All images are published to `ghcr.io/jannik000/youtube-dl`.
* **`unstable`**
    * Built on every push to `master`, after all tests (unit, shellcheck, end-to-end) passed.
    * Has the self-updater enabled, so it tracks the newest yt-dlp from git while running.
* **`latest`**
    * Built when a new version of yt-dlp is released (or when the image for the current version is
      missing), after the tests passed against that version. The release check runs twice a day.
    * yt-dlp is pinned to that release; update by pulling a new image.
* **`v<VERSION>`**
    * Same build as `latest`, tagged with the yt-dlp version.
    * Pinned to that exact version. The self-updater is included but off; setting
      `youtubedl_autoupdate=true` makes the container leave the pinned version.

No images are published for other branches or pull requests.

# Web UI authentication
The web UI is protected by two independent mechanisms:

* **Browser:** HTTP Basic Auth using `WEBUI_USERNAME` and `WEBUI_PASSWORD`.
* **API / automation:** a bearer token in the `Authorization` header
  (`Authorization: Bearer <WEBUI_API_TOKEN>`), accepted only on the download
  endpoints `POST /download` and `POST /api/download`. It cannot read the
  dashboard, its status API or logs, cancel or retry jobs, or edit the
  configuration, so a leaked token only allows queueing downloads.

| Request | Basic auth | API token | No credentials |
| :--- | :---: | :---: | :---: |
| `POST /download`, `POST /api/download` | yes (browser form needs the CSRF token) | yes (JSON answer) | 401 |
| Pages, `GET /api/status`, `GET /api/log/...`, `GET /log/...` | yes | 401 | 401 |
| Cancel, retry, *Download now*, *Run now*, saving files | yes, with CSRF token | 401 | 401 |
| `/static/app.js`, `/static/app.css`, `/favicon.ico` | public | public | public |

Credentials are read only from environment variables and compared in constant
time. Set your own values; the examples in this README are placeholders.

Because Basic Auth and bearer tokens are only confidential over HTTPS, serve
the UI behind a reverse proxy with a certificate (e.g. the Synology DSM
reverse proxy for `youtube.jannikseuss.de`) and bind the container port to
`127.0.0.1` rather than exposing it on the LAN. The UI also sets a strict CSRF
check on browser form posts and is intended for trusted networks only.

The pages load nothing from other hosts. The Content Security Policy is
`default-src 'none'` with only the app itself allowed for scripts, styles,
images and requests (no inline scripts or styles, no CDN, system fonts).

# Dashboard
The start page of the web UI (`youtubedl_webui=true`) shows what the
container is doing. It refreshes itself every 2 s while something runs
(otherwise every 10 s) and pauses while the browser tab is hidden. It works
on a phone, a tablet and a desktop, in light and dark mode.

**Status cards**

| Card | What it shows |
| :--- | :--- |
| Scheduler | Running (since when), sleeping until the next pass, interrupted, or finished (one-shot mode). |
| Last run | Start, duration and result of the newest finished pass: new videos, or failures and the exit code. |
| Last 24 h | Videos downloaded in the last 24 hours, split into scheduled and manual, with a 7-day mini chart. |
| This run | New, skipped, failed and filtered videos of the running pass (or of the last one while idle). Tagged *from log*, see the limits below. |
| Pending | Manual downloads waiting in the queue; during a pass also the channels left and "≤ *k* more in this channel". Tagged *estimate*. |
| Disk | Free space under `/downloads`; warns below 20 GB or 5 %. |
| Cookies | Whether `/config/cookies.txt` exists and has YouTube login cookies, when they expire, and whether YouTube asked for a bot check or rotated them. |

**Panels**
* **Now:** the current video (title, channel, phase, percent, speed, ETA) and
  an overall bar for the pass.
* **Manual downloads:** the running download with its own bars, the queue
  (cancel), and finished ones with *Retry*, *Re-download* and their log. A
  job page (`/download/<id>`) shows one download with its log.
* **Needs attention:** failures of the current and the last pass and failed
  manual downloads, grouped by cause (bot check, rate limit, forbidden,
  unavailable, private or members-only, geo-blocked, disk full, cookies
  expired) with a one-line hint. Videos
  with a YouTube id get *Download now*, which queues the watch URL as a
  manual download.
* **Recently downloaded**, **Channels** (per `channels.txt` line: last
  checked, last new video, last result; a channel without a new video for
  90 days is marked stale) and **Recent runs** (the last 10 passes).
* **Log:** the pass log or a download's log, tailed live, with filters
  (errors and warnings, hide archive skips), follow, copy and download.

**Honest limits**
* *Pending* is never an exact count. yt-dlp only learns what is new by
  fetching a channel, so channels not reached yet are unknown, and
  "≤ *k* more" is an upper bound (archived videos are skipped instantly).
  Queued manual downloads and channels left are exact.
* *from log*: new and failed videos come from yt-dlp's own events. Skipped
  and filtered counts and the cause of a failure come from yt-dlp's log
  lines. A video title can imitate such lines and inflate them; with
  `--quiet` in `args.conf` they read 0.
* Counters start at 0 after the upgrade (`archive.txt` has no dates), and
  the 24-hour boundary uses yt-dlp's extraction time.
* Channels and recent runs appear after the first pass with the web UI
  enabled has finished.
* The dashboard keeps its state in the web UI's single process: uvicorn
  runs with one worker. Do not add workers, or the queue and the
  dashboard break.

**Without JavaScript** the page says that live status needs JavaScript; the
download form and the editors still work. Browser navigations get HTML error
pages (for example the confirmation for *Run now* while a pass runs); API
clients keep getting JSON.

**Status files.** While the web UI is enabled, the downloader writes events to
`/config/.youtube-dl-status/events.jsonl` (directory 0700, files 0600, owned
by `PUID`; rotated at 2 MiB, one `.1` kept), and manual download logs go to
`/config/.youtube-dl-status/jobs/` (newest 50 kept). With the web UI off,
nothing is written there and the downloader runs as before.

**Extra container log lines.** While the web UI is enabled, `docker logs` and
`/var/log/youtube-dl/youtube-dl.log` get a progress line every 2 seconds
during a download, a few `[postprocess]` lines and two `[info] Writing ...`
lines per video, and one marker line per channel. They carry a random
per-pass key that only the server knows, so a video title cannot fake them.
The log views of the web UI strip these lines and the key; they return
complete lines only.

# Manual downloads
URLs from the dashboard's form or from the [iOS Shortcut](#ios-shortcut) go
into a queue:

* `WEBUI_MAX_PARALLEL_DOWNLOADS` downloads (default 1) run at a time, at most
  20 wait. A full queue answers `429 Too Many Requests` with `Retry-After: 60`.
* A URL that is already queued or running is not added twice; you get the
  existing download back.
* Manual downloads use `/config/cookies.txt` if it exists and the download
  archive `/config/archive.txt` (unless `args.conf` sets its own
  `--download-archive`), like the scheduled passes. A YouTube video that is
  already in the archive is answered at once with "Already downloaded",
  without starting yt-dlp. Each manual download gets a private copy of
  `cookies.txt` (deleted when it ends), because yt-dlp rewrites its cookie
  file on exit and parallel runs would wipe each other's cookies; only the
  scheduled pass updates `/config/cookies.txt`.
* A download where `args.conf`'s `--match-filter` (by default `'!is_live'`)
  skipped everything ends as *failed* with that reason, not as finished.
* **Re-download anyway** (checkbox in the form, *Re-download* on a finished
  download) runs yt-dlp without any archive (`--no-download-archive`), so
  the video is fetched again and not recorded in the archive.
* Running and queued downloads can be cancelled; failed, cancelled and
  interrupted ones can be retried (the URL is validated again).
* If the web UI restarts, queued downloads are queued again and a running one
  is shown as interrupted, with *Retry*.

# docker-compose
```yaml
services:
  youtube-dl:
    image: ghcr.io/jannik000/youtube-dl:latest
    container_name: youtube-dl
    environment:
      TZ: Europe/Berlin
      PUID: "1026"
      PGID: "100"
      youtubedl_webui: "true"
      youtubedl_subscriptions: "true"
      youtubedl_quality: "1080"
      # Web UI credentials — replace the placeholders with your own values.
      WEBUI_USERNAME: "CHANGE_ME"
      WEBUI_PASSWORD: "CHANGE_ME_long_unique_password"
      WEBUI_API_TOKEN: "CHANGE_ME_long_random_token"
      # Optional hardening:
      WEBUI_ALLOWED_DOMAINS: "youtube.com,youtu.be"
      # WEBUI_READONLY: "true"
    volumes:
      - youtube-dl_data:/config
      - /volume1/media/youtube-dl:/downloads
    # Bind to localhost and put the UI behind an HTTPS reverse proxy.
    ports:
      - "127.0.0.1:8080:8080"
    restart: unless-stopped

volumes:
  youtube-dl_data:
```
Do not commit real passwords or tokens. Keep them in a local `.env` file or
your Synology's environment settings, not in a file you push to GitHub.

# iOS Shortcut
`POST /api/download` returns JSON with no redirect, which suits the iOS
*Shortcuts* app and the Share Sheet:

```json
{"id": "6f1c...", "state": "queued", "position": 2, "message": "Queued (position 2)."}
```

`state` is `running`, `queued` or `already` (in the download archive);
`position` is the place in the queue (`null` when running), and `message` is
a sentence meant for the user. To see it, add **Get Dictionary Value**
(`message`) and **Show Notification** after the request. Errors answer with a
`detail` text: `400` for a URL that is not accepted, `429` when the queue is
full (20 waiting; `Retry-After: 60`, with `message` too), `503` while the web
UI shuts down.
The token cannot read the job's progress; open the dashboard for that.

1. Create a shortcut that accepts URLs from the Share Sheet.
2. Add a **Get Contents of URL** action:
   * **URL:** `https://youtube.jannikseuss.de/api/download`
   * **Method:** `POST`
   * **Headers:** `Authorization` = `Bearer YOUR_WEBUI_API_TOKEN`
   * **Request Body:** `Form`, with one field `url` set to the Shortcut Input.

Because the request carries the bearer token, it is exempt from the browser
CSRF check. The browser form in the UI uses a CSRF token instead and does not
need the bearer token.

An existing shortcut that posts to `/download` keeps working once it sends the
`Authorization` header; with a token, `/download` also answers JSON instead of
redirecting. Leading or trailing whitespace in the shared URL is ignored.

# Updating
By default the container does not update yt-dlp by itself. To update, pull a
newer image (`docker compose pull && docker compose up -d`); a fresh image is
built automatically whenever yt-dlp publishes a release. If you prefer the old
behaviour of upgrading yt-dlp inside the running container, set
`youtubedl_autoupdate=true` — be aware this installs unpinned code from PyPI at
runtime.

# Configure youtube-dl
* **Authentication**

    Cookies are used to authenticate your YouTube account which is necessary if you want to download members only videos you have access to or your own private videos and playlists like Watch Later.

    In order to pass your cookies to youtube-dl, you first need a browser extension to extract your cookies, for example, [Get cookies.txt](https://chrome.google.com/webstore/detail/get-cookiestxt/bgaddhkoddajcdgocldbbfleckgcbcid/) (Chrome) or [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/) (Firefox).  
    Once you've extracted your cookies, place the `cookies.txt` file inside your Docker volume named `youtube-dl_data` or the folder `/config/` inside your container. One way of doing this would be using the command: `docker cp ./cookies.txt youtube-dl:/config/`.  
    Then youtube-dl will find and use the `cookies.txt` file automatically.

* **channels.txt**

    File location: `/config/channels.txt`.  
    This is where you input all the YouTube channels (or any valid URL) you want to have videos downloaded from.
    ```
    # One per line
    # Name
    https://www.youtube.com/user/channel_username
    # Another one
    https://www.youtube.com/channel/UC0vaVaSyV14uvJ4hEZDOl0Q
    ```
    <a id="one-yt-dlp-per-line"></a>**Each line runs as its own yt-dlp process**, one after the
    other (lines starting with `#` are comments; for a plain URL without ` | ` also lines
    starting with `;` or `]` and a trailing ` #comment`, as yt-dlp's batch file reader did). Each
    line has its own exit code and shows up as its own row on the dashboard. Options that stop
    yt-dlp early therefore apply **per line**: `--break-on-existing` stops only the current line,
    and `--max-downloads N` allows *N* videos per line, not per pass. Each line costs about one
    second of yt-dlp startup. The subscriptions feed and Watch Later count as one line each.

    You can also specify additional args to be used per URL. This is done by adding args after the URL separated by the ` | ` character.  
    These will override any conflicting args from `args.conf`. They are split with shell-style quoting
    but never run through a shell (`;`, `$(...)` and variables are passed to yt-dlp literally).
    A line whose args cannot be parsed (e.g. an unclosed quote) is skipped and logged. Use absolute
    `/downloads/...` paths for output options here.
    ```
    # Examples
    # Output to 'named' folder instead of channel name
    https://www.youtube.com/channel/UC0vaVaSyV14uvJ4hEZDOl0Q | --output '/downloads/named/%(title)s.%(ext)s'

    # Use regex to only download videos matching words
    https://www.youtube.com/channel/UC0vaVaSyV14uvJ4hEZDOl0Q | --no-match-filter --match-filter '!is_live & title~=(?i)words.*to.*match'

    # Use regex to only download videos not matching words
    https://www.youtube.com/channel/UC0vaVaSyV14uvJ4hEZDOl0Q | --no-match-filter --match-filter '!is_live & title!~=(?i)words.*to.*exclude'

    # Download a whole playlist, also disable reverse download order
    https://www.youtube.com/playlist?list=D9sLB5EVaCarZ7lbpQfGch3jJuYCRt | --playlist-end '-1' --no-playlist-reverse
    ```
    Adding with Docker:  
    `docker exec youtube-dl bash -c 'echo "# NAME" >> ./channels.txt'`  
    `docker exec youtube-dl bash -c 'echo "URL" >> ./channels.txt'`

    It is recommended to use the UCID-based URLs, they look like: `/channel/UC0vaVaSyV14uvJ4hEZDOl0Q`, as the other ones might get changed.
    You find the UCID-based URL by going to a video and clicking on the uploader.

* **pre-execution.sh**

    File location: `/config/pre-execution.sh`.  
    This is an optional user defined script that is executed before youtube-dl starts downloading videos.

* **post-execution.sh**

    File location: `/config/post-execution.sh`.  
    This is an optional user defined script that is executed after youtube-dl has finished its full execution.

* **archive.txt**

    File location: `/config/archive.txt`.&nbsp;&nbsp;&nbsp;*delete to make youtube-dl forget downloaded videos*  
    This is where youtube-dl stores all previously downloaded video IDs.

* **args.conf**

    File location: `/config/args.conf`.&nbsp;&nbsp;&nbsp;*delete and restart container to restore [default arguments](https://github.com/jannik000/youtube-dl/blob/master/root/config.default/args.conf)*  
    This is where all youtube-dl execution arguments are, you can add or remove them however you like. If unmodified this file is automatically updated.

    **Validation when saving via the web UI**  
    When you save `args.conf` or `channels.txt` through the web UI, the content
    is validated with yt-dlp's own option parser and rejected if it contains
    options that would run commands or write outside `/downloads` (for example
    `--exec`, `--netrc-cmd`, `--plugin-dirs`, `--config-locations`,
    `--batch-file`, `--alias`, `--ppa`, or an output path outside `/downloads`).
    yt-dlp runs with `/config` as its working directory, so relative output
    paths are only accepted together with `-P /downloads`. This is
    a safety net, not a sandbox — see [SECURITY-REVIEW.md](SECURITY-REVIEW.md)
    for its limits. Set `WEBUI_READONLY=true` to forbid editing entirely, or
    `WEBUI_ALLOW_UNSAFE_ARGS=true` to disable validation. Editing the file
    directly on disk (not through the UI) is never validated.

    **Unsupported arguments**
    * `--config-location`, hardcoded to `/config/args.conf`.
    * `--batch-file`; URLs come from `/config/channels.txt`, one yt-dlp process per line.
    * Shell command substitution `$(...)` in `--output` is no longer evaluated
      (it used to run as a shell command). Use yt-dlp output templates such as
      `%(upload_date>%Y)s` instead.

    **Default arguments**
    * `--output '/downloads/%(uploader)s/%(title)s.%(ext)s'`, makes youtube-dl create separate folders for each channel and use the video title for the filename.
    * `--playlist-end '16'`, makes youtube-dl only download the latest 16 videos, per channel.
    * `--playlist-reverse`, makes youtube-dl download channel videos in the order they were uploaded.
    * `--match-filter '!is_live'`, makes youtube-dl ignore live streams.
    * `--windows-filenames`, restricts filenames to only Windows allowed characters.
    * `--ignore-no-formats-error`, keeps youtube-dl from crashing when trying to download a video premiere.
    * `--newline`, makes youtube-dl print download progress while a download is in progress.
    * `--progress-delta '10'`, makes youtube-dl calculate and print progress every 10 seconds.
    * `--sleep-requests '1'`, makes youtube-dl wait 1 second between requests. Be careful changing this! YouTube might feel you are making too many requests and ip ban you.
    * `--merge-output-format 'mp4'`, makes youtube-dl create mp4 video files.
    * `--sub-langs 'all,-live_chat'`, makes youtube-dl embed subtitles.
    * `--embed-metadata`, makes youtube-dl embed metadata like video description and chapters.
    * `--sponsorblock-mark 'all'`, makes youtube-dl create chapters from [SponsorBlock](https://sponsor.ajay.app/) segments.

    yt-dlp configuration options documentation [here](https://github.com/yt-dlp/yt-dlp#usage-and-options).
