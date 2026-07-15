#!/usr/bin/env python3
"""Vergleich: KLGA1M Push + Polymarket + stündliche Open-Meteo-Vorhersage.

Antwort auf „gibt es stündliche Wettevorhersagedaten?“:
  Ja – über Open-Meteo Previous-Runs (hourly temperature_2m + previous_day1).
  Daraus: stündliche Kurs/Temp-Kurve und implizites Tageshoch (max der
  Resttages-Stunden), als dritte Quelle neben Push und Markt.

Nutzung:
  python3 compare_push_pm_hourly_forecast.py \\
      --csv /opt/cursor/artifacts/klga1m_push_last_days.csv \\
      --out-dir /opt/cursor/artifacts
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

USER_AGENT = "weather/1.0 (push pm hourly forecast compare)"
NY = ZoneInfo("America/New_York")
KLGA_LAT = 40.7769
KLGA_LON = -73.8740
PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
GAMMA = "https://gamma-api.polymarket.com/events"
CLOB_HISTORY = "https://clob.polymarket.com/prices-history"
MONTHS = (
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


def fetch_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def round_f(f: float) -> int:
    return int(math.floor(f + 0.5))


@dataclass
class HourlyForecast:
    time: datetime
    temp_f: float
    temp_pd1_f: float | None
    # Implizites Tageshoch aus Restkurve (dieser + spätere Stunden)
    implied_tmax_f: float
    implied_tmax_round: int
    # Gesamtkurven-Max des Tages (pd0 / pd1)
    day_curve_max_f: float
    day_curve_max_pd1_f: float | None


def fetch_open_meteo_hourly(days: list[date]) -> dict[date, list[HourlyForecast]]:
    start = min(days).isoformat()
    end = max(days).isoformat()
    params = {
        "latitude": KLGA_LAT,
        "longitude": KLGA_LON,
        "hourly": "temperature_2m,temperature_2m_previous_day1",
        "timezone": "America/New_York",
        "start_date": start,
        "end_date": end,
    }
    payload = fetch_json(f"{PREV_RUNS}?{urllib.parse.urlencode(params)}")
    hourly = payload["hourly"]
    times = [
        datetime.fromisoformat(t).replace(tzinfo=NY) for t in hourly["time"]
    ]
    temps = [c_to_f(float(v)) for v in hourly["temperature_2m"]]
    pd1_raw = hourly.get("temperature_2m_previous_day1") or [None] * len(temps)
    pd1 = [None if v is None else c_to_f(float(v)) for v in pd1_raw]

    by_day: dict[date, list[tuple[datetime, float, float | None]]] = defaultdict(list)
    for t, temp, prev in zip(times, temps, pd1):
        by_day[t.date()].append((t, temp, prev))

    out: dict[date, list[HourlyForecast]] = {}
    for day, rows in by_day.items():
        rows = sorted(rows)
        day_max = max(r[1] for r in rows)
        day_max_pd1 = None
        pd1_vals = [r[2] for r in rows if r[2] is not None]
        if pd1_vals:
            day_max_pd1 = max(pd1_vals)

        day_rows: list[HourlyForecast] = []
        for index, (t, temp, prev) in enumerate(rows):
            rest = [r[1] for r in rows[index:]]
            implied = max(rest)
            day_rows.append(
                HourlyForecast(
                    time=t,
                    temp_f=temp,
                    temp_pd1_f=prev,
                    implied_tmax_f=implied,
                    implied_tmax_round=round_f(implied),
                    day_curve_max_f=day_max,
                    day_curve_max_pd1_f=day_max_pd1,
                )
            )
        out[day] = day_rows
    return out


def load_push_hourly_max(csv_path: Path, days: list[date]) -> dict[date, list[tuple[datetime, float, float]]]:
    """Pro Stunde: letzter Push-Temp und Running-Max °F."""
    rows = []
    with csv_path.open(encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            observed = datetime.fromisoformat(raw["observed_at_ny"]).astimezone(NY)
            received = (
                datetime.fromisoformat(raw["received_at_ny"]).astimezone(NY)
                if raw.get("received_at_ny")
                else datetime.fromisoformat(raw["received_at_utc"]).astimezone(NY)
            )
            rows.append(
                {
                    "day": date.fromisoformat(raw["local_date"]),
                    "received": received,
                    "temp_f": float(raw["temp_f"]),
                    "run_max": float(raw["running_max_f"]),
                }
            )
    out: dict[date, list[tuple[datetime, float, float]]] = {}
    for day in days:
        day_rows = [r for r in rows if r["day"] == day]
        if not day_rows:
            continue
        # letzte Beobachtung je Stunde (ET)
        by_hour: dict[datetime, tuple[float, float]] = {}
        for r in day_rows:
            hour = r["received"].replace(minute=0, second=0, microsecond=0)
            by_hour[hour] = (r["temp_f"], r["run_max"])
        out[day] = [(h, *by_hour[h]) for h in sorted(by_hour)]
    return out


def fetch_pm_favorite_hourly(day: date) -> list[tuple[datetime, str, float]]:
    """Stündliche Yes-Preise der Top-3 Buckets (fidelity=60)."""
    slug = f"highest-temperature-in-nyc-on-{MONTHS[day.month]}-{day.day}-{day.year}"
    events = fetch_json(f"{GAMMA}?{urllib.parse.urlencode({'slug': slug})}")
    if not events:
        return []
    markets = []
    for market in events[0].get("markets", []):
        prices = json.loads(market.get("outcomePrices", "[0,1]"))
        tokens = json.loads(market.get("clobTokenIds", "[]"))
        if not tokens:
            continue
        markets.append(
            {
                "label": market.get("groupItemTitle") or "?",
                "yes": float(prices[0]),
                "token": tokens[0],
            }
        )
    markets.sort(key=lambda m: m["yes"], reverse=True)
    top = markets[:3]
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=NY)
    day_end = day_start + timedelta(days=1)
    series: dict[str, list[tuple[datetime, float]]] = {}
    for market in top:
        query = urllib.parse.urlencode(
            {
                "market": market["token"],
                "startTs": int(day_start.timestamp()),
                "endTs": int(min(datetime.now(NY), day_end).timestamp()),
                "fidelity": 60,
            }
        )
        hist = fetch_json(f"{CLOB_HISTORY}?{query}").get("history", [])
        series[market["label"]] = [
            (datetime.fromtimestamp(p["t"], NY), p["p"] * 100.0) for p in hist
        ]

    # Merge auf volle Stunden: favoriten-label = höchstes Yes zu dieser Stunde
    hours = []
    cursor = day_start
    end = min(datetime.now(NY), day_end)
    while cursor < end:
        best_label, best_price = None, -1.0
        for label, points in series.items():
            last = None
            for t, price in points:
                if t <= cursor + timedelta(minutes=59):
                    last = price
                else:
                    break
            if last is not None and last > best_price:
                best_label, best_price = label, last
        if best_label is not None:
            hours.append((cursor, best_label, best_price))
        cursor += timedelta(hours=1)
    return hours


def band_for_temp(whole_f: int, labels: list[str]) -> str | None:
    import re

    for label in labels:
        lower = label.lower().replace("°f", "")
        digits = [int(x) for x in re.findall(r"\d+", lower)]
        if not digits:
            continue
        if "below" in lower and whole_f <= digits[0]:
            return label
        if "higher" in lower and whole_f >= digits[0]:
            return label
        if len(digits) >= 2 and digits[0] <= whole_f <= digits[1]:
            return label
        if len(digits) == 1 and whole_f == digits[0]:
            return label
    return None


def plot_day_comparison(
    day: date,
    forecasts: list[HourlyForecast],
    push_hourly: list[tuple[datetime, float, float]],
    pm_hourly: list[tuple[datetime, str, float]],
    out_path: Path,
) -> Path:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, (ax_t, ax_p) = plt.subplots(
        2, 1, figsize=(13, 8.2), sharex=True, gridspec_kw={"height_ratios": [1.25, 1]}
    )

    # --- Temperatur / Vorhersage ---
    if forecasts:
        ax_t.step(
            [f.time for f in forecasts],
            [f.temp_f for f in forecasts],
            where="post",
            color="#264653",
            linewidth=1.6,
            label="OM stündl. T (pd0)",
        )
        ax_t.step(
            [f.time for f in forecasts],
            [f.implied_tmax_f for f in forecasts],
            where="post",
            color="#e9c46a",
            linewidth=2.0,
            linestyle="--",
            label="OM implizites Tageshoch (Restkurve)",
        )
        if forecasts[0].day_curve_max_pd1_f is not None:
            ax_t.axhline(
                forecasts[0].day_curve_max_pd1_f,
                color="#f4a261",
                linewidth=1.2,
                alpha=0.8,
                label=f"OM Tmax vorhergesagt −24h ({forecasts[0].day_curve_max_pd1_f:.1f}°F)",
            )
        ax_t.axhline(
            forecasts[0].day_curve_max_f,
            color="#e9c46a",
            linewidth=1.0,
            alpha=0.45,
            label=f"OM Kurven-Max Tag ({forecasts[0].day_curve_max_f:.1f}°F)",
        )

    if push_hourly:
        ax_t.plot(
            [t for t, _, _ in push_hourly],
            [tmax for _, _, tmax in push_hourly],
            color="#2a9d8f",
            linewidth=2.2,
            marker="o",
            markersize=4,
            label="Push Running-Max (stündl.)",
        )
        ax_t.plot(
            [t for t, _, _ in push_hourly],
            [temp for _, temp, _ in push_hourly],
            color="#2a9d8f",
            linewidth=1.0,
            alpha=0.45,
            label="Push Temp (Stunde)",
        )

    ax_t.set_ylabel("°F")
    ax_t.set_title(
        f"Drei Quellen: Push · Open-Meteo (stündl.) · Polymarket — {day:%d.%m.%Y}",
        fontsize=13,
        fontweight="bold",
    )
    ax_t.grid(True, alpha=0.28)
    ax_t.legend(loc="upper left", fontsize=8, frameon=False, ncol=2)

    # --- Polymarket ---
    if pm_hourly:
        labels_seen = []
        colors = {"default": "#c44e52"}
        palette = ["#c44e52", "#6d6875", "#457b9d"]
        # plot each label as separate series from hourly points
        by_label: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
        for t, label, price in pm_hourly:
            by_label[label].append((t, price))
        for index, (label, points) in enumerate(by_label.items()):
            color = palette[index % len(palette)]
            ax_p.step(
                [t for t, _ in points],
                [p for _, p in points],
                where="post",
                color=color,
                linewidth=1.8,
                label=f"PM Favorit-Pfad / {label}",
            )
            ax_p.scatter(
                [t for t, _ in points],
                [p for _, p in points],
                color=color,
                s=22,
                zorder=3,
            )

        # Marker: wann OM-implied Tmax das Settlement-Band trifft
        if forecasts and push_hourly:
            # Favoriten-Labels
            label_list = list(by_label.keys())
            crossed = None
            for f in forecasts:
                band = band_for_temp(f.implied_tmax_round, label_list)
                if band and f.implied_tmax_round >= 90:
                    crossed = f
                    break
            if crossed:
                ax_p.axvline(crossed.time, color="#e9c46a", linestyle=":", linewidth=1.4)
                ax_t.axvline(crossed.time, color="#e9c46a", linestyle=":", linewidth=1.4)
                ax_p.annotate(
                    f"OM impl. Hoch {crossed.implied_tmax_round}°F",
                    xy=(crossed.time, 8),
                    fontsize=8,
                    color="#8c6d46",
                )

    ax_p.set_ylim(0, 100)
    ax_p.set_ylabel("Yes-Preis (%)")
    ax_p.set_xlabel("America/New_York")
    ax_p.grid(True, alpha=0.28)
    ax_p.legend(loc="upper left", fontsize=8, frameon=False)
    ax_p.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=NY))
    fig.autofmt_xdate()
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out_path


def write_summary(
    path: Path,
    days: list[date],
    forecast_by_day: dict[date, list[HourlyForecast]],
    push_by_day: dict,
    pm_by_day: dict,
) -> str:
    lines = [
        "Stündliche Wettevorhersagedaten + Push + Polymarket",
        "",
        "Quelle Vorhersage: Open-Meteo Previous-Runs API (KLGA)",
        "  – temperature_2m (pd0, aktuelle/aktualisierte Modellkurve)",
        "  – temperature_2m_previous_day1 (Vorhersage von −24h)",
        "  – implizites Tageshoch = max(Reststunden der pd0-Kurve)",
        "Polymarket: stündliche Top-Bucket-Yes-Preise (CLOB fidelity=60)",
        "Push: stündlich aggregiertes Running-Max aus synoptic_push_obs",
        "",
    ]
    for day in days:
        forecasts = forecast_by_day.get(day) or []
        push = push_by_day.get(day) or []
        pm = pm_by_day.get(day) or []
        lines.append(f"=== {day} ===")
        if forecasts:
            f0 = forecasts[0]
            lines.append(
                f"OM Kurven-Max: {f0.day_curve_max_f:.1f}°F "
                f"(gerundet {round_f(f0.day_curve_max_f)}°F)"
            )
            if f0.day_curve_max_pd1_f is not None:
                lines.append(
                    f"OM Tmax −24h (pd1): {f0.day_curve_max_pd1_f:.1f}°F "
                    f"(gerundet {round_f(f0.day_curve_max_pd1_f)}°F)"
                )
            # stündliche implizite Hochs (Auswahl)
            for f in forecasts:
                if f.time.hour in {6, 8, 10, 12, 14, 16}:
                    lines.append(
                        f"  {f.time:%H:%M}  OM T={f.temp_f:.1f}°F  "
                        f"impl.Hoch={f.implied_tmax_f:.1f}→{f.implied_tmax_round}°F"
                    )
        if push:
            peak = max(push, key=lambda r: r[2])
            lines.append(
                f"Push Running-Max Peak: {peak[2]:.1f}°F @ {peak[0]:%H:%M} "
                f"(gerundet {round_f(peak[2])}°F)"
            )
        if pm:
            # letzter Favorit
            t, label, price = pm[-1]
            lines.append(f"PM letzter Favorit @{t:%H:%M}: {label} = {price:.1f}%")
            # Vergleich OM-Band vs PM-Favorit um 12:00 und 15:00
            for hour in (10, 12, 14):
                moment = datetime.combine(day, datetime.min.time(), tzinfo=NY).replace(
                    hour=hour
                )
                f_match = next((f for f in forecasts if f.time == moment), None)
                p_match = next((p for p in pm if p[0] == moment), None)
                if f_match and p_match:
                    lines.append(
                        f"  @{hour:02d}:00  OM-impl {f_match.implied_tmax_round}°F vs "
                        f"PM {p_match[1]} ({p_match[2]:.0f}%)"
                    )
        lines.append("")
    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    # Tage aus Push-CSV ableiten
    days_set = set()
    with args.csv.open(encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            days_set.add(date.fromisoformat(raw["local_date"]))
    days = sorted(days_set)
    if not days:
        print("Keine Tage in CSV.", flush=True)
        return 1

    forecast_by_day = fetch_open_meteo_hourly(days)
    push_by_day = load_push_hourly_max(args.csv, days)
    pm_by_day = {day: fetch_pm_favorite_hourly(day) for day in days}

    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    charts = []
    for day in days:
        if day not in forecast_by_day and day not in push_by_day:
            continue
        path = plot_day_comparison(
            day,
            forecast_by_day.get(day, []),
            push_by_day.get(day, []),
            pm_by_day.get(day, []),
            out / f"push_pm_om_hourly_{day.isoformat()}.png",
        )
        charts.append(path)
        print(f"Chart: {path}")

    summary = write_summary(
        out / "push_pm_om_hourly_summary.txt",
        days,
        forecast_by_day,
        push_by_day,
        pm_by_day,
    )
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
