#!/usr/bin/env python3
"""Validiert Open-Meteo Historical Forecast gegen METAR EDDM (München-Flughafen).

Vergleicht Tagesmaximum, stündliche Prognose 10–18 Uhr und Peak-Zeiten.
METAR-Quelle: IEM ASOS-Archiv. Prognose: Open-Meteo Historical Forecast API.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from Main import truncate_celsius

USER_AGENT = "weather/1.0 (Munich Open-Meteo vs METAR validation)"
BERLIN = ZoneInfo("Europe/Berlin")
LAT, LON = 48.3538, 11.7861
IEM_STATION = "EDDM"
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "open_meteo_metar_validation.txt"
DEFAULT_START = date(2024, 1, 1)
PROGNOSIS_START_HOUR = 10
PROGNOSIS_END_HOUR = 18
MIN_METAR_READINGS = 8


@dataclass(frozen=True)
class DayComparison:
    day: date
    metar_max: float
    metar_resolved: int
    metar_peak_minutes: int
    forecast_max: float
    forecast_resolved: int
    forecast_peak_hour: int
    resolved_delta: int
    abs_error: float
    hourly_pairs: tuple[tuple[int, float, float], ...]


def fahrenheit_to_celsius(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


def fetch_bytes(url: str, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    ranges: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        month_end = min(end, next_month - timedelta(days=1))
        ranges.append((max(start, cursor), month_end))
        cursor = next_month
    return ranges


def load_iem_metar(start: date, end: date) -> dict[date, list[tuple[datetime, float]]]:
    query = urllib.parse.urlencode(
        [
            ("station", IEM_STATION),
            ("data", "tmpf"),
            ("year1", str(start.year)),
            ("month1", str(start.month)),
            ("day1", str(start.day)),
            ("year2", str(end.year)),
            ("month2", str(end.month)),
            ("day2", str(end.day)),
            ("tz", "Europe/Berlin"),
            ("format", "onlycomma"),
            ("latlon", "no"),
            ("elev", "no"),
            ("missing", "M"),
            ("trace", "T"),
            ("direct", "no"),
            ("report_type", "3"),
            ("report_type", "4"),
        ]
    )
    payload = fetch_bytes(
        f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?{query}"
    ).decode("utf-8")
    by_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    for row in csv.DictReader(io.StringIO(payload)):
        if row.get("tmpf") in {None, "", "M"}:
            continue
        moment = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)
        if moment.date() < start or moment.date() > end:
            continue
        by_day[moment.date()].append((moment, fahrenheit_to_celsius(float(row["tmpf"]))))
    for day in by_day:
        by_day[day].sort()
    return by_day


def load_open_meteo_forecast(start: date, end: date) -> tuple[dict[date, float], dict[datetime, float]]:
    daily_maxima: dict[date, float] = {}
    hourly_values: dict[datetime, float] = {}
    for chunk_start, chunk_end in month_ranges(start, end):
        query = urllib.parse.urlencode(
            {
                "latitude": LAT,
                "longitude": LON,
                "start_date": chunk_start.isoformat(),
                "end_date": chunk_end.isoformat(),
                "hourly": "temperature_2m",
                "daily": "temperature_2m_max",
                "timezone": "Europe/Berlin",
            }
        )
        payload = json.loads(
            fetch_bytes(f"https://historical-forecast-api.open-meteo.com/v1/forecast?{query}").decode(
                "utf-8"
            )
        )
        daily = payload.get("daily", {})
        for day_text, value in zip(daily.get("time", []), daily.get("temperature_2m_max", [])):
            if value is not None:
                daily_maxima[date.fromisoformat(day_text)] = float(value)
        hourly = payload.get("hourly", {})
        for time_label, value in zip(hourly.get("time", []), hourly.get("temperature_2m", [])):
            if value is not None:
                hourly_values[datetime.fromisoformat(time_label).replace(tzinfo=BERLIN)] = float(
                    value
                )
    return daily_maxima, hourly_values


def metar_hourly_max(readings: list[tuple[datetime, float]], hour: int) -> float | None:
    values = [temp for moment, temp in readings if moment.hour == hour]
    return max(values) if values else None


def forecast_peak_hour(
    day: date,
    hourly_values: dict[datetime, float],
) -> int | None:
    day_hours = [
        (moment.hour, temp)
        for moment, temp in hourly_values.items()
        if moment.date() == day and PROGNOSIS_START_HOUR <= moment.hour <= PROGNOSIS_END_HOUR
    ]
    if not day_hours:
        return None
    return max(day_hours, key=lambda item: item[1])[0]


def compare_day(
    day: date,
    metar_readings: list[tuple[datetime, float]],
    forecast_max: float,
    hourly_values: dict[datetime, float],
) -> DayComparison | None:
    if len(metar_readings) < MIN_METAR_READINGS:
        return None

    metar_peak_moment, metar_max = max(metar_readings, key=lambda item: item[1])
    metar_peak_minutes = metar_peak_moment.hour * 60 + metar_peak_moment.minute
    forecast_peak = forecast_peak_hour(day, hourly_values)
    if forecast_peak is None:
        return None

    hourly_pairs: list[tuple[int, float, float]] = []
    for hour in range(PROGNOSIS_START_HOUR, PROGNOSIS_END_HOUR + 1):
        metar_value = metar_hourly_max(metar_readings, hour)
        forecast_value = hourly_values.get(datetime(day.year, day.month, day.day, hour, tzinfo=BERLIN))
        if metar_value is None or forecast_value is None:
            continue
        hourly_pairs.append((hour, forecast_value, metar_value))

    if not hourly_pairs:
        return None

    metar_resolved = truncate_celsius(metar_max)
    forecast_resolved = truncate_celsius(forecast_max)
    return DayComparison(
        day=day,
        metar_max=metar_max,
        metar_resolved=metar_resolved,
        metar_peak_minutes=metar_peak_minutes,
        forecast_max=forecast_max,
        forecast_resolved=forecast_resolved,
        forecast_peak_hour=forecast_peak,
        resolved_delta=forecast_resolved - metar_resolved,
        abs_error=abs(forecast_max - metar_max),
        hourly_pairs=tuple(hourly_pairs),
    )


def format_time(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def summarize_hourly_bias(days: list[DayComparison]) -> list[str]:
    by_hour: dict[int, list[float]] = defaultdict(list)
    for entry in days:
        for hour, forecast_value, metar_value in entry.hourly_pairs:
            by_hour[hour].append(forecast_value - metar_value)

    lines = [
        "",
        f"Stündlicher Bias Open-Meteo − METAR ({PROGNOSIS_START_HOUR:02d}–{PROGNOSIS_END_HOUR:02d} Uhr):",
        f"{'Stunde':>6} {'n':>5} {'Bias':>8} {'MAE':>8} {'Ø Ist':>8} {'Ø Prog':>8}",
        "-" * 50,
    ]
    for hour in range(PROGNOSIS_START_HOUR, PROGNOSIS_END_HOUR + 1):
        deltas = by_hour.get(hour, [])
        if not deltas:
            continue
        mae = mean(abs(delta) for delta in deltas)
        metar_values = [
            metar_value
            for entry in days
            for h, _, metar_value in entry.hourly_pairs
            if h == hour
        ]
        forecast_values = [
            forecast_value
            for entry in days
            for h, forecast_value, _ in entry.hourly_pairs
            if h == hour
        ]
        lines.append(
            f"{hour:02d}:00 {len(deltas):5d} {mean(deltas):+8.2f} {mae:8.2f} "
            f"{mean(metar_values):8.1f} {mean(forecast_values):8.1f}"
        )
    return lines


def summarize_tmax(days: list[DayComparison], title: str) -> list[str]:
    if not days:
        return [f"\n{title}: keine Daten"]

    abs_errors = [entry.abs_error for entry in days]
    resolved_deltas = [entry.resolved_delta for entry in days]
    exact_hits = sum(entry.forecast_resolved == entry.metar_resolved for entry in days)
    within_one = sum(abs(entry.resolved_delta) <= 1 for entry in days)
    peak_hour_deltas = [
        abs(entry.forecast_peak_hour * 60 - entry.metar_peak_minutes) for entry in days
    ]

    lines = [
        f"\n{title}  (n={len(days)})",
        f"  Tmax Bias (Prog−METAR):   {mean(resolved_deltas):+.2f} °C (aufgelöst)",
        f"  Tmax MAE:                 {mean(abs_errors):.2f} °C",
        f"  Tmax Median-Fehler:       {median(abs_errors):.2f} °C",
        f"  Treffer exakt (resolved): {100 * exact_hits / len(days):.1f}%",
        f"  Treffer ±1 °C:            {100 * within_one / len(days):.1f}%",
        (
            f"  Peak-Zeit Δ:              median {median(peak_hour_deltas):.0f} min  "
            f"Ø {mean(peak_hour_deltas):.0f} min"
        ),
    ]

    under = sum(1 for delta in resolved_deltas if delta < 0)
    over = sum(1 for delta in resolved_deltas if delta > 0)
    equal = len(days) - under - over
    lines.append(f"  Prog zu niedrig / gleich / zu hoch: {under} / {equal} / {over}")
    return lines


def format_recent_days(days: list[DayComparison], limit: int = 14) -> list[str]:
    lines = ["", f"Letzte {limit} Tage im Detail:"]
    for entry in sorted(days, key=lambda item: item.day)[-limit:]:
        peak_delta = entry.forecast_peak_hour * 60 - entry.metar_peak_minutes
        lines.append(
            f"  {entry.day.strftime('%d.%m.%Y')}  "
            f"METAR {entry.metar_max:.1f}→{entry.metar_resolved}°C @{format_time(entry.metar_peak_minutes)}  "
            f"Prog {entry.forecast_max:.1f}→{entry.forecast_resolved}°C @{entry.forecast_peak_hour:02d}:00  "
            f"Δresolved {entry.resolved_delta:+d}  PeakΔ {peak_delta:+d} min"
        )
    return lines


def build_report(days: list[DayComparison], start: date, end: date) -> str:
    summer_days = [entry for entry in days if entry.day.month in {6, 7, 8}]
    hot_days = [entry for entry in days if entry.metar_resolved >= 25]

    lines = [
        "Validierung Open-Meteo vs. METAR – München-Flughafen (EDDM)",
        f"Zeitraum: {start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')}",
        "METAR: IEM ASOS-Archiv  |  Prognose: Open-Meteo Historical Forecast API",
        f"Auflösung: int(T + 0.5)  |  Stundenfenster: {PROGNOSIS_START_HOUR:02d}–{PROGNOSIS_END_HOUR:02d}",
        f"Vergleichstage: {len(days)}",
        "",
        "Methodik:",
        "  - METAR-Istwert je Stunde = Maximum der METAR-Meldungen in dieser Stunde",
        "  - Open-Meteo = archivierte Modellprognose (Historical Forecast, ab ~2021)",
        "  - Peak-Zeit METAR = Zeitpunkt des Tagesmaximums",
        "  - Peak-Zeit Prognose = Stunde mit höchster Prognose 10–18 Uhr",
    ]

    lines.extend(summarize_tmax(days, "Alle Tage"))
    lines.extend(summarize_tmax(summer_days, "Sommer (Jun–Aug)"))
    if hot_days:
        lines.extend(summarize_tmax(hot_days, "Heiße Tage (METAR ≥ 25 °C aufgelöst)"))
    lines.extend(summarize_hourly_bias(summer_days or days))
    lines.extend(format_recent_days(days))

    lines.extend(
        [
            "",
            "Interpretation:",
            "  - Negative Tmax-Bias = Open-Meteo unterschätzt das METAR-Tagesmaximum.",
            "  - Stunden-Bias zeigt, ab welcher Uhrzeit die Prognose typischerweise hinterherhinkt.",
            "  - Peak-Zeit-Vergleich ist grob (METAR :20/:50 vs. OM volle Stunde).",
            "  - Für Polymarket: METAR-Tmax ist Referenz; Prognose oft 0–1 °C zu niedrig.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open-Meteo vs. METAR Validierung EDDM")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    return parser.parse_args()


def main() -> None:
    try:
        args = parse_args()
        end = min(args.end or datetime.now(BERLIN).date(), datetime.now(BERLIN).date())
        if args.start > end:
            raise ValueError("start muss vor end liegen")

        print(f"Lade METAR {args.start} – {end} …", file=sys.stderr)
        metar_by_day = load_iem_metar(args.start, end)
        print(f"Lade Open-Meteo Historical Forecast …", file=sys.stderr)
        forecast_daily, forecast_hourly = load_open_meteo_forecast(args.start, end)

        comparisons: list[DayComparison] = []
        for day in sorted(set(metar_by_day) & set(forecast_daily)):
            entry = compare_day(day, metar_by_day[day], forecast_daily[day], forecast_hourly)
            if entry is not None:
                comparisons.append(entry)

        report = build_report(comparisons, args.start, end)
        args.output.write_text(report, encoding="utf-8")
        print(report)
        print(f"Report gespeichert: {args.output.resolve()}", file=sys.stderr)
    except Exception as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
