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

Jeder Poll-Lauf legt einen Eintrag in synoptic_poll_runs an (poll_counter) und
schreibt ALLE gelesenen Beobachtungen nach synoptic_5min_obs
(UNIQUE KEY poll_counter+station+observed_at_utc). Damit sind Telegram-Status
und Poll-Latenzen aus der DB rekonstruierbar.

Telegram: Sind TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID gesetzt, wird bei jedem
Lauf eine Statusmeldung mit dem aktuellen Wert je Station geschickt – auch
bei gleicher Temperatur. Hat sich der neueste 5-Minuten-Wert gegenüber dem
letzten in der DB gespeicherten Wert geändert (nur frische Werte ≤ 30 Min alt,
Backfill zählt nicht), wird die Änderung zusätzlich mit Pfeil markiert.

Konfiguration über Umgebungsvariablen oder .env.db:
  MADIS_STATION        Stations-ID(s), kommagetrennt, Standard: KLGA
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
  TELEGRAM_BOT_TOKEN   optional – Telegram bei Wertänderung
  TELEGRAM_CHAT_ID     optional

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
TELEGRAM_API = "https://api.telegram.org"
DEFAULT_STATION = "KLGA"
DEFAULT_HOURS_BACK = 2
# Nur Werte, die höchstens so alt sind, lösen eine Telegram-Meldung aus.
REALTIME_MAX_LAG_MINUTES = 30
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env.db"

INSERT_POLL_RUN_SQL = """
INSERT INTO synoptic_poll_runs (polled_at_utc, observation_count)
VALUES (%s, 0)
"""

UPDATE_POLL_RUN_SQL = """
UPDATE synoptic_poll_runs SET observation_count = %s WHERE poll_counter = %s
"""

# Jeder Poll schreibt alle Beobachtungen unter seiner poll_counter-ID.
INSERT_SQL = """
INSERT INTO synoptic_5min_obs (
    poll_counter, station, observed_at_utc, air_temp_c, air_temp_f,
    is_metar, metar_raw, fetched_at_utc
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""

# Letzter gespeicherter 5-Minuten-Wert einer Station – Vergleichsbasis für
# die Telegram-Meldung bei Wertänderung (METARs bleiben außen vor, sonst
# würde jede stündliche Dezimaltemperatur eine Pseudo-Änderung auslösen).
SELECT_LATEST_5MIN_SQL = """
SELECT observed_at_utc, air_temp_c FROM synoptic_5min_obs
WHERE station = %s AND is_metar = 0 AND air_temp_c IS NOT NULL
ORDER BY observed_at_utc DESC, poll_counter DESC LIMIT 1
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


@dataclass(frozen=True)
class ValueChange:
    station: str
    observed_at: datetime
    new_value: float
    previous_value: float


def store_new_observations(
    observations: list[Observation], dry_run: bool
) -> tuple[int, int, list[ValueChange]]:
    """Schreibt alle Beobachtungen eines Polls unter neuer poll_counter-ID.

    Rückgabe: (geschrieben, 0, Wertänderungen). Der zweite Wert bleibt 0
    (kein Skip mehr); Signatur bleibt kompatibel zum Aufrufer.
    """
    if not observations:
        return 0, 0, []

    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)

    # Pro (station, observed_at, is_metar) nur einmal – Quellen können überlappen.
    deduped: dict[tuple[str, datetime, bool], Observation] = {}
    for obs in observations:
        deduped[(obs.station, obs.observed_at, obs.is_metar)] = obs
    observations = sorted(
        deduped.values(), key=lambda obs: (obs.station, obs.observed_at, obs.is_metar)
    )

    if dry_run:
        print(f"[dry-run] würde Poll mit {len(observations)} Beobachtungen schreiben")
        for obs in observations:
            kind = "METAR" if obs.is_metar else "5min "
            print(
                f"[dry-run] {obs.station} {obs.observed_at:%Y-%m-%d %H:%M}Z "
                f"{kind} {obs.air_temp_c}°C"
            )
        return len(observations), 0, []

    by_station: dict[str, list[Observation]] = {}
    for obs in observations:
        by_station.setdefault(obs.station, []).append(obs)

    changes: list[ValueChange] = []
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            previous_by_station: dict[str, tuple | None] = {}
            for station in by_station:
                cursor.execute(SELECT_LATEST_5MIN_SQL, (station,))
                previous_by_station[station] = cursor.fetchone()

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

            for station, station_obs in by_station.items():
                inserted_5min = [
                    obs
                    for obs in station_obs
                    if not obs.is_metar and obs.air_temp_c is not None
                ]
                change = detect_value_change(
                    previous_by_station.get(station), inserted_5min, fetched_at
                )
                if change:
                    changes.append(change)

        connection.commit()
        print(f"Poll #{poll_counter}: {len(observations)} Beobachtungen geschrieben.")
    finally:
        connection.close()

    return len(observations), 0, changes


def detect_value_change(
    previous: tuple | None, inserted_5min: list[Observation], now: datetime
) -> ValueChange | None:
    """Wertänderung nur für frische Daten; Erstbefüllung und Backfill bleiben stumm."""
    if previous is None or not inserted_5min:
        return None
    prev_observed, prev_temp = previous[0], float(previous[1])
    newest = max(inserted_5min, key=lambda obs: obs.observed_at)
    if newest.observed_at <= prev_observed:
        return None
    if (now - newest.observed_at).total_seconds() > REALTIME_MAX_LAG_MINUTES * 60:
        return None
    if newest.air_temp_c == prev_temp:
        return None
    return ValueChange(
        station=newest.station,
        observed_at=newest.observed_at,
        new_value=newest.air_temp_c,
        previous_value=prev_temp,
    )


def post_telegram_message(text: str) -> None:
    """Schickt eine Textnachricht an den konfigurierten Chat (best effort)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not text or not token or not chat_id:
        return

    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        data=payload,
        headers={"User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        if not body.get("ok"):
            print(f"Telegram-API-Fehler: {body}", file=sys.stderr)
    except Exception as error:
        # Telegram-Ausfälle dürfen den DB-Pfad nie stören.
        print(f"Telegram-Sendefehler: {error}", file=sys.stderr)


def latest_metar_kind(observations: list[Observation]) -> str:
    """Erste 5 Zeichen von metar_raw des neuesten METAR/SPECI (sonst leer)."""
    candidates = [
        obs
        for obs in observations
        if obs.metar_raw and obs.metar_raw[:5] in ("METAR", "SPECI")
    ]
    if not candidates:
        return ""
    latest = max(candidates, key=lambda obs: obs.observed_at)
    return latest.metar_raw[:5]


def build_run_report(observations: list[Observation], changes: list[ValueChange]) -> str:
    """Statuszeile je Station für jeden Lauf – auch ohne Wertänderung."""
    change_by_station = {change.station: change for change in changes}
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    lines: list[str] = []
    for station in sorted({obs.station for obs in observations}):
        with_temp = [
            obs
            for obs in observations
            if obs.station == station and obs.air_temp_c is not None
        ]
        if not with_temp:
            continue
        latest = max(with_temp, key=lambda obs: obs.observed_at)
        lag_minutes = (now - latest.observed_at).total_seconds() / 60
        fahrenheit = latest.air_temp_c * 9 / 5 + 32
        line = (
            f"{station} {latest.observed_at:%H:%M}Z: {latest.air_temp_c:.1f} °C "
            f"= {fahrenheit:.1f} °F (+{lag_minutes:.0f} Min.)"
        )
        change = change_by_station.get(station)
        if change:
            arrow = "↑" if change.new_value > change.previous_value else "↓"
            line += f" {arrow} von {change.previous_value:.1f} °C"
        else:
            line += " · unverändert"
        lines.append(line)

    if not lines:
        return ""
    kind = latest_metar_kind(observations)
    header = f"📡 MADIS Poll {kind}".rstrip()
    return header + "\n" + "\n".join(lines)


def send_telegram_run_report(observations: list[Observation], changes: list[ValueChange]) -> None:
    post_telegram_message(build_run_report(observations, changes))


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
        new_count, existing_count, changes = store_new_observations(observations, args.dry_run)
    except Exception as error:
        print(f"DB-Fehler: {error}", file=sys.stderr)
        return 1

    if not args.dry_run:
        send_telegram_run_report(observations, changes)
    for change in changes:
        print(
            f"Wertänderung {change.station}: {change.previous_value} → {change.new_value} °C "
            f"um {change.observed_at:%H:%M}Z (Telegram gesendet)"
        )

    print(
        f"{station_arg}: Poll schrieb {new_count} Beobachtungen "
        f"({existing_count} Skips – immer 0 bei Poll-Counter-Modus)."
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
