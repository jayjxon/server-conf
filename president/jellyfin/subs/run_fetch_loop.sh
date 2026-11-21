#!/bin/bash
# Re-runs the subtitle fetcher until no un-attempted ready items remain.
while true; do
  out=$(python3 /srv/apps/jellyfin/subs/fetch_subs.py --workers 24 2>&1 | tail -2)
  echo "$out" >> /srv/apps/jellyfin/subs/loop.log
  if echo "$out" | grep -q 'fetch start: 0 ready'; then
    echo "$(date '+%F %T') queue drained" >> /srv/apps/jellyfin/subs/loop.log
    break
  fi
  sleep 60
done
