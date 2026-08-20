#!/usr/bin/env bash
set -euo pipefail

# Log helper — Python logging emits timestamps; make our echo lines match.
ts() { echo "$(date +'%Y-%m-%d %H:%M:%S,000') INFO run.sh $*"; }
tserr() { echo "$(date +'%Y-%m-%d %H:%M:%S,000') ERROR run.sh $*" >&2; }

OPTIONS=/data/options.json
STATE_FILE=/data/state.json
CRONTAB=/data/crontab

mkdir -p /data/logs /data/debug
if [ -f /data/logs/main.log ] && [ "$(stat -c%s /data/logs/main.log)" -gt 10485760 ]; then
  tail -c 5242880 /data/logs/main.log > /data/logs/main.log.tmp && mv /data/logs/main.log.tmp /data/logs/main.log
fi

if [[ ! -f "${OPTIONS}" ]]; then
  tserr "${OPTIONS} not found — waiting for add-on options (supervisor will restart me when you save config)"
  sleep infinity
fi

export PP_USERNAME=$(jq -r '.username' "${OPTIONS}")
export PP_PASSWORD=$(jq -r '.password' "${OPTIONS}")
export PP_METER_ID=$(jq -r '.meter_id // empty' "${OPTIONS}")
export STATISTIC_ID=$(jq -r '.statistic_id' "${OPTIONS}")
export STATISTIC_NAME=$(jq -r '.statistic_name' "${OPTIONS}")
export COST_STATISTIC_ID=$(jq -r '.cost_statistic_id' "${OPTIONS}")
export COST_STATISTIC_NAME=$(jq -r '.cost_statistic_name' "${OPTIONS}")
export DATA_DIR=/data

RUN_BACKFILL=$(jq -r '.run_backfill_on_start' "${OPTIONS}")
SCHEDULE=$(jq -r '.daily_schedule' "${OPTIONS}")

# Exported so the hourly-trickle Python process can rewrite the crontab
# to the steady-state schedule when it flips the switchover flag.
export PP_DAILY_SCHEDULE="${SCHEDULE}"
export PP_CRONTAB_PATH="${CRONTAB}"

HOURLY_MODE=$(jq -r '.hourly_mode // false' "${OPTIONS}")
export HOURLY_BACKFILL_DAYS_PER_HOUR=$(jq -r '.hourly_backfill_days_per_hour // 4' "${OPTIONS}")
export HOURLY_BACKFILL_WINDOW_DAYS=$(jq -r '.hourly_backfill_window_days // 730' "${OPTIONS}")

# Runtime toggle: gates both the scraper's debug-dump writes and the
# ingress viewer's rendered content. When false, the sidebar entry still
# resolves (server always runs) but shows a "disabled" placeholder.
export PP_DIAGNOSTICS_ENABLED=$(jq -r '.diagnostics_enabled // false' "${OPTIONS}")

if [[ -z "${PP_USERNAME}" || "${PP_USERNAME}" == "null" ]]; then
  tserr "username not set — waiting for you to configure the add-on (Configuration tab → Save; supervisor will restart me)"
  sleep infinity
fi

# Bump this when the backfill strategy changes incompatibly. Startup
# triggers a fresh backfill (which clears prior stats first) when the
# saved version is below this number.
CURRENT_BACKFILL_VERSION=5
SAVED_BACKFILL_VERSION=$(jq -r '.backfill_version // 0' "${STATE_FILE}" 2>/dev/null || echo 0)

python -m pacificpower_import.web >> /data/logs/main.log 2>&1 &
WEB_PID=$!
ts "ingress web server started (pid=${WEB_PID})"

if [[ "${HOURLY_MODE}" == "true" ]]; then
  # Read the mode that was active when state was last written.
  SAVED_LAST_MODE=$(jq -r '.last_mode // "daily"' "${STATE_FILE}" 2>/dev/null || echo "daily")

  if [[ "${SAVED_LAST_MODE}" != "hourly" ]]; then
    # Toggle flip from daily → hourly. Clear stats and reset hourly fields.
    ts "hourly_mode enabled and last_mode was '${SAVED_LAST_MODE}' — clearing statistics and resetting for hourly backfill"
    python -m pacificpower_import --mode backfill
    # Overwrite the state fields needed for hourly trickle. We use Python
    # to preserve all other state fields rather than blowing away the file.
    python - <<'PYEOF'
import json, sys
from datetime import date, timedelta
from pathlib import Path

state_path = Path("/data/state.json")
raw = json.loads(state_path.read_text()) if state_path.exists() else {}
yesterday = (date.today() - timedelta(days=1)).isoformat()
raw["hourly_backfill_cursor"] = yesterday
raw["hourly_backfill_complete"] = False
raw["last_mode"] = "daily"  # will be updated to "hourly" by first trickle run
state_path.write_text(json.dumps(raw, indent=2))
print(f"Reset hourly backfill fields; cursor={yesterday}")
PYEOF
    ts "toggle-flip reset done"
  fi

  HOURLY_BACKFILL_COMPLETE=$(jq -r '.hourly_backfill_complete // false' "${STATE_FILE}" 2>/dev/null || echo false)

  if [[ "${HOURLY_BACKFILL_COMPLETE}" == "false" ]]; then
    # Compute trickle interval: 60 / days_per_hour minutes, minimum 1.
    TRICKLE_MINS=$(( 60 / HOURLY_BACKFILL_DAYS_PER_HOUR ))
    if [[ "${TRICKLE_MINS}" -lt 1 ]]; then
      TRICKLE_MINS=1
    fi
    export PP_TRICKLE_INTERVAL_MINUTES="${TRICKLE_MINS}"

    ts "starting hourly trickle backfill (${HOURLY_BACKFILL_DAYS_PER_HOUR} downloads/hour, every ${TRICKLE_MINS} min)"
    cat > "${CRONTAB}" <<EOF
*/${TRICKLE_MINS} * * * * python -m pacificpower_import --mode hourly-trickle
EOF
  else
    ts "hourly trickle complete — starting steady-state hourly-daily cron: ${SCHEDULE}"
    cat > "${CRONTAB}" <<EOF
${SCHEDULE} python -m pacificpower_import --mode hourly-daily
EOF
  fi

else
  # Daily mode (default) — existing behavior unchanged.
  if [[ "${RUN_BACKFILL}" == "true" && "${SAVED_BACKFILL_VERSION}" -lt "${CURRENT_BACKFILL_VERSION}" ]]; then
    ts "backfill needed (saved=${SAVED_BACKFILL_VERSION}, current=${CURRENT_BACKFILL_VERSION})"
    python -m pacificpower_import --mode backfill
    ts "backfill done"
  fi

  cat > "${CRONTAB}" <<EOF
${SCHEDULE} python -m pacificpower_import --mode daily
EOF
fi

ts "starting supercronic with schedule from ${CRONTAB}"
# Loop around supercronic so the hourly-trickle job can hot-swap the crontab
# by killing supercronic; we then restart it against the rewritten file.
{
  while true; do
    supercronic "${CRONTAB}" || tserr "supercronic exited with status $?"
    ts "supercronic exited; restarting against ${CRONTAB} in 2s"
    sleep 2
  done
} 2>&1 | tee -a /data/logs/main.log
