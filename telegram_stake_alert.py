#!/usr/bin/env python3
"""Sendet flexible Polymarket-Wetter-Alerts per Telegram."""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from Main import analyze_peak_forecast, load_today_metar_readings, peak_status_label, truncate_celsius

USER_AGENT = "weather/1.0 (Munich polymarket stake alert)"
BERLIN = ZoneInfo("Europe/Berlin")
POLYMARKET_API_URL = "https://gamma-api.polymarket.com/events"
AVIATION_METAR_URL = "https://aviationweather.gov/api/data/metar?ids=EDDM&format=json"
METAR_TIME_RE = re.compile(r"\b(\d{2})(\d{2})(\d{2})Z\b")
METAR_PUBLISH_LAG = timedelta(minutes=12)
METAR_RETRY_WAIT_SECONDS = 45
METAR_MAX_RETRIES = 2
FOCUS_SPREAD = 2
PROGNOSIS_START_HOUR = 10
PROGNOSIS_END_HOUR = 18
OPEN_METEO_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude=48.3538&longitude=11.7861"
    "&hourly=temperature_2m"
    "&daily=temperature_2m_max"
    "&timezone=Europe%2FBerlin&forecast_days=3"
)
DWD_STATION_ID = "01262"
DWD_BASE = "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/10_minutes"
DWD_TEMP_NOW_URL = f"{DWD_BASE}/air_temperature/now/10minutenwerte_TU_{DWD_STATION_ID}_now.zip"
DWD_TEMP_RECENT_URL = f"{DWD_BASE}/air_temperature/recent/10minutenwerte_TU_{DWD_STATION_ID}_akt.zip"
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".telegram.env"
LAST_SENT_PATH = SCRIPT_DIR / ".telegram_last_sent"
ALERT_INTERVAL_MINUTES = 10
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


@dataclass(frozen=True)
class OutcomePrice:
    label: str
    price: float

    @property
    def probability(self) -> float:
        return self.price * 100


@dataclass(frozen=True)
class MetarStatus:
    latest_temp: float
    latest_time: datetime
    max_temp: float
    max_time: datetime
    count: int


@dataclass(frozen=True)
class DwdStatus:
    latest_temp: float
    latest_time: datetime
    max_temp: float
    max_time: datetime
    count: int


@dataclass(frozen=True)
class DayForecast:
    tmax: float
    peak_time: str | None
    hourly: dict[int, float]


@dataclass(frozen=True)
class MetarPeakPrognosis:
    status: str
    status_label: str
    tmax_estimate: str
    peak_window: str
    run_max: float
    run_max_time: str


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def alert_slot_key(reference: datetime | None = None) -> str:
    reference = reference or datetime.now(BERLIN)
    minute = (reference.minute // ALERT_INTERVAL_MINUTES) * ALERT_INTERVAL_MINUTES
    slot = reference.replace(minute=minute, second=0, microsecond=0)
    return slot.strftime("%Y%m%d%H%M")


def duplicate_send_blocked(slot_key: str, force: bool) -> bool:
    if force:
        return False
    if not LAST_SENT_PATH.exists():
        return False
    return LAST_SENT_PATH.read_text(encoding="utf-8").strip() == slot_key


def mark_alert_sent(slot_key: str) -> None:
    LAST_SENT_PATH.write_text(slot_key, encoding="utf-8")


def fetch_json(url: str, timeout: int = 20) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_json_safe(url: str, timeout: int = 20) -> object | None:
    try:
        return fetch_json(url, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def polymarket_slug(target_date: date) -> str:
    month = MONTH_NAMES[target_date.month]
    return f"highest-temperature-in-munich-on-{month}-{target_date.day}-{target_date.year}"


def parse_temperature(label: str) -> int | None:
    digits = "".join(character for character in label if character.isdigit())
    if not digits:
        return None
    return int(digits)


def truncate_celsius(value: float) -> int:
    return int(value + 0.5)


def focus_temperatures(forecast_max: float | None, spread: int = FOCUS_SPREAD) -> list[int] | None:
    if forecast_max is None:
        return None
    center = truncate_celsius(forecast_max)
    return list(range(center - spread, center + spread + 1))


def fetch_open_meteo_forecasts() -> dict[date, DayForecast]:
    payload = fetch_json_safe(OPEN_METEO_FORECAST_URL)
    if not isinstance(payload, dict):
        return {}

    daily = payload.get("daily", {})
    hourly = payload.get("hourly", {})
    times = daily.get("time", [])
    values = daily.get("temperature_2m_max", [])
    hourly_times = hourly.get("time", [])
    hourly_temps = hourly.get("temperature_2m", [])

    temps_by_day: dict[date, dict[int, float]] = {}
    for time_label, temp in zip(hourly_times, hourly_temps):
        if temp is None:
            continue
        day = date.fromisoformat(time_label[:10])
        hour = int(time_label[11:13])
        temps_by_day.setdefault(day, {})[hour] = float(temp)

    forecasts: dict[date, DayForecast] = {}
    for day_text, value in zip(times, values):
        if value is None:
            continue
        target_date = date.fromisoformat(day_text)
        day_hours = temps_by_day.get(target_date, {})
        peak_time = None
        if day_hours:
            peak_hour = max(day_hours, key=day_hours.get)
            peak_time = f"{peak_hour:02d}:00"
        hourly = {
            hour: day_hours[hour]
            for hour in range(PROGNOSIS_START_HOUR, PROGNOSIS_END_HOUR + 1)
            if hour in day_hours
        }
        forecasts[target_date] = DayForecast(
            tmax=float(value),
            peak_time=peak_time,
            hourly=hourly,
        )
    return forecasts


def fetch_open_meteo_daily_maxima() -> dict[date, float]:
    return {day: forecast.tmax for day, forecast in fetch_open_meteo_forecasts().items()}


def fetch_polymarket_prices(target_date: date) -> dict[int, OutcomePrice] | None:
    query = urllib.parse.urlencode({"slug": polymarket_slug(target_date)})
    events = fetch_json_safe(f"{POLYMARKET_API_URL}?{query}")
    if not events:
        return None

    prices: dict[int, OutcomePrice] = {}
    for market in events[0].get("markets", []):
        label = market.get("groupItemTitle", "")
        temperature = parse_temperature(label)
        if temperature is None or "or below" in label.lower() or "or higher" in label.lower():
            continue
        yes_price = float(json.loads(market.get("outcomePrices", "[0, 1]"))[0])
        prices[temperature] = OutcomePrice(label=label, price=yes_price)
    return prices


def format_age(moment: datetime, reference: datetime | None = None) -> str:
    reference = reference or datetime.now(BERLIN)
    minutes = max(0, int((reference - moment).total_seconds() // 60))
    if minutes < 1:
        return "gerade eben"
    if minutes == 1:
        return "vor 1 Min."
    return f"vor {minutes} Min."


def expected_latest_metar_slot(reference: datetime) -> datetime:
    """Return the newest :20/:50 METAR slot that should already be online."""
    adjusted = reference - METAR_PUBLISH_LAG
    if adjusted.minute >= 50:
        slot_minute = 50
    elif adjusted.minute >= 20:
        slot_minute = 20
    else:
        adjusted = adjusted - timedelta(hours=1)
        slot_minute = 50
    return adjusted.replace(minute=slot_minute, second=0, microsecond=0)


def observation_moment(observation: dict, target_date: date) -> datetime | None:
    raw_ob = observation.get("rawOb", "")
    match = METAR_TIME_RE.search(raw_ob)
    if match:
        day = int(match.group(1))
        hour = int(match.group(2))
        minute = int(match.group(3))
        try:
            moment = datetime(
                target_date.year,
                target_date.month,
                day,
                hour,
                minute,
                tzinfo=timezone.utc,
            ).astimezone(BERLIN)
        except ValueError:
            moment = None
        if moment is not None and moment.date() == target_date:
            return moment

    obs_time = observation.get("obsTime")
    if obs_time is None:
        return None
    return datetime.fromtimestamp(obs_time, tz=timezone.utc).astimezone(BERLIN)


def parse_metar_payload(payload: object, target_date: date) -> MetarStatus | None:
    if not isinstance(payload, list):
        return None

    readings: list[tuple[datetime, float]] = []
    for observation in payload:
        moment = observation_moment(observation, target_date)
        if moment is None or moment.date() != target_date:
            continue

        temp = observation.get("temp")
        if temp is None:
            continue

        readings.append((moment, float(temp)))

    if not readings:
        return None

    readings.sort(key=lambda item: item[0])
    latest_time, latest_temp = readings[-1]
    max_time, max_temp = max(readings, key=lambda item: item[1])
    return MetarStatus(
        latest_temp=latest_temp,
        latest_time=latest_time,
        max_temp=max_temp,
        max_time=max_time,
        count=len(readings),
    )


def fetch_metar_status(target_date: date, reference: datetime | None = None) -> MetarStatus | None:
    reference = reference or datetime.now(BERLIN)
    expected = expected_latest_metar_slot(reference)
    status = parse_metar_payload(
        fetch_json_safe(f"{AVIATION_METAR_URL}&hours=24"),
        target_date,
    )
    if status is None:
        return None

    for _ in range(METAR_MAX_RETRIES):
        if status.latest_time >= expected:
            return status
        time.sleep(METAR_RETRY_WAIT_SECONDS)
        refreshed = parse_metar_payload(
            fetch_json_safe(f"{AVIATION_METAR_URL}&hours=24"),
            target_date,
        )
        if refreshed is None:
            return status
        if refreshed.latest_time >= status.latest_time:
            status = refreshed
        if status.latest_time >= expected:
            return status

    return status


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
    parsed: list[tuple[datetime, float]] = []
    for row in rows:
        mess_datum = row.get("MESS_DATUM", "")
        if not mess_datum.startswith(prefix):
            continue
        temp = row.get("TT_10", "")
        if temp in {"", "-999"}:
            continue
        moment = datetime.strptime(mess_datum, "%Y%m%d%H%M").replace(tzinfo=BERLIN)
        parsed.append((moment, float(temp)))
    parsed.sort(key=lambda item: item[0])
    return parsed


def fetch_dwd_status(target_date: date) -> DwdStatus | None:
    readings: list[tuple[datetime, float]] = []
    for url in (DWD_TEMP_NOW_URL, DWD_TEMP_RECENT_URL):
        try:
            readings = parse_dwd_temperature_rows(fetch_dwd_zip_rows(url), target_date)
        except (urllib.error.URLError, TimeoutError, zipfile.BadZipFile, StopIteration, ValueError):
            readings = []
        if readings:
            break

    if not readings:
        return None

    latest_time, latest_temp = readings[-1]
    max_time, max_temp = max(readings, key=lambda item: item[1])
    return DwdStatus(
        latest_temp=latest_temp,
        latest_time=latest_time,
        max_temp=max_temp,
        max_time=max_time,
        count=len(readings),
    )


def format_market_lines(
    prices: dict[int, OutcomePrice] | None,
    focus_temps: list[int] | None,
) -> list[str]:
    if focus_temps is None:
        return ["Fokus: keine Open-Meteo-Prognose verfügbar"]
    if prices is None:
        return ["Kein Polymarket-Markt verfügbar"]

    lines = ["Kurse:"]
    parts = []
    available_prices: list[float] = []
    missing: list[int] = []
    for temp in focus_temps:
        if temp in prices:
            parts.append(f"{temp}°C {prices[temp].probability:4.1f}%")
            available_prices.append(prices[temp].price)
        else:
            parts.append(f"{temp}°C —")
            missing.append(temp)

    lines.append(" | ".join(parts))
    if available_prices:
        combo = sum(available_prices) * 100
        lines.append(
            f"Combo Fokus: {combo:.1f}% "
            f"({len(available_prices)}/{len(focus_temps)} Märkte)"
        )
    if missing:
        lines.append(f"Nicht verfügbar: {', '.join(f'{temp}°C' for temp in missing)}")
    return lines


def format_forecast_line(forecast_max: float | None, focus_temps: list[int] | None) -> str:
    if forecast_max is None:
        return "Open-Meteo Tmax: —"
    resolved = truncate_celsius(forecast_max)
    if focus_temps is None:
        return f"Open-Meteo Tmax: {forecast_max:.1f}°C → ca. {resolved}°C"
    focus_text = ", ".join(f"{temp}°C" for temp in focus_temps)
    return (
        f"Open-Meteo Tmax: {forecast_max:.1f}°C → ca. {resolved}°C\n"
        f"Fokus ±{FOCUS_SPREAD}°C: {focus_text}"
    )


def format_hourly_prognosis_lines(hourly: dict[int, float] | None) -> list[str]:
    if not hourly:
        return []

    lines = [
        f"Prognose {PROGNOSIS_START_HOUR:02d}–{PROGNOSIS_END_HOUR:02d} Uhr:"
    ]
    for hour in range(PROGNOSIS_START_HOUR, PROGNOSIS_END_HOUR + 1):
        temp = hourly.get(hour)
        if temp is None:
            lines.append(f"  {hour:02d}:00  —")
        else:
            lines.append(f"  {hour:02d}:00  {temp:.1f}°C → {truncate_celsius(temp)}°C")
    return lines


def fetch_metar_peak_prognosis(target_date: date) -> MetarPeakPrognosis | None:
    readings = load_today_metar_readings(target_date)
    forecast = analyze_peak_forecast(readings)
    if forecast is None:
        return None

    est_lo = truncate_celsius(forecast.est_tmax_low)
    est_hi = truncate_celsius(forecast.est_tmax_high)
    if est_lo == est_hi:
        tmax_estimate = f"{est_lo}°C"
    else:
        tmax_estimate = f"{est_lo}–{est_hi}°C"

    return MetarPeakPrognosis(
        status=forecast.status,
        status_label=peak_status_label(forecast.status),
        tmax_estimate=tmax_estimate,
        peak_window=forecast.peak_window,
        run_max=forecast.run_max,
        run_max_time=forecast.run_max_time.strftime("%H:%M"),
    )


def format_metar_peak_lines(prognosis: MetarPeakPrognosis | None) -> list[str]:
    if prognosis is None:
        return []
    return [
        f"METAR-Signal: {prognosis.status_label}",
        (
            f"METAR Tmax-Schätzung: {prognosis.tmax_estimate} "
            f"(laufendes Max {prognosis.run_max:.0f}°C um {prognosis.run_max_time})"
        ),
        f"METAR Peak-Fenster: {prognosis.peak_window}",
    ]


def format_day_section(
    target_date: date,
    heading: str,
    day_forecast: DayForecast | None,
    prices: dict[int, OutcomePrice] | None,
    metar_peak: MetarPeakPrognosis | None = None,
    metar: MetarStatus | None = None,
    dwd: DwdStatus | None = None,
    now_moment: datetime | None = None,
) -> list[str]:
    now_moment = now_moment or datetime.now(BERLIN)
    forecast_max = day_forecast.tmax if day_forecast else None
    focus_temps = focus_temperatures(forecast_max)
    lines = [
        f"— {heading} {target_date.strftime('%d.%m.%Y')} —",
        format_forecast_line(forecast_max, focus_temps),
        *format_hourly_prognosis_lines(day_forecast.hourly if day_forecast else None),
        *format_metar_peak_lines(metar_peak),
        *format_market_lines(prices, focus_temps),
    ]

    if metar is not None:
        lines.extend(
            [
                f"METAR EDDM: {metar.latest_temp:.0f}°C um "
                f"{metar.latest_time.strftime('%H:%M')} "
                f"({format_age(metar.latest_time, now_moment)})",
                f"METAR Max: {metar.max_temp:.0f}°C um "
                f"{metar.max_time.strftime('%H:%M')} ({metar.count} Meldungen)",
            ]
        )

    if dwd is not None:
        lines.append(
            f"DWD TT_10: {dwd.latest_temp:.1f}°C um "
            f"{dwd.latest_time.strftime('%H:%M')} "
            f"({format_age(dwd.latest_time, now_moment)}, Max {dwd.max_temp:.1f}°C "
            f"um {dwd.max_time.strftime('%H:%M')})"
        )

    return lines


def build_message(
    today: date,
    tomorrow: date,
    forecast_by_day: dict[date, DayForecast],
    prices_by_day: dict[date, dict[int, OutcomePrice] | None],
    metar_peak_today: MetarPeakPrognosis | None,
    metar: MetarStatus | None,
    dwd: DwdStatus | None,
) -> str:
    now_moment = datetime.now(BERLIN)
    now = now_moment.strftime("%d.%m.%Y %H:%M")

    lines = [
        "🌡 Polymarket München",
        f"Stand: {now} Ortszeit",
        "",
        *format_day_section(
            today,
            "Heute",
            forecast_by_day.get(today),
            prices_by_day.get(today),
            metar_peak=metar_peak_today,
            metar=metar,
            dwd=dwd,
            now_moment=now_moment,
        ),
        "",
        *format_day_section(
            tomorrow,
            "Morgen",
            forecast_by_day.get(tomorrow),
            prices_by_day.get(tomorrow),
            now_moment=now_moment,
        ),
    ]
    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API Fehler: {body}")


def main() -> int:
    load_env_file(ENV_FILE)
    dry_run = "--dry-run" in sys.argv or os.environ.get("DRY_RUN") == "1"
    force = "--force" in sys.argv
    slot_key = alert_slot_key()

    if duplicate_send_blocked(slot_key, force):
        print(
            f"Übersprungen: Nachricht für {slot_key} bereits gesendet "
            f"(10-Min-Slot). Nutze --force zum erneuten Senden.",
            file=sys.stderr,
        )
        return 0

    today = datetime.now(BERLIN).date()
    tomorrow = today + timedelta(days=1)
    forecast_by_day = fetch_open_meteo_forecasts()
    prices_by_day = {
        today: fetch_polymarket_prices(today),
        tomorrow: fetch_polymarket_prices(tomorrow),
    }

    if prices_by_day[today] is None and prices_by_day[tomorrow] is None:
        print("Kein Polymarket-Markt für heute oder morgen verfügbar.", file=sys.stderr)
        return 1

    dwd = fetch_dwd_status(today)
    metar = fetch_metar_status(today)
    metar_peak_today = fetch_metar_peak_prognosis(today)

    message = build_message(
        today,
        tomorrow,
        forecast_by_day,
        prices_by_day,
        metar_peak_today,
        metar,
        dwd,
    )
    print(message)

    if dry_run:
        print("\n[dry-run] Keine Telegram-Nachricht gesendet.", file=sys.stderr)
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print(
            "\nTelegram nicht konfiguriert. Bitte .telegram.env anlegen mit:\n"
            "  TELEGRAM_BOT_TOKEN=...\n"
            "  TELEGRAM_CHAT_ID=...\n",
            file=sys.stderr,
        )
        return 2

    try:
        send_telegram_message(token, chat_id, message)
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        print(f"Telegram HTTP-Fehler: {error.code} {details}", file=sys.stderr)
        return 3
    except urllib.error.URLError as error:
        print(f"Telegram Netzwerkfehler: {error.reason}", file=sys.stderr)
        return 3

    mark_alert_sent(slot_key)
    print("\nTelegram-Nachricht gesendet.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
