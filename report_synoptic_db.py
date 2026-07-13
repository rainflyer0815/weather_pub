#!/usr/bin/env python3
"""Liest die Tabelle synoptic_5min_obs aus und druckt eine Füllstands-Auswertung.

Läuft als Report-Schritt im Workflow synoptic_5min_sync.yml (nutzt die
DB_*-Secrets) oder lokal mit .env.db.

Nutzung:
  python3 report_synoptic_db.py               # Zusammenfassung + letzte Werte
  python3 report_synoptic_db.py --hours 12    # Fenster für Stunden-Statistik
  python3 report_synoptic_db.py --tail 30     # Anzahl letzter Werte
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env.db"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def connect_db():
    import pymysql

    return pymysql.connect(
        host=os.environ["DB_HOST"].strip(),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"].strip(),
        password=os.environ["DB_PASSWORD"].strip(),
        database=os.environ["DB_NAME"].strip(),
        charset="utf8mb4",
    )


def report_push_channel(cursor, tail: int) -> None:
    """Push-Kanal (synoptic_push_obs) auswerten, falls die Tabelle existiert."""
    cursor.execute("SHOW TABLES LIKE 'synoptic_push_obs'")
    if not cursor.fetchone():
        return

    cursor.execute(
        "SELECT station, COUNT(*), MIN(observed_at_utc), MAX(observed_at_utc), "
        "ROUND(MIN(value_num),1), ROUND(MAX(value_num),1) "
        "FROM synoptic_push_obs WHERE sensor = 'air_temp' GROUP BY station"
    )
    rows = cursor.fetchall()
    if not rows:
        return

    print("\n=== Push-Kanal (synoptic_push_obs, air_temp) ===")
    for station, total, first, last, vmin, vmax in rows:
        print(f"{station}: {total} Zeilen | {first} bis {last} UTC | Temp {vmin}–{vmax} °C")

    cursor.execute(
        "SELECT station, TIMESTAMPDIFF(SECOND, observed_at_utc, received_at_utc) "
        "FROM synoptic_push_obs "
        "WHERE sensor = 'air_temp' "
        "AND TIMESTAMPDIFF(SECOND, observed_at_utc, received_at_utc) BETWEEN 0 AND 7200"
    )
    lags_by_station: dict[str, list[float]] = {}
    for station, lag_seconds in cursor.fetchall():
        lags_by_station.setdefault(station, []).append(lag_seconds / 60)

    for station in sorted(lags_by_station):
        lags = sorted(lags_by_station[station])

        def pct(p: float) -> float:
            return lags[min(len(lags) - 1, int(p * len(lags)))]

        print(f"\n=== Push-Delay observed → received: {station} ({len(lags)} Zeilen) ===")
        print(
            f"min {lags[0]:.1f} | p25 {pct(0.25):.1f} | median {pct(0.5):.1f} | "
            f"p75 {pct(0.75):.1f} | p90 {pct(0.9):.1f} | p95 {pct(0.95):.1f} | "
            f"max {lags[-1]:.1f} Minuten"
        )

    # Direkter Vergleich: gleiche (station, observed_at) in beiden Kanälen –
    # wie viel früher war der Push da als der Poll-Fetch?
    cursor.execute(
        "SELECT p.station, COUNT(*), "
        "ROUND(AVG(TIMESTAMPDIFF(SECOND, p.received_at_utc, o.fetched_at_utc)) / 60, 1), "
        "ROUND(MIN(TIMESTAMPDIFF(SECOND, p.received_at_utc, o.fetched_at_utc)) / 60, 1), "
        "ROUND(MAX(TIMESTAMPDIFF(SECOND, p.received_at_utc, o.fetched_at_utc)) / 60, 1) "
        "FROM synoptic_push_obs p "
        "JOIN synoptic_5min_obs o "
        "  ON o.station = p.station AND o.observed_at_utc = p.observed_at_utc "
        "WHERE p.sensor = 'air_temp' AND o.fetched_at_utc IS NOT NULL "
        "AND ABS(TIMESTAMPDIFF(SECOND, p.received_at_utc, o.fetched_at_utc)) <= 7200 "
        "GROUP BY p.station"
    )
    comparison = cursor.fetchall()
    if comparison:
        print("\n=== Push vs. Poll (gleiche Beobachtungen; positiv = Push früher) ===")
        for station, count, avg_min, min_min, max_min in comparison:
            print(
                f"{station}: {count} gemeinsame Werte | Push im Schnitt {avg_min} Min. "
                f"früher (min {min_min}, max {max_min})"
            )

    cursor.execute(
        "SELECT station, sensor, observed_at_utc, value_num, received_at_utc "
        "FROM synoptic_push_obs ORDER BY observed_at_utc DESC LIMIT %s",
        (tail,),
    )
    print(f"\n=== Letzte {tail} Push-Werte ===")
    for station, sensor, observed, value, received in reversed(cursor.fetchall()):
        lag = (received - observed).total_seconds() / 60
        print(
            f"{station:8s} {observed:%Y-%m-%d %H:%M}Z  {sensor}  {value} °C  "
            f"(empfangen +{lag:.1f} Min.)"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="Fenster für Stunden-Statistik")
    parser.add_argument("--tail", type=int, default=15, help="Anzahl letzter Werte")
    args = parser.parse_args()

    load_env_file(ENV_FILE)

    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT station, COUNT(*), MIN(observed_at_utc), MAX(observed_at_utc), "
                "SUM(is_metar), ROUND(MIN(air_temp_c),1), ROUND(MAX(air_temp_c),1) "
                "FROM synoptic_5min_obs GROUP BY station"
            )
            print("=== Gesamtfüllung ===")
            for row in cursor.fetchall():
                station, total, first, last, metars, tmin, tmax = row
                print(
                    f"{station}: {total} Zeilen | {first} bis {last} UTC | "
                    f"{int(metars or 0)} METAR/SPECI | Temp {tmin}–{tmax} °C"
                )

            cursor.execute(
                "SELECT station, DATE_FORMAT(observed_at_utc, '%%Y-%%m-%%d %%H:00') AS h, "
                "COUNT(*), SUM(is_metar), ROUND(AVG(air_temp_c),1) "
                "FROM synoptic_5min_obs "
                "WHERE observed_at_utc >= UTC_TIMESTAMP() - INTERVAL %s HOUR "
                "GROUP BY station, h ORDER BY station, h",
                (args.hours,),
            )
            print(f"\n=== Werte pro Stunde (letzte {args.hours} h; Soll: 1M-Station ~60, sonst 12 + 1 METAR) ===")
            for station, hour, count, metars, avg_temp in cursor.fetchall():
                target = 55 if station.upper().endswith("1M") else 12
                flag = "" if count >= target else "  ← LÜCKE"
                print(f"{station:8s} {hour}  {count:2d} Werte  {int(metars or 0)} METAR  Ø {avg_temp} °C{flag}")

            # Delay-Verteilung observed -> fetched. Zeilen mit Lag > 120 Min sind
            # Backfill der ersten Läufe (180-Min-Fenster) und werden ausgeschlossen.
            cursor.execute(
                "SELECT station, TIMESTAMPDIFF(SECOND, observed_at_utc, fetched_at_utc) "
                "FROM synoptic_5min_obs "
                "WHERE fetched_at_utc IS NOT NULL "
                "AND TIMESTAMPDIFF(SECOND, observed_at_utc, fetched_at_utc) <= 7200"
            )
            lags_by_station: dict[str, list[float]] = {}
            for station, lag_seconds in cursor.fetchall():
                lags_by_station.setdefault(station, []).append(lag_seconds / 60)

            for station in sorted(lags_by_station):
                lags = sorted(lags_by_station[station])

                def pct(p: float) -> float:
                    return lags[min(len(lags) - 1, int(p * len(lags)))]

                print(f"\n=== Delay observed → fetched: {station} ({len(lags)} Zeilen, ohne Backfill) ===")
                print(
                    f"min {lags[0]:.1f} | p25 {pct(0.25):.1f} | median {pct(0.5):.1f} | "
                    f"p75 {pct(0.75):.1f} | p90 {pct(0.9):.1f} | p95 {pct(0.95):.1f} | "
                    f"max {lags[-1]:.1f} Minuten"
                )
                buckets: dict[str, int] = {}
                for lag in lags:
                    if lag < 6:
                        key = f"{int(lag):02d}–{int(lag) + 1:02d} Min"
                    elif lag < 10:
                        key = "06–10 Min"
                    elif lag < 15:
                        key = "10–15 Min"
                    elif lag < 30:
                        key = "15–30 Min"
                    else:
                        key = ">30 Min"
                    buckets[key] = buckets.get(key, 0) + 1
                for key in sorted(buckets):
                    count = buckets[key]
                    bar = "#" * max(1, round(40 * count / len(lags)))
                    print(f"{key:12s} {count:4d}  {bar}")

            report_push_channel(cursor, args.tail)

            cursor.execute(
                "SELECT station, observed_at_utc, air_temp_c, air_temp_f, is_metar, fetched_at_utc "
                "FROM synoptic_5min_obs ORDER BY observed_at_utc DESC LIMIT %s",
                (args.tail,),
            )
            print(f"\n=== Letzte {args.tail} Werte ===")
            for station, observed, temp_c, temp_f, is_metar, fetched in reversed(cursor.fetchall()):
                kind = "METAR" if is_metar else "obs  "
                lag = (fetched - observed).total_seconds() / 60
                print(
                    f"{station:8s} {observed:%Y-%m-%d %H:%M}Z  {kind}  {temp_c} °C = {temp_f} °F  "
                    f"(gespeichert +{lag:.0f} Min.)"
                )
    finally:
        connection.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
