#!/usr/bin/env python3
"""Visualisiert Push→Polymarket-Reaktion auf Sekunden-/Trade-Ebene.

Nutzt data-api.polymarket.com/trades (echte Trade-Timestamps) und stellt sie
dem Push-Empfangszeitpunkt (received_at, ms) gegenüber.

Nutzung:
  python3 visualize_push_market_seconds.py \\
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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

USER_AGENT = "weather/1.0 (push market seconds)"
NY = ZoneInfo("America/New_York")
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
TRADES_URL = "https://data-api.polymarket.com/trades"
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


def fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read().decode("utf-8"))


def polymarket_slug(target: date) -> str:
    return (
        f"highest-temperature-in-nyc-on-{MONTH_NAMES[target.month]}-"
        f"{target.day}-{target.year}"
    )


def round_f(fahrenheit: float) -> int:
    return int(math.floor(fahrenheit + 0.5))


@dataclass
class PushRow:
    observed_at: datetime
    received_at: datetime
    temp_f: float
    running_max_round: int
    local_date: date


@dataclass
class JumpEvent:
    day: date
    observed_at: datetime
    received_at: datetime
    temp_f: float
    from_max: int
    to_max: int


@dataclass
class Trade:
    time: datetime
    price_pct: float
    size: float
    side: str
    label: str
    outcome: str


def load_push(path: Path) -> list[PushRow]:
    rows: list[PushRow] = []
    with path.open(encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            observed = datetime.fromisoformat(raw["observed_at_ny"]).astimezone(NY)
            if raw.get("received_at_ny"):
                received = datetime.fromisoformat(raw["received_at_ny"]).astimezone(NY)
            else:
                received = datetime.fromisoformat(raw["received_at_utc"]).astimezone(NY)
            rows.append(
                PushRow(
                    observed_at=observed,
                    received_at=received,
                    temp_f=float(raw["temp_f"]),
                    running_max_round=int(float(raw["running_max_f_round"])),
                    local_date=date.fromisoformat(raw["local_date"]),
                )
            )
    rows.sort(key=lambda row: row.received_at)
    return rows


def detect_new_max(rows: list[PushRow], min_max: int = 90) -> list[JumpEvent]:
    events: list[JumpEvent] = []
    prev: dict[date, int] = {}
    for row in rows:
        last = prev.get(row.local_date)
        curr = row.running_max_round
        if last is None:
            prev[row.local_date] = curr
            continue
        if curr > last:
            if curr >= min_max:
                events.append(
                    JumpEvent(
                        day=row.local_date,
                        observed_at=row.observed_at,
                        received_at=row.received_at,
                        temp_f=row.temp_f,
                        from_max=last,
                        to_max=curr,
                    )
                )
            prev[row.local_date] = curr
    return events


def fetch_event_markets(day: date) -> tuple[int | None, list[dict]]:
    slug = polymarket_slug(day)
    events = fetch_json(f"{GAMMA_API_URL}?{urllib.parse.urlencode({'slug': slug})}")
    if not events:
        short = f"highest-temperature-in-nyc-on-{MONTH_NAMES[day.month]}-{day.day}"
        events = fetch_json(
            f"{GAMMA_API_URL}?{urllib.parse.urlencode({'slug': short})}"
        )
    if not events:
        return None, []
    event = events[0]
    markets = []
    for market in event.get("markets", []):
        tokens = json.loads(market.get("clobTokenIds", "[]"))
        prices = json.loads(market.get("outcomePrices", "[0,1]"))
        if not tokens:
            continue
        markets.append(
            {
                "label": market.get("groupItemTitle") or "?",
                "condition_id": market["conditionId"],
                "yes_token": tokens[0],
                "yes_now": float(prices[0]) * 100.0,
            }
        )
    markets.sort(key=lambda item: item["yes_now"], reverse=True)
    return int(event["id"]), markets


def band_contains(label: str, whole_f: int) -> bool:
    import re

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


def select_labels(markets: list[dict], to_max: int) -> list[str]:
    labels: list[str] = []

    def add(label: str) -> None:
        if label not in labels:
            labels.append(label)

    for market in markets:
        if band_contains(market["label"], to_max):
            add(market["label"])
    for market in markets:
        if band_contains(market["label"], to_max + 2) or band_contains(
            market["label"], to_max - 2
        ):
            add(market["label"])
    for market in markets[:3]:
        add(market["label"])
    return labels[:4]


def fetch_trades_window(
    condition_id: str,
    label: str,
    yes_token: str,
    start: datetime,
    end: datetime,
) -> list[Trade]:
    """Alle Yes-Trades im Fenster, paginiert."""
    collected: list[Trade] = []
    offset = 0
    limit = 500
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    while offset <= 9500:
        params = {
            "market": condition_id,
            "limit": limit,
            "offset": offset,
            "takerOnly": "false",
            "start": start_ts,
            "end": end_ts,
        }
        payload = fetch_json(f"{TRADES_URL}?{urllib.parse.urlencode(params)}")
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            # Nur Yes-Token (erster Outcome)
            if item.get("asset") != yes_token:
                continue
            ts = int(item["timestamp"])
            if ts < start_ts or ts > end_ts:
                continue
            collected.append(
                Trade(
                    time=datetime.fromtimestamp(ts, NY),
                    price_pct=float(item["price"]) * 100.0,
                    size=float(item["size"]),
                    side=str(item.get("side") or ""),
                    label=label,
                    outcome=str(item.get("outcome") or "Yes"),
                )
            )
        if len(payload) < limit:
            break
        offset += limit
        # API offset max 10000
        if offset >= 10000:
            break

    collected.sort(key=lambda trade: trade.time)
    return collected


def last_price_series(trades: list[Trade]) -> list[tuple[datetime, float]]:
    """Sekundenraster: letzter Trade-Preis je Sekunde (step-hold)."""
    if not trades:
        return []
    by_second: dict[datetime, Trade] = {}
    for trade in trades:
        second = trade.time.replace(microsecond=0)
        by_second[second] = trade  # letzter in derselben Sekunde gewinnt
    times = sorted(by_second)
    return [(t, by_second[t].price_pct) for t in times]


def plot_event(
    event: JumpEvent,
    push_rows: list[PushRow],
    series_by_label: dict[str, list[Trade]],
    out_path: Path,
    pre_sec: int,
    post_sec: int,
) -> Path:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    t0 = event.received_at
    window_start = t0 - timedelta(seconds=pre_sec)
    window_end = t0 + timedelta(seconds=post_sec)

    fig, (ax_t, ax_p) = plt.subplots(
        2,
        1,
        figsize=(13, 8),
        sharex=True,
        gridspec_kw={"height_ratios": [1.0, 1.35]},
    )

    # Push temps in window (1-min points, but plotted with exact received_at)
    day_push = [
        row
        for row in push_rows
        if row.local_date == event.day
        and window_start - timedelta(minutes=2)
        <= row.received_at
        <= window_end + timedelta(minutes=2)
    ]
    if day_push:
        ax_t.step(
            [row.received_at for row in day_push],
            [row.temp_f for row in day_push],
            where="post",
            color="#1b9e77",
            linewidth=1.8,
            label="KLGA1M Push °F (empfangen)",
        )
        ax_t.scatter(
            [row.received_at for row in day_push],
            [row.temp_f for row in day_push],
            color="#1b9e77",
            s=28,
            zorder=4,
        )

    ax_t.axvline(event.observed_at, color="#a6761d", linestyle=":", linewidth=1.4, label="obs")
    ax_t.axvline(t0, color="#7570b3", linestyle="-", linewidth=1.6, label="recv")
    ax_t.axvspan(event.observed_at, t0, color="#a6761d", alpha=0.10, label="obs→recv gap")
    ax_t.set_ylabel("°F")
    ax_t.set_title(
        f"Push→Markt Sekunden-Zoom  {event.day:%d.%m.%Y}  "
        f"Max {event.from_max}→{event.to_max}°F\n"
        f"obs {event.observed_at:%H:%M:%S} ET · recv {event.received_at:%H:%M:%S.%f} ET",
        fontsize=12,
        fontweight="bold",
    )
    ax_t.grid(True, alpha=0.3)
    ax_t.legend(loc="upper left", fontsize=8)

    colors = ["#7570b3", "#e7298a", "#66a61e", "#e6ab02"]
    for index, (label, trades) in enumerate(series_by_label.items()):
        color = colors[index % len(colors)]
        window_trades = [t for t in trades if window_start <= t.time <= window_end]
        if not window_trades:
            continue
        # einzelne Trades als Punkte (sekundengenau)
        ax_p.scatter(
            [t.time for t in window_trades],
            [t.price_pct for t in window_trades],
            s=[max(10, min(80, t.size)) for t in window_trades],
            alpha=0.55,
            color=color,
            zorder=3,
            label=f"{label} trades (n={len(window_trades)})",
        )
        step = last_price_series(window_trades)
        if step:
            ax_p.step(
                [t for t, _ in step] + [window_end],
                [p for _, p in step] + [step[-1][1]],
                where="post",
                color=color,
                linewidth=1.5,
                alpha=0.95,
            )

    ax_p.axvline(event.observed_at, color="#a6761d", linestyle=":", linewidth=1.4)
    ax_p.axvline(t0, color="#7570b3", linestyle="-", linewidth=1.6)
    ax_p.axvspan(event.observed_at, t0, color="#a6761d", alpha=0.10)
    ax_p.axvspan(t0, min(window_end, t0 + timedelta(seconds=60)), color="#7570b3", alpha=0.08)
    ax_p.set_ylabel("Yes-Preis (%)")
    ax_p.set_ylim(0, 100)
    ax_p.set_xlabel("America/New_York (Sekunden)")
    ax_p.grid(True, alpha=0.3)
    ax_p.legend(loc="best", fontsize=8)
    ax_p.set_xlim(window_start, window_end)

    ax_p.xaxis.set_major_locator(mdates.SecondLocator(interval=max(15, pre_sec // 8)))
    ax_p.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S", tz=NY))
    fig.autofmt_xdate()
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out_path


def reaction_stats(
    event: JumpEvent, trades: list[Trade], horizons_sec: list[int]
) -> dict:
    """Preis unmittelbar vor recv vs. N Sekunden danach (letzter Trade ≤ t)."""
    t0 = event.received_at

    def price_at(moment: datetime) -> float | None:
        last = None
        for trade in trades:
            if trade.time <= moment:
                last = trade.price_pct
            else:
                break
        return last

    before = price_at(t0 - timedelta(milliseconds=1))
    stats = {
        "label": trades[0].label if trades else "?",
        "n_trades": len(trades),
        "price_before_recv": before,
    }
    for horizon in horizons_sec:
        after = price_at(t0 + timedelta(seconds=horizon))
        delta = None if before is None or after is None else after - before
        stats[f"price_plus_{horizon}s"] = after
        stats[f"delta_{horizon}s"] = delta
        # first trade after recv
    first_after = next((t for t in trades if t.time >= t0), None)
    if first_after is not None:
        stats["first_trade_lag_s"] = (first_after.time - t0).total_seconds()
        stats["first_trade_price"] = first_after.price_pct
        if before is not None:
            stats["first_trade_delta"] = first_after.price_pct - before
    return stats


def write_report(path: Path, blocks: list[str]) -> None:
    path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--pre-sec", type=int, default=180)
    parser.add_argument("--post-sec", type=int, default=300)
    parser.add_argument("--min-max", type=int, default=90)
    args = parser.parse_args()

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    push_rows = load_push(args.csv)
    events = detect_new_max(push_rows, min_max=args.min_max)
    if not events:
        print("Keine Running-Max Events ≥ Schwelle.", flush=True)
        return 1

    # Für Zoom: die „härtesten“ Events prioritieren (max to_max + große Sprünge)
    events_sorted = sorted(events, key=lambda e: (e.to_max, e.to_max - e.from_max), reverse=True)
    focus = []
    seen_days_peaks = set()
    for event in events_sorted:
        key = (event.day, event.to_max)
        if key in seen_days_peaks:
            continue
        # finales Tageshoch und letzter großer Sprung davor
        focus.append(event)
        seen_days_peaks.add(key)
        if len(focus) >= 6:
            break
    # Chronologisch für Report
    focus.sort(key=lambda e: e.received_at)

    report: list[str] = [
        "Push→Polymarket Reaktion auf Sekundenebene (Trade-Ticks)",
        f"Fenster: −{args.pre_sec}s / +{args.post_sec}s um Push-Empfang",
        "",
    ]
    chart_paths: list[Path] = []

    markets_cache: dict[date, tuple[int | None, list[dict]]] = {}

    for event in focus:
        if event.day not in markets_cache:
            markets_cache[event.day] = fetch_event_markets(event.day)
        _, markets = markets_cache[event.day]
        if not markets:
            continue
        labels = select_labels(markets, event.to_max)
        market_by_label = {m["label"]: m for m in markets}

        window_start = event.received_at - timedelta(seconds=args.pre_sec)
        window_end = event.received_at + timedelta(seconds=args.post_sec)

        series: dict[str, list[Trade]] = {}
        for label in labels:
            market = market_by_label.get(label)
            if not market:
                continue
            trades = fetch_trades_window(
                market["condition_id"],
                label,
                market["yes_token"],
                window_start - timedelta(seconds=30),
                window_end + timedelta(seconds=30),
            )
            series[label] = trades

        chart = plot_event(
            event,
            push_rows,
            series,
            out_dir
            / (
                f"push_pm_seconds_{event.day.isoformat()}_"
                f"{event.to_max}F_{event.received_at:%H%M%S}.png"
            ),
            args.pre_sec,
            args.post_sec,
        )
        chart_paths.append(chart)

        report.append(
            f"## {event.day}  Max {event.from_max}→{event.to_max}°F  "
            f"obs {event.observed_at:%H:%M:%S}  recv {event.received_at:%H:%M:%S.%f} ET"
        )
        for label, trades in series.items():
            window_trades = [
                t
                for t in trades
                if window_start <= t.time <= window_end
            ]
            stats = reaction_stats(event, window_trades, [1, 5, 15, 30, 60, 120])
            report.append(
                f"  {label}: {stats['n_trades']} Trades im Fenster | "
                f"vor recv={stats.get('price_before_recv')}% | "
                f"Δ1s={stats.get('delta_1s')} | Δ5s={stats.get('delta_5s')} | "
                f"Δ15s={stats.get('delta_15s')} | Δ30s={stats.get('delta_30s')} | "
                f"Δ60s={stats.get('delta_60s')} | Δ120s={stats.get('delta_120s')}"
            )
            if stats.get("first_trade_lag_s") is not None:
                lag = stats["first_trade_lag_s"]
                price = stats["first_trade_price"]
                delta = stats.get("first_trade_delta")
                if delta is None:
                    report.append(
                        f"    erster Trade nach recv: +{lag:.1f}s @ {price}%"
                    )
                else:
                    report.append(
                        f"    erster Trade nach recv: +{lag:.1f}s @ {price}% "
                        f"(Δ {delta:+.2f} pp)"
                    )
                # nächste Trades in der ersten Minute
                first_minute = [
                    t
                    for t in window_trades
                    if event.received_at
                    <= t.time
                    <= event.received_at + timedelta(seconds=60)
                ]
                if first_minute:
                    path = ", ".join(
                        f"{t.time:%H:%M:%S} {t.price_pct:.1f}%/{t.side[0]}{t.size:.0f}"
                        for t in first_minute[:12]
                    )
                    report.append(f"    Trades ≤60s: {path}")
        report.append(f"  Chart: {chart.name}")
        report.append("")

    summary_path = out_dir / "push_pm_seconds_summary.txt"
    write_report(summary_path, report)
    print("\n".join(report))
    print(f"Summary: {summary_path}")
    for chart in chart_paths:
        print(f"Chart: {chart}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
