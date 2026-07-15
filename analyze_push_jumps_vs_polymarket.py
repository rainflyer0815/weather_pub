#!/usr/bin/env python3
"""Push-Sprünge (KLGA1M) vs. Polymarket-Kursänderungen – Event-Studie.

Erkennt:
  1. Neue ganzzahlige Running-Maxima (°F, WU/Polymarket-Rundung)
  2. Abrupte Temperatur-Sprünge (Δ°F über kurzes Fenster)

Misst danach je Event die Yes-Preis-Δ der Top-Buckets in einem
Zeitfenster vor/nach dem Push-Empfang (received_at).

Nutzung:
  python3 analyze_push_jumps_vs_polymarket.py \\
      --csv /opt/cursor/artifacts/klga1m_push_last_days.csv \\
      --out-dir /opt/cursor/artifacts
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

USER_AGENT = "weather/1.0 (push jump vs polymarket)"
NY = ZoneInfo("America/New_York")
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


def fetch_json(url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def polymarket_slug(target: date) -> str:
    return (
        f"highest-temperature-in-nyc-on-{MONTH_NAMES[target.month]}-"
        f"{target.day}-{target.year}"
    )


def round_f_wu(fahrenheit: float) -> int:
    return int(math.floor(fahrenheit + 0.5))


@dataclass
class PushRow:
    observed_at: datetime
    received_at: datetime
    temp_f: float
    temp_f_round: int
    running_max_f: float
    running_max_f_round: int
    local_date: date


@dataclass
class JumpEvent:
    kind: str  # new_max | temp_jump
    day: date
    observed_at: datetime
    received_at: datetime
    temp_f: float
    delta_f: float
    running_max_f_round: int
    note: str


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def load_push_csv(path: Path) -> list[PushRow]:
    rows: list[PushRow] = []
    with path.open(encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            observed = parse_iso(raw["observed_at_ny"]).astimezone(NY)
            received_raw = raw.get("received_at_ny") or ""
            if received_raw:
                received = parse_iso(received_raw).astimezone(NY)
            else:
                # Fallback: received_at_utc → NY, oder observed + lag
                if raw.get("received_at_utc"):
                    received = parse_iso(raw["received_at_utc"]).astimezone(NY)
                else:
                    lag = float(raw.get("lag_min") or 0)
                    received = observed + timedelta(minutes=lag)
            rows.append(
                PushRow(
                    observed_at=observed,
                    received_at=received,
                    temp_f=float(raw["temp_f"]),
                    temp_f_round=int(float(raw["temp_f_round"])),
                    running_max_f=float(raw["running_max_f"]),
                    running_max_f_round=int(float(raw["running_max_f_round"])),
                    local_date=date.fromisoformat(raw["local_date"]),
                )
            )
    rows.sort(key=lambda row: row.received_at)
    return rows


def detect_new_max_events(rows: list[PushRow]) -> list[JumpEvent]:
    events: list[JumpEvent] = []
    prev_max: dict[date, int] = {}
    for row in rows:
        prev = prev_max.get(row.local_date)
        curr = row.running_max_f_round
        if prev is None:
            prev_max[row.local_date] = curr
            # Erster Wert eines Tages zählt nur, wenn sinnvoll (nicht Stream-Start-Artefakt)
            continue
        if curr > prev:
            delta = curr - prev
            events.append(
                JumpEvent(
                    kind="new_max",
                    day=row.local_date,
                    observed_at=row.observed_at,
                    received_at=row.received_at,
                    temp_f=row.temp_f,
                    delta_f=float(delta),
                    running_max_f_round=curr,
                    note=f"Running-Max {prev}→{curr}°F",
                )
            )
            prev_max[row.local_date] = curr
    return events


def detect_temp_jumps(
    rows: list[PushRow],
    min_delta_f: float = 1.8,
    window_minutes: int = 5,
) -> list[JumpEvent]:
    """Abrupte Anstiege: temp jetzt minus Minimum der letzten window_minutes ≥ min_delta_f."""
    events: list[JumpEvent] = []
    for index, row in enumerate(rows):
        window_start = row.received_at - timedelta(minutes=window_minutes)
        baseline = None
        for earlier in reversed(rows[:index]):
            if earlier.local_date != row.local_date:
                break
            if earlier.received_at < window_start:
                break
            baseline = (
                earlier.temp_f
                if baseline is None
                else min(baseline, earlier.temp_f)
            )
        if baseline is None:
            continue
        delta = row.temp_f - baseline
        if delta < min_delta_f:
            continue
        # Dedup: nur lokal neues Hoch im 10-Min-Umfeld behalten
        if events and events[-1].day == row.local_date:
            if (row.received_at - events[-1].received_at) <= timedelta(minutes=10):
                if delta <= events[-1].delta_f:
                    continue
                events[-1] = JumpEvent(
                    kind="temp_jump",
                    day=row.local_date,
                    observed_at=row.observed_at,
                    received_at=row.received_at,
                    temp_f=row.temp_f,
                    delta_f=delta,
                    running_max_f_round=row.running_max_f_round,
                    note=f"+{delta:.1f}°F in ≤{window_minutes} Min (von {baseline:.1f})",
                )
                continue
        events.append(
            JumpEvent(
                kind="temp_jump",
                day=row.local_date,
                observed_at=row.observed_at,
                received_at=row.received_at,
                temp_f=row.temp_f,
                delta_f=delta,
                running_max_f_round=row.running_max_f_round,
                note=f"+{delta:.1f}°F in ≤{window_minutes} Min (von {baseline:.1f})",
            )
        )
    return events


def fetch_markets(day: date) -> list[dict]:
    slug = polymarket_slug(day)
    events = fetch_json(f"{GAMMA_API_URL}?{urllib.parse.urlencode({'slug': slug})}")
    if not events:
        short = f"highest-temperature-in-nyc-on-{MONTH_NAMES[day.month]}-{day.day}"
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
                "label": market.get("groupItemTitle") or "?",
                "yes_now": float(prices[0]) * 100.0,
                "token": tokens[0],
            }
        )
    markets.sort(key=lambda item: item["yes_now"], reverse=True)
    return markets


def fetch_history(
    token_id: str, start: datetime, end: datetime, fidelity: int = 1
) -> list[tuple[datetime, float]]:
    query = urllib.parse.urlencode(
        {
            "market": token_id,
            "startTs": int(start.timestamp()),
            "endTs": int(end.timestamp()),
            "fidelity": fidelity,
        }
    )
    payload = fetch_json(f"{CLOB_HISTORY_URL}?{query}")
    history = payload.get("history", []) if isinstance(payload, dict) else []
    return [
        (datetime.fromtimestamp(point["t"], NY), point["p"] * 100.0)
        for point in history
    ]


def price_at(
    series: list[tuple[datetime, float]], moment: datetime
) -> float | None:
    """Letzter bekannter Preis ≤ moment; sonst None."""
    last = None
    for time, price in series:
        if time <= moment:
            last = price
        else:
            break
    return last


def max_move_after(
    series: list[tuple[datetime, float]],
    t0: datetime,
    horizon: timedelta,
) -> tuple[float | None, datetime | None, float | None]:
    """Größte absolute Preisänderung nach t0 innerhalb horizon, relativ zu Preis@t0."""
    base = price_at(series, t0)
    if base is None:
        return None, None, None
    end = t0 + horizon
    best_delta = 0.0
    best_time = None
    best_price = base
    for time, price in series:
        if time <= t0:
            continue
        if time > end:
            break
        delta = price - base
        if abs(delta) >= abs(best_delta):
            best_delta = delta
            best_time = time
            best_price = price
    if best_time is None:
        # kein Trade im Horizont → Δ=0 am Ende
        return 0.0, None, base
    return best_delta, best_time, best_price


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


def analyze_events(
    events: list[JumpEvent],
    markets_by_day: dict[date, list[dict]],
    histories: dict[tuple[date, str], list[tuple[datetime, float]]],
    pre_min: int,
    post_min: int,
) -> list[dict]:
    results = []
    pre = timedelta(minutes=pre_min)
    post = timedelta(minutes=post_min)
    for event in events:
        markets = markets_by_day.get(event.day) or []
        if not markets:
            continue
        # Fokusbuckets: Max-Band, Band darüber/darunter, plus Live-Top-3
        focus_labels: list[str] = []
        labels = [m["label"] for m in markets]

        def add(label: str) -> None:
            if label and label not in focus_labels:
                focus_labels.append(label)

        for market in markets:
            if band_contains(market["label"], event.running_max_f_round):
                add(market["label"])
        # Nachbarbänder relativ zum Running-Max
        for market in markets:
            label = market["label"]
            if band_contains(label, event.running_max_f_round + 2) or band_contains(
                label, event.running_max_f_round - 2
            ):
                add(label)
        for market in markets[:3]:
            add(market["label"])
        # Falls immer noch wenig: Top5
        for market in markets[:5]:
            add(market["label"])
        focus_labels = focus_labels[:6]

        row: dict = {
            "day": event.day.isoformat(),
            "kind": event.kind,
            "observed_et": event.observed_at.strftime("%H:%M"),
            "received_et": event.received_at.strftime("%H:%M:%S"),
            "temp_f": round(event.temp_f, 2),
            "delta_f": round(event.delta_f, 2),
            "running_max_round": event.running_max_f_round,
            "note": event.note,
        }
        t0 = event.received_at
        for label in focus_labels:
            series = histories.get((event.day, label), [])
            before = price_at(series, t0 - pre)
            at = price_at(series, t0)
            after_delta, after_time, after_price = max_move_after(series, t0, post)
            # auch Preis am Ende des Post-Fensters
            end_price = price_at(series, t0 + post)
            pre_delta = None if before is None or at is None else at - before
            post_delta = None if at is None or end_price is None else end_price - at
            lag_min = None
            if after_time is not None:
                lag_min = (after_time - t0).total_seconds() / 60.0
            safe = label.replace("°", "").replace(" ", "_").replace("-", "_")
            row[f"{safe}_pre{pre_min}"] = None if before is None else round(before, 2)
            row[f"{safe}_at"] = None if at is None else round(at, 2)
            row[f"{safe}_post{post_min}"] = (
                None if end_price is None else round(end_price, 2)
            )
            row[f"{safe}_d_pre"] = None if pre_delta is None else round(pre_delta, 2)
            row[f"{safe}_d_post"] = None if post_delta is None else round(post_delta, 2)
            row[f"{safe}_maxmove"] = (
                None if after_delta is None else round(after_delta, 2)
            )
            row[f"{safe}_maxmove_lag_min"] = (
                None if lag_min is None else round(lag_min, 1)
            )
            row[f"{safe}_is_maxband"] = band_contains(label, event.running_max_f_round)
        results.append(row)
    return results


def write_event_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # stabile Spaltenreihenfolge: Meta zuerst, dann dynamische Keys
    meta = [
        "day",
        "kind",
        "observed_et",
        "received_et",
        "temp_f",
        "delta_f",
        "running_max_round",
        "note",
    ]
    extras = []
    for row in rows:
        for key in row:
            if key not in meta and key not in extras:
                extras.append(key)
    fieldnames = meta + extras
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def summarize(rows: list[dict], pre_min: int, post_min: int) -> str:
    lines = [
        "Push-Sprünge vs. Polymarket-Kursänderung",
        f"Fenster: −{pre_min} Min vor Empfang / +{post_min} Min danach",
        "",
    ]
    if not rows:
        return "Keine Events."

    # Pro Tag nur new_max Events hervorheben
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        by_day.setdefault(row["day"], []).append(row)

    for day, day_rows in by_day.items():
        lines.append(f"=== {day} ===")
        new_max = [r for r in day_rows if r["kind"] == "new_max"]
        jumps = [r for r in day_rows if r["kind"] == "temp_jump"]
        lines.append(f"new_max Events: {len(new_max)} | temp_jump Events: {len(jumps)}")

        for row in new_max:
            lines.append(
                f"  NEW MAX {row['running_max_round']}°F @ obs {row['observed_et']} / "
                f"recv {row['received_et']} ({row['note']})"
            )
            # finde Band-Spalten
            band_keys = [k for k in row if k.endswith("_is_maxband") and row[k]]
            if band_keys:
                prefix = band_keys[0][: -len("_is_maxband")]
                at = row.get(f"{prefix}_at")
                d_pre = row.get(f"{prefix}_d_pre")
                d_post = row.get(f"{prefix}_d_post")
                maxmove = row.get(f"{prefix}_maxmove")
                lag = row.get(f"{prefix}_maxmove_lag_min")
                lines.append(
                    f"    Max-Band-Quote: at={at}% | Δpre({pre_min}m)={d_pre}pp | "
                    f"Δpost({post_min}m)={d_post}pp | maxMove={maxmove}pp @ +{lag}m"
                )
            # größte post-Bewegung irgend eines Bucket
            move_items = []
            for key, value in row.items():
                if key.endswith("_d_post") and value is not None:
                    label = key[: -len(f"_d_post")]
                    move_items.append((abs(value), value, label))
            move_items.sort(reverse=True)
            if move_items:
                _, value, label = move_items[0]
                lines.append(
                    f"    stärkste Δpost: {label.replace('_', '-')} = {value:+.2f} pp"
                )
        lines.append("")

    # Aggregat: new_max → Reaktion des Max-Bands
    post_moves = []
    pre_moves = []
    lags = []
    for row in rows:
        if row["kind"] != "new_max":
            continue
        for key, value in row.items():
            if key.endswith("_is_maxband") and value:
                prefix = key[: -len("_is_maxband")]
                d_post = row.get(f"{prefix}_d_post")
                d_pre = row.get(f"{prefix}_d_pre")
                lag = row.get(f"{prefix}_maxmove_lag_min")
                if d_post is not None:
                    post_moves.append(d_post)
                if d_pre is not None:
                    pre_moves.append(d_pre)
                if lag is not None:
                    lags.append(lag)
    lines.append("=== Aggregat (alle new_max → jeweiliges Max-Band) ===")
    if post_moves:
        lines.append(
            f"Δ Yes-Preis nach Push-Empfang ({post_min} Min): "
            f"n={len(post_moves)} median={statistics.median(post_moves):+.2f} pp "
            f"mean={statistics.mean(post_moves):+.2f} pp "
            f"min={min(post_moves):+.2f} max={max(post_moves):+.2f}"
        )
    if pre_moves:
        lines.append(
            f"Δ Yes-Preis vor Push-Empfang ({pre_min} Min): "
            f"n={len(pre_moves)} median={statistics.median(pre_moves):+.2f} pp "
            f"mean={statistics.mean(pre_moves):+.2f} pp"
        )
    if lags:
        lines.append(
            f"Lag bis max |Move| im Post-Fenster: "
            f"median={statistics.median(lags):.1f} Min mean={statistics.mean(lags):.1f} Min"
        )
    # Antecedence: |Δpre| vs |Δpost|
    paired = []
    for row in rows:
        if row["kind"] != "new_max":
            continue
        for key, value in row.items():
            if key.endswith("_is_maxband") and value:
                prefix = key[: -len("_is_maxband")]
                d_pre = row.get(f"{prefix}_d_pre")
                d_post = row.get(f"{prefix}_d_post")
                if d_pre is not None and d_post is not None:
                    paired.append((abs(d_pre), abs(d_post)))
    if paired:
        market_first = sum(1 for pre, post in paired if pre > post + 0.5)
        push_first = sum(1 for pre, post in paired if post > pre + 0.5)
        tied = len(paired) - market_first - push_first
        lines.append(
            f"Wer bewegt sich zuerst? ( |Δpre| vs |Δpost|, Tol 0.5pp ): "
            f"Markt schon vorher {market_first} | Push dann Markt {push_first} | unklar {tied}"
        )
    lines.append("")
    return "\n".join(lines)


def plot_event_study(
    rows: list[dict],
    histories: dict[tuple[date, str], list[tuple[datetime, float]]],
    push_rows: list[PushRow],
    out_dir: Path,
    post_min: int,
) -> list[Path]:
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    paths = []
    # Nur new_max Events mit Δ≥1°F (ganze Grad)
    focus = [r for r in rows if r["kind"] == "new_max" and r["delta_f"] >= 1]
    # Pro Tag ein Panel: Push temp + Top-Band quotes, Events markiert
    days = sorted({date.fromisoformat(r["day"]) for r in focus})
    for day in days:
        day_push = [r for r in push_rows if r.local_date == day]
        day_events = [r for r in focus if r["day"] == day.isoformat()]
        if not day_push or not day_events:
            continue

        fig, axes = plt.subplots(
            2, 1, figsize=(13, 8), sharex=True, gridspec_kw={"height_ratios": [1.2, 1]}
        )
        ax_t, ax_p = axes

        ax_t.plot(
            [r.received_at for r in day_push],
            [r.temp_f for r in day_push],
            color="#1b9e77",
            linewidth=1.2,
            label="KLGA1M Push °F",
        )
        ax_t.plot(
            [r.received_at for r in day_push],
            [r.running_max_f for r in day_push],
            color="#d95f02",
            linewidth=1.4,
            linestyle="--",
            label="Running Max °F",
        )
        for event in day_events:
            t0 = datetime.strptime(
                f"{event['day']} {event['received_et']}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=NY)
            ax_t.axvline(t0, color="#7570b3", alpha=0.45, linewidth=1)
            ax_t.annotate(
                f"{int(event['running_max_round'])}°F",
                xy=(t0, event["temp_f"]),
                xytext=(0, 8),
                textcoords="offset points",
                ha="center",
                fontsize=8,
                color="#7570b3",
            )

        ax_t.set_ylabel("°F")
        ax_t.set_title(
            f"Push-Sprünge (new Max) vs. Polymarket – {day.strftime('%d.%m.%Y')}",
            fontsize=13,
            fontweight="bold",
        )
        ax_t.grid(True, alpha=0.3)
        ax_t.legend(loc="upper left")

        # Buckets: nach Peak-Yes-Preis im Tagesverlauf, Favoriten zuerst
        day_labels = [label for (d, label) in histories if d == day]
        ranked = []
        for label in day_labels:
            series = histories.get((day, label), [])
            if not series:
                continue
            ranked.append((max(p for _, p in series), label))
        ranked.sort(reverse=True)
        colors = ["#7570b3", "#e7298a", "#66a61e", "#e6ab02", "#a6761d"]
        for plotted, (_, label) in enumerate(ranked[:4]):
            series = histories[(day, label)]
            ax_p.plot(
                [t for t, _ in series],
                [p for _, p in series],
                linewidth=1.6,
                label=label,
                color=colors[plotted % len(colors)],
            )

        for event in day_events:
            t0 = datetime.strptime(
                f"{event['day']} {event['received_et']}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=NY)
            ax_p.axvline(t0, color="#7570b3", alpha=0.45, linewidth=1)
            ax_p.axvspan(
                t0,
                t0 + timedelta(minutes=post_min),
                color="#7570b3",
                alpha=0.08,
            )

        ax_p.set_ylabel("Yes-Preis (%)")
        ax_p.set_ylim(0, 100)
        ax_p.set_xlabel("America/New_York")
        ax_p.grid(True, alpha=0.3)
        ax_p.legend(loc="upper left", fontsize=8)
        ax_p.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=NY))
        fig.autofmt_xdate()
        plt.tight_layout()
        out = out_dir / f"push_jumps_vs_pm_{day.isoformat()}.png"
        fig.savefig(out, dpi=150, bbox_inches="tight")
        plt.close(fig)
        paths.append(out)

    # Response chart: scatter ΔF vs Δpp for new_max
    xs, ys, labels = [], [], []
    for row in rows:
        if row["kind"] != "new_max":
            continue
        for key, value in row.items():
            if key.endswith("_is_maxband") and value:
                prefix = key[: -len("_is_maxband")]
                d_post = row.get(f"{prefix}_d_post")
                if d_post is None:
                    continue
                xs.append(row["delta_f"])
                ys.append(d_post)
                labels.append(f"{row['day'][5:]} {row['observed_et']}")

    if xs:
        fig, ax = plt.subplots(figsize=(8, 5.5))
        ax.scatter(xs, ys, color="#1b9e77", s=55, zorder=3)
        for x, y, lab in zip(xs, ys, labels):
            ax.annotate(lab, (x, y), textcoords="offset points", xytext=(4, 4), fontsize=7)
        ax.axhline(0, color="#888", linewidth=1)
        ax.axvline(0, color="#888", linewidth=1)
        ax.set_xlabel("Push Running-Max Sprung (Δ°F ganzzahlig)")
        ax.set_ylabel(f"Δ Yes-Preis Max-Band in +{post_min} Min (pp)")
        ax.set_title(
            "Event-Studie: Größe des Push-Max-Sprungs vs. Marktreaktion",
            fontsize=12,
            fontweight="bold",
        )
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        out = out_dir / "push_jump_size_vs_pm_response.png"
        fig.savefig(out, dpi=150, bbox_inches="tight")
        plt.close(fig)
        paths.append(out)

    return paths


def write_decisive_report(
    results: list[dict],
    histories: dict[tuple[date, str], list[tuple[datetime, float]]],
    path: Path,
    post_min: int,
) -> str:
    """Nur Sprünge ≥90°F bzw. in/knapp unter dem spätesten Max – mit Minutenpfad."""
    lines = [
        "Entscheidende Push-Sprünge (Running-Max ≥ 90°F) – Minutenpfad der Top-Buckets",
        "",
    ]
    focus_rows = [
        r
        for r in results
        if r["kind"] == "new_max" and int(r["running_max_round"]) >= 90
    ]
    for row in focus_rows:
        day = date.fromisoformat(row["day"])
        t0 = datetime.strptime(
            f"{row['day']} {row['received_et']}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=NY)
        lines.append(
            f"## {row['day']}  {row['note']}  obs {row['observed_et']} ET / "
            f"empfangen {row['received_et']} ET"
        )

        # Top moved buckets by |d_post|
        moves = []
        for key, value in row.items():
            if not key.endswith("_d_post") or value is None:
                continue
            label_key = key[: -len("_d_post")]
            at = row.get(f"{label_key}_at")
            d_pre = row.get(f"{label_key}_d_pre")
            moves.append((abs(float(value)), float(value), label_key, at, d_pre))
        moves.sort(reverse=True)
        lines.append("  Bucket-Reaktionen (Δ in pp):")
        for _, d_post, label_key, at, d_pre in moves[:4]:
            pretty = label_key.replace("_", "-")
            lines.append(
                f"    {pretty:18s}  at={at}%  Δpre10={d_pre:+}pp  "
                f"Δpost{post_min}={d_post:+.2f}pp"
            )

        # Minutenpfad für die Top-2 bewegten Labels
        for _, _, label_key, _, _ in moves[:2]:
            # Rekonstruiere Label aus histories keys
            match = None
            for (d, label), series in histories.items():
                if d != day:
                    continue
                safe = label.replace("°", "").replace(" ", "_").replace("-", "_")
                if safe == label_key:
                    match = (label, series)
                    break
            if not match:
                continue
            label, series = match
            lines.append(f"  Minutenpfad {label} um Empfang:")
            for minute in range(-5, post_min + 1, 1):
                moment = t0 + timedelta(minutes=minute)
                price = price_at(series, moment)
                if price is None:
                    continue
                marker = " ← recv" if minute == 0 else ""
                lines.append(f"    t{minute:+03d}m  {moment:%H:%M}  {price:6.2f}%{marker}")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--pre-min", type=int, default=10)
    parser.add_argument("--post-min", type=int, default=20)
    parser.add_argument("--jump-delta-f", type=float, default=1.8)
    parser.add_argument("--jump-window-min", type=int, default=5)
    args = parser.parse_args()

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    push_rows = load_push_csv(args.csv)
    if not push_rows:
        print("Keine Push-Zeilen.", flush=True)
        return 1

    new_max = detect_new_max_events(push_rows)
    temp_jumps = detect_temp_jumps(
        push_rows, min_delta_f=args.jump_delta_f, window_minutes=args.jump_window_min
    )
    # Für die Event-Studie: new_max priorisieren; temp_jumps als Ergänzung
    events = new_max + temp_jumps
    events.sort(key=lambda e: e.received_at)

    days = sorted({row.local_date for row in push_rows})
    markets_by_day: dict[date, list[dict]] = {}
    histories: dict[tuple[date, str], list[tuple[datetime, float]]] = {}
    for day in days:
        markets = fetch_markets(day)
        markets_by_day[day] = markets
        day_start = datetime.combine(day, datetime.min.time(), tzinfo=NY)
        day_end = min(
            datetime.now(NY),
            day_start + timedelta(days=1),
        )
        # alle Märkte mit jemals relevantem Preis laden (Top 6 + alle die >1% jetzt)
        # Alle Buckets laden (kleine Märkte, aber für Zwischen-Maxima nötig)
        for market in markets:
            series = fetch_history(
                market["token"],
                day_start - timedelta(minutes=args.pre_min),
                day_end + timedelta(minutes=5),
                fidelity=1,
            )
            histories[(day, market["label"])] = series

    results = analyze_events(
        events, markets_by_day, histories, args.pre_min, args.post_min
    )
    # Nur Events behalten, die wirklich Infos haben; new_max immer, temp_jump wenn |Δpost| groß
    csv_path = out_dir / "push_jumps_vs_pm_events.csv"
    write_event_csv(csv_path, results)

    summary = summarize(results, args.pre_min, args.post_min)
    summary_path = out_dir / "push_jumps_vs_pm_summary.txt"
    summary_path.write_text(summary, encoding="utf-8")

    decisive = write_decisive_report(
        results, histories, out_dir / "push_jumps_decisive_minute_paths.txt", args.post_min
    )

    charts = plot_event_study(
        results, histories, push_rows, out_dir, args.post_min
    )

    print(summary)
    print(decisive)
    print(f"CSV: {csv_path}")
    print(f"Summary: {summary_path}")
    for chart in charts:
        print(f"Chart: {chart}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
