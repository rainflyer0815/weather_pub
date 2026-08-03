#!/usr/bin/env python3
"""LEGACY: Pollt Synoptic-Feeds (HFMETAR 5-min / HF-ASOS 1-min) → MariaDB.

Nicht mehr im CI verdrahtet. Aktueller Poll-Pfad: poll_madis_hfmetar.py
(Workflow madis_5min_sync.yml). Gleiche Zieltabelle synoptic_5min_obs und
gleiche Poll-Counter-Semantik (jeder Lauf schreibt alle Beobachtungen).

Früher: cron-job.org → synoptic_5min_sync.yml → dieses Skript. Holt die
letzten ~3 Stunden von der Synoptic-API (Stationen kommagetrennt, z. B.
KLGA,KLGA1M) und schreibt sie unter neuer poll_counter-ID.

Konfiguration über Umgebungsvariablen oder .env.db:
  SYNOPTIC_TOKEN     API-Token (Pflicht)
  SYNOPTIC_STATION   Stations-ID(s), kommagetrennt, Standard: KLGA
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

Nutzung:
  python3 poll_synoptic_5min.py             # pollen + speichern
  python3 poll_synoptic_5min.py --dry-run   # nur anzeigen, nicht schreiben
  python3 poll_synoptic_5min.py --recent 360
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = "weather/1.0 (Synoptic 5-min feed poller)"
SYNOPTIC_TIMESERIES_URL = "https://api.synopticdata.com/v2/stations/timeseries"
DEFAULT_STATION = "KLGA"
DEFAULT_RECENT_MINUTES = 180
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env.db"

# Jeder Poll schreibt unter neuer poll_counter-ID (wie poll_madis_hfmetar.py).
INSERT_POLL_RUN_SQL = """
INSERT INTO synoptic_poll_runs (polled_at_utc, observation_count)
VALUES (%s, 0)
"""

UPDATE_POLL_RUN_SQL = """
UPDATE synoptic_poll_runs SET observation_count = %s WHERE poll_counter = %s
"""

INSERT_SQL = """
INSERT INTO synoptic_5min_obs (
    poll_counter, station, observed_at_utc, air_temp_c, air_temp_f,
    is_metar, metar_raw, fetched_at_utc
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""


@dataclass(frozen=True)
class Observation:
    station: str
    observed_at: datetime
    air_temp_c: float | None
    metar_raw: str | None

    @property
    def air_temp_f(self) -> float | None:
        if self.air_temp_c is None:
            return None
        return round(self.air_temp_c * 9 / 5 + 32, 1)

    @property
    def is_metar(self) -> bool:
        # 1M-Stationen (HF-ASOS) liefern Minutenwerte, dort greift die
        # Raster-Heuristik nicht. Bei METAR-Stationen laufen reguläre Reports
        # um :51; SPECI/Sonderreports liegen außerhalb des 5-Minuten-Rasters.
        if self.station.upper().endswith("1M"):
            return False
        return self.observed_at.minute % 5 != 0


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def fetch_observations(token: str, station: str, recent_minutes: int) -> list[Observation]:
    query = urllib.parse.urlencode(
        {
            "stid": station,
            "recent": str(recent_minutes),
            "vars": "air_temp,metar",
            "hfmetars": "1",
            "obtimezone": "UTC",
            "token": token,
        }
    )
    request = urllib.request.Request(
        f"{SYNOPTIC_TIMESERIES_URL}?{query}",
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    summary = payload.get("SUMMARY", {})
    if summary.get("RESPONSE_CODE") != 1:
        raise RuntimeError(f"Synoptic-API: {summary.get('RESPONSE_MESSAGE', 'unbekannter Fehler')}")

    observations: list[Observation] = []
    for station_data in payload.get("STATION", []):
        obs = station_data.get("OBSERVATIONS", {})
        times = obs.get("date_time", [])
        temps = obs.get("air_temp_set_1", [])
        metars = obs.get("metar_set_1", [])
        for index, time_label in enumerate(times):
            observed_at = datetime.fromisoformat(time_label.replace("Z", "+00:00"))
            observed_at = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
            temp = temps[index] if index < len(temps) else None
            metar = metars[index] if index < len(metars) else None
            observations.append(
                Observation(
                    station=station_data.get("STID", station),
                    observed_at=observed_at,
                    air_temp_c=float(temp) if temp is not None else None,
                    metar_raw=(str(metar)[:255] if metar else None),
                )
            )
    return observations


def connect_db():
    try:
        import pymysql
    except ImportError as error:
        raise RuntimeError("pymysql fehlt. Installiere mit: pip install pymysql") from error

    config = {
        "DB_HOST": os.environ.get("DB_HOST", "").strip(),
        "DB_USER": os.environ.get("DB_USER", "").strip(),
        "DB_PASSWORD": os.environ.get("DB_PASSWORD", "").strip(),
        "DB_NAME": os.environ.get("DB_NAME", "").strip(),
    }
    missing = [name for name, value in config.items() if not value]
    if missing:
        raise RuntimeError(f"Fehlende DB-Konfiguration: {', '.join(missing)}")

    host = config["DB_HOST"]
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
    host = host.rstrip("/")

    return pymysql.connect(
        host=host,
        port=int(os.environ.get("DB_PORT", "3306")),
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
        database=config["DB_NAME"],
        charset="utf8mb4",
        autocommit=False,
    )


def store_new_observations(observations: list[Observation], dry_run: bool) -> tuple[int, int]:
    """Schreibt alle Beobachtungen unter neuer poll_counter-ID."""
    if not observations:
        return 0, 0

    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)

    if dry_run:
        for obs in observations:
            print(f"[dry-run] {obs.station} {obs.observed_at:%Y-%m-%d %H:%M}Z {obs.air_temp_c}°C")
        return len(observations), 0

    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(INSERT_POLL_RUN_SQL, (fetched_at,))
            poll_counter = int(cursor.lastrowid)
            for obs in observations:
                cursor.execute(
                    INSERT_SQL,
                    (
                        poll_counter,
                        obs.station,
                        obs.observed_at,
                        obs.air_temp_c,
                        obs.air_temp_f,
                        int(obs.is_metar),
                        obs.metar_raw,
                        fetched_at,
                    ),
                )
            cursor.execute(UPDATE_POLL_RUN_SQL, (len(observations), poll_counter))
        connection.commit()
        print(f"Poll #{poll_counter}: {len(observations)} Beobachtungen geschrieben.")
    finally:
        connection.close()

    return len(observations), 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--station",
        default=None,
        help="Stations-ID(s), kommagetrennt (Standard: env SYNOPTIC_STATION oder KLGA)",
    )
    parser.add_argument("--recent", type=int, default=DEFAULT_RECENT_MINUTES, help="Zeitfenster in Minuten")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht in DB schreiben")
    args = parser.parse_args()

    load_env_file(ENV_FILE)

    token = os.environ.get("SYNOPTIC_TOKEN", "").strip()
    if not token:
        print("SYNOPTIC_TOKEN fehlt (Umgebungsvariable oder .env.db).", file=sys.stderr)
        return 2

    station = (args.station or os.environ.get("SYNOPTIC_STATION", DEFAULT_STATION)).strip().upper()

    try:
        observations = fetch_observations(token, station, args.recent)
    except Exception as error:
        print(f"Abruffehler: {error}", file=sys.stderr)
        return 1

    if not observations:
        print(f"Keine Beobachtungen für {station} erhalten.", file=sys.stderr)
        return 1

    try:
        new_count, existing_count = store_new_observations(observations, args.dry_run)
    except Exception as error:
        print(f"DB-Fehler: {error}", file=sys.stderr)
        return 1

    print(
        f"{station}: {len(observations)} Werte im Fenster, {new_count} neu gespeichert, "
        f"{existing_count} bereits vorhanden."
    )
    seen_stations = sorted({obs.station for obs in observations})
    for name in seen_stations:
        latest = max(
            (obs for obs in observations if obs.station == name),
            key=lambda obs: obs.observed_at,
        )
        print(f"  {name}: neuester Wert {latest.observed_at:%Y-%m-%d %H:%M}Z {latest.air_temp_c}°C")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
