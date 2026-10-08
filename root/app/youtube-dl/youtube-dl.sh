#!/bin/bash
# The youtubedl_* variables are provided by the container environment.
# shellcheck disable=SC2154

# Shared splitter: turns a string of yt-dlp arguments into NUL-terminated
# tokens using the same shlex rules yt-dlp uses, so per-URL "| args" from
# channels.txt are parsed without eval. See app/youtube-dl-webui/ytdlp_args.py.
YTDLP_ARGS_HELPER='/app/youtube-dl-webui/ytdlp_args.py'

split_args() {
  # Splits the string $1, writes NUL-terminated tokens to stdout; fails (and
  # writes nothing) if the string cannot be parsed, e.g. an unclosed quote.
  python3 -I "$YTDLP_ARGS_HELPER" split "$1"
}

youtubedl_binary='yt-dlp'

# Base command, built as an array (no eval, no word-splitting surprises).
base_cmd=("$youtubedl_binary" --config-location '/config/args.conf')

if [ "$youtubedl_debug" = true ]; then base_cmd+=(--verbose); fi
if [ -f '/config/cookies.txt' ]; then base_cmd+=(--cookies '/config/cookies.txt'); fi
if ! grep -qPe '^(--format |-f )' '/config/args.conf'; then
  base_cmd+=(--format "$(cat '/config.default/format')")
fi
if ! grep -qPe '^--download-archive ' '/config/args.conf'; then
  base_cmd+=(--download-archive '/config/archive.txt')
fi

# Build the list of entries: channels.txt plus optional feeds.
urls_temp="$(mktemp)"
batch_file="$(mktemp)"
args_file="$(mktemp)"
trap 'rm -f "$urls_temp" "$batch_file" "$args_file"' EXIT
cat '/config/channels.txt' > "$urls_temp"
echo '' >> "$urls_temp"
if [ "$youtubedl_subscriptions" = true ]; then
  echo 'https://www.youtube.com/feed/channels' >> "$urls_temp"
fi
if [ "$youtubedl_watchlater" = true ]; then
  echo ":ytwatchlater | --playlist-end '-1' --no-playlist-reverse" >> "$urls_temp"
fi

if [ -f '/config/pre-execution.sh' ]; then
  echo '[pre-execution] running pre-execution script...'
  bash '/config/pre-execution.sh'
  echo '[pre-execution] finished pre-execution script.'
fi

while [ -f '/tmp/updater-running' ]; do sleep 1s; done
youtubedl_version="$($youtubedl_binary --version)"
youtubedl_last_run_time="$(date '+%s')"
echo ''; echo "$(date '+%Y-%m-%d %H:%M:%S') - starting execution"

if [ "$youtubedl_lockfile" = true ]; then
  touch '/downloads/.youtubedl-running' && rm -f '/downloads/.youtubedl-completed'
fi

# Separate plain URLs (run together via --batch-file) from lines carrying
# per-URL "| args" (run individually with their own parsed arguments).
: > "$batch_file"
while IFS= read -r line || [ -n "$line" ]; do
  trimmed="${line#"${line%%[![:space:]]*}"}"   # strip leading whitespace
  [ -z "$trimmed" ] && continue
  case "$trimmed" in
    \#*) continue ;;                            # comment line
  esac
  if [[ "$line" == *'|'* ]]; then
    url="${line%%|*}"
    argstr="${line#*|}"
    # Trim surrounding whitespace from the URL part.
    url="${url#"${url%%[![:space:]]*}"}"
    url="${url%"${url##*[![:space:]]}"}"
    extra=()
    if [ -n "${argstr//[[:space:]]/}" ]; then
      # Never run a URL without its limiting args (--playlist-end,
      # --match-filter, ...): skip the line if they cannot be parsed.
      if ! split_args "$argstr" > "$args_file"; then
        echo "[channels] skipping '$url': cannot parse the arguments after '|'"
        continue
      fi
      mapfile -t -d '' extra < "$args_file"
    fi
    "${base_cmd[@]}" "${extra[@]}" -- "$url"
  else
    printf '%s\n' "$line" >> "$batch_file"
  fi
done < "$urls_temp"

if [ -s "$batch_file" ]; then
  "${base_cmd[@]}" --batch-file "$batch_file"
fi

if [ "$youtubedl_lockfile" = true ]; then
  touch '/downloads/.youtubedl-completed' && rm -f '/downloads/.youtubedl-running'
fi

if [ $(( ($(date '+%s') - $youtubedl_last_run_time) / 60 )) -ge 2 ]
then
  echo ''; echo "$(date '+%Y-%m-%d %H:%M:%S') - execution took $(( ($(date '+%s') - $youtubedl_last_run_time) / 60 )) minutes"
else
  echo ''; echo "$(date '+%Y-%m-%d %H:%M:%S') - execution took $(( ($(date '+%s') - $youtubedl_last_run_time) )) seconds"
fi

if [ -f '/config/post-execution.sh' ]; then
  echo '[post-execution] running post-execution script...'
  bash '/config/post-execution.sh'
  echo '[post-execution] finished post-execution script.'
fi

echo "$youtubedl_binary version: $youtubedl_version"

if [ "$youtubedl_interval" != 'false' ]
then
  echo "waiting $youtubedl_interval.."
  # GNU sleep accepts several units ('1d 3h'), so split into words (without
  # glob expansion). A rejected value must never cause back-to-back passes
  # (YouTube may ban the IP), so fall back to the default interval.
  read -r -a interval_parts <<<"$youtubedl_interval"
  sleep "${interval_parts[@]}"
  if [ $? -eq 1 ]; then   # 1 = invalid interval (signals give >128)
    echo "invalid youtubedl_interval '$youtubedl_interval', waiting the default 3h instead.."
    sleep 3h
  fi
else
  echo "youtubedl_interval is set to 'false', container will now exit."
  # 'terminate' sends SIGQUIT to supervisord, which then stops every program
  # and exits with 0. Never stop programs from here: this script belongs to
  # the youtube-dl process group, which supervisor stops as a whole, so
  # 'supervisorctl stop all' would end it before 'terminate' is started.
  # Wait to be stopped instead of exiting: on exit, autorestart would begin
  # another pass before the shutdown.
  supervisorctl start terminate
  sleep infinity
fi
