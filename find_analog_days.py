#!/usr/bin/env python3
"""Findet historische Analog-Tage zu aktuellen Bedingungen (München-Flughafen)."""

import argparse
import io
import json
import sys
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

USER_AGENT = "weather/1.0 (Munich Airport analog days)"
BERLIN = ZoneInfo("Europe/Berlin")
STATION_ID = "01262"
LAT, LON = 48.3538, 11.7861
DWD_CLIMATE_BASE = (
    "https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate"
)
DWD_10MIN_BASE = f"{DWD_CLIMATE_BASE}/10_minutes"
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "analog_days.txt"

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

TEMP_URLS = (
    f"{DWD_10MIN_BASE}/air_temperature/recent/10minutenwerte_TU_{STATION_ID}_akt.zip",
    f"{DWD_10MIN_BASE}/air_temperature/historical/"
    f"10minutenwerte_TU_{STATION_ID}_20200101_20251231_hist.zip",
    f"{DWD_10MIN_BASE}/air_temperature/now/10minutenwerte_TU_{STATION_ID}_now.zip",
)
WIND_URLS = (
    f"{DWD_10MIN_BASE}/wind/recent/10minutenwerte_wind_{STATION_ID}_akt.zip",
    f"{DWD_10MIN_BASE}/wind/historical/"
    f"10minutenwerte_wind_{STATION_ID}_20200101_20251231_hist.zip",
    f"{DWD_10MIN_BASE}/wind/now/10minutenwerte_wind_{STATION_ID}_now.zip",
)
PRECIP_URLS = (
    f"{DWD_10MIN_BASE}/precipitation/recent/10minutenwerte_nieder_{STATION_ID}_akt.zip",
    f"{DWD_10MIN_BASE}/precipitation/historical/"
    f"10minutenwerte_nieder_{STATION_ID}_20200101_20251231_hist.zip",
    f"{DWD_10MIN_BASE}/precipitation/now/10minutenwerte_nieder_{STATION_ID}_now.zip",
)
KL_URLS = (
    f"{DWD_CLIMATE_BASE}/daily/kl/recent/tageswerte_KL_{STATION_ID}_akt.zip",
    f"{DWD_CLIMATE_BASE}/daily/kl/historical/"
    f"tageswerte_KL_{STATION_ID}_19920517_20251231_hist.zip",
)


@dataclass
class Snapshot:
    tt: float
    rf: float | None
    wind: float | None
    precip: float
    cloud_pct: float | None = None


@dataclass
class AnalogDay:
    day: date
    score: float
    slot: int
    snap: Snapshot
    txk: float | None
    day_max: float
    peak_slot: int
    nm: float | None
    sdk: float | None
    rsk: float | None
    kw: int


def fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


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


def parse_float(value: str | None) -> float | None:
    if not value or value in {"-999", "-999.0"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def load_merged_rows(urls: tuple[str, ...]) -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for url in urls:
        for row in fetch_dwd_zip_rows(url):
            mess_datum = row.get("MESS_DATUM", "")
            if len(mess_datum) >= 12:
                merged[mess_datum] = row
    return merged


def load_daily_kl() -> dict[date, dict[str, str]]:
    daily: dict[date, dict[str, str]] = {}
    for url in KL_URLS:
        for row in fetch_dwd_zip_rows(url):
            mess_datum = row.get("MESS_DATUM", "")
            if len(mess_datum) == 8:
                day = date(int(mess_datum[:4]), int(mess_datum[4:6]), int(mess_datum[6:8]))
                daily[day] = row
    return daily


def load_intraday_series() -> dict[date, dict[int, Snapshot]]:
    temp_rows = load_merged_rows(TEMP_URLS)
    wind_rows = load_merged_rows(WIND_URLS)
    precip_rows = load_merged_rows(PRECIP_URLS)
    by_day: dict[date, dict[int, Snapshot]] = defaultdict(dict)

    for mess_datum, row in temp_rows.items():
        tt = parse_float(row.get("TT_10"))
        if tt is None:
            continue
        moment = datetime.strptime(mess_datum, "%Y%m%d%H%M").replace(tzinfo=BERLIN)
        wind = wind_rows.get(mess_datum, {})
        precip = precip_rows.get(mess_datum, {})
        slot = moment.hour * 60 + moment.minute
        by_day[moment.date()][slot] = Snapshot(
            tt=tt,
            rf=parse_float(row.get("RF_10")),
            wind=parse_float(wind.get("FF_10")),
            precip=parse_float(precip.get("RWS_10")) or 0.0,
        )
    return by_day


def format_time(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def snap_to_ten_minutes(moment: datetime) -> int:
    return moment.hour * 60 + (moment.minute // 10) * 10


def season_weeks(iso_week: int, window: int = 2) -> list[int]:
    weeks = []
    for offset in range(-window, window + 1):
        week = iso_week + offset
        if 1 <= week <= 52:
            weeks.append(week)
    return weeks


def fetch_open_meteo_hourly(target_date: date, forecast_days: int = 3) -> dict[str, list]:
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&hourly=temperature_2m,relative_humidity_2m,windspeed_10m,precipitation,cloudcover"
        f"&daily=temperature_2m_max&timezone=Europe%2FBerlin&forecast_days={forecast_days}"
    )
    payload = fetch_json(url)
    return payload.get("hourly", {})


def hourly_value_for_slot(hourly: dict[str, list], target_date: date, ref_slot: int) -> dict[str, float | None]:
    hour = ref_slot // 60
    for index, time_label in enumerate(hourly.get("time", [])):
        if not time_label.startswith(str(target_date)):
            continue
        if int(time_label[11:13]) != hour:
            continue
        return {
            "tt": float(hourly["temperature_2m"][index]),
            "rf": float(hourly["relative_humidity_2m"][index]),
            "wind": float(hourly["windspeed_10m"][index]),
            "precip": float(hourly["precipitation"][index]) / 6.0,
            "cloud_pct": float(hourly["cloudcover"][index]),
        }
    return {"tt": None, "rf": None, "wind": None, "precip": None, "cloud_pct": None}


def fetch_cloud_cover_pct(target_date: date, hour: int) -> float | None:
    hourly = fetch_open_meteo_hourly(target_date)
    values = hourly_value_for_slot(hourly, target_date, hour * 60)
    return values["cloud_pct"]


def fetch_forecast_max(target_date: date | None = None) -> float | None:
    if target_date is None:
        target_date = datetime.now(BERLIN).date()
    days_ahead = max(1, (target_date - datetime.now(BERLIN).date()).days + 1)
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        f"&daily=temperature_2m_max&timezone=Europe%2FBerlin&forecast_days={days_ahead}"
    )
    payload = fetch_json(url)
    daily = payload.get("daily", {})
    for day_label, temp in zip(daily.get("time", []), daily.get("temperature_2m_max", [])):
        if day_label == str(target_date):
            return float(temp)
    return None


def build_forecast_reference(target_date: date, ref_slot: int) -> Snapshot:
    hourly = fetch_open_meteo_hourly(target_date)
    values = hourly_value_for_slot(hourly, target_date, ref_slot)
    if values["tt"] is None:
        raise ValueError(
            f"Keine Open-Meteo-Stunde für {target_date} um {format_time(ref_slot)} gefunden"
        )
    return Snapshot(
        tt=values["tt"],
        rf=values["rf"],
        wind=values["wind"],
        precip=values["precip"] or 0.0,
        cloud_pct=values["cloud_pct"],
    )


def fetch_polymarket_markets(target_date: date) -> list[tuple[float, str]]:
    slug = (
        f"highest-temperature-in-munich-on-{MONTH_NAMES[target_date.month]}-"
        f"{target_date.day}-{target_date.year}"
    )
    query = urllib.parse.urlencode({"slug": slug})
    events = fetch_json(f"https://gamma-api.polymarket.com/events?{query}")
    if not events:
        return []
    markets = []
    for market in events[0].get("markets", []):
        yes_price = float(json.loads(market.get("outcomePrices", "[0, 1]"))[0]) * 100
        markets.append((yes_price, market.get("groupItemTitle", "?")))
    markets.sort(reverse=True)
    return markets


def build_reference(
    by_day: dict[date, dict[int, Snapshot]],
    target_date: date,
    ref_slot: int,
) -> Snapshot:
    today = by_day.get(target_date, {})
    if ref_slot in today:
        ref = today[ref_slot]
    elif today:
        nearest_slot = min(today, key=lambda slot: abs(slot - ref_slot))
        ref = today[nearest_slot]
    else:
        ref = Snapshot(tt=20.0, rf=55.0, wind=2.0, precip=0.0)

    cloud = fetch_cloud_cover_pct(target_date, ref_slot // 60)
    return Snapshot(
        tt=ref.tt,
        rf=ref.rf,
        wind=ref.wind,
        precip=ref.precip,
        cloud_pct=cloud,
    )


def score_snapshot(snap: Snapshot, ref: Snapshot, cloud_nm: float | None) -> float:
    score = abs(snap.tt - ref.tt) * 4.0
    if snap.rf is not None and ref.rf is not None:
        score += abs(snap.rf - ref.rf) * 0.08
    if snap.wind is not None and ref.wind is not None:
        score += abs(snap.wind - ref.wind) * 2.0
    score += snap.precip * 8.0
    if ref.precip > 0 and snap.precip == 0:
        score += 2.0
    if cloud_nm is not None and ref.cloud_pct is not None:
        score += abs(cloud_nm / 8 * 100 - ref.cloud_pct) * 0.04
    return score


def find_analog_days(
    by_day: dict[date, dict[int, Snapshot]],
    kl: dict[date, dict[str, str]],
    target_date: date,
    ref: Snapshot,
    ref_slot: int,
    week_window: int = 2,
    time_window_min: int = 20,
) -> list[AnalogDay]:
    season = season_weeks(target_date.isocalendar().week, week_window)
    analogs: list[AnalogDay] = []

    for day, times in by_day.items():
        if day >= target_date or day.isocalendar().week not in season:
            continue

        best: tuple[float, int, Snapshot] | None = None
        for slot, snap in times.items():
            if abs(slot - ref_slot) > time_window_min:
                continue
            daily = kl.get(day, {})
            score = score_snapshot(snap, ref, parse_float(daily.get("NM")))
            if best is None or score < best[0]:
                best = (score, slot, snap)

        if best is None:
            continue

        score, slot, snap = best
        daily = kl.get(day, {})
        txk = parse_float(daily.get("TXK"))
        day_max = max(entry.tt for entry in times.values())
        peak_slot = max(times, key=lambda minute: times[minute].tt)
        analogs.append(
            AnalogDay(
                day=day,
                score=score,
                slot=slot,
                snap=snap,
                txk=txk,
                day_max=day_max,
                peak_slot=peak_slot,
                nm=parse_float(daily.get("NM")),
                sdk=parse_float(daily.get("SDK")),
                rsk=parse_float(daily.get("RSK")),
                kw=day.isocalendar().week,
            )
        )

    analogs.sort(key=lambda item: item.score)
    return analogs


def day_max_value(entry: AnalogDay) -> float:
    return entry.txk if entry.txk is not None else entry.day_max


def summarize_maxima(entries: list[AnalogDay]) -> str:
    if not entries:
        return "keine Daten"
    values = [day_max_value(entry) for entry in entries]
    return (
        f"Ø {mean(values):.1f}°C  Median {median(values):.1f}°C  "
        f"Range {min(values):.1f}–{max(values):.1f}°C"
    )


def integer_distribution(entries: list[AnalogDay]) -> str:
    if not entries:
        return "–"
    counts = Counter(round(day_max_value(entry)) for entry in entries)
    return ", ".join(f"{temp}°C×{count}" for temp, count in counts.most_common())


def summarize_peak_times(entries: list[AnalogDay]) -> str:
    if not entries:
        return "keine Daten"
    peak_slots = [entry.peak_slot for entry in entries]
    sorted_slots = sorted(peak_slots)
    q1 = sorted_slots[len(sorted_slots) // 4]
    q3 = sorted_slots[(3 * len(sorted_slots)) // 4]
    return (
        f"Median {format_time(int(median(peak_slots)))}  "
        f"Ø {format_time(int(mean(peak_slots)))}  "
        f"Q1–Q3 {format_time(q1)}–{format_time(q3)}  "
        f"Range {format_time(min(peak_slots))}–{format_time(max(peak_slots))}"
    )


def peak_time_distribution(entries: list[AnalogDay]) -> str:
    if not entries:
        return "–"
    counts = Counter(format_time(entry.peak_slot) for entry in entries)
    return ", ".join(f"{slot}×{count}" for slot, count in counts.most_common(8))


def format_analog_line(entry: AnalogDay) -> str:
    snap = entry.snap
    txk = day_max_value(entry)
    nm = f"{entry.nm:.1f}" if entry.nm is not None else "–"
    sdk = f"{entry.sdk:.1f}" if entry.sdk is not None else "–"
    rsk = f"{entry.rsk:.1f}" if entry.rsk is not None else "–"
    return (
        f"{entry.day.strftime('%d.%m.%Y')}  {format_time(entry.slot)}  "
        f"TT={snap.tt:.1f}°C  RF={snap.rf or 0:.0f}%  Wind={snap.wind or 0:.1f}m/s  "
        f"NM={nm}/8  SDK={sdk}h  Regen={rsk}mm  → Tagesmax {txk:.1f}°C  "
        f"Peak {format_time(entry.peak_slot)}  Score={entry.score:.1f}"
    )


def build_report(
    target_date: date,
    ref_slot: int,
    ref: Snapshot,
    analogs: list[AnalogDay],
    forecast_max: float | None,
    markets: list[tuple[float, str]],
    reference_source: str = "DWD live",
) -> str:
    lines = [
        f"Analog-Tage – {target_date.strftime('%d.%m.%Y')} {format_time(ref_slot)} Ortszeit "
        f"(KW {target_date.isocalendar().week})",
        f"Station {STATION_ID} München-Flughafen",
        f"Referenzquelle: {reference_source}",
        "",
        "Referenz:",
        f"  TT_10:       {ref.tt:.1f}°C",
        f"  Luftfeuchte: {ref.rf:.0f}%" if ref.rf is not None else "  Luftfeuchte: n/a",
        (
            f"  Wind:        {ref.wind:.1f} m/s ({ref.wind * 3.6:.0f} km/h)"
            if ref.wind is not None
            else "  Wind:        n/a"
        ),
        f"  Regen 10min: {ref.precip:.2f} mm",
        (
            f"  Wolken:      {ref.cloud_pct:.0f}% (Prognose)"
            if ref.cloud_pct is not None
            else "  Wolken:      n/a"
        ),
        f"  Jahreszeit:  KW {season_weeks(target_date.isocalendar().week)[0]}"
        f"–{season_weeks(target_date.isocalendar().week)[-1]}",
        "",
    ]
    if analogs:
        lines.extend(
            [
                "Erwartete Peak-Zeit (historische Analoge):",
                f"  Top 10:   {summarize_peak_times(analogs[:10])}",
                f"  Top 15:   {summarize_peak_times(analogs[:15])}",
                f"  Verteilung Top 10: {peak_time_distribution(analogs[:10])}",
                "",
            ]
        )

    lines.extend([f"Top 15 analoge Tage ({len(analogs)} Kandidaten):"])
    for entry in analogs[:15]:
        lines.append(f"  {format_analog_line(entry)}")

    cloudy = [entry for entry in analogs if entry.nm is not None and entry.nm >= 5.0]
    rainy = [entry for entry in analogs if entry.rsk is not None and entry.rsk >= 1.0]

    lines.extend(["", "Bewölkt (NM ≥ 5/8, Top 10):"])
    if cloudy:
        for entry in cloudy[:10]:
            lines.append(f"  {format_analog_line(entry)}")
        lines.append(f"  {summarize_maxima(cloudy[:10])}")
        lines.append(f"  Verteilung: {integer_distribution(cloudy[:10])}")
    else:
        lines.append("  keine Treffer")

    lines.extend(["", "Regen (≥ 1 mm, Top 8):"])
    if rainy:
        for entry in rainy[:8]:
            lines.append(f"  {format_analog_line(entry)}")
        lines.append(f"  {summarize_maxima(rainy[:8])}")
    else:
        lines.append("  keine Treffer")

    lines.extend(["", "Vergleich Prognose / Markt / Analoge:"])
    if forecast_max is not None:
        lines.append(f"  Open-Meteo Tmax:     {forecast_max:.1f}°C → {int(forecast_max + 0.5)}°C")
    if markets:
        lines.append(f"  Polymarket Favorit:  {markets[0][1]} ({markets[0][0]:.1f}%)")
    if cloudy:
        lines.append(f"  Bewölkte Analoge:    {summarize_maxima(cloudy[:10])}")
    if rainy:
        lines.append(f"  Regen-Analoge:       {summarize_maxima(rainy[:8])}")
    if analogs:
        lines.append(f"  Beste 10 gesamt:     {summarize_maxima(analogs[:10])}")
        lines.append(f"  Peak Top 10:         {summarize_peak_times(analogs[:10])}")

    return "\n".join(lines) + "\n"


def parse_ref_time(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def run_analog_search(
    output_path: Path = OUTPUT_PATH,
    target_date: date | None = None,
    ref_slot: int | None = None,
    use_forecast_ref: bool = False,
) -> Path:
    now = datetime.now(BERLIN)
    target_date = target_date or now.date()
    ref_slot = ref_slot if ref_slot is not None else snap_to_ten_minutes(now)

    by_day = load_intraday_series()
    kl = load_daily_kl()
    if use_forecast_ref:
        ref = build_forecast_reference(target_date, ref_slot)
        reference_source = "Open-Meteo Prognose"
    else:
        ref = build_reference(by_day, target_date, ref_slot)
        reference_source = "DWD live"
    analogs = find_analog_days(by_day, kl, target_date, ref, ref_slot)
    forecast_max = fetch_forecast_max(target_date)
    markets = fetch_polymarket_markets(target_date)

    report = build_report(
        target_date,
        ref_slot,
        ref,
        analogs,
        forecast_max,
        markets,
        reference_source=reference_source,
    )
    output_path.write_text(report, encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Historische Analog-Tage für München-Flughafen")
    parser.add_argument(
        "--date",
        help="Zieldatum YYYY-MM-DD (Standard: heute)",
    )
    parser.add_argument(
        "--tomorrow",
        action="store_true",
        help="Morgen als Zieldatum",
    )
    parser.add_argument(
        "--ref-time",
        default="07:20",
        help="Referenz-Uhrzeit HH:MM Ortszeit (Standard: 07:20)",
    )
    parser.add_argument(
        "--forecast-ref",
        action="store_true",
        help="Referenz aus Open-Meteo statt DWD-Live",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help="Ausgabedatei für den Report",
    )
    return parser.parse_args()


def resolve_target_date(args: argparse.Namespace) -> date:
    today = datetime.now(BERLIN).date()
    if args.tomorrow:
        return today + timedelta(days=1)
    if args.date:
        return date.fromisoformat(args.date)
    return today


def main() -> None:
    try:
        args = parse_args()
        target_date = resolve_target_date(args)
        ref_slot = parse_ref_time(args.ref_time)
        use_forecast_ref = args.forecast_ref or args.tomorrow or (
            target_date > datetime.now(BERLIN).date()
        )
        output = run_analog_search(
            output_path=args.output,
            target_date=target_date,
            ref_slot=ref_slot,
            use_forecast_ref=use_forecast_ref,
        )
        print(output.read_text(encoding="utf-8"))
        print(f"Report gespeichert: {output.resolve()}")
    except Exception as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
