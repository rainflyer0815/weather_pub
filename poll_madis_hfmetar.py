#!/usr/bin/env python3
"""Pollt MADIS-HFMETAR (5-min ASOS) + aviationweather.gov (METAR/SPECI) und
speichert neue Werte in MariaDB – kostenloser Ersatz für den Synoptic-Poller.

Quellen:
  * MADIS Public (anonymer Zugang): stündliche netCDF-Dateien unter
    https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/hfmetar/netCDF/
    → 5-Minuten-Raster, ganze °C, Latenz ~10–15 Min (gleiche Rohquelle,
    die auch Synoptic als HFMETAR ausliefert).
  * aviationweather.gov Data API: METAR/SPECI mit Dezimaltemperatur aus der
    T-Group → ersetzt die :51-METARs, die Synoptic mitlieferte.

Beide Quellen schreiben in dieselbe Tabelle synoptic_5min_obs; Dedup/Upsert
identisch zum bisherigen Poller (UNIQUE KEY station+observed_at_utc).

Konfiguration über Umgebungsvariablen oder .env.db:
  MADIS_STATION      Stations-ID(s), kommagetrennt, Standard: KLGA
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

Nutzung:
  python3 poll_madis_hfmetar.py             # pollen + speichern
  python3 poll_madis_hfmetar.py --dry-run   # nur anzeigen, nicht schreiben
  python3 poll_madis_hfmetar.py --hours-back 4
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

USER_AGENT = "weather/1.0 (MADIS 5-min feed poller)"
MADIS_BASE_URL = "https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/hfmetar/netCDF"
AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"
DEFAULT_STATION = "KLGA"
DEFAULT_HOURS_BACK = 2
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env.db"

# Identisch zum Synoptic-Poller: nachgereichte Werte heilen sich selbst,
# vorhandene Werte werden nie durch NULL überschrieben.
INSERT_SQL = """
INSERT INTO synoptic_5min_obs (
    station, observed_at_utc, air_temp_c, air_temp_f, is_metar, metar_raw, fetched_at_utc
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    air_temp_c = COALESCE(VALUES(air_temp_c), air_temp_c),
    air_temp_f = COALESCE(VALUES(air_temp_f), air_temp_f),
    metar_raw = COALESCE(VALUES(metar_raw), metar_raw)
"""

SELECT_EXISTING_SQL = """
SELECT observed_at_utc FROM synoptic_5min_obs
WHERE station = %s AND observed_at_utc >= %s AND air_temp_c IS NOT NULL
"""


@dataclass(frozen=True)
class Observation:
    station: str
    observed_at: datetime
    air_temp_c: float | None
    metar_raw: str | None
    is_metar: bool

    @property
    def air_temp_f(self) -> float | None:
        if self.air_temp_c is None:
            return None
        return round(self.air_temp_c * 9 / 5 + 32, 1)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def http_get(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_madis_observations(stations: set[str], hours_back: int) -> list[Observation]:
    """Lädt die stündlichen netCDF-Dateien (aktuelle + vergangene Stunden)."""
    try:
        import numpy as np
        from netCDF4 import Dataset, chartostring
    except ImportError as error:
        raise RuntimeError("netCDF4 fehlt. Installiere mit: pip install netCDF4") from error

    observations: list[Observation] = []
    now = datetime.now(timezone.utc)
    for offset in range(hours_back + 1):
        hour = (now - timedelta(hours=offset)).replace(minute=0, second=0, microsecond=0)
        url = f"{MADIS_BASE_URL}/{hour:%Y%m%d_%H%M}.gz"
        try:
            payload = gzip.decompress(http_get(url))
        except urllib.error.HTTPError as error:
            if error.code == 404:  # Datei der aktuellen Stunde existiert evtl. noch nicht
                continue
            raise

        ds = Dataset("inmemory.nc", memory=payload)
        try:
            ids = np.char.strip(chartostring(ds.variables["stationId"][:]).astype(str))
            times = ds.variables["observationTime"][:]
            temps = ds.variables["temperature"][:]
            raws = chartostring(ds.variables["rawMessage"][:])
            for index in np.where(np.isin(ids, list(stations)))[0]:
                observed_at = datetime.fromtimestamp(float(times[index]), tz=timezone.utc)
                kelvin = temps[index]
                temp_c = None
                if kelvin is not None and not np.ma.is_masked(kelvin):
                    temp_c = round(float(kelvin) - 273.15, 1)
                raw = str(raws[index]).strip()
                observations.append(
                    Observation(
                        station=str(ids[index]),
                        observed_at=observed_at.replace(tzinfo=None),
                        air_temp_c=temp_c,
                        metar_raw=(raw[:255] or None),
                        is_metar=False,
                    )
                )
        finally:
            ds.close()
    return observations


def fetch_awc_metars(stations: set[str], hours: int = 3) -> list[Observation]:
    """METAR/SPECI mit Dezimaltemperatur von aviationweather.gov."""
    query = urllib.parse.urlencode(
        {"ids": ",".join(sorted(stations)), "format": "json", "hours": str(hours)}
    )
    payload = json.loads(http_get(f"{AWC_METAR_URL}?{query}", timeout=30))

    observations: list[Observation] = []
    for entry in payload:
        station = str(entry.get("icaoId", "")).upper()
        obs_epoch = entry.get("obsTime")
        if station not in stations or not obs_epoch:
            continue
        temp = entry.get("temp")
        raw = str(entry.get("rawOb", "")).strip()
        observations.append(
            Observation(
                station=station,
                observed_at=datetime.fromtimestamp(int(obs_epoch), tz=timezone.utc).replace(tzinfo=None),
                air_temp_c=(round(float(temp), 1) if temp is not None else None),
                metar_raw=(raw[:255] or None),
                is_metar=True,
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

    return pymysql.connect(
        host=config["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
        database=config["DB_NAME"],
        charset="utf8mb4",
        autocommit=False,
    )


def store_new_observations(observations: list[Observation], dry_run: bool) -> tuple[int, int]:
    """Liefert (neu, bereits vorhanden). Dedup läuft pro Station."""
    if not observations:
        return 0, 0

    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)

    if dry_run:
        for obs in sorted(observations, key=lambda o: (o.station, o.observed_at)):
            kind = "METAR" if obs.is_metar else "5min "
            print(f"[dry-run] {obs.station} {obs.observed_at:%Y-%m-%d %H:%M}Z {kind} {obs.air_temp_c}°C")
        return len(observations), 0

    by_station: dict[str, list[Observation]] = {}
    for obs in observations:
        by_station.setdefault(obs.station, []).append(obs)

    new_count = 0
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            for station, station_obs in by_station.items():
                earliest = min(obs.observed_at for obs in station_obs)
                cursor.execute(SELECT_EXISTING_SQL, (station, earliest))
                existing = {row[0] for row in cursor.fetchall()}

                for obs in station_obs:
                    if obs.observed_at in existing:
                        continue
                    cursor.execute(
                        INSERT_SQL,
                        (
                            obs.station,
                            obs.observed_at,
                            obs.air_temp_c,
                            obs.air_temp_f,
                            int(obs.is_metar),
                            obs.metar_raw,
                            fetched_at,
                        ),
                    )
                    new_count += 1
        connection.commit()
    finally:
        connection.close()

    return new_count, len(observations) - new_count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--station",
        default=None,
        help="Stations-ID(s), kommagetrennt (Standard: env MADIS_STATION oder KLGA)",
    )
    parser.add_argument(
        "--hours-back",
        type=int,
        default=DEFAULT_HOURS_BACK,
        help="Wieviele vergangene Stundendateien zusätzlich geladen werden (Heilung)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht in DB schreiben")
    args = parser.parse_args()

    load_env_file(ENV_FILE)

    station_arg = (args.station or os.environ.get("MADIS_STATION", DEFAULT_STATION)).strip().upper()
    stations = {name.strip() for name in station_arg.split(",") if name.strip()}

    observations: list[Observation] = []
    errors: list[str] = []
    try:
        observations.extend(fetch_madis_observations(stations, args.hours_back))
    except Exception as error:
        errors.append(f"MADIS: {error}")
    try:
        observations.extend(fetch_awc_metars(stations))
    except Exception as error:
        errors.append(f"AWC: {error}")

    for message in errors:
        print(f"Abruffehler: {message}", file=sys.stderr)
    if not observations:
        print(f"Keine Beobachtungen für {station_arg} erhalten.", file=sys.stderr)
        return 1

    try:
        new_count, existing_count = store_new_observations(observations, args.dry_run)
    except Exception as error:
        print(f"DB-Fehler: {error}", file=sys.stderr)
        return 1

    print(
        f"{station_arg}: {len(observations)} Werte im Fenster, {new_count} neu gespeichert, "
        f"{existing_count} bereits vorhanden."
    )
    for name in sorted({obs.station for obs in observations}):
        latest = max(
            (obs for obs in observations if obs.station == name),
            key=lambda obs: obs.observed_at,
        )
        lag = (datetime.now(timezone.utc).replace(tzinfo=None) - latest.observed_at).total_seconds() / 60
        print(
            f"  {name}: neuester Wert {latest.observed_at:%Y-%m-%d %H:%M}Z "
            f"{latest.air_temp_c}°C (+{lag:.0f} Min.)"
        )
    return 1 if errors and not observations else 0


if __name__ == "__main__":
    raise SystemExit(main())
