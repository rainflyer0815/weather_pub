#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="${SCRIPT_DIR}/dwd_lag_monitor.log"
CRON_LINE="*/10 * * * * cd ${SCRIPT_DIR} && ${PYTHON_BIN} ${SCRIPT_DIR}/monitor_dwd_feed_lag.py >> ${LOG_FILE} 2>&1"

chmod +x "${SCRIPT_DIR}/monitor_dwd_feed_lag.py"

existing="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "${existing}" | rg -v "monitor_dwd_feed_lag\\.py" || true)"
{
  printf '%s\n' "${filtered}" | sed '/^$/d'
  printf '%s\n' "${CRON_LINE}"
} | crontab -

echo "Cronjob installiert/aktualisiert: alle 10 Minuten"
echo "CSV-Log: ${SCRIPT_DIR}/dwd_feed_lag_log.csv"
echo "Shell-Log: ${LOG_FILE}"
echo
echo "Testlauf..."
cd "${SCRIPT_DIR}"
"${PYTHON_BIN}" "${SCRIPT_DIR}/monitor_dwd_feed_lag.py"
