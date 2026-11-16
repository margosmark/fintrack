#!/usr/bin/env bash
# autoheal.sh — restart a compose service that keeps reporting unhealthy (M148).
#
# Why this exists: `restart: unless-stopped` restarts a container that CRASHES,
# never one that stays RUNNING but answers nothing. On 2026-10-11 the api
# container sat `(unhealthy)` for more than two hours during the pool-exhaustion
# outage (docs/01 § "Sessions and the event loop") — nothing self-healed, and a
# human had to `docker restart` it by hand. This is the hook that does that.
#
# It watches HEALTH, not exit state: docker's own healthcheck (compose, 30 s
# interval / 10 s timeout / 3 retries) is the signal, so a service that is only
# briefly busy is not touched — the counter must reach $THRESHOLD consecutive
# unhealthy rounds (one per minute) first. That keeps a restart for a service
# that is genuinely stuck, which is the only case a restart actually fixes.
#
# Install (example.internal), once, after the checkout exists:
#   (crontab -l 2>/dev/null; echo '* * * * * cd /home/developer/fin_tracker && ./scripts/autoheal.sh >> /tmp/fintrack-autoheal.log 2>&1') | crontab -
#
# Usage: autoheal.sh [project] [service ...]     default: fin_tracker api
set -uo pipefail

PROJECT="${1:-fin_tracker}"
shift || true
SERVICES=("$@")
if [ "${#SERVICES[@]}" -eq 0 ]; then
  SERVICES=(api)
fi

THRESHOLD=3
STATE_DIR="${AUTOHEAL_STATE_DIR:-/var/tmp/fintrack-autoheal}"
mkdir -p "$STATE_DIR"

stamp() {
  date -u +%FT%TZ
}

for svc in "${SERVICES[@]}"; do
  name="${PROJECT}-${svc}-1"
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$name" 2>/dev/null)"
  if [ -z "$health" ]; then
    # Not running at all (or never created) — `restart: unless-stopped` and
    # compose own that case; a cron that starts containers would fight them.
    echo "$(stamp) $name: not inspectable, left alone"
    continue
  fi

  counter_file="$STATE_DIR/${svc}.unhealthy_rounds"
  if [ "$health" != "unhealthy" ]; then
    if [ -f "$counter_file" ]; then
      rm -f "$counter_file"
    fi
    continue
  fi

  rounds=$(( $(cat "$counter_file" 2>/dev/null || echo 0) + 1 ))
  echo "$rounds" >"$counter_file"
  echo "$(stamp) $name: unhealthy, round $rounds/$THRESHOLD"
  if [ "$rounds" -ge "$THRESHOLD" ]; then
    echo "$(stamp) $name: restarting (health never recovered on its own)"
    docker restart "$name" >/dev/null 2>&1 || echo "$(stamp) $name: restart FAILED"
    rm -f "$counter_file"
  fi
done
