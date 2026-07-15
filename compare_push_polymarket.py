#!/usr/bin/env python3
"""Vergleicht KLGA1M Synoptic-Push (1-Min) aus MariaDB mit NYC-Polymarket-Quotes.

Polymarket „Highest temperature in NYC“ resolved nach Wunderground KLGA (°F).
Der Push-Kanal liefert denselben Standort früher (~3–4 Min Delay observed→received).

Nutzung:
  python3 compare_push_polymarket.py
  python3 compare_push_polymarket.py --days 3 --out-dir ./artifacts
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

USER_AGENT = "weather/1.0 (KLGA1M push vs Polymarket)"
NY = ZoneInfo("America/New_York")
UTC = timezone.utc
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
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env.db"
DEFAULT_OUT = Path("/opt/cursor/artifacts")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def connect_db():
    import pymysql

    return pymysql.connect(
        host=os.environ["DB_HOST"].strip(),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ["DB_USER"].strip(),
        password=os.environ["DB_PASSWORD"].strip(),
        database=os.environ["DB_NAME"].strip(),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0


def round_f_wu(fahrenheit: float) -> int:
    """Ganzzahl-°F wie Wunderground/Polymarket (half-up)."""
    return int(math.floor(fahrenheit + 0.5))


def fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def polymarket_slug(target: date) -> str:
    return (
        f"highest-temperature-in-nyc-on-{MONTH_NAMES[target.month]}-"
        f"{target.day}-{target.year}"
    )


def fetch_push_rows(days: int) -> list[dict]:
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT station, observed_at_utc, value_num, received_at_utc
                FROM synoptic_push_obs
                WHERE station = 'KLGA1M'
                  AND sensor = 'air_temp'
                  AND observed_at_utc >= %s
                ORDER BY observed_at_utc ASC
                """,
                (cutoff,),
            )
            rows = cursor.fetchall()
    finally:
        connection.close()
    return rows


def enrich_push_rows(rows: list[dict]) -> list[dict]:
    enriched = []
    for row in rows:
        observed = row["observed_at_utc"]
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        received = row["received_at_utc"]
        if received.tzinfo is None:
            # DATETIME(3) from MariaDB comes back naive; treat as UTC.
            received = received.replace(tzinfo=UTC)
        temp_c = float(row["value_num"])
        temp_f = c_to_f(temp_c)
        enriched.append(
            {
                "station": row["station"],
                "observed_at_utc": observed,
                "observed_at_ny": observed.astimezone(NY),
                "received_at_utc": received,
                "received_at_ny": received.astimezone(NY),
                "temp_c": temp_c,
                "temp_f": temp_f,
                "temp_f_round": round_f_wu(temp_f),
                "lag_min": (received - observed).total_seconds() / 60.0,
                "local_date": observed.astimezone(NY).date(),
            }
        )
    return enriched


def running_daily_max(rows: list[dict]) -> list[dict]:
    """Pro Kalendertag (NY) kumuliertes Max in °F und gerundet."""
    out = []
    by_day: dict[date, float] = {}
    by_day_round: dict[date, int] = {}
    for row in rows:
        day = row["local_date"]
        by_day[day] = max(by_day.get(day, float("-inf")), row["temp_f"])
        by_day_round[day] = max(by_day_round.get(day, -10**9), row["temp_f_round"])
        out.append(
            {
                **row,
                "running_max_f": by_day[day],
                "running_max_f_round": by_day_round[day],
            }
        )
    return out


def fetch_event_markets(target: date) -> list[dict]:
    slug = polymarket_slug(target)
    events = fetch_json(f"{GAMMA_API_URL}?{urllib.parse.urlencode({'slug': slug})}")
    if not events:
        # Fallback ohne Jahreszahl (manche Events nutzen kurze Slugs)
        short = (
            f"highest-temperature-in-nyc-on-{MONTH_NAMES[target.month]}-{target.day}"
        )
        events = fetch_json(
            f"{GAMMA_API_URL}?{urllib.parse.urlencode({'slug': short})}"
        )
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
                "label": market.get("groupItemTitle") or market.get("question", "?"),
                "yes_price": float(prices[0]),
                "token": tokens[0],
            }
        )
    markets.sort(key=lambda item: item["yes_price"], reverse=True)
    return markets


def fetch_price_history(
    token_id: str, start_ts: int, end_ts: int, fidelity: int = 5
) -> list[tuple[datetime, float]]:
    query = urllib.parse.urlencode(
        {
            "market": token_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": fidelity,
        }
    )
    payload = fetch_json(f"{CLOB_HISTORY_URL}?{query}")
    history = payload.get("history", []) if isinstance(payload, dict) else []
    return [
        (datetime.fromtimestamp(point["t"], NY), point["p"] * 100.0)
        for point in history
    ]


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "observed_at_utc",
        "observed_at_ny",
        "received_at_utc",
        "received_at_ny",
        "temp_c",
        "temp_f",
        "temp_f_round",
        "running_max_f",
        "running_max_f_round",
        "lag_min",
        "local_date",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    **row,
                    "observed_at_utc": row["observed_at_utc"].isoformat(),
                    "observed_at_ny": row["observed_at_ny"].isoformat(),
                    "received_at_utc": row["received_at_utc"].isoformat(),
                    "local_date": row["local_date"].isoformat(),
                    "temp_c": f"{row['temp_c']:.2f}",
                    "temp_f": f"{row['temp_f']:.2f}",
                    "running_max_f": f"{row['running_max_f']:.2f}",
                    "lag_min": f"{row['lag_min']:.2f}",
                }
            )


def summarize_day(day: date, day_rows: list[dict], markets: list[dict]) -> str:
    if not day_rows:
        return f"{day.isoformat()}: keine Push-Daten"

    peak = max(day_rows, key=lambda r: r["temp_f"])
    first = day_rows[0]
    last = day_rows[-1]
    lines = [
        f"=== {day.strftime('%a %Y-%m-%d')} (America/New_York) ===",
        f"Push KLGA1M: {len(day_rows)} Werte | "
        f"{first['observed_at_ny']:%H:%M}–{last['observed_at_ny']:%H:%M} ET",
        f"Aktuell/letzter Wert: {last['temp_c']:.1f}°C = {last['temp_f']:.1f}°F "
        f"(gerundet {last['temp_f_round']}°F)",
        f"Push-Tageshoch: {peak['temp_c']:.1f}°C = {peak['temp_f']:.1f}°F "
        f"(gerundet {peak['temp_f_round']}°F) um {peak['observed_at_ny']:%H:%M} ET "
        f"(empfangen {peak['received_at_ny']:%H:%M} ET, Lag {peak['lag_min']:.1f} Min)",
        f"Push Lag median: "
        f"{sorted(r['lag_min'] for r in day_rows)[len(day_rows)//2]:.1f} Min",
    ]
    if markets:
        top = markets[:5]
        lines.append(
            "Polymarket Top-Outcomes: "
            + ", ".join(f"{m['label']}={m['yes_price']*100:.1f}%" for m in top)
        )
        favorite = markets[0]["label"]
        lines.append(
            f"Markt-Favorit „{favorite}“ vs. Push-Hoch gerundet "
            f"{peak['temp_f_round']}°F "
            f"({'im Favoriten-Band' if favorite_contains(favorite, peak['temp_f_round']) else 'Favorit ≠ Push-Hoch'})"
        )
    else:
        lines.append("Polymarket: kein Event gefunden")
    return "\n".join(lines)


def favorite_contains(label: str, whole_f: int) -> bool:
    lower = label.lower().replace("°f", "").strip()
    digits = [int(tok) for tok in re.findall(r"\d+", lower)]
    if not digits:
        return False
    if "below" in lower:
        return whole_f <= digits[0]
    if "higher" in lower:
        return whole_f >= digits[0]
    if len(digits) >= 2:
        return digits[0] <= whole_f <= digits[1]
    return whole_f == digits[0]


def plot_day(
    day: date,
    day_rows: list[dict],
    markets: list[dict],
    out_path: Path,
) -> Path | None:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    if not day_rows:
        return None

    day_start = datetime.combine(day, datetime.min.time(), tzinfo=NY)
    day_end = day_start + timedelta(days=1)
    start_ts = int(day_start.timestamp())
    end_ts = int(min(datetime.now(NY), day_end).timestamp())

    top_markets = markets[:4]
    histories = {
        m["label"]: fetch_price_history(m["token"], start_ts, end_ts)
        for m in top_markets
    }

    fig, ax_temp = plt.subplots(figsize=(13, 7))
    ax_pm = ax_temp.twinx()

    times = [r["received_at_ny"] for r in day_rows]
    temps = [r["temp_f"] for r in day_rows]
    run_max = [r["running_max_f"] for r in day_rows]

    ax_temp.plot(
        times,
        temps,
        color="#1b9e77",
        linewidth=1.4,
        label="KLGA1M Push °F (empfangen)",
        zorder=3,
    )
    ax_temp.plot(
        times,
        run_max,
        color="#d95f02",
        linewidth=2.0,
        linestyle="--",
        label="laufendes Push-Hoch °F",
        zorder=4,
    )
    peak = max(day_rows, key=lambda r: r["temp_f"])
    ax_temp.axhline(
        peak["temp_f_round"],
        color="#d95f02",
        alpha=0.25,
        linewidth=1,
    )
    ax_temp.annotate(
        f"Hoch {peak['temp_f']:.1f}°F → {peak['temp_f_round']}°F",
        xy=(peak["received_at_ny"], peak["temp_f"]),
        xytext=(10, 12),
        textcoords="offset points",
        fontsize=9,
        color="#d95f02",
    )

    colors = ["#7570b3", "#e7298a", "#66a61e", "#e6ab02"]
    for (label, history), color in zip(histories.items(), colors):
        if not history:
            continue
        pm_times, pm_prices = zip(*history)
        ax_pm.plot(
            pm_times,
            pm_prices,
            color=color,
            linewidth=1.8,
            alpha=0.9,
            label=f"PM {label}",
            zorder=2,
        )

    ax_temp.set_ylabel("Temperatur (°F, KLGA1M Push)")
    ax_pm.set_ylabel("Polymarket Yes-Preis (%)")
    ax_pm.set_ylim(0, 100)
    ax_temp.set_xlabel("Uhrzeit (America/New_York)")
    ax_temp.set_title(
        f"KLGA1M 1-Min Push vs. Polymarket NYC – {day.strftime('%d.%m.%Y')}\n"
        f"Resolution: Wunderground KLGA · Push-Hoch gerundet "
        f"{peak['temp_f_round']}°F",
        fontsize=13,
        fontweight="bold",
    )
    ax_temp.grid(True, alpha=0.3)
    ax_temp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=NY))

    handles_t, labels_t = ax_temp.get_legend_handles_labels()
    handles_p, labels_p = ax_pm.get_legend_handles_labels()
    ax_temp.legend(handles_t + handles_p, labels_t + labels_p, loc="upper left")

    fig.autofmt_xdate()
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_overview(rows: list[dict], out_path: Path) -> Path | None:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    if not rows:
        return None

    fig, ax = plt.subplots(figsize=(14, 5.5))
    times = [r["received_at_ny"] for r in rows]
    temps = [r["temp_f"] for r in rows]
    ax.plot(times, temps, color="#1b9e77", linewidth=1.0, label="KLGA1M Push °F")

    # Tagesgrenzen + Tageshoch-Marken
    days = sorted({r["local_date"] for r in rows})
    for day in days:
        day_rows = [r for r in rows if r["local_date"] == day]
        peak = max(day_rows, key=lambda r: r["temp_f"])
        ax.scatter(
            [peak["received_at_ny"]],
            [peak["temp_f"]],
            color="#d95f02",
            s=36,
            zorder=5,
        )
        ax.annotate(
            f"{day:%m/%d}\n{peak['temp_f_round']}°F",
            xy=(peak["received_at_ny"], peak["temp_f"]),
            xytext=(0, 10),
            textcoords="offset points",
            ha="center",
            fontsize=8,
            color="#d95f02",
        )
        midnight = datetime.combine(day, datetime.min.time(), tzinfo=NY)
        ax.axvline(midnight, color="#888888", alpha=0.25, linewidth=1)

    ax.set_title(
        "KLGA1M Synoptic Push (1-Min) – letzte Tage\n"
        "Markers: Tageshoch (für Polymarket-KLGA-Settlement gerundet)",
        fontsize=13,
        fontweight="bold",
    )
    ax.set_ylabel("°F")
    ax.set_xlabel("America/New_York")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M", tz=NY))
    fig.autofmt_xdate()
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=3, help="Lookback in Tagen")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT,
        help="Zielverzeichnis für CSV/Charts",
    )
    args = parser.parse_args()

    load_env_file(ENV_FILE)
    for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        if not os.environ.get(key, "").strip():
            print(f"Fehlt Umgebungsvariable {key}", file=sys.stderr)
            return 1

    raw = fetch_push_rows(args.days)
    if not raw:
        print("Keine Push-Daten im Fenster.", file=sys.stderr)
        return 1

    rows = running_daily_max(enrich_push_rows(raw))
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "klga1m_push_last_days.csv"
    write_csv(csv_path, rows)
    print(f"CSV: {csv_path} ({len(rows)} Zeilen)")

    overview = plot_overview(rows, out_dir / "klga1m_push_overview.png")
    if overview:
        print(f"Overview: {overview}")

    days = sorted({r["local_date"] for r in rows})
    summary_lines = [
        f"KLGA1M Push vs. Polymarket NYC | Lookback {args.days} Tage | "
        f"{len(rows)} Beobachtungen | "
        f"{rows[0]['observed_at_utc']:%Y-%m-%d %H:%M}Z – "
        f"{rows[-1]['observed_at_utc']:%Y-%m-%d %H:%M}Z",
        "",
    ]

    for day in days:
        day_rows = [r for r in rows if r["local_date"] == day]
        markets = fetch_event_markets(day)
        summary_lines.append(summarize_day(day, day_rows, markets))
        summary_lines.append("")
        chart = plot_day(
            day,
            day_rows,
            markets,
            out_dir / f"klga1m_vs_polymarket_{day.isoformat()}.png",
        )
        if chart:
            print(f"Chart {day}: {chart}")

    summary_path = out_dir / "klga1m_push_vs_polymarket_summary.txt"
    summary_path.write_text("\n".join(summary_lines).rstrip() + "\n", encoding="utf-8")
    print(f"Summary: {summary_path}")
    print()
    print(summary_path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
