#!/usr/bin/env python3
"""Log DWD /now feed lag versus METAR for München-Flughafen (01262 / EDDM)."""

from __future__ import annotations

import csv
import io
import json
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram_stake_alert import (
    AVIATION_METAR_URL,
    BERLIN,
    DWD_TEMP_NOW_URL,
    fetch_json_safe,
    observation_moment,
    parse_metar_payload,
)

USER_AGENT = "weather/1.0 (DWD feed lag monitor)"
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / "dwd_feed_lag_log.csv"
SUMMARY_COLUMNS = (
    "logged_at_berlin",
    "dwd_latest",
    "dwd_lag_min",
    "dwd_values_today",
    "dwd_max",
    "dwd_max_time",
    "metar_latest",
    "metar_lag_min",
    "metar_values_today",
    "metar_max",
    "metar_max_time",
)
DWD_DETAIL_COLUMNS = (
    "dwd_STATIONS_ID",
    "dwd_MESS_DATUM",
    "dwd_QN",
    "dwd_PP_10",
    "dwd_TT_10",
    "dwd_TM5_10",
    "dwd_RF_10",
    "dwd_TD_10",
)
METAR_DETAIL_COLUMNS = (
    "metar_icaoId",
    "metar_receiptTime",
    "metar_obsTime",
    "metar_reportTime",
    "metar_temp",
    "metar_dewp",
    "metar_wdir",
    "metar_wspd",
    "metar_visib",
    "metar_altim",
    "metar_qcField",
    "metar_metarType",
    "metar_rawOb",
    "metar_lat",
    "metar_lon",
    "metar_elev",
    "metar_name",
    "metar_cover",
    "metar_fltCat",
    "metar_clouds",
)
CSV_COLUMNS = SUMMARY_COLUMNS + DWD_DETAIL_COLUMNS + METAR_DETAIL_COLUMNS


@dataclass(frozen=True)
class FeedSnapshot:
    source: str
    latest_time: datetime | None
    lag_minutes: int | None
    count_today: int
    max_temp: float | None
    max_time: datetime | None
    latest_record: dict[str, object]


def lag_minutes(latest: datetime | None, reference: datetime) -> int | None:
    if latest is None:
        return None
    return max(0, int((reference - latest).total_seconds() // 60))


def fetch_dwd_now_rows() -> list[dict[str, str]]:
    request = urllib.request.Request(DWD_TEMP_NOW_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))

    data_file = next(
        name
        for name in archive.namelist()
        if name.endswith(".txt") and not name.startswith("Metadaten_")
    )
    lines = archive.read(data_file).decode("latin-1").splitlines()
    header = [column.strip() for column in lines[0].split(";")]
    rows = []
    for line in lines[1:]:
        if not line.startswith(" "):
            continue
        values = [value.strip() for value in line.split(";")]
        row = dict(zip(header, values))
        if row.get("eor") == "eor":
            row.pop("eor", None)
        rows.append(row)
    return rows


def parse_dwd_now_status(target_date: date, reference: datetime) -> FeedSnapshot:
    empty = FeedSnapshot("DWD /now", None, None, 0, None, None, {})
    try:
        rows = fetch_dwd_now_rows()
    except (urllib.error.URLError, TimeoutError, zipfile.BadZipFile, StopIteration, ValueError):
        return empty

    prefix = target_date.strftime("%Y%m%d")
    readings: list[tuple[datetime, float, dict[str, str]]] = []
    for row in rows:
        mess_datum = row.get("MESS_DATUM", "")
        if not mess_datum.startswith(prefix):
            continue
        temp = row.get("TT_10", "")
        if temp in {"", "-999"}:
            continue
        moment = datetime.strptime(mess_datum, "%Y%m%d%H%M").replace(tzinfo=BERLIN)
        readings.append((moment, float(temp), row))

    if not readings:
        return empty

    readings.sort(key=lambda item: item[0])
    latest_time, _, latest_record = readings[-1]
    max_time, max_temp, _ = max(readings, key=lambda item: item[1])
    return FeedSnapshot(
        "DWD /now",
        latest_time,
        lag_minutes(latest_time, reference),
        len(readings),
        max_temp,
        max_time,
        latest_record,
    )


def latest_metar_observation(target_date: date) -> dict[str, object] | None:
    payload = fetch_json_safe(f"{AVIATION_METAR_URL}&hours=24")
    if not isinstance(payload, list):
        return None

    observations: list[tuple[datetime, dict[str, object]]] = []
    for observation in payload:
        if not isinstance(observation, dict):
            continue
        moment = observation_moment(observation, target_date)
        if moment is None or moment.date() != target_date:
            continue
        observations.append((moment, observation))

    if not observations:
        return None

    observations.sort(key=lambda item: item[0])
    return observations[-1][1]


def fetch_metar_snapshot(target_date: date, reference: datetime) -> FeedSnapshot:
    status = parse_metar_payload(
        fetch_json_safe(f"{AVIATION_METAR_URL}&hours=24"),
        target_date,
    )
    latest_record = latest_metar_observation(target_date) or {}
    if status is None:
        return FeedSnapshot("METAR EDDM", None, None, 0, None, None, latest_record)

    return FeedSnapshot(
        "METAR EDDM",
        status.latest_time,
        lag_minutes(status.latest_time, reference),
        status.count,
        status.max_temp,
        status.max_time,
        latest_record,
    )


def format_dt(moment: datetime | None) -> str:
    if moment is None:
        return ""
    return moment.strftime("%Y-%m-%d %H:%M")


def serialize_metar_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def prefixed_dwd_fields(record: dict[str, object]) -> dict[str, str]:
    return {
        f"dwd_{column}": str(record.get(column, "") or "")
        for column in ("STATIONS_ID", "MESS_DATUM", "QN", "PP_10", "TT_10", "TM5_10", "RF_10", "TD_10")
    }


def prefixed_metar_fields(record: dict[str, object]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for column in (
        "icaoId",
        "receiptTime",
        "obsTime",
        "reportTime",
        "temp",
        "dewp",
        "wdir",
        "wspd",
        "visib",
        "altim",
        "qcField",
        "metarType",
        "rawOb",
        "lat",
        "lon",
        "elev",
        "name",
        "cover",
        "fltCat",
    ):
        fields[f"metar_{column}"] = serialize_metar_value(record.get(column))
    fields["metar_clouds"] = serialize_metar_value(record.get("clouds"))
    return fields


def ensure_log_header() -> None:
    if not LOG_FILE.exists():
        with LOG_FILE.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(CSV_COLUMNS)
        return

    with LOG_FILE.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        existing_header = next(reader, [])

    if existing_header == list(CSV_COLUMNS):
        return

    existing_rows: list[dict[str, str]] = []
    if existing_header:
        with LOG_FILE.open("r", encoding="utf-8", newline="") as handle:
            existing_rows = list(csv.DictReader(handle))

    with LOG_FILE.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for old_row in existing_rows:
            writer.writerow({column: old_row.get(column, "") for column in CSV_COLUMNS})


def append_log_row(
    reference: datetime,
    dwd: FeedSnapshot,
    metar: FeedSnapshot,
) -> dict[str, str]:
    row: dict[str, str] = {
        "logged_at_berlin": reference.strftime("%Y-%m-%d %H:%M"),
        "dwd_latest": format_dt(dwd.latest_time),
        "dwd_lag_min": "" if dwd.lag_minutes is None else str(dwd.lag_minutes),
        "dwd_values_today": str(dwd.count_today),
        "dwd_max": "" if dwd.max_temp is None else f"{dwd.max_temp:.1f}",
        "dwd_max_time": format_dt(dwd.max_time),
        "metar_latest": format_dt(metar.latest_time),
        "metar_lag_min": "" if metar.lag_minutes is None else str(metar.lag_minutes),
        "metar_values_today": str(metar.count_today),
        "metar_max": "" if metar.max_temp is None else f"{metar.max_temp:.0f}",
        "metar_max_time": format_dt(metar.max_time),
    }
    row.update(prefixed_dwd_fields(dwd.latest_record))
    row.update(prefixed_metar_fields(metar.latest_record))

    ensure_log_header()
    with LOG_FILE.open("a", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=CSV_COLUMNS).writerow(row)
    return row


def print_summary(reference: datetime, dwd: FeedSnapshot, metar: FeedSnapshot) -> None:
    print(f"Stand: {reference:%d.%m.%Y %H:%M} Ortszeit")
    if dwd.latest_time is None:
        print("DWD /now:   keine Daten")
    else:
        print(
            f"DWD /now:   letzter {dwd.latest_time:%H:%M}, "
            f"Verzögerung {dwd.lag_minutes} Min., "
            f"Max {dwd.max_temp:.1f}°C um {dwd.max_time:%H:%M} ({dwd.count_today} Werte)"
        )
        print(f"  DWD Rohdaten: {prefixed_dwd_fields(dwd.latest_record)}")
    if metar.latest_time is None:
        print("METAR:      keine Daten")
    else:
        print(
            f"METAR:      letzter {metar.latest_time:%H:%M}, "
            f"Verzögerung {metar.lag_minutes} Min., "
            f"Max {metar.max_temp:.0f}°C um {metar.max_time:%H:%M} ({metar.count_today} Meldungen)"
        )
        print(f"  METAR Rohdaten: {prefixed_metar_fields(metar.latest_record)}")


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    reference = datetime.now(BERLIN)
    target_date = reference.date()

    dwd = parse_dwd_now_status(target_date, reference)
    metar = fetch_metar_snapshot(target_date, reference)
    print_summary(reference, dwd, metar)

    if dry_run:
        print(f"\n[dry-run] Kein Eintrag in {LOG_FILE.name}.", file=sys.stderr)
        return 0

    row = append_log_row(reference, dwd, metar)
    print(f"\nEintrag in {LOG_FILE.name}: {row['logged_at_berlin']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
