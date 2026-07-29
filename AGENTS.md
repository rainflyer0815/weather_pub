# AGENTS.md

## Cursor Cloud specific instructions

Public export of the weather-data pipeline: standalone Python 3 scripts
(Synoptic/DWD/METAR/Open-Meteo ingestion, MariaDB storage, analysis,
visualization, Telegram/Polymarket alerts). No build step and no package
manifest — scripts are run directly with `python3 <script>.py`.

### Dependencies
Third-party deps are installed to the user site by the startup update script:
`pymysql`, `websocket-client` (module `websocket`), `matplotlib`, `numpy`,
`netCDF4`. Everything else is Python stdlib. `pip install` here targets
`~/.local` (no venv, no `--break-system-packages` needed).

### Running / demonstrating (no credentials required)
- `python3 Main.py` — fetches live public data (DWD Open Data, Aviation METAR,
  Polymarket) and prints the Munich-airport weather + peak-forecast report. Best
  smoke test; needs outbound network but no secrets.
- `python3 generate_all_charts.py` — renders temperature/peak-time PNGs. Set
  `MPLBACKEND=Agg` when running headless.
- `python3 analyze_dwd_feed_lag.py` — offline analysis over the committed
  `dwd_feed_lag_log.csv`; use `MPLBACKEND=Agg` for the plot.

### Things that DO need external secrets/services (will fail without them)
- DB scripts (`poll_madis_hfmetar.py`, `poll_synoptic_5min.py`,
  `stream_synoptic_push.py`, `report_synoptic_db.py`, `query_db.py`,
  `compare_push_polymarket.py`) need `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD`
  (a MariaDB instance).
- `telegram_stake_alert.py` needs `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
- Synoptic scripts need `SYNOPTIC_TOKEN`.
Local config: copy the `*.example` files (`.env.db`, `.env.db.api`,
`.telegram.env`, `config.db.php`) — these are gitignored.

### Lint / test
There is no configured linter or automated test suite. Use
`python3 -m py_compile *.py` as a fast syntax/import sanity check.

### Notes
- CI workflows in `.github/workflows/` are `workflow_dispatch`-triggered and
  install their own deps inline (see the `pip install` lines there).
