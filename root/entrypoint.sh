#!/bin/bash

# Root processes (init scripts, supervisord and the root-run 'terminate'
# program) use system directories only. The image PATH puts the virtualenv
# first, which the unprivileged user owns when the self-updater is enabled;
# root must never resolve commands there. abc's programs get the venv on their
# PATH via 'environment=' in their supervisor config.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

echo "[startup] Running init scripts..."

for init in /etc/cont-init.d/*; do
  [ -f "$init" ] || continue
  if ! "$init"; then
    echo "[startup] init script $init failed, aborting."
    exit 1
  fi
done

echo -e "[startup] Finished.\n"

/bin/supervisord -c /etc/supervisor/supervisord.conf
