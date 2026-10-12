#!/bin/bash
# End-to-end tests against the real image. Builds the image, starts containers
# in several configurations and checks behaviour from the outside (HTTP, logs,
# file ownership, processes). Used by CI; runnable locally where Docker can
# build the image:  tests/e2e/run.sh
set -uo pipefail

IMAGE="${E2E_IMAGE:-youtube-dl-e2e:test}"
PORT="${E2E_PORT:-18080}"
USER_NAME='e2e-admin'
USER_PASS='e2e-password-not-secret'
API_TOKEN='e2e-token-0123456789abcdef'
WORK="$(mktemp -d)"
FAILURES=0
CONTAINERS=()

cleanup() {
  for c in "${CONTAINERS[@]}"; do docker rm -f "$c" >/dev/null 2>&1 || true; done
  # Files in the bind mounts are owned by the container's PUID; remove via a container.
  docker run --rm -v "$WORK:/w" --entrypoint /bin/sh "$IMAGE" -c 'rm -rf /w/*' >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

pass() { echo "  PASS: $*"; }
fail() { echo "  FAIL: $*"; FAILURES=$((FAILURES + 1)); }
check() {  # check "description" command...
  local desc="$1"; shift
  if "$@"; then pass "$desc"; else fail "$desc"; fi
}

log_has() {  # log_has container grep-args...
  # Reads the whole log before grepping: with pipefail, 'docker logs | grep -q'
  # fails when grep exits on the first match and docker logs gets SIGPIPE.
  local logs
  logs="$(docker logs "$1" 2>&1)" || return 1
  shift
  grep -q "$@" <<<"$logs"
}

log_count() {  # log_count container grep-args... -> number of matching lines
  local logs
  logs="$(docker logs "$1" 2>&1)" || return 1
  shift
  grep -c "$@" <<<"$logs"
}

wait_for() {  # wait_for seconds command...
  local timeout="$1"; shift
  for _ in $(seq 1 "$timeout"); do
    if "$@" >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

http_code() {  # http_code [curl args...] URL
  curl -s -o /dev/null -w '%{http_code}' "$@"
}

csp_strict() {  # csp_strict response-headers -> the dashboard's strict CSP
  # Nothing by default, only the app itself for scripts, styles, images and
  # fetches: no external host, scheme, wildcard or 'unsafe-*' source.
  local csp
  csp="$(grep -i '^content-security-policy:' <<<"$1")" || return 1
  grep -q "default-src 'none'" <<<"$csp" && grep -q "style-src 'self'" <<<"$csp" &&
    grep -q "connect-src 'self'" <<<"$csp" &&
    ! grep -qiE "https?:|wss?:|data:|blob:|[*]|unsafe-" <<<"$csp"
}

new_config() {  # new_config name -> prints dir with config/ and downloads/
  local dir="$WORK/$1"
  mkdir -p "$dir/config" "$dir/downloads"
  chmod 777 "$dir/config" "$dir/downloads"
  echo "$dir"
}

if [ -z "${E2E_SKIP_BUILD:-}" ]; then
  # Optional extra 'docker build' arguments, e.g.
  # E2E_BUILD_ARGS='--build-arg YTDLP_VERSION=2026.08.19'
  read -r -a build_args <<<"${E2E_BUILD_ARGS:-}"
  echo "== Building $IMAGE ${E2E_BUILD_ARGS:-}"
  docker build "${build_args[@]}" -t "$IMAGE" . || { echo "build failed"; exit 1; }
fi

job_state() {  # job_state id -> state of that manual job in $BASE/api/status
  local json
  json="$(curl -sf -u "$USER_NAME:$USER_PASS" "$BASE/api/status")" || return 1
  python3 -I -c '
import json, sys
jobs = json.load(sys.stdin)["jobs"]
for job in jobs["running"] + jobs["queued"] + jobs["finished"]:
    if job["id"] == sys.argv[1]:
        print(job["state"])
' "$1" <<<"$json"
}
job_finished() { case "$(job_state "$1")" in done|failed) return 0 ;; esac; return 1; }

STATUS_DIR=/config/.youtube-dl-status
pass_events_ok() {  # pass_events_ok container -> run_start and run_end (with 'next')
  # Read as abc: the status directory is 0700.
  docker exec -u abc "$1" /opt/venv/bin/python3 -I -c '
import json, sys
seen = set()
with open(sys.argv[1], encoding="utf-8") as f:
    for line in f:
        event = json.loads(line)
        if event.get("t") == "run_start":
            seen.add("start")
        elif event.get("t") == "run_end" and isinstance(event.get("next"), int):
            seen.add("end")
sys.exit(seen != {"start", "end"})
' "$STATUS_DIR/events.jsonl"
}
pass_nonce() {  # pass_nonce container -> the nonce of the newest run_start
  docker exec -u abc "$1" /opt/venv/bin/python3 -I -c '
import json, sys
nonce = ""
with open(sys.argv[1], encoding="utf-8") as f:
    for line in f:
        event = json.loads(line)
        if event.get("t") == "run_start":
            nonce = event["nonce"]
print(nonce)
' "$STATUS_DIR/events.jsonl"
}
served_without_nonce() {  # served_without_nonce nonce path -> 200, non-empty, no nonce
  local body
  body="$(curl -sf -u "$USER_NAME:$USER_PASS" "$BASE$2")" || return 1
  [ -n "$body" ] && ! grep -qF -e "$1" <<<"$body" && ! grep -qE '@@ytdl:[0-9a-f]{16}' <<<"$body"
}

########################################################################
echo "== Scenario A: web UI with credentials, PUID=1026/PGID=100"
A="$(new_config a)"
# channels.txt carries a shell-injection payload in the '| args' part; the
# hosts are non-resolvable so no real downloads happen.
cat > "$A/config/channels.txt" <<'EOF'
# e2e
https://example.invalid/plain-channel
https://example.invalid/pwn | --playlist-end 1; touch /config/PWNED_channels
EOF
# args.conf with the removed $(...) output expansion.
cat > "$A/config/args.conf" <<'EOF'
--output "/downloads/$(touch /config/PWNED_args)%(title)s.%(ext)s"
--no-cache-dir
EOF
CA=e2e-a
CONTAINERS+=("$CA")
docker run -d --name "$CA" -p "127.0.0.1:$PORT:8080" \
  -e PUID=1026 -e PGID=100 \
  -e youtubedl_webui=true -e youtubedl_interval=1h \
  -e WEBUI_USERNAME="$USER_NAME" -e WEBUI_PASSWORD="$USER_PASS" -e WEBUI_API_TOKEN="$API_TOKEN" \
  -v "$A/config:/config" -v "$A/downloads:/downloads" "$IMAGE" >/dev/null
BASE="http://127.0.0.1:$PORT"

check "web UI comes up and demands auth (401)" \
  wait_for 90 sh -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' $BASE/)\" = 401 ]"
check "dashboard with Basic auth (200)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" "$BASE/")" = 200
check "wrong password rejected (401)" \
  test "$(http_code -u "$USER_NAME:wrong" "$BASE/")" = 401
check "API token does not open config editing (401)" \
  test "$(http_code -H "Authorization: Bearer $API_TOKEN" "$BASE/edit/args")" = 401
check "static app.js served" \
  test "$(http_code "$BASE/static/app.js")" = 200
check "static app.css served without auth" \
  test "$(http_code "$BASE/static/app.css")" = 200
check "GET /api/status with Basic auth (200)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" "$BASE/api/status")" = 200
check "API token does not read /api/status (401)" \
  test "$(http_code -H "Authorization: Bearer $API_TOKEN" "$BASE/api/status")" = 401

headers="$(curl -s -D - -o /dev/null -u "$USER_NAME:$USER_PASS" "$BASE/")"
check "CSP header with script-src 'self'" grep -qi "content-security-policy:.*script-src 'self'" <<<"$headers"
check "X-Frame-Options DENY" grep -qi 'x-frame-options: DENY' <<<"$headers"
check "CSP is default-src 'none' without external hosts or unsafe-inline" csp_strict "$headers"

csrf="$(curl -s -u "$USER_NAME:$USER_PASS" "$BASE/" | grep -o 'name="csrf_token" value="[^"]*"' | head -1 | sed 's/.*value="//; s/"$//')"
check "CSRF token present in forms" test -n "$csrf"

check "browser POST without CSRF token rejected (403)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" -H 'Sec-Fetch-Site: same-origin' --data-urlencode 'url=https://example.invalid/v' "$BASE/download")" = 403
check "cross-site POST rejected even with token (403)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" -H 'Sec-Fetch-Site: cross-site' --data-urlencode 'url=https://example.invalid/v' --data-urlencode "csrf_token=$csrf" "$BASE/download")" = 403
check "GET restart no longer allowed" \
  sh -c "c=\$(curl -s -o /dev/null -w '%{http_code}' -u '$USER_NAME:$USER_PASS' $BASE/restart-youtube-dl); [ \"\$c\" = 405 ] || [ \"\$c\" = 404 ]"
check "option-injection URL rejected (400)" \
  test "$(http_code -H "Authorization: Bearer $API_TOKEN" --data-urlencode 'url=--exec=touch /config/PWNED_opt' "$BASE/api/download")" = 400

# Shell metacharacters without whitespace ('${IFS}' instead of a space), so the
# URL passes validation and really reaches yt-dlp; it must stay inert there.
payload="https://example.invalid/x';touch\${IFS}/config/PWNED_url;'"
resp="$(curl -s -H "Authorization: Bearer $API_TOKEN" --data-urlencode "url=$payload" "$BASE/api/download")"
dl_id="$(sed -n 's/.*"id":"\([0-9a-f-]*\)".*/\1/p' <<<"$resp")"
check "API download with shell metacharacters returns an id" test -n "$dl_id"
# The iOS Shortcut shows 'message'; 'state' and 'position' say where it is.
check "token answer carries state, position and message" \
  sh -c 'printf "%s" "$1" | grep -Eq "\"state\":\"(queued|running|failed|done)\"" &&
    printf "%s" "$1" | grep -q "\"position\":" && printf "%s" "$1" | grep -q "\"message\":\""' sh "$resp"
check "download process finishes and logs" \
  wait_for 60 sh -c "curl -s -u '$USER_NAME:$USER_PASS' $BASE/log/download/$dl_id | grep -q 'Download process ended'"
# The dashboard must show the job as finished, never stuck as queued/running.
check "manual job reaches done or failed in /api/status" \
  wait_for 60 job_finished "$dl_id"
check "token on POST /download answers JSON (iOS shortcut)" \
  sh -c "curl -s -H 'Authorization: Bearer $API_TOKEN' --data-urlencode 'url=https://example.invalid/t' $BASE/download | grep -q '\"id\"'"
check "URL with surrounding whitespace accepted" \
  sh -c "curl -s -H 'Authorization: Bearer $API_TOKEN' --data-urlencode 'url= https://example.invalid/ws ' $BASE/api/download | grep -q '\"id\"'"
check "API docs disabled (404 even with auth)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" "$BASE/docs")" = 404
check "unauthenticated body is rejected before parsing (401, not 400/422)" \
  test "$(http_code -H 'Content-Type: multipart/form-data; boundary=x' --data-binary 'not multipart' "$BASE/edit/args/save")" = 401

check "saving args.conf with --exec rejected (400)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" -H 'Sec-Fetch-Site: same-origin' --data-urlencode "args_new=--exec 'touch /config/PWNED_exec'" --data-urlencode "csrf_token=$csrf" "$BASE/edit/args/save")" = 400
check "saving channels.txt with valid content works (303)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" -H 'Sec-Fetch-Site: same-origin' --data-urlencode "$(printf 'channels_new=# e2e\nhttps://example.invalid/plain-channel\n')" --data-urlencode "csrf_token=$csrf" "$BASE/edit/channels/save")" = 303

# The pass only counts if yt-dlp itself ran under supervisor: it must report
# its version and its own error for the unresolvable plain URL (the script
# prints 'execution took' even when every yt-dlp call fails).
pass_done=false
if wait_for 180 sh -c "docker logs $CA 2>&1 | grep -q 'execution took'" \
   && log_has "$CA" -E 'yt-dlp version: [0-9]{4}\.' \
   && log_has "$CA" 'ERROR: \[generic\] plain-channel'; then
  pass_done=true; pass "downloader pass completes and yt-dlp really ran"
else
  fail "downloader pass completes and yt-dlp really ran"
fi
check "channels.txt payload reached yt-dlp as literal arguments" \
  sh -c "docker logs $CA 2>&1 | grep -q \"invalid integer value: '1;'\""

# Dashboard events. Checked before the restart below, which would cut the
# pass short before its run_end. The run_end is written after the summary
# lines, so wait for it.
check "events.jsonl has the pass's run_start and run_end with 'next'" \
  wait_for 60 pass_events_ok "$CA"
check "events.jsonl owned by PUID 1026 with mode 0600, its directory 0700" \
  test "$(docker exec "$CA" stat -c '%u %a' "$STATUS_DIR" "$STATUS_DIR/events.jsonl" | tr '\n' ' ')" = '1026 700 1026 600 '
check "the pass log carries the event markers (web UI on)" log_has "$CA" -E '@@ytdl:[0-9a-f]{16}'
nonce="$(pass_nonce "$CA")"
check "the pass's nonce is known" test -n "$nonce"
for path in /api/status /log/youtube-dl '/api/log/youtube-dl?since=0' "/api/log/download/$dl_id?since=0"; do
  check "nonce never leaves the server ($path)" served_without_nonce "$nonce" "$path"
done
# Channel manager (the pass has ended: run_end above). Add, refuse unsafe
# options, remove; the file ends up as before.
check "channel list shows channels.txt with the CSRF field" \
  sh -c "curl -s -u '$USER_NAME:$USER_PASS' $BASE/edit/channels | grep -q 'example.invalid/plain-channel' && curl -s -u '$USER_NAME:$USER_PASS' $BASE/edit/channels | grep -qF 'name=\"csrf_token\" value=\"$csrf\"'"
check "adding a channel works (303) and appends one line" \
  sh -c "test \"\$(curl -s -o /dev/null -w '%{http_code}' -u '$USER_NAME:$USER_PASS' -H 'Sec-Fetch-Site: same-origin' --data-urlencode 'url=@e2e-added' --data-urlencode 'options=--playlist-end 2' --data-urlencode 'csrf_token=$csrf' $BASE/edit/channels/add)\" = 303 && grep -qxF 'https://www.youtube.com/@e2e-added | --playlist-end 2' '$A/config/channels.txt'"
check "adding a channel with --exec options is refused (400)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" -H 'Sec-Fetch-Site: same-origin' --data-urlencode 'url=@e2e-evil' --data-urlencode "options=--exec 'touch /config/PWNED_channel_add'" --data-urlencode "csrf_token=$csrf" "$BASE/edit/channels/add")" = 400
check "removing a channel works (303)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" -H 'Sec-Fetch-Site: same-origin' --data-urlencode 'line=https://www.youtube.com/@e2e-added | --playlist-end 2' --data-urlencode "csrf_token=$csrf" "$BASE/edit/channels/remove")" = 303
check "channels.txt is back as before (and has no refused channel)" \
  sh -c "grep -q 'plain-channel' '$A/config/channels.txt' && ! grep -q 'e2e-' '$A/config/channels.txt'"

# confirm=1: the pass may still be writing its summary, and while a pass
# runs, 'Run now' answers 409 unless the client confirms the abort.
check "restart via POST with CSRF token (303)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" -H 'Sec-Fetch-Site: same-origin' --data-urlencode "csrf_token=$csrf" --data-urlencode confirm=1 "$BASE/restart-youtube-dl")" = 303

# The marker checks only mean something if the payloads were actually processed.
for marker in PWNED_channels PWNED_args PWNED_url PWNED_opt PWNED_exec PWNED_channel_add; do
  if [ "$pass_done" = true ]; then
    check "no injected command ran ($marker absent)" test ! -e "$A/config/$marker"
  else
    fail "no injected command ran ($marker): cannot verify, downloader pass did not complete"
  fi
done

supervisorctl() { docker exec "$1" supervisorctl -c /etc/supervisor/supervisord.conf "${@:2}"; }
owner_of_pid() {  # owner_of_pid container program -> user owning the program's process
  local pid
  pid="$(supervisorctl "$1" pid "$2")"
  docker exec "$1" stat -c %U "/proc/$pid"
}
# Negative checks must not pass just because the container is dead: each one
# also requires a positive signal from the running container.
updater_absent() {
  local s
  s="$(supervisorctl "$1" status 2>&1)" || true   # non-zero while 'terminate' is STOPPED
  grep -q '^youtube-dl ' <<<"$s" && ! grep -q 'youtube-dl-updater' <<<"$s"
}
count_cmdline() {  # count_cmdline container 'argv joined by spaces' -> count
  # procps is not in the image, so scan /proc. The whole argv is compared, so
  # the outer 'bash -c "youtube-dl.sh ... | tee ..."' (and this sh) never match.
  # shellcheck disable=SC2016  # expanded inside the container
  docker exec "$1" sh -c 'n=0; for f in /proc/[0-9]*/cmdline; do
      [ "$(tr "\0" " " 2>/dev/null < "$f")" = "$1 " ] && n=$((n + 1)); done
    echo "$n"' sh "$2"
}
one_downloader_pass() {  # exactly one youtube-dl.sh and one tee writing its log
  [ "$(count_cmdline "$1" '/bin/bash /app/youtube-dl/youtube-dl.sh')" = 1 ] &&
    [ "$(count_cmdline "$1" 'tee /var/log/youtube-dl/youtube-dl.log')" = 1 ]
}
downloader_spawns() {  # downloader_spawns container -> times supervisord started it
  # supervisord (nodaemon) logs to the container's stdout; the closing quote
  # keeps youtube-dl-webui and youtube-dl-updater out of the count.
  log_count "$1" "spawned: 'youtube-dl'"
}
downloader_restarted() { [ "$(downloader_spawns "$1")" -ge 2 ]; }

check "abc has UID 1026" test "$(docker exec "$CA" id -u abc)" = 1026
check "/app is root-owned" test "$(docker exec "$CA" stat -c %U /app/youtube-dl-webui/youtube-dl-webui.py)" = root
check "venv is root-owned (updater off)" test "$(docker exec "$CA" stat -c %U /opt/venv/pyvenv.cfg)" = root
check "abc can use but not replace the venv" \
  docker exec -u abc "$CA" sh -c 'test -x /opt/venv/bin/python3 && ! mv /opt/venv /opt/venv.x 2>/dev/null && ! touch /opt/venv/bin/e2e 2>/dev/null'
# shellcheck disable=SC2016  # expanded inside the container
check "root processes (supervisord) do not have the venv on PATH" \
  docker exec "$CA" sh -c 'p="$(tr "\0" "\n" < /proc/$(cat /etc/supervisor/supervisord.pid)/environ | grep "^PATH=")"; [ -n "$p" ] && ! echo "$p" | grep -q venv'
check "abc can read but not modify /app" \
  docker exec -u abc "$CA" sh -c 'f=/app/youtube-dl-webui/youtube-dl-webui.py; test -r "$f" && ! (echo x >> "$f") 2>/dev/null'
check "abc can write its HOME" docker exec -u abc "$CA" sh -c 'touch /home/abc/.e2e && rm /home/abc/.e2e'
check "ffmpeg, ffprobe and deno present, everything in /usr/local/bin root-owned" \
  docker exec "$CA" sh -c 'for b in ffmpeg ffprobe deno; do test -x /usr/local/bin/$b || exit 1; done; test -z "$(find /usr/local/bin -mindepth 1 ! -user root)"'
# Read as abc: root in 'docker exec' lacks CAP_SYS_PTRACE and may not read the
# environ of another user's process.
# shellcheck disable=SC2016  # expanded inside the container
check "downloader gets venv PATH and HOME=/home/abc from supervisor" \
  docker exec -u abc "$CA" sh -c 'e="$(tr "\0" "\n" < /proc/$(supervisorctl -c /etc/supervisor/supervisord.conf pid youtube-dl)/environ)"; echo "$e" | grep -q "^PATH=/opt/venv/bin:" && echo "$e" | grep -q "^HOME=/home/abc$"'
check "abc can run yt-dlp from the venv" docker exec -u abc "$CA" yt-dlp --version
check "web UI runs as abc" test "$(owner_of_pid "$CA" youtube-dl-webui)" = abc
check "downloader runs as abc" test "$(owner_of_pid "$CA" youtube-dl)" = abc
check "updater not running by default" updater_absent "$CA"
# POST /restart-youtube-dl answers 303 whatever supervisorctl does, so require
# that supervisord really started the downloader a second time.
check "restart really restarted the downloader" wait_for 30 downloader_restarted "$CA"
check "downloader RUNNING after restart" \
  wait_for 30 sh -c "docker exec $CA supervisorctl -c /etc/supervisor/supervisord.conf status youtube-dl | grep -q RUNNING"
# The restart must stop the old pass's whole process group (stopasgroup), not
# only 'bash -c': its youtube-dl.sh and tee would keep running next to the new.
check "restart leaves exactly one downloader pass (old process group gone)" \
  wait_for 30 one_downloader_pass "$CA"

########################################################################
echo "== Scenario B: web UI enabled without credentials (fail closed)"
B="$(new_config b)"
CB=e2e-b
CONTAINERS+=("$CB")
docker run -d --name "$CB" -p "127.0.0.1:$((PORT + 1)):8080" \
  -e youtubedl_webui=true -e youtubedl_interval=1h \
  -v "$B/config:/config" -v "$B/downloads:/downloads" "$IMAGE" >/dev/null
check "fail-closed message logged" \
  wait_for 60 sh -c "docker logs $CB 2>&1 | grep -q 'Refusing to start the'"
sleep 5
check "web UI not reachable while the container runs" \
  sh -c "[ \"\$(docker inspect -f '{{.State.Running}}' $CB)\" = true ] && [ \"\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:$((PORT + 1))/)\" = 000 ]"
check "downloader still running" \
  sh -c "docker exec $CB supervisorctl -c /etc/supervisor/supervisord.conf status youtube-dl | grep -q RUNNING"

########################################################################
echo "== Scenario C: PUID=0 is refused"
C="$(new_config c)"
CC=e2e-c
CONTAINERS+=("$CC")
docker run -d --name "$CC" -e PUID=0 \
  -v "$C/config:/config" -v "$C/downloads:/downloads" "$IMAGE" >/dev/null
check "container exits" wait_for 60 sh -c "[ \"\$(docker inspect -f '{{.State.Running}}' $CC)\" = false ]"
check "exit code non-zero" sh -c "[ \"\$(docker inspect -f '{{.State.ExitCode}}' $CC)\" != 0 ]"
check "PUID refusal logged" sh -c "docker logs $CC 2>&1 | grep -q 'PUID/PGID must not be 0'"

########################################################################
echo "== Scenario D: self-updater opt-in and base path"
D="$(new_config d)"
CD=e2e-d
CONTAINERS+=("$CD")
docker run -d --name "$CD" -p "127.0.0.1:$((PORT + 2)):8080" \
  -e youtubedl_autoupdate=true -e youtubedl_interval=1h \
  -e youtubedl_webui=true -e youtubedl_webuipath=/yt \
  -e WEBUI_USERNAME="$USER_NAME" -e WEBUI_PASSWORD="$USER_PASS" \
  -v "$D/config:/config" -v "$D/downloads:/downloads" "$IMAGE" >/dev/null
BASE_D="http://127.0.0.1:$((PORT + 2))"
check "web UI under base path (200)" \
  wait_for 90 sh -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -u '$USER_NAME:$USER_PASS' $BASE_D/yt/)\" = 200 ]"
check "static under base path (200)" test "$(http_code "$BASE_D/yt/static/app.js")" = 200
check "stylesheet under base path (200)" test "$(http_code "$BASE_D/yt/static/app.css")" = 200
check "page links use the base path" \
  sh -c "curl -s -u '$USER_NAME:$USER_PASS' $BASE_D/yt/ | grep -q 'src=\"/yt/static/app.js\"'"
# supervisor reports RUNNING only after startsecs (1 s), so wait for it.
check "updater running when enabled" \
  wait_for 30 sh -c "docker exec $CD supervisorctl -c /etc/supervisor/supervisord.conf status youtube-dl-updater | grep -q RUNNING"
check "venv owned by abc when updater enabled" \
  test "$(docker exec "$CD" stat -c %U /opt/venv/pyvenv.cfg)" = abc

########################################################################
echo "== Scenario E: youtubedl_interval=false runs one pass, then the container exits"
E="$(new_config e)"
CE=e2e-e
CONTAINERS+=("$CE")
# Web UI off (the default). One unresolvable URL, so yt-dlp really runs.
printf '%s\n' 'https://example.invalid/oneshot-channel' > "$E/config/channels.txt"
docker run -d --name "$CE" -e youtubedl_interval=false \
  -v "$E/config:/config" -v "$E/downloads:/downloads" "$IMAGE" >/dev/null
check "container exits by itself" \
  wait_for 120 sh -c "[ \"\$(docker inspect -f '{{.State.Running}}' $CE)\" = false ]"
check "exit announced in the log" log_has "$CE" 'container will now exit'
# Without the web UI there are no dashboard events: no markers or flags in the
# log and no status directory (positive signal: yt-dlp ran the entry).
web_ui_off_without_events() {
  log_has "$1" 'ERROR: \[generic\] oneshot-channel' && ! log_has "$1" '@@ytdl:' &&
    ! log_has "$1" 'Writing .{"v":1' && test ! -e "$2/config/.youtube-dl-status"
}
check "web UI off: no event markers in the log, no status directory" \
  web_ui_off_without_events "$CE" "$E"
# On exit, autorestart must not have begun a second pass before the shutdown.
# Count supervisord's spawns: whether a second pass gets as far as printing
# 'starting execution' before the shutdown stops it is a race.
check "exactly one pass ran (one spawn, one start)" \
  test "$(downloader_spawns "$CE") $(log_count "$CE" 'starting execution')" = '1 1'
# 'terminate' sends SIGQUIT; supervisord then stops all programs and exits 0.
# Docker reports exit code 0 for a running container too, so check both.
check "exited with code 0" \
  test "$(docker inspect -f '{{.State.Running}} {{.State.ExitCode}}' "$CE")" = 'false 0'

########################################################################
echo "== Scenario F: credentials from files (*_FILE), PUID=1032/PGID=65537"
echo "image config: $(docker image inspect "$IMAGE" --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}}')"
check "image starts via CMD [\"/entrypoint.sh\"] without ENTRYPOINT" \
  test "$(docker image inspect "$IMAGE" --format '{{json .Config.Entrypoint}} {{json .Config.Cmd}}')" = 'null ["/entrypoint.sh"]'
F="$(new_config f)"
mkdir -p "$F/secrets"
F_PW='e2e-file-password-not-secret'
F_TOKEN='e2e-file-token-0123456789'
# Like secrets on the host: root:root 0644, bind-mounted as single files. The
# password file ends with a newline, the token file does not.
docker run --rm -v "$F/secrets:/s" --entrypoint /bin/sh "$IMAGE" -c "
  printf '%s\n' '$F_PW' > /s/pw && printf '%s' '$F_TOKEN' > /s/token &&
  : > /s/empty && printf 'x' > /s/private &&
  chown 0:0 /s/pw /s/token /s/empty /s/private &&
  chmod 644 /s/pw /s/token /s/empty && chmod 600 /s/private"
CF=e2e-f
CONTAINERS+=("$CF")
docker run -d --name "$CF" -p "127.0.0.1:$((PORT + 3)):8080" \
  -e PUID=1032 -e PGID=65537 -e youtubedl_webui=true -e youtubedl_interval=1h \
  -e youtubedl_debug=true \
  -e WEBUI_USERNAME="$USER_NAME" \
  -e WEBUI_PASSWORD_FILE=/run/secrets/youtube_dl_webui_password \
  -e WEBUI_API_TOKEN_FILE=/run/secrets/youtube_dl_webui_api_token \
  -v "$F/secrets/pw:/run/secrets/youtube_dl_webui_password:ro" \
  -v "$F/secrets/token:/run/secrets/youtube_dl_webui_api_token:ro" \
  -v "$F/config:/config" -v "$F/downloads:/downloads" "$IMAGE" >/dev/null
BASE_F="http://127.0.0.1:$((PORT + 3))"
check "web UI from files comes up and demands auth (401)" \
  wait_for 90 sh -c "[ \"\$(curl -s -o /dev/null -w '%{http_code}' $BASE_F/)\" = 401 ]"
check "secret files are root:root 0644 in the container" \
  test "$(docker exec "$CF" stat -c '%u:%g %a' /run/secrets/youtube_dl_webui_password)" = '0:0 644'
check "abc has UID 1032 / GID 65537" \
  test "$(docker exec "$CF" id -u abc):$(docker exec "$CF" id -g abc)" = '1032:65537'
check "sources logged by name (init check and web UI)" \
  sh -c "docker logs $CF 2>&1 | grep -q '\[web-ui\] WEBUI_PASSWORD set from WEBUI_PASSWORD_FILE' &&
    docker exec $CF grep -q 'WEBUI_API_TOKEN set from WEBUI_API_TOKEN_FILE' /var/log/youtube-dl-webui/youtube-dl-webui.log"
check "Basic auth with the file's password (trailing newline stripped) (200)" \
  test "$(http_code -u "$USER_NAME:$F_PW" "$BASE_F/")" = 200
check "wrong password rejected (401)" \
  test "$(http_code -u "$USER_NAME:wrong" "$BASE_F/")" = 401
check "Bearer token from file accepted by POST /api/download" \
  sh -c "curl -s -H 'Authorization: Bearer $F_TOKEN' --data-urlencode 'url=https://example.invalid/secret-file' $BASE_F/api/download | grep -q '\"id\"'"
check "wrong Bearer token rejected (401)" \
  test "$(http_code -H 'Authorization: Bearer wrong' --data-urlencode 'url=https://example.invalid/x' "$BASE_F/api/download")" = 401
inspect_hides_values() {
  local s
  s="$(docker inspect "$1")" || return 1
  grep -q 'WEBUI_PASSWORD_FILE=/run/secrets/youtube_dl_webui_password' <<<"$s" &&
    ! grep -qF "$F_PW" <<<"$s" && ! grep -qF "$F_TOKEN" <<<"$s"
}
check "docker inspect shows the paths, never the values" inspect_hides_values "$CF"
logs_hide_values() {  # positive signal first: the web UI started and logged
  local logs
  logs="$(docker logs "$1" 2>&1; docker exec "$1" cat /var/log/youtube-dl-webui/youtube-dl-webui.log)" || return 1
  grep -q 'webserver is enabled' <<<"$logs" && ! grep -qF "$F_PW" <<<"$logs" &&
    ! grep -qF "$F_TOKEN" <<<"$logs"
}
check "values never logged (youtubedl_debug=true)" logs_hide_values "$CF"
webui_env_has_path_only() {
  local e
  e="$(docker exec -u abc "$1" sh -c 'tr "\0" "\n" < /proc/$(supervisorctl -c /etc/supervisor/supervisord.conf pid youtube-dl-webui)/environ')" || return 1
  grep -q '^WEBUI_PASSWORD_FILE=/run/secrets/' <<<"$e" && ! grep -q '^WEBUI_PASSWORD=' <<<"$e" &&
    ! grep -qF "$F_PW" <<<"$e"
}
check "web UI process gets the path, not the value, in its environment" \
  webui_env_has_path_only "$CF"

########################################################################
echo "== Scenario G: unusable credential files keep the web UI off (fail closed)"
G_PORT=$((PORT + 4))
refused_case() {  # refused_case name port docker-args... -> starts a container
  local name="e2e-g-$1" port="$2"; shift 2
  local dir; dir="$(new_config "g-$name")"
  CONTAINERS+=("$name")
  docker run -d --name "$name" -p "127.0.0.1:$port:8080" \
    -e PUID=1032 -e PGID=65537 -e youtubedl_webui=true -e youtubedl_interval=1h \
    -e WEBUI_USERNAME="$USER_NAME" "$@" \
    -v "$dir/config:/config" -v "$dir/downloads:/downloads" "$IMAGE" >/dev/null
}
refused_case both "$G_PORT" -e WEBUI_PASSWORD=plain \
  -e WEBUI_PASSWORD_FILE=/run/secrets/pw -v "$F/secrets/pw:/run/secrets/pw:ro"
refused_case missing "$((G_PORT + 1))" -e WEBUI_PASSWORD_FILE=/run/secrets/does-not-exist
refused_case empty "$((G_PORT + 2))" \
  -e WEBUI_PASSWORD_FILE=/run/secrets/pw -v "$F/secrets/empty:/run/secrets/pw:ro"
refused_case private "$((G_PORT + 3))" \
  -e WEBUI_PASSWORD_FILE=/run/secrets/pw -v "$F/secrets/private:/run/secrets/pw:ro"
refused_ok() {  # refused_ok name port message -> refusal logged, UI off, downloader on
  wait_for 60 log_has "$1" 'Refusing to start the web UI' &&
    log_has "$1" -F "$3" && ! log_has "$1" -F "$F_PW" &&
    sleep 3 &&
    [ "$(docker inspect -f '{{.State.Running}}' "$1")" = true ] &&
    [ "$(http_code "http://127.0.0.1:$2/")" = 000 ] &&
    grep -q RUNNING <<<"$(docker exec "$1" supervisorctl -c /etc/supervisor/supervisord.conf status youtube-dl)"
}
check "X and X_FILE both set: refused with a message" \
  refused_ok e2e-g-both "$G_PORT" 'WEBUI_PASSWORD and WEBUI_PASSWORD_FILE are both set'
check "missing file: refused with a message" \
  refused_ok e2e-g-missing "$((G_PORT + 1))" 'which does not exist'
check "empty file: refused with a message" \
  refused_ok e2e-g-empty "$((G_PORT + 2))" 'which is empty'
check "file not readable by PUID 1032 (root 0600): refused with a message" \
  refused_ok e2e-g-private "$((G_PORT + 3))" 'not readable by the web UI user (uid 1032'

########################################################################
echo
if [ "$FAILURES" -gt 0 ]; then
  echo "E2E: $FAILURES check(s) failed"
  for c in "${CONTAINERS[@]}"; do
    echo "--- logs: $c"; docker logs "$c" 2>&1 | tail -40
  done
  exit 1
fi
echo "E2E: all checks passed"
