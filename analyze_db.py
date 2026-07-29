#!/usr/bin/env python3
"""Read-only Analyse aller MariaDB-Tabellen (nutzt DB_*-Env / .env.db)."""

from __future__ import annotations

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


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    load_env_file(ENV_FILE)
    missing = [k for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD") if not os.environ.get(k)]
    if missing:
        print(f"Fehlende Env-Vars: {', '.join(missing)}", file=sys.stderr)
        return 1

    db = connect_db()
    try:
        with db.cursor() as cur:
            section("Verbindung")
            cur.execute("SELECT DATABASE(), VERSION(), NOW(), UTC_TIMESTAMP()")
            name, version, now_local, now_utc = cur.fetchone()
            print(f"database={name} | version={version}")
            print(f"server_now={now_local} | utc_now={now_utc}")

            section("Tabellen")
            cur.execute(
                "SELECT table_name, table_rows, "
                "ROUND(data_length/1024/1024, 2), ROUND(index_length/1024/1024, 2), "
                "engine, table_collation, create_time, update_time "
                "FROM information_schema.tables "
                "WHERE table_schema = DATABASE() "
                "ORDER BY table_name"
            )
            for row in cur.fetchall():
                tname, approx, data_mb, idx_mb, engine, coll, created, updated = row
                print(
                    f"{tname:24s} approx_rows={approx}  "
                    f"data={data_mb}MB idx={idx_mb}MB  {engine} {coll}  "
                    f"created={created} updated={updated}"
                )

            cur.execute("SHOW TABLES")
            tables = [r[0] for r in cur.fetchall()]

            if "dwd_feed_lag" in tables:
                section("dwd_feed_lag")
                cur.execute(
                    "SELECT COUNT(*), MIN(logged_at_berlin), MAX(logged_at_berlin), "
                    "MIN(dwd_lag_min), ROUND(AVG(dwd_lag_min),1), MAX(dwd_lag_min), "
                    "MIN(metar_lag_min), ROUND(AVG(metar_lag_min),1), MAX(metar_lag_min), "
                    "MIN(dwd_tt10), MAX(dwd_tt10), MIN(metar_temp), MAX(metar_temp) "
                    "FROM dwd_feed_lag"
                )
                (
                    n,
                    first,
                    last,
                    dwd_lo,
                    dwd_avg,
                    dwd_hi,
                    metar_lo,
                    metar_avg,
                    metar_hi,
                    tt_lo,
                    tt_hi,
                    mt_lo,
                    mt_hi,
                ) = cur.fetchone()
                print(f"rows={n} | logged {first} → {last}")
                print(f"dwd_lag_min:   min={dwd_lo} avg={dwd_avg} max={dwd_hi}")
                print(f"metar_lag_min: min={metar_lo} avg={metar_avg} max={metar_hi}")
                print(f"dwd_tt10: {tt_lo}–{tt_hi} °C | metar_temp: {mt_lo}–{mt_hi} °C")

                cur.execute(
                    "SELECT DATE(logged_at_berlin), COUNT(*), "
                    "ROUND(AVG(dwd_lag_min),1), ROUND(AVG(metar_lag_min),1), "
                    "ROUND(MAX(dwd_max),1), ROUND(MAX(metar_max),1) "
                    "FROM dwd_feed_lag GROUP BY DATE(logged_at_berlin) ORDER BY 1"
                )
                print("pro Tag:")
                for day, count, dwd_a, metar_a, dwd_max, metar_max in cur.fetchall():
                    print(
                        f"  {day}  n={count:3d}  dwd_lagØ={dwd_a}  metar_lagØ={metar_a}  "
                        f"dwd_max={dwd_max}  metar_max={metar_max}"
                    )

                cur.execute(
                    "SELECT logged_at_berlin, dwd_lag_min, metar_lag_min, "
                    "dwd_tt10, metar_temp, metar_raw_ob "
                    "FROM dwd_feed_lag ORDER BY logged_at_berlin DESC LIMIT 10"
                )
                print("letzte 10:")
                for logged, dl, ml, tt, mt, raw in cur.fetchall():
                    raw_s = (raw or "")[:60]
                    print(
                        f"  {logged}  dwd_lag={dl} metar_lag={ml}  "
                        f"tt10={tt} metar={mt}  {raw_s}"
                    )

            if "synoptic_5min_obs" in tables:
                section("synoptic_5min_obs")
                cur.execute(
                    "SELECT station, COUNT(*), SUM(is_metar), "
                    "MIN(observed_at_utc), MAX(observed_at_utc), "
                    "ROUND(MIN(air_temp_c),1), ROUND(MAX(air_temp_c),1), "
                    "ROUND(AVG(air_temp_c),1) "
                    "FROM synoptic_5min_obs GROUP BY station ORDER BY station"
                )
                for station, n, metars, first, last, tmin, tmax, tavg in cur.fetchall():
                    print(
                        f"{station}: rows={n} metar={int(metars or 0)} | "
                        f"{first} → {last} | temp {tmin}–{tmax} (Ø {tavg}) °C"
                    )

                cur.execute(
                    "SELECT DATE(observed_at_utc), station, COUNT(*), "
                    "SUM(is_metar), ROUND(MIN(air_temp_c),1), ROUND(MAX(air_temp_c),1) "
                    "FROM synoptic_5min_obs "
                    "GROUP BY DATE(observed_at_utc), station ORDER BY 1 DESC, 2 LIMIT 40"
                )
                print("letzte Tage (max 40 Zeilen):")
                for day, station, n, metars, tmin, tmax in cur.fetchall():
                    print(
                        f"  {day} {station:8s} n={n:4d} metar={int(metars or 0)} "
                        f"temp {tmin}–{tmax}"
                    )

                cur.execute(
                    "SELECT station, observed_at_utc, air_temp_c, is_metar, "
                    "TIMESTAMPDIFF(MINUTE, observed_at_utc, fetched_at_utc) AS lag_min, "
                    "LEFT(metar_raw, 70) "
                    "FROM synoptic_5min_obs ORDER BY observed_at_utc DESC LIMIT 15"
                )
                print("letzte 15:")
                for station, obs, temp, is_metar, lag, raw in cur.fetchall():
                    kind = "METAR" if is_metar else "obs"
                    print(
                        f"  {station:8s} {obs}  {kind:5s} {temp}°C  lag={lag}min  {raw or ''}"
                    )

            if "synoptic_push_obs" in tables:
                section("synoptic_push_obs")
                cur.execute(
                    "SELECT station, sensor, COUNT(*), "
                    "MIN(observed_at_utc), MAX(observed_at_utc), "
                    "ROUND(MIN(value_num),2), ROUND(MAX(value_num),2), "
                    "ROUND(AVG(TIMESTAMPDIFF(SECOND, observed_at_utc, received_at_utc))/60, 2) "
                    "FROM synoptic_push_obs "
                    "GROUP BY station, sensor ORDER BY station, sensor"
                )
                for station, sensor, n, first, last, vmin, vmax, lag_avg in cur.fetchall():
                    print(
                        f"{station}/{sensor}: rows={n} | {first} → {last} | "
                        f"value {vmin}–{vmax} | avg_receive_lag={lag_avg} min"
                    )

                cur.execute(
                    "SELECT station, sensor, observed_at_utc, value_num, qc_flags, "
                    "TIMESTAMPDIFF(SECOND, observed_at_utc, received_at_utc) "
                    "FROM synoptic_push_obs ORDER BY observed_at_utc DESC LIMIT 15"
                )
                print("letzte 15:")
                for station, sensor, obs, value, qc, lag_s in cur.fetchall():
                    print(
                        f"  {station:8s} {obs}  {sensor}={value}  "
                        f"qc={qc or '-'}  lag={lag_s}s"
                    )

            if "synoptic_push_state" in tables:
                section("synoptic_push_state")
                cur.execute("SELECT stream_key, session_id, updated_at_utc FROM synoptic_push_state")
                rows = cur.fetchall()
                if not rows:
                    print("(leer)")
                for stream_key, session_id, updated in rows:
                    print(f"{stream_key} | session={session_id} | updated={updated}")

            section("Fertig")
            print("Read-only Analyse abgeschlossen.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
