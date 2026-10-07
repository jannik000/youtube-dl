#!/bin/bash

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
