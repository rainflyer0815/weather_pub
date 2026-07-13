#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOG_FILE="${SCRIPT_DIR}/telegram_stake.log"
CRON_LINE="*/10 * * * * cd ${SCRIPT_DIR} && sleep 30 && ${PYTHON_BIN} ${SCRIPT_DIR}/telegram_stake_alert.py >> ${LOG_FILE} 2>&1"

if [[ ! -f "${SCRIPT_DIR}/.telegram.env" ]]; then
  echo "Fehlt: ${SCRIPT_DIR}/.telegram.env"
  echo "Kopiere .telegram.env.example nach .telegram.env und trage Bot-Token + Chat-ID ein."
  exit 1
fi

chmod +x "${SCRIPT_DIR}/telegram_stake_alert.py"

existing="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "${existing}" | rg -v "telegram_stake_alert\\.py" || true)"
{
  printf '%s\n' "${filtered}" | sed '/^$/d'
  printf '%s\n' "${CRON_LINE}"
} | crontab -
echo "Cronjob installiert/aktualisiert: alle 10 Minuten bei Sekunde :30"
echo "Hinweis: Nicht parallel cron-job.org UND lokalen Cron nutzen (Doppel-Nachrichten)."

echo "Sende Testnachricht..."
cd "${SCRIPT_DIR}"
"${PYTHON_BIN}" "${SCRIPT_DIR}/telegram_stake_alert.py"
