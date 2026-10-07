#!/bin/bash
# Runs as root (started via supervisorctl, which abc may do): use absolute
# paths only, never commands resolved through PATH.

/bin/sleep 2s
kill -3 "$(/bin/cat "/etc/supervisor/supervisord.pid")"
