#!/usr/bin/env python3
"""Live-Wetterstationen am Münchner Flughafen (EDDM/MUC) mit Polymarket-Vergleich."""

import io
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

USER_AGENT = "weather/1.0 (Munich Airport live stations)"
BERLIN = ZoneInfo("Europe/Berlin")

MUNICH_AIRPORT_LAT = 48.3538
MUNICH_AIRPORT_LON = 11.7861

DWD_CLIMATE_BASE = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate"
DWD_BASE = f"{DWD_CLIMATE_BASE}/10_minutes"
STATION_ID = "01262"
STATION_NAME = "München-Flughafen"

DWD_NOW_FILES = {
    "temperature": f"{DWD_BASE}/air_temperature/now/10minutenwerte_TU_{STATION_ID}_now.zip",
    "wind": f"{DWD_BASE}/wind/now/10minutenwerte_wind_{STATION_ID}_now.zip",
    "precipitation": f"{DWD_BASE}/precipitation/now/10minutenwerte_nieder_{STATION_ID}_now.zip",
    "gust": f"{DWD_BASE}/extreme_wind/now/10minutenwerte_extrema_wind_{STATION_ID}_now.zip",
}

DWD_DAILY_HISTORICAL_URL = (
    f"{DWD_CLIMATE_BASE}/daily/kl/historical/"
    f"tageswerte_KL_{STATION_ID}_19920517_20251231_hist.zip"
)
DWD_DAILY_RECENT_URL = (
    f"{DWD_CLIMATE_BASE}/daily/kl/recent/tageswerte_KL_{STATION_ID}_akt.zip"
)
HISTORY_DAYS_SHOWN = 14

AVIATION_METAR_URL = "https://aviationweather.gov/api/data/metar?ids=EDDM&format=json"
POLYMARKET_API_URL = "https://gamma-api.polymarket.com/events"
OPEN_METEO_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={MUNICH_AIRPORT_LAT}&longitude={MUNICH_AIRPORT_LON}"
    "&daily=temperature_2m_max&timezone=Europe%2FBerlin&forecast_days=1"
)

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


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read()


def fetch_json(url: str, timeout: int = 30) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_json_safe(url: str, timeout: int = 45) -> object | None:
    try:
        return fetch_json(url, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def fetch_dwd_zip_rows(url: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(fetch_bytes(url))) as archive:
        data_file = next(
            name
            for name in archive.namelist()
            if name.endswith(".txt") and not name.startswith("Metadaten_")
        )
        content = archive.read(data_file).decode("latin-1")

    lines = [line for line in content.splitlines() if line.strip()]
    header = [column.strip() for column in lines[0].split(";")]
    rows = []
    for line in lines[1:]:
        if not line.startswith(" "):
            continue
        values = [value.strip() for value in line.split(";")]
        rows.append(dict(zip(header, values)))
    return rows


def fetch_dwd_latest_row(url: str) -> dict[str, str]:
    rows = fetch_dwd_zip_rows(url)
    return rows[-1]


def parse_dwd_timestamp(mess_datum: str) -> datetime:
    return datetime.strptime(mess_datum, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def parse_dwd_local_timestamp(mess_datum: str) -> datetime:
    return datetime.strptime(mess_datum, "%Y%m%d%H%M").replace(tzinfo=BERLIN)


@dataclass
class PeakForecast:
    status: str
    run_max: float
    run_max_time: datetime
    current_temp: float
    current_time: datetime
    last_delta: float | None
    rate_30min: float | None
    est_tmax_low: float
    est_tmax_high: float
    peak_window: str
    peak_probability: int
    signals: list[str]
    recent_deltas: list[float]


def load_today_metar_readings(target_date: date) -> list[tuple[datetime, float]]:
    metar_data = fetch_json_safe(f"{AVIATION_METAR_URL}&hours=24")
    if not isinstance(metar_data, list):
        return []

    readings: list[tuple[datetime, float]] = []
    for observation in metar_data:
        moment = datetime.fromtimestamp(observation["obsTime"], tz=timezone.utc).astimezone(BERLIN)
        if moment.date() != target_date:
            continue

        temp = observation.get("temp")
        if temp is None:
            continue

        readings.append((moment, float(temp)))

    readings.sort()
    return readings


def load_today_temperature_readings(target_date: date) -> list[tuple[datetime, float]]:
    return load_today_metar_readings(target_date)


def analyze_peak_forecast(readings: list[tuple[datetime, float]]) -> PeakForecast | None:
    if len(readings) < 2:
        return None

    temps = [temp for _, temp in readings]
    times = [moment for moment, _ in readings]
    deltas = [0.0] + [temps[index] - temps[index - 1] for index in range(1, len(temps))]

    run_max = max(temps)
    run_max_index = temps.index(run_max)
    run_max_time = times[run_max_index]
    current_temp = temps[-1]
    current_time = times[-1]
    last_delta = deltas[-1]
    recent_deltas = deltas[-3:] if len(deltas) >= 3 else deltas[1:]

    rate_30min = temps[-1] - temps[-2] if len(temps) >= 2 else None
    prev_rate_30min = temps[-2] - temps[-3] if len(temps) >= 3 else None

    hour = current_time.hour
    signals: list[str] = []
    status = "mixed"

    decelerating = False
    if len(deltas) >= 4:
        delta_a, delta_b, delta_c = deltas[-3], deltas[-2], deltas[-1]
        decelerating = delta_a > 0 and delta_b > 0 and delta_c > 0 and delta_a > delta_b > delta_c

    cooling_streak = len(deltas) >= 3 and deltas[-1] <= 0 and deltas[-2] <= 0
    intervals_since_peak = len(temps) - 1 - run_max_index
    max_stable = intervals_since_peak >= 2 and current_temp <= run_max

    if hour < 11:
        status = "early"
        est_low = run_max
        est_high = run_max + (7.0 if current_time.month in {6, 7, 8} else 5.0)
        peak_window = "typisch Nachmittag (noch zu früh für METAR-Signal)"
        peak_probability = 15
    elif cooling_streak and max_stable:
        status = "likely_passed"
        signals.append("2× ΔT ≤ 0 und Maximum seit ≥30 min stabil")
        est_low = run_max - 0.5
        est_high = run_max + 0.5
        peak_window = f"ca. {run_max_time.strftime('%H:%M')} Uhr (wahrscheinlich erreicht)"
        peak_probability = 85
    elif (
        rate_30min is not None
        and prev_rate_30min is not None
        and 0 < rate_30min < 1.0
        and rate_30min < prev_rate_30min
    ):
        status = "slowing"
        signals.append(f"30-min-Aufheizrate {rate_30min:+.2f}°C, verlangsamt")
        est_low = run_max
        est_high = run_max + min(max(last_delta, 0.0) + 0.5, 2.0)
        peak_window = "ca. 30–60 min"
        peak_probability = 60
    elif decelerating and hour >= 11:
        status = "slowing"
        signals.append(
            "ΔT fällt: "
            f"{deltas[-3]:+.0f} → {deltas[-2]:+.0f} → {deltas[-1]:+.0f}°C / Bericht"
        )
        est_low = run_max
        est_high = run_max + min(max(last_delta, 0.0) + 1.0, 2.0)
        peak_window = "ca. 40–90 min"
        peak_probability = 45
    elif last_delta is not None and last_delta >= 1.0:
        status = "warming"
        signals.append(f"starker Anstieg ΔT={last_delta:+.0f}°C / Bericht")
        est_low = current_temp
        est_high = current_temp + last_delta + 1.0
        peak_window = "noch offen (starkes Aufheizen)"
        peak_probability = 25
    elif last_delta is not None and last_delta == 0:
        status = "plateau"
        signals.append("Temperaturplateau (ΔT = 0)")
        est_low = run_max
        est_high = run_max + 1.0
        peak_window = "Peak möglich jetzt ±30 min"
        peak_probability = 70
    else:
        status = "mixed"
        est_low = run_max
        est_high = run_max + 1.0
        peak_window = "unsicher"
        peak_probability = 35

    return PeakForecast(
        status=status,
        run_max=run_max,
        run_max_time=run_max_time,
        current_temp=current_temp,
        current_time=current_time,
        last_delta=last_delta,
        rate_30min=rate_30min,
        est_tmax_low=est_low,
        est_tmax_high=est_high,
        peak_window=peak_window,
        peak_probability=peak_probability,
        signals=signals,
        recent_deltas=recent_deltas,
    )


def format_delta_sequence(deltas: list[float]) -> str:
    if not deltas:
        return "–"
    return ", ".join(f"{delta:+.0f}" for delta in deltas)


def peak_status_label(status: str) -> str:
    labels = {
        "early": "Vormittag – noch kein belastbares Signal",
        "warming": "Aufheizen",
        "slowing": "Verlangsamung vor Peak",
        "plateau": "Plateau",
        "likely_passed": "Peak wahrscheinlich vorbei",
        "mixed": "Uneindeutig",
    }
    return labels.get(status, status)


def format_peak_forecast_section(target_date: date) -> str:
    readings = load_today_temperature_readings(target_date)
    forecast = analyze_peak_forecast(readings)
    if forecast is None:
        return (
            "[Peak-Prognose] Zu wenige METAR-Meldungen für heute "
            f"({target_date.strftime('%d.%m.%Y')})"
        )

    est_resolved_low = truncate_celsius(forecast.est_tmax_low)
    est_resolved_high = truncate_celsius(forecast.est_tmax_high)
    if est_resolved_low == est_resolved_high:
        resolved_hint = f"{est_resolved_low}°C"
    else:
        resolved_hint = f"{est_resolved_low}–{est_resolved_high}°C"

    lines = [
        f"[Peak-Prognose] METAR ΔT-Analyse – EDDM ({STATION_NAME})",
        f"  Stand:         {forecast.current_time.strftime('%H:%M')} Ortszeit "
        f"({len(readings)} METAR-Meldungen heute)",
        f"  Status:        {peak_status_label(forecast.status)}",
        f"  Laufendes Max: {forecast.run_max:.0f}°C um {forecast.run_max_time.strftime('%H:%M')} Uhr",
        f"  Aktuell:       {forecast.current_temp:.0f}°C",
        f"  Letzte ΔT:     {format_delta_sequence(forecast.recent_deltas)} °C / Bericht",
    ]

    if forecast.rate_30min is not None:
        lines.append(f"  30-min-Rate:   {forecast.rate_30min:+.2f} °C")

    lines.extend(
        [
            f"  Peak-Fenster:  {forecast.peak_window}",
            (
                f"  Tmax-Schätzung:{forecast.est_tmax_low:.1f}–{forecast.est_tmax_high:.1f}°C "
                f"(Polymarket ca. {resolved_hint})"
            ),
            f"  Peak-Wahrsch.: {forecast.peak_probability}%",
        ]
    )

    if forecast.signals:
        lines.append("  Signale:")
        for signal in forecast.signals:
            lines.append(f"    - {signal}")

    lines.append(
        "  Hinweis:       METAR alle ~30 min, ganze °C – Clearing-Quelle für Polymarket"
    )
    return "\n".join(lines)


def parse_kl_value(value: str | None) -> float | None:
    if not value or value in {"-999", "-999.0"}:
        return None
    return float(value)


def parse_kl_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def load_dwd_daily_rows(url: str, source: str) -> list[dict[str, object]]:
    rows = []
    for row in fetch_dwd_zip_rows(url):
        rows.append(
            {
                "date": parse_kl_date(row["MESS_DATUM"]),
                "max_c": parse_kl_value(row.get("TXK")),
                "min_c": parse_kl_value(row.get("TNK")),
                "mean_c": parse_kl_value(row.get("TMK")),
                "precip_mm": parse_kl_value(row.get("RSK")),
                "source": source,
            }
        )
    return rows


def load_dwd_daily_history() -> list[dict[str, object]]:
    merged: dict[date, dict[str, object]] = {}

    for entry in load_dwd_daily_rows(DWD_DAILY_HISTORICAL_URL, "DWD"):
        merged[entry["date"]] = entry

    for entry in load_dwd_daily_rows(DWD_DAILY_RECENT_URL, "DWD"):
        merged[entry["date"]] = entry

    return sorted(merged.values(), key=lambda item: item["date"])


def get_metar_daily_maxima(days: int = 15) -> dict[date, float]:
    metar_data = fetch_json_safe(f"{AVIATION_METAR_URL}&hours={days * 24}")
    if not isinstance(metar_data, list):
        return {}

    daily_maxima: dict[date, float] = {}
    for observation in metar_data:
        moment = datetime.fromtimestamp(observation["obsTime"], tz=timezone.utc)
        local_date = moment.astimezone(BERLIN).date()
        temp = observation.get("temp")
        if temp is None:
            continue

        value = float(temp)
        current = daily_maxima.get(local_date)
        if current is None or value > current:
            daily_maxima[local_date] = value

    return daily_maxima


def merge_recent_daily_history(
    daily_history: list[dict[str, object]],
    metar_daily_maxima: dict[date, float],
) -> list[dict[str, object]]:
    merged = {entry["date"]: dict(entry) for entry in daily_history}

    for day, max_temp in metar_daily_maxima.items():
        existing = merged.get(day)
        if existing is None:
            merged[day] = {
                "date": day,
                "max_c": max_temp,
                "min_c": None,
                "mean_c": None,
                "precip_mm": None,
                "source": "METAR",
            }
            continue

        existing_max = existing.get("max_c")
        if existing_max is None or max_temp > float(existing_max):
            existing["max_c"] = max_temp
            existing["source"] = "DWD+METAR"

    return sorted(merged.values(), key=lambda entry: entry["date"])


def format_optional_value(value: object | None, suffix: str = "") -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.1f}{suffix}"
    return f"{value}{suffix}"


def format_historical_section() -> str:
    daily_history = load_dwd_daily_history()
    metar_daily_maxima = get_metar_daily_maxima()
    merged_history = merge_recent_daily_history(daily_history, metar_daily_maxima)
    recent_days = merged_history[-HISTORY_DAYS_SHOWN:]

    first_day = merged_history[0]["date"]
    last_day = merged_history[-1]["date"]

    lines = [
        f"[DWD Historie] {STATION_NAME} (Tageswerte KL)",
        f"  Archiv:      {len(merged_history):,} Tage ({first_day.strftime('%d.%m.%Y')} – "
        f"{last_day.strftime('%d.%m.%Y')})",
        f"  Letzte {HISTORY_DAYS_SHOWN} Tage:",
    ]

    for entry in recent_days:
        lines.append(
            "    "
            f"{entry['date'].strftime('%d.%m.%Y')}  "
            f"Max {format_optional_value(entry.get('max_c'), ' °C'):>8}  "
            f"Min {format_optional_value(entry.get('min_c'), ' °C'):>8}  "
            f"Regen {format_optional_value(entry.get('precip_mm'), ' mm'):>7}  "
            f"({entry['source']})"
        )

    lines.append(
        "  Quelle:      DWD Open Data (tageswerte_KL) + METAR für aktuelle Tage"
    )
    return "\n".join(lines)


def polymarket_slug(target_date: date) -> str:
    month = MONTH_NAMES[target_date.month]
    return f"highest-temperature-in-munich-on-{month}-{target_date.day}-{target_date.year}"


def truncate_celsius(value: float) -> int:
    """Ganzzahl-Auflösung wie Wunderground/Polymarket (ab 0,5 aufrunden)."""
    return int(value + 0.5)


def get_metar_max_temperature(target_date: date) -> tuple[float | None, datetime | None]:
    metar_data = fetch_json_safe(f"{AVIATION_METAR_URL}&hours=24")
    if not isinstance(metar_data, list):
        return None, None

    max_temp = None
    max_time = None
    for observation in metar_data:
        moment = datetime.fromtimestamp(observation["obsTime"], tz=timezone.utc)
        if moment.astimezone(BERLIN).date() != target_date:
            continue

        temp = observation.get("temp")
        if temp is None:
            continue

        value = float(temp)
        if max_temp is None or value > max_temp:
            max_temp = value
            max_time = moment

    return max_temp, max_time


def get_today_max_temperature(target_date: date) -> tuple[float | None, datetime | None]:
    return get_metar_max_temperature(target_date)


def get_forecast_max_temperature() -> float | None:
    payload = fetch_json(OPEN_METEO_FORECAST_URL)
    values = payload.get("daily", {}).get("temperature_2m_max", [])
    if not values:
        return None
    return float(values[0])


def fetch_polymarket_market(target_date: date) -> dict | None:
    slug = polymarket_slug(target_date)
    query = urllib.parse.urlencode({"slug": slug})
    events = fetch_json(f"{POLYMARKET_API_URL}?{query}")
    if not events:
        return None
    return events[0]


def parse_polymarket_outcomes(event: dict) -> list[dict[str, object]]:
    outcomes = []
    for market in event.get("markets", []):
        prices = json.loads(market.get("outcomePrices", "[0, 1]"))
        yes_price = float(prices[0])
        outcomes.append(
            {
                "label": market.get("groupItemTitle", "?"),
                "price": yes_price,
                "probability": yes_price * 100,
                "volume": float(market.get("volume", 0)),
                "closed": bool(market.get("closed", False)),
            }
        )
    outcomes.sort(key=lambda item: item["probability"], reverse=True)
    return outcomes


def yes_profit_stats(price: float) -> dict[str, float] | None:
    if price <= 0 or price >= 1:
        return None

    payout_per_dollar = 1 / price
    profit_per_dollar = payout_per_dollar - 1
    return {
        "roi_percent": profit_per_dollar * 100,
        "profit_per_dollar": profit_per_dollar,
        "payout_per_dollar": payout_per_dollar,
    }


def format_profit_line(label: str, outcome: dict[str, object]) -> str:
    stats = yes_profit_stats(float(outcome["price"]))
    if not stats:
        return f"  {label}: nicht berechenbar"

    return (
        f"  {label}:"
        f" +{stats['roi_percent']:.1f}% ROI"
        f" ($1.00 → ${stats['payout_per_dollar']:.2f} bei Gewinn)"
        f" für {outcome['label']}"
    )


def outcome_matches_temperature(label: str, temperature: int) -> bool:
    normalized = label.replace("°", "").strip().lower()
    digits = "".join(character for character in normalized if character.isdigit())
    if not digits:
        return False

    threshold = int(digits)
    if "or below" in normalized:
        return temperature <= threshold
    if "or higher" in normalized:
        return temperature >= threshold
    return threshold == temperature


def format_polymarket_comparison(target_date: date) -> str:
    event = fetch_polymarket_market(target_date)
    if not event:
        return (
            f"[Polymarket] Kein Markt für {target_date.strftime('%d.%m.%Y')} gefunden "
            f"({polymarket_slug(target_date)})"
        )

    outcomes = parse_polymarket_outcomes(event)
    if not outcomes:
        return "[Polymarket] Keine Marktdaten verfügbar."

    observed_max, observed_time = get_today_max_temperature(target_date)
    forecast_max = get_forecast_max_temperature()
    forecast_resolved = truncate_celsius(forecast_max) if forecast_max is not None else None

    leader = outcomes[0]
    forecast_outcome = next(
        (item for item in outcomes if forecast_resolved is not None and outcome_matches_temperature(item["label"], forecast_resolved)),
        None,
    )

    lines = [
        f"[Polymarket] {event.get('title', 'Höchsttemperatur München')}",
        f"  Status:      {'abgeschlossen' if event.get('closed') else 'offen'}",
        f"  Volumen:     ${float(event.get('volume', 0)):,.0f}",
        f"  Marktfavorit:{leader['label']} ({leader['probability']:.1f}%)",
    ]

    if observed_max is not None:
        age = format_age(observed_time) if observed_time else "unbekannt"
        lines.append(
            f"  Bisheriges Max:     {observed_max:.0f} °C ({age}, METAR EDDM)"
        )
    else:
        lines.append("  Bisheriges Max (METAR): noch keine Messwerte")

    if forecast_max is not None and forecast_resolved is not None:
        lines.append(
            f"  Prognose-Max: {forecast_max:.1f} °C → Auflösung ca. {forecast_resolved} °C"
        )

    if forecast_outcome:
        lines.append(
            f"  Prognose-Outcome: {forecast_outcome['label']} "
            f"({forecast_outcome['probability']:.1f}% Marktpreis)"
        )

    lines.append(format_profit_line("Markt-Profit", leader))
    if forecast_outcome:
        lines.append(format_profit_line("Prognose-Profit", forecast_outcome))

    if forecast_outcome:
        if leader["label"] == forecast_outcome["label"]:
            lines.append("  Einschätzung:  Markt und Prognose stimmen überein")
        else:
            lines.append(
                "  Einschätzung:  Markt und Prognose weichen ab – "
                f"Markt favorisiert {leader['label']}, Prognose deutet auf "
                f"{forecast_resolved} °C"
            )

    lines.append("  Top-Outcomes:")
    for outcome in outcomes[:5]:
        lines.append(
            f"    - {outcome['label']:<15} {outcome['probability']:5.1f}%  "
            f"(${outcome['volume']:,.0f})"
        )

    lines.append(
        "  Auflösung:   Wunderground EDDM, ganze °C "
        "(https://www.wunderground.com/history/daily/de/munich/EDDM)"
    )
    return "\n".join(lines)


def format_age(moment: datetime) -> str:
    minutes = max(0, int((datetime.now(timezone.utc) - moment).total_seconds() // 60))
    if minutes < 1:
        return "gerade eben"
    if minutes == 1:
        return "vor 1 Minute"
    return f"vor {minutes} Minuten"


def format_observation_time(moment: datetime) -> str:
    local_time = moment.astimezone(BERLIN)
    return (
        f"{local_time.strftime('%d.%m.%Y %H:%M')} Ortszeit "
        f"({format_age(moment)}, {moment.strftime('%H:%M')} UTC)"
    )


def ms_to_kmh(speed: str | None) -> str:
    if not speed or speed == "-999":
        return "–"
    return f"{round(float(speed) * 3.6, 1)} km/h"


def knots_to_kmh(knots: float | int | None) -> str:
    if knots is None:
        return "–"
    return f"{round(float(knots) * 1.852, 1)} km/h"


def format_dwd_live_station(metar_observation: dict | None = None) -> str:
    temperature = fetch_dwd_latest_row(DWD_NOW_FILES["temperature"])
    wind = fetch_dwd_latest_row(DWD_NOW_FILES["wind"])
    precipitation = fetch_dwd_latest_row(DWD_NOW_FILES["precipitation"])
    gust = fetch_dwd_latest_row(DWD_NOW_FILES["gust"])

    temp_time = parse_dwd_timestamp(temperature["MESS_DATUM"])
    wind_time = parse_dwd_timestamp(wind["MESS_DATUM"])
    precip_time = parse_dwd_timestamp(precipitation["MESS_DATUM"])
    gust_time = parse_dwd_timestamp(gust["MESS_DATUM"])
    latest_time = max(temp_time, wind_time, precip_time, gust_time)

    temp_value = temperature.get("TT_10", "–")
    temp_age = format_age(temp_time)
    dewpoint = temperature.get("TD_10", "–")
    humidity = temperature.get("RF_10", "–")

    if metar_observation:
        metar_time = datetime.fromtimestamp(metar_observation["obsTime"], tz=timezone.utc)
        temp_value = metar_observation.get("temp", temp_value)
        dewpoint = metar_observation.get("dewp", dewpoint)
        temp_age = f"{format_age(metar_time)}, METAR"

    lines = [
        f"[DWD Live 10-Min] {STATION_NAME} (Station {STATION_ID})",
        f"  Aktualisiert: {format_observation_time(latest_time)}",
        f"  Temperatur:  {temp_value} °C ({temp_age})",
        f"  Taupunkt:    {dewpoint} °C",
        f"  Luftfeuchte: {humidity} %",
        f"  Druck:       {temperature.get('PP_10', '–')} hPa (Station)",
        (
            "  Wind:        "
            f"{wind.get('DD_10', '–')}° / {ms_to_kmh(wind.get('FF_10'))} "
            f"({format_age(wind_time)})"
        ),
        f"  Böen:        {ms_to_kmh(gust.get('FX_10'))} ({format_age(gust_time)})",
        (
            "  Niederschlag:"
            f" {precipitation.get('RWS_10', '–')} mm in 10 min "
            f"({format_age(precip_time)})"
        ),
        "  Quelle:      DWD Open Data (10-Minuten-Messwerte, /now)",
    ]
    return "\n".join(lines)


def format_metar_station(data: list[dict] | None) -> str:
    if not data:
        return "[Aviation METAR] Keine METAR-Daten für EDDM verfügbar (Timeout oder API-Fehler)."

    observation = data[0]
    moment = datetime.fromtimestamp(observation["obsTime"], tz=timezone.utc)
    lines = [
        "[Aviation METAR] München Intl (EDDM)",
        f"  Aktualisiert: {format_observation_time(moment)}",
        f"  Temperatur:  {observation.get('temp', '–')} °C",
        f"  Taupunkt:    {observation.get('dewp', '–')} °C",
        (
            "  Wind:        "
            f"{observation.get('wdir', '–')}° / {observation.get('wspd', '–')} kn "
            f"({knots_to_kmh(observation.get('wspd'))})"
        ),
        f"  Sicht:       {observation.get('cover') or observation.get('visib', '–')}",
        f"  Druck:       {observation.get('altim', '–')} hPa (QNH)",
        f"  Kategorie:   {observation.get('fltCat', '–')}",
        f"  RAW:         {observation.get('rawOb', '–')}",
    ]
    return "\n".join(lines)


def get_munich_airport_weather() -> str:
    today = datetime.now(BERLIN).date()
    metar_data = fetch_json_safe(AVIATION_METAR_URL)
    metar_observation = metar_data[0] if isinstance(metar_data, list) and metar_data else None

    sections = [
        "=== Live-Wetterstationen: Münchner Flughafen (EDDM/MUC) ===",
        f"Abruf: {datetime.now(BERLIN).strftime('%d.%m.%Y %H:%M:%S')} Ortszeit",
        "",
        format_dwd_live_station(metar_observation),
        "",
        format_metar_station(metar_data if isinstance(metar_data, list) else None),
        "",
        format_peak_forecast_section(today),
        "",
        format_historical_section(),
        "",
        format_polymarket_comparison(today),
    ]
    return "\n".join(sections)


def main() -> None:
    try:
        print(get_munich_airport_weather())
    except urllib.error.URLError as error:
        print(f"Fehler beim Abruf der Wetterdaten: {error}", file=sys.stderr)
        sys.exit(1)
    except TimeoutError as error:
        print(f"Zeitüberschreitung beim Abruf der Wetterdaten: {error}", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as error:
        print(f"Fehler beim Verarbeiten der Wetterdaten: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
