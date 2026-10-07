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

# Features
* **Easy Usage with Minimal Setup**
    * Quality options with env parameter
    * Included format selection argument
    * Included set of starter arguments
* **Webui Interface**
    * Manage configuration files
    * Trigger interval downloads
    * Manual downloading
    * View logs
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
| `youtubedl_webui` | `true` (`false`) | Used to enable webui feature with the ability to manage configuration files, view logs and perform manual downloads.
| `youtubedl_webuipath` | (`/`) | Set if you wish to change the path the webui is served from e.g. if you want to put the ui behind a path based reverse proxy.
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
| `WEBUI_API_TOKEN` | (none) | Bearer token for `POST /api/download` and `POST /download` (e.g. the iOS shortcut); with the token both answer JSON `{"id": "..."}`. A long random string. Does **not** grant access to config editing or logs.
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
    * Built when a new version of yt-dlp is released, after the tests passed against that version.
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
  endpoints `POST /download` and `POST /api/download`. It cannot edit the
  configuration or read logs, so a leaked token only allows starting downloads.

Credentials are read only from environment variables and compared in constant
time. Set your own values; the examples in this README are placeholders.

Because Basic Auth and bearer tokens are only confidential over HTTPS, serve
the UI behind a reverse proxy with a certificate (e.g. the Synology DSM
reverse proxy for `youtube.jannikseuss.de`) and bind the container port to
`127.0.0.1` rather than exposing it on the LAN. The UI also sets a strict CSRF
check on browser form posts and is intended for trusted networks only.

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
`POST /api/download` returns JSON (`{"id": "..."}`) with no redirect, which
suits the iOS *Shortcuts* app and the Share Sheet.

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
    * `--batch-file`, hardcoded to `/config/channels.txt`.
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
