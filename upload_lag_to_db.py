#!/usr/bin/env python3
"""Lädt neue Zeilen aus dwd_feed_lag_log.csv in die MariaDB-Spieldatenbank."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env.db"
LOG_FILE = SCRIPT_DIR / "dwd_feed_lag_log.csv"

INSERT_SQL = """
INSERT INTO dwd_feed_lag (
    logged_at_berlin,
    dwd_latest,
    dwd_lag_min,
    dwd_values_today,
    dwd_max,
    dwd_max_time,
    metar_latest,
    metar_lag_min,
    metar_values_today,
    metar_max,
    metar_max_time,
    dwd_tt10,
    metar_temp,
    metar_raw_ob
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
)
ON DUPLICATE KEY UPDATE
    dwd_latest = VALUES(dwd_latest),
    dwd_lag_min = VALUES(dwd_lag_min),
    dwd_values_today = VALUES(dwd_values_today),
    dwd_max = VALUES(dwd_max),
    dwd_max_time = VALUES(dwd_max_time),
    metar_latest = VALUES(metar_latest),
    metar_lag_min = VALUES(metar_lag_min),
    metar_values_today = VALUES(metar_values_today),
    metar_max = VALUES(metar_max),
    metar_max_time = VALUES(metar_max_time),
    dwd_tt10 = VALUES(dwd_tt10),
    metar_temp = VALUES(metar_temp),
    metar_raw_ob = VALUES(metar_raw_ob)
"""


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        import os

        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_dt(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


def parse_float(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    return float(value)


def parse_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    return int(value)


def load_csv_rows(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            logged_at = parse_dt(raw.get("logged_at_berlin", ""))
            if logged_at is None:
                continue
            rows.append(
                {
                    "logged_at_berlin": logged_at,
                    "dwd_latest": parse_dt(raw.get("dwd_latest", "")),
                    "dwd_lag_min": parse_int(raw.get("dwd_lag_min", "")),
                    "dwd_values_today": parse_int(raw.get("dwd_values_today", "")),
                    "dwd_max": parse_float(raw.get("dwd_max", "")),
                    "dwd_max_time": parse_dt(raw.get("dwd_max_time", "")),
                    "metar_latest": parse_dt(raw.get("metar_latest", "")),
                    "metar_lag_min": parse_int(raw.get("metar_lag_min", "")),
                    "metar_values_today": parse_int(raw.get("metar_values_today", "")),
                    "metar_max": parse_float(raw.get("metar_max", "")),
                    "metar_max_time": parse_dt(raw.get("metar_max_time", "")),
                    "dwd_tt10": parse_float(raw.get("dwd_TT_10", "")),
                    "metar_temp": parse_float(raw.get("metar_temp", "")),
                    "metar_raw_ob": (raw.get("metar_rawOb", "") or "")[:255] or None,
                }
            )
    return rows


def connect_db():
    try:
        import pymysql
    except ImportError as error:
        raise RuntimeError(
            "pymysql fehlt. Installiere mit: pip install pymysql"
        ) from error

    import os

    host = os.environ.get("DB_HOST", "").strip()
    user = os.environ.get("DB_USER", "").strip()
    password = os.environ.get("DB_PASSWORD", "").strip()
    database = os.environ.get("DB_NAME", "").strip()
    port = int(os.environ.get("DB_PORT", "3306"))

    missing = [name for name, value in (
        ("DB_HOST", host),
        ("DB_USER", user),
        ("DB_PASSWORD", password),
        ("DB_NAME", database),
    ) if not value]
    if missing:
        raise RuntimeError(
            f"Fehlende DB-Konfiguration: {', '.join(missing)}. "
            f"Lege {ENV_FILE.name} an (siehe .env.db.example)."
        )

    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=False,
    )


def upload_rows(rows: list[dict[str, object]], dry_run: bool) -> tuple[int, int]:
    if dry_run:
        print(f"[dry-run] {len(rows)} Zeilen würden hochgeladen.", file=sys.stderr)
        return len(rows), 0

    connection = connect_db()
    inserted = 0
    try:
        with connection.cursor() as cursor:
            for row in rows:
                cursor.execute(
                    INSERT_SQL,
                    (
                        row["logged_at_berlin"],
                        row["dwd_latest"],
                        row["dwd_lag_min"],
                        row["dwd_values_today"],
                        row["dwd_max"],
                        row["dwd_max_time"],
                        row["metar_latest"],
                        row["metar_lag_min"],
                        row["metar_values_today"],
                        row["metar_max"],
                        row["metar_max_time"],
                        row["dwd_tt10"],
                        row["metar_temp"],
                        row["metar_raw_ob"],
                    ),
                )
                inserted += cursor.rowcount
        connection.commit()
    finally:
        connection.close()

    return len(rows), inserted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=LOG_FILE, help="CSV-Quelldatei")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht schreiben")
    args = parser.parse_args()

    load_env_file(ENV_FILE)

    if not args.csv.exists():
        print(f"CSV fehlt: {args.csv}", file=sys.stderr)
        return 1

    rows = load_csv_rows(args.csv)
    if not rows:
        print("Keine verwertbaren CSV-Zeilen.", file=sys.stderr)
        return 1

    try:
        total, affected = upload_rows(rows, args.dry_run)
    except Exception as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1

    action = "würden verarbeitet" if args.dry_run else "verarbeitet"
    print(f"{total} CSV-Zeilen {action} ({affected} DB-Änderungen).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
