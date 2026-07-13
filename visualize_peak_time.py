#!/usr/bin/env python3
"""Peakzeit der DWD TT_10-Temperatur nach Kalenderwoche (München-Flughafen)."""

import io
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, MaxNLocator

USER_AGENT = "weather/1.0 (Munich Airport peak time chart)"
BERLIN = ZoneInfo("Europe/Berlin")
STATION_ID = "01262"
DWD_10MIN_URLS = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    f"climate/10_minutes/air_temperature/recent/10minutenwerte_TU_{STATION_ID}_akt.zip",
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    f"climate/10_minutes/air_temperature/historical/"
    f"10minutenwerte_TU_{STATION_ID}_20200101_20251231_hist.zip",
)
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "peak_time_by_week.png"
DAY_OUTPUT_PATH = SCRIPT_DIR / "peak_time_by_day.png"
TEXT_PATH = SCRIPT_DIR / "peak_time_by_week.txt"
ARTIFACT_PATH = Path("/opt/cursor/artifacts/peak_time_by_week.png")
DAY_ARTIFACT_PATH = Path("/opt/cursor/artifacts/peak_time_by_day.png")

MONTH_BY_WEEK = {
    3: "Januar", 7: "Februar", 11: "März", 16: "April", 20: "Mai",
    24: "Juni", 29: "Juli", 33: "August", 37: "September", 42: "Oktober",
    46: "November", 50: "Dezember",
}


def month_label_for_week(iso_week: int) -> str:
    for week, month in sorted(MONTH_BY_WEEK.items(), reverse=True):
        if iso_week >= week:
            return month
    return "Januar"


def load_daily_peaks() -> list[tuple[date, int, int, float]]:
    by_day: dict[date, list[tuple[datetime, float]]] = defaultdict(list)

    for url in DWD_10MIN_URLS:
        for row in fetch_dwd_zip_rows(url):
            mess_datum = row.get("MESS_DATUM", "")
            temp = row.get("TT_10", "")
            if len(mess_datum) < 12 or temp in {"", "-999"}:
                continue
            moment = datetime.strptime(mess_datum, "%Y%m%d%H%M").replace(tzinfo=BERLIN)
            by_day[moment.date()].append((moment, float(temp)))

    peaks = []
    for day, readings in sorted(by_day.items()):
        peak_time, peak_temp = max(readings, key=lambda item: item[1])
        peak_minutes = peak_time.hour * 60 + peak_time.minute
        iso_week = peak_time.isocalendar().week
        peaks.append((day, iso_week, peak_minutes, peak_temp))
    return peaks


def fetch_dwd_zip_rows(url: str) -> list[dict[str, str]]:
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


def load_daily_peak_minutes() -> list[tuple[date, int, int]]:
    return [(day, week, minutes) for day, week, minutes, _ in load_daily_peaks()]


def write_peak_time_text(
    daily_peaks: list[tuple[date, int, int, float]],
    output_path: Path = TEXT_PATH,
) -> Path:
    by_week_minutes: dict[int, list[int]] = defaultdict(list)
    by_week_temps: dict[int, list[float]] = defaultdict(list)
    for _, iso_week, peak_minutes, peak_temp in daily_peaks:
        by_week_minutes[iso_week].append(peak_minutes)
        by_week_temps[iso_week].append(peak_temp)

    years = {day.year for day, _, _, _ in daily_peaks}
    year_label = (
        f"{min(years)}"
        if len(years) == 1
        else f"{min(years)}–{max(years)}"
    )

    lines = [
        "Peakzeit TT_10 nach Kalenderwoche – München-Flughafen (DWD 01262)",
        f"Zeitraum: {year_label} | {len(daily_peaks)} Tage | Ortszeit Europe/Berlin",
        "",
        f"{'KW':>3}  {'Monat':<10} {'Tage':>4}  {'Median':>6}  {'Q1':>6}  {'Q3':>6}  {'Ø Max°C':>7}  Praxis-Fenster",
        "-" * 72,
    ]

    for iso_week in sorted(by_week_minutes):
        minutes = sorted(by_week_minutes[iso_week])
        temps = by_week_temps[iso_week]
        count = len(minutes)
        q1 = minutes[count // 4]
        q3 = minutes[(3 * count) // 4]
        med = int(median(minutes))
        avg_temp = sum(temps) / len(temps)
        window = f"{minutes_to_time_label(q1)} – {minutes_to_time_label(q3)}"
        lines.append(
            f"{iso_week:3d}  {month_label_for_week(iso_week):<10} {count:4d}  "
            f"{minutes_to_time_label(med):>6}  {minutes_to_time_label(q1):>6}  "
            f"{minutes_to_time_label(q3):>6}  {avg_temp:7.1f}  {window}"
        )

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output_path


def minutes_to_time_label(minutes: float) -> str:
    total = int(round(minutes)) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def plot_peak_time_by_week(output_path: Path = OUTPUT_PATH) -> tuple[Path, Path]:
    daily_peaks = load_daily_peaks()
    if not daily_peaks:
        raise ValueError("Keine DWD-Daten für Peakzeiten gefunden")

    text_path = write_peak_time_text(daily_peaks)

    by_week: dict[int, list[int]] = defaultdict(list)
    for _, iso_week, peak_minutes, _ in daily_peaks:
        by_week[iso_week].append(peak_minutes)

    weeks = sorted(by_week)
    medians = [median(by_week[week]) for week in weeks]
    q1 = []
    q3 = []
    for week in weeks:
        values = sorted(by_week[week])
        q1.append(values[len(values) // 4])
        q3.append(values[(3 * len(values)) // 4])

    years = {day.year for day, _, _, _ in daily_peaks}
    year_label = (
        f"{min(years)}"
        if len(years) == 1
        else f"{min(years)}–{max(years)}"
    )

    fig, ax = plt.subplots(figsize=(13, 6))

    for _, iso_week, peak_minutes, _ in daily_peaks:
        ax.scatter(
            iso_week,
            peak_minutes,
            color="#4c78a8",
            alpha=0.08,
            s=12,
            zorder=1,
        )

    ax.fill_between(weeks, q1, q3, color="#e4572e", alpha=0.15, label="25.–75. Perzentil")
    ax.plot(
        weeks,
        medians,
        color="#e4572e",
        linewidth=2.5,
        marker="o",
        markersize=5,
        label="Median Peakzeit",
        zorder=3,
    )

    ax.set_title(
        "Tagesmaximum TT_10 nach Kalenderwoche – Münchner Flughafen (DWD 01262)\n"
        f"Median der Peak-Uhrzeit, {year_label}, {len(daily_peaks)} Tage",
        fontsize=13,
        fontweight="bold",
    )
    ax.set_xlabel("Kalenderwoche (ISO)")
    ax.set_ylabel("Peakzeit (Ortszeit)")
    ax.set_xlim(min(weeks) - 0.5, max(weeks) + 0.5)
    ax.set_ylim(8 * 60, 18 * 60)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True, nbins=26))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: minutes_to_time_label(value)))
    ax.yaxis.set_major_locator(plt.MultipleLocator(60))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")

    month_ticks = []
    month_labels = []
    for month in range(1, 13):
        sample = date(min(years), month, 15)
        month_ticks.append(sample.isocalendar().week)
        month_labels.append(sample.strftime("%b"))
    ax_top = ax.twiny()
    ax_top.set_xlim(ax.get_xlim())
    ax_top.set_xticks(month_ticks)
    ax_top.set_xticklabels(month_labels)
    ax_top.set_xlabel("Monat (ungefähr)")

    fig.autofmt_xdate()
    plt.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    if output_path != ARTIFACT_PATH:
        ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(ARTIFACT_PATH, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path, text_path


def rolling_median(values: list[float], window: int) -> list[float]:
    medians = []
    for index in range(len(values)):
        start = max(0, index - window + 1)
        medians.append(median(values[start : index + 1]))
    return medians


def plot_peak_time_by_day(output_path: Path = DAY_OUTPUT_PATH) -> Path:
    daily_peaks = load_daily_peaks()
    if not daily_peaks:
        raise ValueError("Keine DWD-Daten für Peakzeiten gefunden")

    days = [datetime.combine(day, datetime.min.time()) for day, _, _, _ in daily_peaks]
    peak_minutes = [minutes for _, _, minutes, _ in daily_peaks]
    peak_temps = [temp for _, _, _, temp in daily_peaks]

    years = {day.year for day, _, _, _ in daily_peaks}
    year_label = (
        f"{min(years)}"
        if len(years) == 1
        else f"{min(years)}–{max(years)}"
    )

    smooth_window = 30
    smoothed = rolling_median(peak_minutes, smooth_window)

    fig, ax = plt.subplots(figsize=(14, 6))
    scatter = ax.scatter(
        days,
        peak_minutes,
        c=peak_temps,
        cmap="YlOrRd",
        alpha=0.45,
        s=10,
        linewidths=0,
        label="Tages-Peakzeit",
        zorder=2,
    )
    ax.plot(
        days,
        smoothed,
        color="#4c78a8",
        linewidth=2,
        label=f"{smooth_window}-Tage-Median",
        zorder=3,
    )

    highlight = date(2026, 7, 7)
    for day, minutes, temp in zip(
        [item[0] for item in daily_peaks],
        peak_minutes,
        peak_temps,
    ):
        if day == highlight:
            ax.scatter(
                [datetime.combine(day, datetime.min.time())],
                [minutes],
                color="#7b2cbf",
                s=80,
                zorder=4,
                label=f"07.07.2026 ({minutes_to_time_label(minutes)}, {temp:.1f}°C)",
            )
            break

    ax.set_title(
        "Tagesmaximum TT_10 – Peakzeit pro Tag – Münchner Flughafen (DWD 01262)\n"
        f"{year_label}, {len(daily_peaks)} Tage, Farbe = Tagesmaximum (°C)",
        fontsize=13,
        fontweight="bold",
    )
    ax.set_xlabel("Tag")
    ax.set_ylabel("Peakzeit (Ortszeit)")
    ax.set_ylim(8 * 60, 18 * 60)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: minutes_to_time_label(value)))
    ax.yaxis.set_major_locator(plt.MultipleLocator(60))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right", fontsize=9)
    colorbar = fig.colorbar(scatter, ax=ax, pad=0.01)
    colorbar.set_label("Tagesmaximum (°C)")

    fig.autofmt_xdate()
    plt.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    if output_path != DAY_ARTIFACT_PATH:
        DAY_ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(DAY_ARTIFACT_PATH, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def main() -> None:
    try:
        chart_path, text_path = plot_peak_time_by_week()
        day_chart_path = plot_peak_time_by_day()
        print(text_path.read_text(encoding="utf-8"))
        print(f"Chart (KW) gespeichert:  {chart_path.resolve()}")
        print(f"Chart (Tag) gespeichert: {day_chart_path.resolve()}")
        print(f"Liste gespeichert:       {text_path.resolve()}")
    except Exception as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
