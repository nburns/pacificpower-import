#!/usr/bin/env bash
set -euo pipefail

# Log helper — Python logging emits timestamps; make our echo lines match.
ts() { echo "$(date +'%Y-%m-%d %H:%M:%S,000') INFO run.sh $*"; }
tserr() { echo "$(date +'%Y-%m-%d %H:%M:%S,000') ERROR run.sh $*" >&2; }

OPTIONS=/data/options.json
STATE_FILE=/data/state.json
CRONTAB=/data/crontab

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

if [[ -z "${PP_USERNAME}" || "${PP_USERNAME}" == "null" ]]; then
  tserr "username not set — waiting for you to configure the add-on (Configuration tab → Save; supervisor will restart me)"
  sleep infinity
fi

# Bump this when the backfill strategy changes incompatibly. Startup
# triggers a fresh backfill (which clears prior stats first) when the
# saved version is below this number.
CURRENT_BACKFILL_VERSION=5
SAVED_BACKFILL_VERSION=$(jq -r '.backfill_version // 0' "${STATE_FILE}" 2>/dev/null || echo 0)

if [[ "${RUN_BACKFILL}" == "true" && "${SAVED_BACKFILL_VERSION}" -lt "${CURRENT_BACKFILL_VERSION}" ]]; then
  ts "backfill needed (saved=${SAVED_BACKFILL_VERSION}, current=${CURRENT_BACKFILL_VERSION})"
  python -m pacificpower_import --mode backfill
  ts "backfill done"
fi

# Build a crontab for supercronic. Job invokes the same entry with mode=daily.
cat > "${CRONTAB}" <<EOF
${SCHEDULE} python -m pacificpower_import --mode daily
EOF

ts "starting supercronic with schedule: ${SCHEDULE}"
exec supercronic "${CRONTAB}"
