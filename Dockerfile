FROM debian:12-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PUID="911" \
    PGID="911" \
    UMASK="022" \
    youtubedl_debug="false" \
    youtubedl_lockfile="false" \
    youtubedl_webui="false" \
    youtubedl_webuiport="8080" \
    youtubedl_subscriptions="false" \
    youtubedl_watchlater="false" \
    youtubedl_interval="3h" \
    youtubedl_quality="1080" \
    OPENSSL_CONF=

RUN set -x && \
    addgroup --gid "$PGID" abc && \
    adduser \
        --gecos "" \
        --disabled-password \
        --uid "$PUID" \
        --ingroup abc \
        --shell /bin/bash \
        abc

COPY root/app/requirements.txt /app/

RUN set -x && \
    apt update && \
    apt install -y \
        supervisor \
        file \
        wget \
        unzip \
        python3 \
        python3-venv \
        python3-pip && \
    apt clean && \
    python3 -m venv /opt/venv && \
    /opt/venv/bin/pip --no-cache-dir install -r /app/requirements.txt && \
    rm -rf \
        /var/lib/apt/lists/* \
        /tmp/*

COPY root/ /

# ffmpeg/ffprobe/ffplay from yt-dlp's FFmpeg builds for both architectures.
# Downloads are retried and checked against the checksum file published in
# the same release before extraction (this catches truncated or wrong
# responses, not a compromised release). The binaries are installed
# root-owned: the archive's files belong to uid 1001, which could be abc's
# PUID.
RUN set -eux && \
    case "$(uname -m)" in \
        x86_64) ffarch=linux64 ;; \
        aarch64) ffarch=linuxarm64 ;; \
        *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac && \
    base='https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest' && \
    file="ffmpeg-master-latest-${ffarch}-gpl.tar.xz" && \
    mkdir /tmp/ffmpeg && cd /tmp/ffmpeg && \
    wget -q --tries=5 --waitretry=10 --retry-connrefused "$base/$file" "$base/checksums.sha256" && \
    grep " $file\$" checksums.sha256 | sha256sum -c - && \
    tar -xJf "$file" --no-same-owner --strip-components=1 && \
    install -o root -g root -m 0755 bin/ffmpeg bin/ffprobe bin/ffplay /usr/local/bin/ && \
    cd / && rm -rf /tmp/*

# deno (JavaScript runtime used by yt-dlp for YouTube), same safeguards.
RUN set -eux && \
    base='https://github.com/denoland/deno/releases/latest/download' && \
    file="deno-$(uname -m)-unknown-linux-gnu.zip" && \
    mkdir /tmp/deno && cd /tmp/deno && \
    wget -q --tries=5 --waitretry=10 --retry-connrefused "$base/$file" "$base/$file.sha256sum" && \
    sha256sum -c "$file.sha256sum" && \
    unzip -q "$file" && \
    install -o root -g root -m 0755 deno /usr/local/bin/ && \
    cd / && rm -rf /tmp/*

# YTDLP_VERSION pins yt-dlp for release images; empty installs the latest.
# AUTOUPDATE sets the default for the runtime self-updater (true for the
# rolling 'unstable' image, false otherwise).
ARG YTDLP_VERSION=
ARG AUTOUPDATE=false
ENV youtubedl_autoupdate=$AUTOUPDATE
RUN set -x && \
    if [ -n "$YTDLP_VERSION" ]; then \
        spec="yt-dlp[default]==$YTDLP_VERSION" ; \
    else \
        spec="yt-dlp[default]" ; \
    fi && \
    /opt/venv/bin/pip --no-cache-dir install "$spec"

VOLUME /config /downloads

WORKDIR /config

EXPOSE 8080/tcp

CMD ["/entrypoint.sh"]
