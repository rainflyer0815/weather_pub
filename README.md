# weather

Wetterdaten-Pipeline: Sammlung, Speicherung und Auswertung von Beobachtungs-
und Prognosedaten (Synoptic, DWD, METAR, Open-Meteo) mit Telegram-Alerts.

## Komponenten

| Bereich | Dateien | Zweck |
| --- | --- | --- |
| Synoptic Poll | `poll_synoptic_5min.py`, `schema_synoptic_5min.sql` | Pollt 5-min-HFMETAR/1-min-HF-ASOS und speichert in MariaDB (Dedup + Upsert) |
| Synoptic Push | `stream_synoptic_push.py`, `schema_synoptic_push.sql` | WebSocket-Stream in eigene Tabelle, Session-Resume, optionale Telegram-Meldung bei Wertänderung |
| DB-Report | `report_synoptic_db.py` | Füllstand, Latenz-Verteilungen, Push-vs-Poll-Vergleich |
| DWD Feed Lag | `monitor_dwd_feed_lag.py`, `analyze_dwd_feed_lag.py` | Misst Verzögerung DWD-10-min-Feed vs. METAR |
| Telegram Alert | `telegram_stake_alert.py` | Tägliche Wetter-/Markt-Zusammenfassung |
| DB-Query-API | `db_query_api.php`, `query_db.py` | Lesender SQL-Zugriff auf die MariaDB via HTTPS |

## Konfiguration

Alle Zugangsdaten laufen über Umgebungsvariablen bzw. GitHub-Secrets
(`SYNOPTIC_TOKEN`, `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Lokal: `.env.db` / `.telegram.env`
nach den `*.example`-Vorlagen anlegen (werden nicht committet).

## Workflows

- `synoptic_5min_sync.yml` – minütlicher Poll (Trigger: cron-job.org → workflow_dispatch)
- `synoptic_push_stream.yml` – Dauer-Stream in ~340-Min-Zyklen (Cron + workflow_dispatch, Concurrency-Gruppe)
- `dwd_feed_lag_monitor.yml` – alle 10 Minuten (GitHub-Cron)
- `telegram_alert.yml` – Trigger via cron-job.org
