#!/bin/bash
# The youtubedl_* variables are provided by the container environment.
# shellcheck disable=SC2154

# Shared splitter: turns a string of yt-dlp arguments into NUL-terminated
# tokens using the same shlex rules yt-dlp uses, so per-URL "| args" from
# channels.txt are parsed without eval. See app/youtube-dl-webui/ytdlp_args.py.
YTDLP_ARGS_HELPER='/app/youtube-dl-webui/ytdlp_args.py'
# Status events for the web UI's dashboard (only while the web UI is enabled),
# see app/youtube-dl-webui/status_events.py.
STATUS_HELPER='/app/youtube-dl-webui/status_events.py'
STATUS_DIR='/config/.youtube-dl-status'
PASS_LOG='/var/log/youtube-dl/youtube-dl.log'

split_args() {
  # Splits the string $1, writes NUL-terminated tokens to stdout; fails (and
  # writes nothing) if the string cannot be parsed, e.g. an unclosed quote.
  python3 -I "$YTDLP_ARGS_HELPER" split "$1"
}

status_helper() {
  # Any call may fail. The pass then runs without the event flags (or
  # without that marker), exactly as with the web UI disabled.
  python3 -I "$STATUS_HELPER" "$@"
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
args_file="$(mktemp)"
ev_file="$(mktemp)"
trap 'rm -f "$urls_temp" "$args_file" "$ev_file"' EXIT
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

# Every entry runs as its own yt-dlp process, so each one has its own exit
# code and the dashboard can show 'channel j of m'.
entries=()
while IFS= read -r line || [ -n "$line" ]; do
  line="${line#$'\xef\xbb\xbf'}"                # byte order mark
  trimmed="${line#"${line%%[![:space:]]*}"}"   # strip leading whitespace
  [ -z "$trimmed" ] && continue
  case "$trimmed" in
    \#*) continue ;;                            # comment line
  esac
  # Plain URLs used to go through yt-dlp's --batch-file, which also skips
  # lines starting with ';' or ']'.
  if [[ "$trimmed" != *'|'* ]]; then
    case "$trimmed" in
      \;*|\]*) continue ;;
    esac
  fi
  entries+=("$line")
done < "$urls_temp"

ev_run=''
ev_nonce=''
if [ "$youtubedl_webui" = true ] &&
   status_helper begin --status-dir="$STATUS_DIR" --channels="${#entries[@]}" \
     --interval="$youtubedl_interval" --pid="$$" --ytdlp="$youtubedl_version" \
     > "$ev_file"; then
  mapfile -t -d '' ev_ids < "$ev_file"
  if [ "${#ev_ids[@]}" -eq 2 ] && [[ "${ev_ids[0]}" =~ ^[0-9a-f]{16,32}$ ]] &&
     [[ "${ev_ids[1]}" =~ ^[0-9a-f]{16,32}$ ]]; then
    ev_run="${ev_ids[0]}"
    ev_nonce="${ev_ids[1]}"
  fi
fi
ev_common=(--status-dir="$STATUS_DIR" --run="$ev_run" --nonce="$ev_nonce")

worst_rc=0
idx=0
for line in "${entries[@]}"; do
  idx=$((idx + 1))
  extra=()
  skip_reason=''
  if [[ "$line" == *'|'* ]]; then
    url="${line%%|*}"
    argstr="${line#*|}"
    # Trim surrounding whitespace from the URL part.
    url="${url#"${url%%[![:space:]]*}"}"
    url="${url%"${url##*[![:space:]]}"}"
    if [ -n "${argstr//[[:space:]]/}" ]; then
      # Never run a URL without its limiting args (--playlist-end,
      # --match-filter, ...): skip the line if they cannot be parsed.
      if split_args "$argstr" > "$args_file"; then
        mapfile -t -d '' extra < "$args_file"
      else
        skip_reason="cannot parse the arguments after '|'"
      fi
    fi
  else
    # A plain URL, cleaned up like yt-dlp's --batch-file reader did: no
    # surrounding whitespace and no ' #comment' at the end.
    url="${line#"${line%%[![:space:]]*}"}"
    url="${url%%[[:space:]]#*}"
    url="${url%"${url##*[![:space:]]}"}"
  fi

  # Event flags go after the entry's own arguments, so they win over them
  # and over args.conf, and before '--'.
  ev_args=()
  if [ -n "$ev_nonce" ]; then
    : > "$ev_file"
    if status_helper channel-start "${ev_common[@]}" --index="$idx" \
         --total="${#entries[@]}" --url="$url" --flags-out="$ev_file"; then
      mapfile -t -d '' ev_args < "$ev_file"
    fi
  fi

  entry_start=$SECONDS
  rc=0
  if [ -n "$skip_reason" ]; then
    echo "[channels] skipping '$url': $skip_reason"
    rc=2
  else
    "${base_cmd[@]}" "${extra[@]}" "${ev_args[@]}" -- "$url" || rc=$?
  fi
  # 101: yt-dlp stopped early on purpose (--break-on-existing,
  # --max-downloads), which is not a failure.
  if [ "$rc" -ne 101 ] && [ "$rc" -gt "$worst_rc" ]; then worst_rc=$rc; fi

  if [ -n "$ev_nonce" ]; then
    status_helper channel-end "${ev_common[@]}" --index="$idx" --rc="$rc" \
      --secs="$((SECONDS - entry_start))" --error="$skip_reason"
  fi
done

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

if [ -n "$ev_nonce" ]; then
  # 'end' waits until tee has written this marker to the pass log, then
  # summarises the log into a run_end event with the time of the next pass.
  echo "@@ytdl:$ev_nonce pass_end"
  status_helper end "${ev_common[@]}" --rc="$worst_rc" \
    --interval="$youtubedl_interval" --log="$PASS_LOG"
fi

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
