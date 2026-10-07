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

new_config() {  # new_config name -> prints dir with config/ and downloads/
  local dir="$WORK/$1"
  mkdir -p "$dir/config" "$dir/downloads"
  chmod 777 "$dir/config" "$dir/downloads"
  echo "$dir"
}

if [ -z "${E2E_SKIP_BUILD:-}" ]; then
  echo "== Building $IMAGE"
  docker build -t "$IMAGE" . || { echo "build failed"; exit 1; }
fi

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

headers="$(curl -s -D - -o /dev/null -u "$USER_NAME:$USER_PASS" "$BASE/")"
check "CSP header with script-src 'self'" grep -qi "content-security-policy:.*script-src 'self'" <<<"$headers"
check "X-Frame-Options DENY" grep -qi 'x-frame-options: DENY' <<<"$headers"

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

resp="$(curl -s -H "Authorization: Bearer $API_TOKEN" \
  --data-urlencode "url=https://example.invalid/x';touch /config/PWNED_url;'" "$BASE/api/download")"
dl_id="$(sed -n 's/.*"id":"\([0-9a-f-]*\)".*/\1/p' <<<"$resp")"
check "API download returns an id" test -n "$dl_id"
check "download process finishes and logs" \
  wait_for 60 sh -c "curl -s -u '$USER_NAME:$USER_PASS' $BASE/log/download/$dl_id | grep -q 'Download process ended'"

check "saving args.conf with --exec rejected (400)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" -H 'Sec-Fetch-Site: same-origin' --data-urlencode "args_new=--exec 'touch /config/PWNED_exec'" --data-urlencode "csrf_token=$csrf" "$BASE/edit/args/save")" = 400
check "saving channels.txt with valid content works (303)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" -H 'Sec-Fetch-Site: same-origin' --data-urlencode "$(printf 'channels_new=# e2e\nhttps://example.invalid/plain-channel\n')" --data-urlencode "csrf_token=$csrf" "$BASE/edit/channels/save")" = 303

check "downloader pass completes" \
  wait_for 180 sh -c "docker logs $CA 2>&1 | grep -q 'execution took'"
check "restart via POST with CSRF token (303)" \
  test "$(http_code -u "$USER_NAME:$USER_PASS" -H 'Sec-Fetch-Site: same-origin' --data-urlencode "csrf_token=$csrf" "$BASE/restart-youtube-dl")" = 303

for marker in PWNED_channels PWNED_args PWNED_url PWNED_opt PWNED_exec; do
  check "no injected command ran ($marker absent)" test ! -e "$A/config/$marker"
done

supervisorctl() { docker exec "$1" supervisorctl -c /etc/supervisor/supervisord.conf "${@:2}"; }
owner_of_pid() {  # owner_of_pid container program -> user owning the program's process
  local pid
  pid="$(supervisorctl "$1" pid "$2")"
  docker exec "$1" stat -c %U "/proc/$pid"
}
not_in() { ! grep -q "$1" <<<"$2"; }

check "abc has UID 1026" test "$(docker exec "$CA" id -u abc)" = 1026
check "/app is root-owned" test "$(docker exec "$CA" stat -c %U /app/youtube-dl-webui/youtube-dl-webui.py)" = root
check "venv is root-owned (updater off)" test "$(docker exec "$CA" stat -c %U /home/abc/.venv/pyvenv.cfg)" = root
check "abc cannot modify /app" \
  sh -c "! docker exec -u abc $CA sh -c 'echo x >> /app/youtube-dl-webui/youtube-dl-webui.py' 2>/dev/null"
check "abc can write its HOME" docker exec -u abc "$CA" sh -c 'touch /home/abc/.e2e && rm /home/abc/.e2e'
check "abc can run yt-dlp from the venv" docker exec -u abc "$CA" yt-dlp --version
check "web UI runs as abc" test "$(owner_of_pid "$CA" youtube-dl-webui)" = abc
check "downloader runs as abc" test "$(owner_of_pid "$CA" youtube-dl)" = abc
check "updater not running by default" not_in youtube-dl-updater "$(supervisorctl "$CA" status 2>&1)"
check "downloader RUNNING after restart" \
  wait_for 30 sh -c "docker exec $CA supervisorctl -c /etc/supervisor/supervisord.conf status youtube-dl | grep -q RUNNING"

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
check "web UI not reachable" \
  test "$(http_code "http://127.0.0.1:$((PORT + 1))/")" = 000
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
check "page links use the base path" \
  sh -c "curl -s -u '$USER_NAME:$USER_PASS' $BASE_D/yt/ | grep -q 'src=\"/yt/static/app.js\"'"
check "updater running when enabled" \
  sh -c "docker exec $CD supervisorctl -c /etc/supervisor/supervisord.conf status youtube-dl-updater | grep -q RUNNING"
check "venv owned by abc when updater enabled" \
  test "$(docker exec "$CD" stat -c %U /home/abc/.venv/pyvenv.cfg)" = abc

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
