#!/usr/bin/env python3
"""Visualisiert Temperaturverlauf und Polymarket-30°C-Quote am Münchner Flughafen."""

import io
import json
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

USER_AGENT = "weather/1.0 (Munich Airport temperature chart)"
BERLIN = ZoneInfo("Europe/Berlin")
STATION_ID = "01262"
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
MONTH_NAMES = (
    "",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
DWD_TEMP_NOW_URL = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    f"climate/10_minutes/air_temperature/now/10minutenwerte_TU_{STATION_ID}_now.zip"
)
DWD_TEMP_RECENT_URL = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/"
    f"climate/10_minutes/air_temperature/recent/10minutenwerte_TU_{STATION_ID}_akt.zip"
)
METAR_URL = "https://aviationweather.gov/api/data/metar?ids=EDDM&format=json&hours=48"
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "temperature_munich.png"
ARTIFACT_PATH = Path("/opt/cursor/artifacts/temperature_munich.png")


def fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_dwd_zip_rows(url: str) -> list[dict[str, str]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
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
        rows.append(dict(zip(header, values)))
    return rows


def parse_dwd_temperature_rows(
    rows: list[dict[str, str]], target_date: date
) -> list[tuple[datetime, float]]:
    prefix = target_date.strftime("%Y%m%d")
    parsed = []
    for row in rows:
        mess_datum = row.get("MESS_DATUM", "")
        if not mess_datum.startswith(prefix):
            continue
        temp = row.get("TT_10", "")
        if temp in {"", "-999"}:
            continue
        moment = datetime.strptime(mess_datum, "%Y%m%d%H%M").replace(tzinfo=BERLIN)
        parsed.append((moment, float(temp)))
    return parsed


def fetch_dwd_temperatures(target_date: date) -> list[tuple[datetime, float]]:
    for url in (DWD_TEMP_NOW_URL, DWD_TEMP_RECENT_URL):
        rows = parse_dwd_temperature_rows(fetch_dwd_zip_rows(url), target_date)
        if rows:
            return rows
    return []


def fetch_metar_temperatures(target_date: date) -> list[tuple[datetime, float]]:
    observations = fetch_json(METAR_URL)
    if not isinstance(observations, list):
        return []

    rows = []
    for observation in observations:
        moment = datetime.fromtimestamp(observation["obsTime"], tz=BERLIN)
        if moment.date() != target_date:
            continue
        temp = observation.get("temp")
        if temp is None:
            continue
        rows.append((moment, float(temp)))

    return sorted(rows)


def latest_day_with_data() -> date:
    today = datetime.now(BERLIN).date()
    if fetch_dwd_temperatures(today):
        return today
    return today - timedelta(days=1)


def polymarket_slug(target_date: date) -> str:
    month = MONTH_NAMES[target_date.month]
    return (
        f"highest-temperature-in-munich-on-{month}-"
        f"{target_date.day}-{target_date.year}"
    )


def fetch_polymarket_30c_history(
    target_date: date,
) -> list[tuple[datetime, float]]:
    query = urllib.parse.urlencode({"slug": polymarket_slug(target_date)})
    events = fetch_json(f"{GAMMA_API_URL}?{query}")
    if not events:
        return []

    token_id = None
    for market in events[0].get("markets", []):
        if market.get("groupItemTitle") == "30°C":
            token_id = json.loads(market.get("clobTokenIds", "[]"))[0]
            break
    if not token_id:
        return []

    day_start = datetime.combine(target_date, datetime.min.time(), tzinfo=BERLIN)
    day_end = day_start + timedelta(days=1)
    history_query = urllib.parse.urlencode(
        {
            "market": token_id,
            "startTs": int(day_start.timestamp()),
            "endTs": int(day_end.timestamp()),
            "fidelity": 5,
        }
    )
    payload = fetch_json(f"{CLOB_HISTORY_URL}?{history_query}")
    history = payload.get("history", []) if isinstance(payload, dict) else []
    return [
        (datetime.fromtimestamp(point["t"], BERLIN), point["p"] * 100)
        for point in history
    ]


def plot_temperature(target_date: date | None = None, output_path: Path = OUTPUT_PATH) -> tuple[Path, date, int]:
    target_date = target_date or latest_day_with_data()
    dwd_rows = fetch_dwd_temperatures(target_date)
    metar_rows = fetch_metar_temperatures(target_date)
    polymarket_rows = fetch_polymarket_30c_history(target_date)

    if not dwd_rows and not metar_rows:
        raise ValueError(f"Keine Temperaturdaten für {target_date.strftime('%d.%m.%Y')}")

    fig, ax_temp = plt.subplots(figsize=(12, 6.5))
    ax_pm = ax_temp.twinx()

    if dwd_rows:
        dwd_times, dwd_temps = zip(*dwd_rows)
        ax_temp.plot(
            dwd_times,
            dwd_temps,
            color="#e4572e",
            linewidth=2,
            marker="o",
            markersize=3,
            label="DWD 10-Min (TT_10)",
            zorder=3,
        )
        max_time, max_temp = max(dwd_rows, key=lambda row: row[1])
        ax_temp.axhline(max_temp, color="#e4572e", linestyle="--", alpha=0.35, linewidth=1)
        ax_temp.annotate(
            f"DWD-Max {max_temp:.1f}°C",
            xy=(max_time, max_temp),
            xytext=(8, 8),
            textcoords="offset points",
            fontsize=9,
            color="#e4572e",
        )

    if metar_rows:
        metar_times, metar_temps = zip(*metar_rows)
        ax_temp.scatter(
            metar_times,
            metar_temps,
            color="#4c78a8",
            s=55,
            zorder=4,
            label="METAR EDDM",
        )
        metar_max = max(metar_rows, key=lambda row: row[1])
        ax_temp.annotate(
            f"METAR-Max {metar_max[1]:.0f}°C",
            xy=(metar_max[0], metar_max[1]),
            xytext=(8, -14),
            textcoords="offset points",
            fontsize=9,
            color="#4c78a8",
        )

    pm_label = "Polymarket 30°C (%)"
    if polymarket_rows:
        pm_times, pm_prices = zip(*polymarket_rows)
        ax_pm.plot(
            pm_times,
            pm_prices,
            color="#7b2cbf",
            linewidth=3,
            linestyle="-",
            marker="s",
            markersize=4,
            markevery=max(1, len(pm_prices) // 40),
            label=pm_label,
            zorder=5,
            alpha=0.95,
        )
        ax_pm.fill_between(pm_times, pm_prices, alpha=0.08, color="#7b2cbf", zorder=2)
        ax_pm.set_ylabel("Polymarket 30°C – Wettquote (%)", color="#7b2cbf", fontsize=11)
        ax_pm.tick_params(axis="y", labelcolor="#7b2cbf", width=1.5)
        ax_pm.set_ylim(0, 100)
        ax_pm.spines["right"].set_color("#7b2cbf")
        ax_pm.spines["right"].set_linewidth(1.5)
    else:
        pm_label = None

    ax_temp.set_title(
        "Temperatur & Polymarket 30°C – Münchner Flughafen (EDDM / DWD 01262)\n"
        f"{target_date.strftime('%d.%m.%Y')} Ortszeit",
        fontsize=13,
        fontweight="bold",
    )
    ax_temp.set_xlabel("Uhrzeit (Europe/Berlin)")
    ax_temp.set_ylabel("Temperatur (°C)")
    ax_temp.grid(True, alpha=0.3, zorder=1)
    ax_temp.set_zorder(2)
    ax_temp.patch.set_alpha(0.0)

    temp_handles, temp_labels = ax_temp.get_legend_handles_labels()
    if pm_label:
        pm_handles, pm_labels = ax_pm.get_legend_handles_labels()
        ax_temp.legend(
            temp_handles + pm_handles,
            temp_labels + pm_labels,
            loc="upper left",
            framealpha=0.95,
        )
    else:
        ax_temp.legend(loc="upper left", framealpha=0.95)

    ax_temp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=BERLIN))
    fig.autofmt_xdate()
    plt.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    if output_path != ARTIFACT_PATH:
        ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(ARTIFACT_PATH, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path, target_date, len(polymarket_rows)


def main() -> None:
    try:
        output, target_date, pm_points = plot_temperature()
        print(f"Datum:         {target_date.strftime('%d.%m.%Y')}")
        print(f"Polymarket:    {pm_points} Datenpunkte")
        if pm_points == 0:
            print("Warnung: Keine Polymarket-Daten – Wettkurve fehlt im Chart.", file=sys.stderr)
        print(f"Chart gespeichert: {output.resolve()}")
    except Exception as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
