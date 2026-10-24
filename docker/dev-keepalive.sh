#!/bin/sh
# TDM-3: run a service's dev command. The apps are still placeholders that
# print a line and exit 0; when that happens we idle instead of exiting, so
# `docker compose up` stays up and real servers drop in later with no compose
# change. A non-zero exit still fails the container. DEV_KEEPALIVE=0 disables.
"$@"
code=$?
if [ "$code" -eq 0 ] && [ "${DEV_KEEPALIVE:-1}" = "1" ]; then
  echo "[dev-keepalive] '$*' exited 0 (placeholder?); idling. Ctrl-C / docker compose down to stop."
  exec tail -f /dev/null
fi
exit "$code"
