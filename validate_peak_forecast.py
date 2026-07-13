#!/usr/bin/env python3
"""Validiert die Peak-Prognose (Main.py) gegen historische DWD-10-Min-Daten.

Wichtig: Die Auslösung nutzt nur Daten bis zum Signalzeitpunkt (kein Blick in die
Zukunft). Peak-Zeit und Tagesmaximum werden erst nachträglich zur Auswertung heran-
gezogen — so wie in Live-Betrieb die Zukunft unbekannt ist.
"""

import io
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

from Main import analyze_peak_forecast, truncate_celsius

USER_AGENT = "weather/1.0 (Munich Airport peak forecast validation)"
BERLIN = ZoneInfo("Europe/Berlin")
STATION_ID = "01262"
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "peak_forecast_validation.txt"

DWD_URLS = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    f"climate/10_minutes/air_temperature/recent/10minutenwerte_TU_{STATION_ID}_akt.zip",
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    f"climate/10_minutes/air_temperature/historical/"
    f"10minutenwerte_TU_{STATION_ID}_20200101_20251231_hist.zip",
)

ACTIONABLE = {"slowing", "plateau", "likely_passed"}


def fetch_rows(url: str) -> list[dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
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
        rows.append(dict(zip(header, values)))
    return rows


def load_daily_readings() -> dict[date, list[tuple[datetime, float]]]:
    by_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)
    for url in DWD_URLS:
        for row in fetch_rows(url):
            mess_datum = row.get("MESS_DATUM", "")
            temp = row.get("TT_10", "")
            if len(mess_datum) < 12 or temp in {"", "-999"}:
                continue
            moment = datetime.strptime(mess_datum, "%Y%m%d%H%M").replace(tzinfo=BERLIN)
            by_day[moment.date()].append((moment, float(temp)))
    for day in by_day:
        by_day[day].sort()
    return by_day


def format_time(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def actual_peak_info(readings: list[tuple[datetime, float]]) -> tuple[float, int]:
    peak_temp = max(temp for _, temp in readings)
    peak_time = max(readings, key=lambda item: item[1])[0]
    peak_minutes = peak_time.hour * 60 + peak_time.minute
    return peak_temp, peak_minutes


def evaluate_forecast(forecast, actual_peak: float) -> dict[str, object]:
    actual_resolved = truncate_celsius(actual_peak)
    est_lo = truncate_celsius(forecast.est_tmax_low)
    est_hi = truncate_celsius(forecast.est_tmax_high)
    est_mid = truncate_celsius((forecast.est_tmax_low + forecast.est_tmax_high) / 2)
    return {
        "actual_peak": actual_peak,
        "actual_resolved": actual_resolved,
        "est_lo": est_lo,
        "est_hi": est_hi,
        "est_mid": est_mid,
        "hit_range": est_lo <= actual_resolved <= est_hi,
        "hit_mid": est_mid == actual_resolved,
        "hit_run_max": truncate_celsius(forecast.run_max) == actual_resolved,
    }


def find_first_live_signal(
    readings: list[tuple[datetime, float]],
    min_hour: int = 12,
    signal_filter: set[str] | None = None,
) -> dict[str, object] | None:
    """Erstes Signal — nur Vergangenheitsdaten, keine Peak-Zeit nötig."""
    for index in range(2, len(readings) + 1):
        forecast = analyze_peak_forecast(readings[:index])
        if forecast is None:
            continue

        signal_minutes = forecast.current_time.hour * 60 + forecast.current_time.minute
        if signal_minutes < min_hour * 60:
            continue
        if forecast.status not in ACTIONABLE:
            continue
        if signal_filter and forecast.status not in signal_filter:
            continue

        return {
            "signal_minutes": signal_minutes,
            "status": forecast.status,
            "forecast": forecast,
        }
    return None


def enrich_with_retrospective(
    match: dict[str, object],
    actual_peak: float,
    actual_peak_minutes: int,
) -> dict[str, object]:
    """Peak-Infos nur für Auswertung nachträglich ergänzen."""
    signal_minutes = int(match["signal_minutes"])
    minutes_before_peak = actual_peak_minutes - signal_minutes
    metrics = evaluate_forecast(match["forecast"], actual_peak)
    return {
        **match,
        **metrics,
        "minutes_before_peak": minutes_before_peak,
        "before_peak": minutes_before_peak > 0,
        "at_or_after_peak": minutes_before_peak <= 0,
    }


def run_backtest(
    by_day: dict[date, list[tuple[datetime, float]]],
    min_hour: int = 12,
    summer_only: bool = False,
    signal_filter: set[str] | None = None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for day, readings in by_day.items():
        if summer_only and day.month not in {6, 7, 8}:
            continue
        if len(readings) < 20:
            continue

        match = find_first_live_signal(readings, min_hour=min_hour, signal_filter=signal_filter)
        if match is None:
            continue

        actual_peak, actual_peak_minutes = actual_peak_info(readings)
        rows.append(
            enrich_with_retrospective(match, actual_peak, actual_peak_minutes) | {"day": day}
        )
    return rows


def summarize_rows(title: str, rows: list[dict[str, object]]) -> list[str]:
    if not rows:
        return [f"\n{title}: keine Daten"]

    signal_times = [int(row["signal_minutes"]) for row in rows]
    before = [row for row in rows if row["before_peak"]]
    after = [row for row in rows if row["at_or_after_peak"]]
    sorted_times = sorted(signal_times)
    time_quarter = max(0, len(sorted_times) // 4)
    time_three_quarter = min(len(sorted_times) - 1, (3 * len(sorted_times)) // 4)

    lines = [
        f"\n{title}  (n={len(rows)})",
        (
            f"  Auslösung:         median {format_time(int(median(signal_times)))}  "
            f"Q1–Q3 {format_time(sorted_times[time_quarter])}–{format_time(sorted_times[time_three_quarter])}"
        ),
        f"  Treffer Range:     {100 * sum(row['hit_range'] for row in rows) / len(rows):.1f}%",
        f"  Treffer Mitte:     {100 * sum(row['hit_mid'] for row in rows) / len(rows):.1f}%",
        f"  Treffer run_max:   {100 * sum(row['hit_run_max'] for row in rows) / len(rows):.1f}%",
    ]

    status_counts = Counter(str(row["status"]) for row in rows)
    lines.append(
        "  Signal-Typ:        "
        + ", ".join(f"{status}={count}" for status, count in status_counts.most_common())
    )

    if before:
        gaps = [int(row["minutes_before_peak"]) for row in before]
        sorted_gaps = sorted(gaps)
        gap_quarter = max(0, len(sorted_gaps) // 4)
        gap_three_quarter = min(len(sorted_gaps) - 1, (3 * len(sorted_gaps)) // 4)
        lines.extend(
            [
                f"  Davon VOR Peak:    {len(before)} ({100 * len(before) / len(rows):.1f}%)",
                (
                    f"    → Min vor Peak:  median {median(gaps):.0f}  "
                    f"Ø {mean(gaps):.0f}  Q1–Q3 {sorted_gaps[gap_quarter]}–{sorted_gaps[gap_three_quarter]}"
                ),
                (
                    f"    → Treffer Range: {100 * sum(row['hit_range'] for row in before) / len(before):.1f}%  "
                    f"run_max: {100 * sum(row['hit_run_max'] for row in before) / len(before):.1f}%"
                ),
            ]
        )

    if after:
        lines.extend(
            [
                f"  Davon NACH Peak:   {len(after)} ({100 * len(after) / len(rows):.1f}%)",
                (
                    f"    → Treffer Range: {100 * sum(row['hit_range'] for row in after) / len(after):.1f}%  "
                    f"run_max: {100 * sum(row['hit_run_max'] for row in after) / len(after):.1f}%"
                ),
            ]
        )

    return lines


def build_report(by_day: dict[date, list[tuple[datetime, float]]]) -> str:
    total_days = sum(1 for readings in by_day.values() if len(readings) >= 20)
    total_summer = sum(
        1 for day, readings in by_day.items() if len(readings) >= 20 and day.month in {6, 7, 8}
    )

    lines = [
        "Validierung Peak-Prognose – München-Flughafen (DWD 01262)",
        "Zeitraum: 2020–2026 | Logik: Main.py analyze_peak_forecast",
        "Auflösung: int(T + 0.5)  (ab 0,5 aufrunden, Polymarket/Wunderground)",
        f"Tage gesamt: {total_days}  |  Sommertage (Jun–Aug): {total_summer}",
        "",
        "Methodik:",
        "  - Auslösung: chronologisch, nur Messwerte bis zum Signalzeitpunkt",
        "  - Peak-Zeit ist bei Auslösung UNBEKANNT (kein Filtern nach Peak-Nähe)",
        "  - Peak-Zeit/Tagesmax nur nachträglich zur Auswertung der Trefferquote",
        "",
        "Treffer-Definitionen (Zieltemperatur = aufgelöstes Tagesmaximum):",
        "  Range   = Ist-°C liegt im Prognose-Intervall [est_lo, est_hi]",
        "  Mitte   = Prognose-Mitte exakt",
        "  run_max = aufgelöstes laufendes Maximum zum Signalzeitpunkt exakt",
    ]

    scenarios = [
        ("Erstes Nachmittags-Signal ab 12:00 (alle)", dict(min_hour=12)),
        ("Erstes Nachmittags-Signal ab 12:00 (Sommer)", dict(min_hour=12, summer_only=True)),
        ("Erstes Nachmittags-Signal ab 13:00 (Sommer)", dict(min_hour=13, summer_only=True)),
    ]

    for title, kwargs in scenarios:
        rows = run_backtest(by_day, **kwargs)
        lines.extend(summarize_rows(title, rows))
        total = total_summer if kwargs.get("summer_only") else total_days
        lines.append(f"  Abdeckung:         {len(rows)}/{total} = {100 * len(rows) / total:.1f}%")

    lines.append("\n--- Nach Signal-Typ (ab 12:00, Sommer, ohne Peak-Vorwissen) ---")
    for signal in ("slowing", "plateau", "likely_passed"):
        rows = run_backtest(by_day, min_hour=12, summer_only=True, signal_filter={signal})
        lines.extend(summarize_rows(f"  {signal}", rows))

    lines.extend(
        [
            "",
            "Interpretation:",
            "  - slowing/plateau feuern oft 1–2 h VOR dem Peak (Fehlalarm möglich).",
            "  - Trefferquote VOR Peak ist deutlich niedriger als NACH Peak-Bestätigung.",
            "  - likely_passed: erst wenn Signal NACH dem Peak kommt, ist run_max verlässlich.",
            "  - Für Polymarket: Zieltemperatur erst nach Peak-Bestätigung sinnvoll setzen.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    try:
        by_day = load_daily_readings()
        report = build_report(by_day)
        output_path = Path(OUTPUT_PATH)
        output_path.write_text(report, encoding="utf-8")
        print(report)
        print(f"Report gespeichert: {output_path.resolve()}")
    except Exception as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
