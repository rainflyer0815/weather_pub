#!/usr/bin/env python3
"""Auswertung und Visualisierung der DWD-/METAR-Feed-Lag-CSV."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from monitor_dwd_feed_lag import LOG_FILE

BERLIN = ZoneInfo("Europe/Berlin")
SCRIPT_DIR = Path(__file__).resolve().parent
REPORT_PATH = SCRIPT_DIR / "dwd_feed_lag_report.txt"
PLOT_PATH = SCRIPT_DIR / "dwd_feed_lag_trend.png"
ARTIFACT_PATH = Path("/opt/cursor/artifacts/dwd_feed_lag_trend.png")


@dataclass(frozen=True)
class LagRow:
    logged_at: datetime
    dwd_latest: datetime | None
    dwd_lag_min: int | None
    dwd_tt10: float | None
    dwd_max: float | None
    dwd_max_time: datetime | None
    metar_latest: datetime | None
    metar_lag_min: int | None
    metar_temp: float | None
    metar_max: float | None
    metar_max_time: datetime | None


def parse_float(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    return float(value)


def parse_int(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    return int(value)


def parse_dt(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=BERLIN)


def load_rows(path: Path) -> list[LagRow]:
    if not path.exists():
        raise FileNotFoundError(f"Logdatei fehlt: {path}")

    rows: list[LagRow] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for raw in csv.DictReader(handle):
            logged_at = parse_dt(raw.get("logged_at_berlin", ""))
            if logged_at is None:
                continue
            rows.append(
                LagRow(
                    logged_at=logged_at,
                    dwd_latest=parse_dt(raw.get("dwd_latest", "")),
                    dwd_lag_min=parse_int(raw.get("dwd_lag_min", "")),
                    dwd_tt10=parse_float(raw.get("dwd_TT_10", "")),
                    dwd_max=parse_float(raw.get("dwd_max", "")),
                    dwd_max_time=parse_dt(raw.get("dwd_max_time", "")),
                    metar_latest=parse_dt(raw.get("metar_latest", "")),
                    metar_lag_min=parse_int(raw.get("metar_lag_min", "")),
                    metar_temp=parse_float(raw.get("metar_temp", "")),
                    metar_max=parse_float(raw.get("metar_max", "")),
                    metar_max_time=parse_dt(raw.get("metar_max_time", "")),
                )
            )

    rows.sort(key=lambda row: row.logged_at)
    return rows


def summarize_lags(values: list[int]) -> str:
    if not values:
        return "keine Daten"
    ordered = sorted(values)
    return (
        f"min {min(values)}  median {median(values)}  "
        f"Ø {mean(values):.0f}  max {max(values)} Min."
    )


def format_intervals(rows: list[LagRow]) -> str:
    if len(rows) < 2:
        return "n/a"
    gaps = [
        int((rows[index].logged_at - rows[index - 1].logged_at).total_seconds() // 60)
        for index in range(1, len(rows))
    ]
    irregular = sum(1 for gap in gaps if gap != 10)
    return (
        f"Median {median(gaps):.0f} Min., Range {min(gaps)}–{max(gaps)} Min., "
        f"{irregular} unregelmäßige Intervalle"
    )


def build_report(rows: list[LagRow], source: Path) -> str:
    if not rows:
        return f"Keine verwertbaren Zeilen in {source.name}."

    dwd_lags = [row.dwd_lag_min for row in rows if row.dwd_lag_min is not None]
    metar_lags = [row.metar_lag_min for row in rows if row.metar_lag_min is not None]
    latest = rows[-1]
    lines = [
        "DWD /now vs. METAR Feed-Lag – Auswertung",
        f"Quelle: {source.name}",
        f"Zeitraum: {rows[0].logged_at:%d.%m.%Y %H:%M} – {latest.logged_at:%d.%m.%Y %H:%M} Ortszeit",
        f"Einträge: {len(rows)}",
        "",
        "Sampling:",
        f"  {format_intervals(rows)}",
        "",
        "Feed-Lag:",
        f"  DWD /now:   {summarize_lags(dwd_lags)}",
        f"  METAR EDDM: {summarize_lags(metar_lags)}",
        "",
        "Tagesmaximum (letzter Eintrag):",
        f"  DWD:   {latest.dwd_max:.1f}°C @ {latest.dwd_max_time:%H:%M}" if latest.dwd_max is not None else "  DWD:   n/a",
        f"  METAR: {latest.metar_max:.0f}°C @ {latest.metar_max_time:%H:%M}" if latest.metar_max is not None else "  METAR: n/a",
        "",
        "Letzter Snapshot:",
    ]

    if latest.dwd_tt10 is not None and latest.dwd_latest is not None:
        lines.append(
            f"  DWD TT_10: {latest.dwd_tt10:.1f}°C @ {latest.dwd_latest:%H:%M} "
            f"(Lag {latest.dwd_lag_min} Min.)"
        )
    else:
        lines.append("  DWD TT_10: n/a")

    if latest.metar_temp is not None and latest.metar_latest is not None:
        lines.append(
            f"  METAR:     {latest.metar_temp:.0f}°C @ {latest.metar_latest:%H:%M} "
            f"(Lag {latest.metar_lag_min} Min.)"
        )
    else:
        lines.append("  METAR: n/a")

    if (
        latest.dwd_tt10 is not None
        and latest.metar_temp is not None
        and latest.dwd_latest is not None
        and latest.metar_latest is not None
    ):
        delta = latest.metar_temp - latest.dwd_tt10
        obs_gap = int((latest.metar_latest - latest.dwd_latest).total_seconds() // 60)
        lines.extend(
            [
                "",
                "Vergleich aktueller Messwerte:",
                f"  METAR − DWD TT_10: {delta:+.1f}°C",
                f"  Beobachtungszeit-Differenz: {obs_gap} Min. (METAR neuer)",
            ]
        )

    return "\n".join(lines)


def plot_lag_trends(rows: list[LagRow], output_path: Path) -> Path:
    if len(rows) < 2:
        raise ValueError("Mindestens zwei Log-Einträge für den Plot nötig.")

    times = [row.logged_at for row in rows]
    dwd_lags = [row.dwd_lag_min for row in rows]
    metar_lags = [row.metar_lag_min for row in rows]
    dwd_temps = [row.dwd_tt10 for row in rows]
    metar_temps = [row.metar_temp for row in rows]

    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
    fig.suptitle(
        "DWD /now vs. METAR – Feed-Lag München-Flughafen (01262 / EDDM)",
        fontsize=13,
        fontweight="bold",
    )

    ax_lag = axes[0]
    ax_lag.plot(times, dwd_lags, marker="o", linewidth=2, label="DWD /now Lag")
    ax_lag.plot(times, metar_lags, marker="s", linewidth=2, label="METAR Lag")
    ax_lag.axhline(median([value for value in dwd_lags if value is not None]), color="#1f77b4", linestyle="--", alpha=0.5)
    ax_lag.axhline(median([value for value in metar_lags if value is not None]), color="#ff7f0e", linestyle="--", alpha=0.5)
    ax_lag.set_ylabel("Verzögerung (Min.)")
    ax_lag.set_title("Feed-Lag über Zeit")
    ax_lag.grid(True, alpha=0.3)
    ax_lag.legend(loc="upper right")

    ax_temp = axes[1]
    ax_temp.plot(times, dwd_temps, marker="o", linewidth=2, label="DWD TT_10 (latest)")
    ax_temp.plot(times, metar_temps, marker="s", linewidth=2, label="METAR temp (latest)")
    if rows[-1].dwd_max is not None:
        ax_temp.axhline(rows[-1].dwd_max, color="#1f77b4", linestyle=":", alpha=0.6, label=f"DWD Tagesmax {rows[-1].dwd_max:.1f}°C")
    if rows[-1].metar_max is not None:
        ax_temp.axhline(rows[-1].metar_max, color="#ff7f0e", linestyle=":", alpha=0.6, label=f"METAR Tagesmax {rows[-1].metar_max:.0f}°C")
    ax_temp.set_ylabel("Temperatur (°C)")
    ax_temp.set_title("Letzte gemeldete Temperatur je Feed")
    ax_temp.grid(True, alpha=0.3)
    ax_temp.legend(loc="upper right")

    ax_gap = axes[2]
    temp_gap = [
        metar - dwd if metar is not None and dwd is not None else None
        for metar, dwd in zip(metar_temps, dwd_temps)
    ]
    valid_gap = [value for value in temp_gap if value is not None]
    ax_gap.plot(times, temp_gap, marker="o", linewidth=2, color="#2ca02c")
    if valid_gap:
        ax_gap.axhline(median(valid_gap), color="#2ca02c", linestyle="--", alpha=0.5, label="Median")
    ax_gap.axhline(0, color="black", linewidth=0.8, alpha=0.4)
    ax_gap.set_ylabel("ΔT (°C)")
    ax_gap.set_xlabel("Log-Zeitpunkt (Europe/Berlin)")
    ax_gap.set_title("METAR temp − DWD TT_10 (jeweils letzter Wert)")
    ax_gap.grid(True, alpha=0.3)
    ax_gap.legend(loc="upper right")

    ax_gap.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=BERLIN))
    fig.autofmt_xdate()
    plt.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    if output_path != ARTIFACT_PATH:
        ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(ARTIFACT_PATH, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--csv",
        type=Path,
        default=LOG_FILE,
        help=f"Pfad zur Lag-CSV (Standard: {LOG_FILE.name})",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=REPORT_PATH,
        help=f"Textreport (Standard: {REPORT_PATH.name})",
    )
    parser.add_argument(
        "--plot",
        type=Path,
        default=PLOT_PATH,
        help=f"Plot-PNG (Standard: {PLOT_PATH.name})",
    )
    parser.add_argument("--no-report", action="store_true", help="Keinen Textreport schreiben")
    parser.add_argument("--no-plot", action="store_true", help="Keinen Plot erzeugen")
    args = parser.parse_args()

    try:
        rows = load_rows(args.csv)
        report = build_report(rows, args.csv)
        print(report)

        if not args.no_report:
            args.report.write_text(report + "\n", encoding="utf-8")
            print(f"\nReport: {args.report.resolve()}")

        if not args.no_plot:
            plot_path = plot_lag_trends(rows, args.plot)
            print(f"Plot:   {plot_path.resolve()}")
            if plot_path != ARTIFACT_PATH and ARTIFACT_PATH.exists():
                print(f"        {ARTIFACT_PATH.resolve()}")
    except Exception as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
