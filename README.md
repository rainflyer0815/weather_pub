# weather

Wetterdaten-Pipeline: Sammlung, Speicherung und Auswertung von Beobachtungs-
und Prognosedaten (MADIS/AWC, Synoptic, DWD, METAR, Open-Meteo) mit Telegram-Alerts.

## Architektur (Kurz)

```
cron-job.org ──dispatch──▶ madis_5min_sync.yml
                              │
                              ▼
                     poll_madis_hfmetar.py
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
         MADIS HFMETAR   AWC METAR/SPECI   Telegram
         (5-Min, °C)     (Dezimal-°C)      (nur bei Wertänderung)
              │               │
              └───────┬───────┘
                      ▼
              synoptic_5min_obs (MariaDB)
                      │
                      ▼
              report_synoptic_db.py
```

Der frühere Synoptic-Poller (`poll_synoptic_5min.py`) ist Legacy: gleiche Zieltabelle
und Upsert-Semantik, aber nicht mehr im Workflow verdrahtet. Der Synoptic-Push-
Stream ist wegen abgelaufenem Trial pausiert (manueller Start bleibt möglich).

## Komponenten

| Bereich | Dateien | Zweck |
| --- | --- | --- |
| MADIS/AWC Poll | `poll_madis_hfmetar.py`, `schema_synoptic_5min.sql` | Kostenloser Ersatz für Synoptic-Poll: 5-Min-HFMETAR (MADIS netCDF) + METAR/SPECI (aviationweather.gov) → `synoptic_5min_obs` |
| Synoptic Poll (Legacy) | `poll_synoptic_5min.py` | Alter Poller gegen Synoptic Timeseries-API; braucht `SYNOPTIC_TOKEN` |
| Synoptic Push | `stream_synoptic_push.py`, `schema_synoptic_push.sql` | WebSocket-Stream → `synoptic_push_obs` + Session-Resume in `synoptic_push_state` (aktuell Trial abgelaufen) |
| DB-Report | `report_synoptic_db.py` | Füllstand, Latenz-Verteilungen, Push-vs-Poll-Vergleich |
| Push vs Polymarket | `compare_push_polymarket.py` | KLGA1M-Push (°F) gegen NYC-Polymarket-Quotes; Artefakte (CSV/Plots) |
| DWD Feed Lag | `monitor_dwd_feed_lag.py`, `analyze_dwd_feed_lag.py`, `upload_lag_to_db.py` | Misst Verzögerung DWD-10-min-Feed vs. METAR (EDDM/01262) |
| Telegram Alert | `telegram_stake_alert.py`, `Main.py` | Tägliche Wetter-/Markt-Zusammenfassung (München / Polymarket) |
| DB-Query-API | `db_query_api.php`, `query_db.py` | Lesender SQL-Zugriff auf die MariaDB via HTTPS |

## Datenquellen und Constraints

| Quelle | Skript | Raster / Inhalt | Typische Latenz | Hinweis |
| --- | --- | --- | --- | --- |
| MADIS Public HFMETAR | `poll_madis_hfmetar.py` | 5-Min, ganze °C (Kelvin→°C) | ~10–15 Min | Anonym; stündliche `.gz`-netCDF; aktuelle Stunde kann 404 sein |
| AWC Data API | `poll_madis_hfmetar.py` | METAR/SPECI, Dezimal-°C aus T-Group | abhängig von Ausgabe | Ersetzt die früheren Synoptic-`:51`-METARs |
| Synoptic Push | `stream_synoptic_push.py` | 1-Min Sensorwerte (z. B. `air_temp`) | ~3–4 Min observed→received | Bezahlter Zugang nötig; ohne Token: „Streaming service not allowed“ |
| DWD CDC 10-min | `monitor_dwd_feed_lag.py` | Station 01262 (München) | gemessen vs. METAR | Log → `dwd_feed_lag_log.csv` + optional DB |

**Poll-Semantik** (`synoptic_5min_obs` + `synoptic_poll_runs`): Jeder Poll-Lauf
bekommt eine `poll_counter`-ID und schreibt **alle** gelesenen Beobachtungen
(`UNIQUE (poll_counter, station, observed_at_utc)`). Altbestand ohne Counter:
Migration `schema_synoptic_5min_poll_counter_migrate.sql` (einmalig).

**Telegram bei MADIS-Poll:** Bei jedem Lauf eine Statuszeile je Station. Pfeil
`↑/↓ von …`, wenn sich der neueste 5-Min-Wert (`is_metar=0`) gegenüber dem
letzten DB-Wert ändert **und** der Wert ≤ 30 Min alt ist. Sonst `· unverändert`.
METARs lösen keine Änderungsmeldung aus. Backfill nach Ausfällen bleibt stumm.

## Konfiguration

Zugangsdaten über Umgebungsvariablen bzw. GitHub-Secrets. Lokal die
`*.example`-Vorlagen kopieren (werden nicht committet):

| Datei lokal | Secrets / Variablen |
| --- | --- |
| `.env.db` | `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`; optional `MADIS_STATION`, `SYNOPTIC_TOKEN`, `SYNOPTIC_STATION` |
| `.telegram.env` | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| `.env.db.api` | `DB_API_URL`, `DB_API_KEY` (Client für `db_query_api.php`) |
| `config.db.php` | Server-seitig aus `config.db.php.example` (`api_key`, `allow_writes`, `max_rows`) |

GitHub-Secrets für Workflows: `DB_*`, optional `TELEGRAM_*`, für Push zusätzlich
`SYNOPTIC_TOKEN`.

## Schema anlegen

In phpMyAdmin (oder CLI) die SQL-Dateien ausführen:

1. `schema_synoptic_5min.sql` – Poll-Tabelle + `synoptic_poll_runs`
   (bei bestehender DB: zusätzlich `schema_synoptic_5min_poll_counter_migrate.sql`)
2. `schema_synoptic_push.sql` – Push-Obs + Session-State (oder automatisch durch den Streamer)
3. `schema_crondb.sql` – `dwd_feed_lag`

## Workflows

| Workflow | Trigger | Job |
| --- | --- | --- |
| `madis_5min_sync.yml` | `workflow_dispatch` (extern via cron-job.org, typisch alle 2 Min) | `poll_madis_hfmetar.py --hours-back 1` + `report_synoptic_db.py` |
| `synoptic_push_stream.yml` | nur `workflow_dispatch` (kein Schedule; Trial abgelaufen) | `stream_synoptic_push.py --max-runtime 340 --telegram-all` |
| `dwd_feed_lag_monitor.yml` | GitHub-Cron `*/10` + dispatch | Lag loggen, CSV committen, `upload_lag_to_db.py` |
| `telegram_alert.yml` | `workflow_dispatch` (cron-job.org; kein GitHub-Schedule → keine Doppel-Nachrichten) | `telegram_stake_alert.py` inkl. 10-Min-Dedup-Cache |
| `compare_push_polymarket.yml` | `workflow_dispatch` (`days`, Default 3) | Vergleich + Artifact-Upload |

Concurrency: MADIS- und Telegram-Workflows warten bei Überlappung
(`cancel-in-progress: false`), damit kein Lauf mitten im Schreiben/Senden
abgebrochen wird.

## Lokaler Schnellstart

```bash
# Abhängigkeiten (je nach Skript)
pip install pymysql netCDF4 numpy          # MADIS-Poll
pip install pymysql websocket-client       # Synoptic-Push
pip install pymysql matplotlib             # Compare / Reports

cp .env.db.example .env.db                 # DB-Zugang eintragen
cp .telegram.env.example .telegram.env     # optional für Alerts

# Poll ohne DB-Schreiben
python3 poll_madis_hfmetar.py --dry-run --hours-back 1

# Poll + Upsert (Standardstation: KLGA bzw. MADIS_STATION)
python3 poll_madis_hfmetar.py --hours-back 1

# Füllstand / Latenz
python3 report_synoptic_db.py

# Push nur mit gültigem Synoptic-Token testen
python3 stream_synoptic_push.py --max-runtime 5 --dry-run

# Telegram-Alert ohne Senden
python3 telegram_stake_alert.py --dry-run
```

Optionale Host-Crons: `setup_dwd_lag_cron.sh`, `setup_telegram_cron.sh`
(Hinweis: Telegram nicht parallel zu cron-job.org betreiben → Doppel-Nachrichten).

## DB-Query-API

`db_query_api.php` auf dem Kasserver (localhost-DB) entgegennehmen:

- Request: `POST` JSON `{"sql":"SELECT ...", "params":[...]}` mit Header `X-API-Key`
- Default: nur lesende Statements (`SELECT`/`SHOW`/`DESCRIBE`/`EXPLAIN`); Writes nur bei `allow_writes=true`
- Client: `python3 query_db.py "SELECT COUNT(*) FROM synoptic_5min_obs"`

## Troubleshooting

| Symptom | Ursache / Fix |
| --- | --- |
| MADIS: leere Beobachtungen / Abruffehler | Aktuelle Stunden-Datei oft noch 404 – normal; `--hours-back` ≥ 1. `netCDF4`/`numpy` installieren. |
| Viele neue Zeilen nach Ausfall, kein Telegram | Beabsichtigt: nur frische Wertänderungen (≤ 30 Min) melden. |
| Push: „Streaming service not allowed“ | Synoptic-Trial/Abo fehlt – Schedule bleibt aus; Token prüfen oder auf MADIS-Poll verlassen. |
| Doppelte Telegram-Alerts | Nur **eine** Triggerquelle: entweder GitHub+cron-job.org **oder** lokaler Cron; Dedup-Datei `.telegram_last_sent` / Actions-Cache nicht löschen. |
| `report_synoptic_db.py` ohne Push-Sektion | Tabelle `synoptic_push_obs` fehlt oder leer – nach Trial-Pause erwartet. |
| DB-API 401 / „api_key fehlt“ | `config.db.php` und `.env.db.api` abgleichen; Key nur über Header senden. |
| Workflow `synoptic_5min_sync` nicht gefunden | Umbenannt nach `madis_5min_sync.yml` – Dispatch-URL und cron-job.org anpassen. |

## Auswertungsskripte (Nebenpfade)

Analog-Tage, Peak-Validierung und Charts: `find_analog_days.py`,
`validate_peak_forecast.py`, `validate_open_meteo_metar.py`,
`visualize_*.py`, `generate_all_charts.py`. Diese lesen öffentliche Feeds bzw.
lokale Artefakte und sind nicht an die GitHub-Sync-Workflows gekoppelt.
