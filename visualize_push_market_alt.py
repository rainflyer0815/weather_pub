#!/usr/bin/env python3
"""Alternative Visualisierungen: Push-Sprünge vs. Polymarket (nicht Dual-Timeseries).

1) Lead-Lag-Race: pro Event eine Spur obs → Markt-Burst → Push-recv
2) Aligned Ribbons: Yes-Preis relativ zu recv=0 (alle Events übereinander)
3) Trade-Heatmap: Trade-Intensität in 5s-Bins relativ zu recv

Nutzung:
  python3 visualize_push_market_alt.py \\
      --csv /opt/cursor/artifacts/klga1m_push_last_days.csv \\
      --out-dir /opt/cursor/artifacts
"""

from __future__ import annotations

import argparse
import math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from visualize_push_market_seconds import (
    NY,
    JumpEvent,
    Trade,
    detect_new_max,
    fetch_event_markets,
    fetch_trades_window,
    load_push,
    select_labels,
)


def biggest_burst(
    trades: list[Trade],
    window_start,
    window_end,
    min_move_pp: float = 8.0,
    cluster_sec: float = 8.0,
) -> tuple[object | None, float | None, float | None]:
    """Finde stärksten Preis-Burst (Startzeit, from%, to%) im Fenster."""
    in_win = [t for t in trades if window_start <= t.time <= window_end]
    if len(in_win) < 2:
        return None, None, None

    best = None  # (abs_delta, start_time, p0, p1)
    for i, trade in enumerate(in_win):
        cluster = [
            other
            for other in in_win[i:]
            if (other.time - trade.time).total_seconds() <= cluster_sec
        ]
        if not cluster:
            continue
        p0 = trade.price_pct
        p1 = cluster[-1].price_pct
        # auch Extrem im Cluster
        highs = max(c.price_pct for c in cluster)
        lows = min(c.price_pct for c in cluster)
        up = highs - p0
        down = p0 - lows
        if up >= down:
            delta, end_p = up, highs
        else:
            delta, end_p = -down, lows
        if abs(delta) < min_move_pp:
            continue
        score = abs(delta)
        if best is None or score > best[0]:
            best = (score, trade.time, p0, end_p)
    if best is None:
        return None, None, None
    return best[1], best[2], best[3]


def pick_focus_band(labels: list[str], to_max: int, markets: list[dict]) -> str:
    """Band das den neuen Max enthält, sonst Top-Live-Favorit."""
    import re

    def contains(label: str, whole: int) -> bool:
        lower = label.lower().replace("°f", "")
        digits = [int(x) for x in re.findall(r"\d+", lower)]
        if not digits:
            return False
        if "below" in lower:
            return whole <= digits[0]
        if "higher" in lower:
            return whole >= digits[0]
        if len(digits) >= 2:
            return digits[0] <= whole <= digits[1]
        return whole == digits[0]

    for label in labels:
        if contains(label, to_max):
            return label
    # sonst höchstes yes_now unter labels
    by = {m["label"]: m["yes_now"] for m in markets}
    return max(labels, key=lambda lab: by.get(lab, 0))


def collect_event_payloads(csv_path: Path, min_max: int = 90):
    push = load_push(csv_path)
    events = detect_new_max(push, min_max=min_max)
    # Fokus: je Tag die größeren Sprünge (≥2°F oder finales Hoch)
    by_day: dict = defaultdict(list)
    for event in events:
        by_day[event.day].append(event)

    focus: list[JumpEvent] = []
    for day, day_events in sorted(by_day.items()):
        day_events = sorted(day_events, key=lambda e: e.received_at)
        if not day_events:
            continue
        # letztes (höchstes) immer
        focus.append(day_events[-1])
        # plus große Zwischensprünge
        for event in day_events[:-1]:
            if event.to_max - event.from_max >= 2 and event.to_max >= 93:
                focus.append(event)
    # dedupe by (day, to_max, received_at)
    uniq: list[JumpEvent] = []
    seen = set()
    for event in sorted(focus, key=lambda e: e.received_at):
        key = (event.day, event.to_max, event.received_at.isoformat())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(event)
    focus = uniq

    markets_cache = {}
    payloads = []
    for event in focus:
        if event.day not in markets_cache:
            markets_cache[event.day] = fetch_event_markets(event.day)
        _, markets = markets_cache[event.day]
        if not markets:
            continue
        labels = select_labels(markets, event.to_max)
        by_label = {m["label"]: m for m in markets}
        start = event.observed_at - timedelta(seconds=90)
        end = event.received_at + timedelta(seconds=240)
        trades_by_label: dict[str, list[Trade]] = {}
        for label in labels:
            market = by_label.get(label)
            if not market:
                continue
            trades_by_label[label] = fetch_trades_window(
                market["condition_id"],
                label,
                market["yes_token"],
                start,
                end,
            )
        focus_label = pick_focus_band(labels, event.to_max, markets)
        focus_trades = trades_by_label.get(focus_label, [])
        burst_t, p0, p1 = biggest_burst(
            focus_trades,
            event.observed_at - timedelta(seconds=30),
            event.received_at + timedelta(seconds=180),
            min_move_pp=5.0,
            cluster_sec=12.0,
        )
        # Fallback: größter Burst über alle Labels
        if burst_t is None:
            for label, trades in trades_by_label.items():
                bt, bp0, bp1 = biggest_burst(
                    trades,
                    event.observed_at - timedelta(seconds=30),
                    event.received_at + timedelta(seconds=180),
                    min_move_pp=5.0,
                    cluster_sec=12.0,
                )
                if bt is not None:
                    burst_t, p0, p1 = bt, bp0, bp1
                    focus_label = label
                    focus_trades = trades
                    break

        payloads.append(
            {
                "event": event,
                "labels": labels,
                "trades_by_label": trades_by_label,
                "focus_label": focus_label,
                "focus_trades": focus_trades,
                "burst_t": burst_t,
                "burst_from": p0,
                "burst_to": p1,
            }
        )
    return payloads


def plot_lead_lag_race(payloads: list[dict], out_path: Path) -> Path:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12.5, 0.95 * max(3, len(payloads)) + 1.8))
    # Farben: sand / teal / rust — kein Lila-Klischee
    c_obs = "#8c6d46"
    c_burst = "#c44e52"
    c_recv = "#2a9d8f"
    c_track = "#e7e1d6"

    ylabels = []
    for index, payload in enumerate(payloads):
        event: JumpEvent = payload["event"]
        y = len(payloads) - 1 - index
        ylabels.append(
            f"{event.day:%m/%d}  {event.from_max}→{event.to_max}°F\n"
            f"{payload['focus_label']}"
        )

        obs = event.observed_at
        recv = event.received_at
        # x = Sekunden relativ zu obs
        recv_x = (recv - obs).total_seconds()
        burst = payload["burst_t"]
        burst_x = None if burst is None else (burst - obs).total_seconds()

        # Track von 0 bis max(recv, burst)+30
        track_end = max(recv_x, burst_x or 0) + 40
        ax.hlines(y, -20, track_end, colors=c_track, linewidth=10, alpha=0.85, zorder=1)

        # Marker
        ax.scatter([-0.0], [y], s=120, color=c_obs, zorder=5, marker="|", linewidths=3)
        ax.scatter([recv_x], [y], s=90, color=c_recv, zorder=5)
        if burst_x is not None:
            ax.scatter([burst_x], [y], s=110, color=c_burst, zorder=6, marker="D")
            # Verbindungssegment obs→burst→recv als „Race“
            ax.plot(
                [0, burst_x],
                [y, y],
                color=c_burst,
                linewidth=3.2,
                solid_capstyle="round",
                zorder=3,
                alpha=0.9,
            )
            lead = recv_x - burst_x
            ax.annotate(
                f"Markt {payload['burst_from']:.0f}→{payload['burst_to']:.0f}%\n"
                f"{'Push +' if lead >= 0 else 'Push '}{lead:+.0f}s",
                xy=(burst_x, y),
                xytext=(8, 10 if index % 2 == 0 else -18),
                textcoords="offset points",
                fontsize=8,
                color=c_burst,
            )
        ax.annotate(
            f"recv +{recv_x:.0f}s",
            xy=(recv_x, y),
            xytext=(6, -14 if index % 2 == 0 else 8),
            textcoords="offset points",
            fontsize=8,
            color=c_recv,
        )
        ax.annotate(
            "obs",
            xy=(0, y),
            xytext=(-18, 8),
            textcoords="offset points",
            fontsize=8,
            color=c_obs,
        )

    ax.set_yticks(range(len(payloads)))
    ax.set_yticklabels(list(reversed(ylabels)))
    ax.set_xlabel("Sekunden nach Beobachtung (obs = 0)")
    ax.set_title(
        "Lead–Lag Race: Wer kommt zuerst — Markt-Burst oder Push-Empfang?",
        fontsize=13,
        fontweight="bold",
        pad=12,
    )
    ax.set_xlim(-30, None)
    ax.axvline(0, color=c_obs, alpha=0.35, linewidth=1)
    # Legende manuell
    ax.scatter([], [], color=c_obs, marker="|", s=120, label="obs")
    ax.scatter([], [], color=c_burst, marker="D", s=80, label="Markt-Burst")
    ax.scatter([], [], color=c_recv, s=70, label="Push recv")
    ax.legend(loc="lower right", frameon=False)
    ax.grid(axis="x", alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_aligned_ribbons(payloads: list[dict], out_path: Path) -> Path:
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(12.5, 6.8))
    palette = ["#2a9d8f", "#c44e52", "#e9c46a", "#264653", "#f4a261", "#6d6875"]

    for index, payload in enumerate(payloads):
        event: JumpEvent = payload["event"]
        trades = payload["focus_trades"]
        if not trades:
            continue
        color = palette[index % len(palette)]
        # relativ zu recv
        xs, ys = [], []
        for trade in trades:
            rel = (trade.time - event.received_at).total_seconds()
            if -180 <= rel <= 240:
                xs.append(rel)
                ys.append(trade.price_pct)
        if not xs:
            continue
        # step-hold auf Sekunden
        ax.step(
            xs,
            ys,
            where="post",
            color=color,
            linewidth=1.7,
            alpha=0.9,
            label=f"{event.day:%m/%d} {event.to_max}°F · {payload['focus_label']}",
        )
        ax.scatter(xs, ys, s=14, color=color, alpha=0.45, zorder=3)

    ax.axvline(0, color="#264653", linewidth=1.6, label="Push recv = 0")
    ax.axvspan(-30, 0, color="#e9c46a", alpha=0.15)
    ax.axvspan(0, 60, color="#2a9d8f", alpha=0.08)
    ax.set_xlim(-180, 240)
    ax.set_ylim(0, 100)
    ax.set_xlabel("Sekunden relativ zum Push-Empfang (recv)")
    ax.set_ylabel("Yes-Preis Fokus-Band (%)")
    ax.set_title(
        "Aligned Ribbons — gleicher Moment, alle Events auf recv=0 gelegt",
        fontsize=13,
        fontweight="bold",
    )
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=1)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_trade_heatmap(payloads: list[dict], out_path: Path, bin_sec: int = 5) -> Path:
    import matplotlib.pyplot as plt
    import numpy as np

    # Nur Fokus-Band Trades; Zeilen = Events, Spalten = Zeitbins relativ recv
    pre, post = 120, 180
    edges = list(range(-pre, post + bin_sec, bin_sec))
    n_bins = len(edges) - 1
    matrix = np.zeros((len(payloads), n_bins))
    row_labels = []

    for row_i, payload in enumerate(payloads):
        event: JumpEvent = payload["event"]
        row_labels.append(f"{event.day:%m/%d} {event.from_max}→{event.to_max}")
        for trade in payload["focus_trades"]:
            rel = (trade.time - event.received_at).total_seconds()
            if rel < -pre or rel >= post:
                continue
            bin_i = int(math.floor((rel + pre) / bin_sec))
            bin_i = max(0, min(n_bins - 1, bin_i))
            matrix[row_i, bin_i] += trade.size

    fig, ax = plt.subplots(figsize=(13, 0.7 * max(3, len(payloads)) + 2.2))
    # log1p für Lesbarkeit
    data = np.log1p(matrix)
    im = ax.imshow(
        data,
        aspect="auto",
        cmap="YlOrRd",
        interpolation="nearest",
        extent=[-pre, post, -0.5, len(payloads) - 0.5],
        origin="upper",
    )
    ax.axvline(0, color="#264653", linewidth=1.8)
    ax.set_yticks(range(len(payloads)))
    ax.set_yticklabels(row_labels)
    ax.set_xlabel(f"Sekunden relativ zu Push-recv (Bins à {bin_sec}s)")
    ax.set_title(
        "Trade-Heatmap — Volumen-Intensität im Fokus-Band um den Push-Empfang",
        fontsize=13,
        fontweight="bold",
    )
    cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("log(1 + share volume)")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_slope_before_after(payloads: list[dict], out_path: Path) -> Path:
    """Slopegraph: Preis 30s vor burst/recv vs. 30s danach."""
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(8.5, 6.5))
    palette = ["#2a9d8f", "#c44e52", "#e9c46a", "#264653", "#f4a261", "#6d6875"]

    def price_at(trades: list[Trade], moment) -> float | None:
        last = None
        for trade in trades:
            if trade.time <= moment:
                last = trade.price_pct
            else:
                break
        return last

    for index, payload in enumerate(payloads):
        event: JumpEvent = payload["event"]
        trades = payload["focus_trades"]
        # Anker = Burst falls vorhanden, sonst recv
        anchor = payload["burst_t"] or event.received_at
        before = price_at(trades, anchor - timedelta(seconds=30))
        after = price_at(trades, anchor + timedelta(seconds=30))
        if before is None or after is None:
            continue
        color = palette[index % len(palette)]
        ax.plot([0, 1], [before, after], color=color, linewidth=2.2, alpha=0.9)
        ax.scatter([0, 1], [before, after], color=color, s=55, zorder=3)
        ax.text(
            -0.04,
            before,
            f"{event.day:%m/%d} {event.to_max}°F  {before:.0f}%",
            ha="right",
            va="center",
            fontsize=8,
            color=color,
        )
        ax.text(
            1.04,
            after,
            f"{after:.0f}%  Δ{after - before:+.0f}pp",
            ha="left",
            va="center",
            fontsize=8,
            color=color,
        )

    ax.set_xlim(-0.35, 1.45)
    ax.set_ylim(0, 100)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["−30s vor Burst/Recv", "+30s danach"])
    ax.set_ylabel("Yes-Preis Fokus-Band (%)")
    ax.set_title(
        "Slopegraph — Repricing in ±30 Sekunden um den Informations-Schock",
        fontsize=13,
        fontweight="bold",
    )
    ax.grid(axis="y", alpha=0.25)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    plt.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--min-max", type=int, default=90)
    args = parser.parse_args()

    payloads = collect_event_payloads(args.csv, min_max=args.min_max)
    if not payloads:
        print("Keine Events.", flush=True)
        return 1

    out = args.out_dir
    paths = [
        plot_lead_lag_race(payloads, out / "push_pm_alt_leadlag_race.png"),
        plot_aligned_ribbons(payloads, out / "push_pm_alt_aligned_ribbons.png"),
        plot_trade_heatmap(payloads, out / "push_pm_alt_trade_heatmap.png"),
        plot_slope_before_after(payloads, out / "push_pm_alt_slopegraph.png"),
    ]
    for path in paths:
        print(f"Chart: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
