#!/usr/bin/env python3
"""HRRR-Archiv-Snapshots vs. KAUS/AUS METAR — 1 Jahr zurück.

Quellen (öffentlich, ohne eigene DB):
  * METAR/ASOS: IEM Iowa Mesonet (`station=AUS`)
  * HRRR: Open-Meteo Previous-Runs `ncep_hrrr_conus` (D0, D-1)
  * Ältere Lags: Open-Meteo `best_match` Previous-Runs (D0…D-5), weil
    reines HRRR bei OM nur D0/D-1 publiziert

Vergleich: Tagesmaximum °F (America/Chicago) je Snapshot-Alter
gegen IEM-Tagesmaximum (alle ASOS-Prints sowie nur :53-METAR).

  python3 validate_hrrr_metar_kaus.py
  python3 validate_hrrr_metar_kaus.py --days 365 --out-dir /opt/cursor/artifacts
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

USER_AGENT = "weather/1.0 (KAUS HRRR vs METAR validation)"
LAT, LON = 30.1945, -97.6699
TZ = "America/Chicago"
HRRR_MODEL = "ncep_hrrr_conus"
# HRRR previous-runs only publishes D0 + D-1 on Open-Meteo.
HRRR_SNAPSHOT_FIELDS = {
    "HRRR-D0": "temperature_2m",
    "HRRR-D-1": "temperature_2m_previous_day1",
}
# Older lags: best_match (often HRRR-led in CONUS) as proxy when pure HRRR lacks D-N.
PROXY_MODEL = "best_match"
PROXY_SNAPSHOT_FIELDS = {
    "BM-D0": "temperature_2m",
    "BM-D-1": "temperature_2m_previous_day1",
    "BM-D-2": "temperature_2m_previous_day2",
    "BM-D-3": "temperature_2m_previous_day3",
    "BM-D-5": "temperature_2m_previous_day5",
}
CHUNK_DAYS = 60


def http_get(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code != 429:
                raise
            wait = 5 * (attempt + 1)
            print(f"  HTTP 429, retry in {wait}s …", file=sys.stderr)
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def http_json(url: str, timeout: int = 180) -> dict:
    return json.loads(http_get(url, timeout=timeout).decode())


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def daterange(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def month_chunks(start: date, end: date, size: int = CHUNK_DAYS):
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=size - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


# ---------------------------------------------------------------------------
# IEM METAR/ASOS
# ---------------------------------------------------------------------------
def fetch_iem_daily_max(start: date, end: date) -> dict[date, dict[str, float]]:
    """Daily max °F from IEM ASOS AUS. all = any print; metar53 = :53 only."""
    daily_all: dict[date, float] = {}
    daily_metar: dict[date, float] = {}
    n_rows = 0
    for c0, c1 in month_chunks(start, end, 45):
        query = urllib.parse.urlencode(
            {
                "station": "AUS",
                "data": "tmpf",
                "tz": TZ,
                "format": "onlycomma",
                "latlon": "no",
                "year1": c0.year,
                "month1": c0.month,
                "day1": c0.day,
                "year2": c1.year,
                "month2": c1.month,
                "day2": c1.day,
            }
        )
        url = f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?{query}"
        print(f"  IEM {c0} … {c1}", file=sys.stderr)
        try:
            text = http_get(url, timeout=300).decode()
        except urllib.error.HTTPError as exc:
            print(f"  IEM warn {exc}", file=sys.stderr)
            continue
        for line in text.splitlines():
            if not line or line.startswith("station") or line.startswith("#"):
                continue
            parts = line.split(",")
            if len(parts) < 3:
                continue
            raw_t, raw_v = parts[1].strip(), parts[2].strip()
            if not raw_v or raw_v.upper() == "M":
                continue
            try:
                val = float(raw_v)
                ts = datetime.strptime(raw_t, "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            n_rows += 1
            d = ts.date()
            if start <= d <= end:
                daily_all[d] = val if d not in daily_all else max(daily_all[d], val)
                if ts.minute == 53:
                    daily_metar[d] = (
                        val if d not in daily_metar else max(daily_metar[d], val)
                    )
    print(f"  IEM rows={n_rows} days_all={len(daily_all)} days_:53={len(daily_metar)}", file=sys.stderr)
    out: dict[date, dict[str, float]] = {}
    for d, vmax in daily_all.items():
        out[d] = {"all": vmax}
        if d in daily_metar:
            out[d]["metar53"] = daily_metar[d]
    return out


# ---------------------------------------------------------------------------
# HRRR via Open-Meteo previous-runs
# ---------------------------------------------------------------------------
def fetch_model_snapshot_daily_max(
    start: date,
    end: date,
    *,
    model: str,
    fields: dict[str, str],
) -> dict[str, dict[date, float]]:
    """Per snapshot label → day → max °F (CT calendar day)."""
    result: dict[str, dict[date, float]] = {label: {} for label in fields}
    hourly_keys = list(dict.fromkeys(fields.values()))
    for c0, c1 in month_chunks(start, end, CHUNK_DAYS):
        query = urllib.parse.urlencode(
            {
                "latitude": LAT,
                "longitude": LON,
                "timezone": TZ,
                "models": model,
                "start_date": c0.isoformat(),
                "end_date": c1.isoformat(),
                "hourly": ",".join(hourly_keys),
            }
        )
        url = f"https://previous-runs-api.open-meteo.com/v1/forecast?{query}"
        print(f"  {model} {c0} … {c1}", file=sys.stderr)
        try:
            payload = http_json(url, timeout=300)
        except urllib.error.HTTPError as exc:
            print(f"  {model} warn {exc}", file=sys.stderr)
            continue
        times = payload["hourly"]["time"]
        series = {k: payload["hourly"][k] for k in hourly_keys}
        day_max_c: dict[str, dict[date, float]] = {k: {} for k in hourly_keys}
        for i, ts in enumerate(times):
            d = date.fromisoformat(ts[:10])
            if not (start <= d <= end):
                continue
            for key in hourly_keys:
                val = series[key][i]
                if val is None:
                    continue
                v = float(val)
                bucket = day_max_c[key]
                bucket[d] = v if d not in bucket else max(bucket[d], v)
        for label, key in fields.items():
            for d, vmax_c in day_max_c[key].items():
                result[label][d] = c_to_f(vmax_c)
    for label, series_map in result.items():
        print(f"  snapshot {label}: {len(series_map)} days", file=sys.stderr)
    return result


def fetch_hrrr_snapshot_daily_max(
    start: date, end: date
) -> dict[str, dict[date, float]]:
    out = fetch_model_snapshot_daily_max(
        start, end, model=HRRR_MODEL, fields=HRRR_SNAPSHOT_FIELDS
    )
    # Older lags only available via best_match on Open-Meteo
    out.update(
        fetch_model_snapshot_daily_max(
            start, end, model=PROXY_MODEL, fields=PROXY_SNAPSHOT_FIELDS
        )
    )
    return out


def snapshot_order() -> list[str]:
    return list(HRRR_SNAPSHOT_FIELDS) + list(PROXY_SNAPSHOT_FIELDS)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
@dataclass
class Skill:
    label: str
    n: int
    bias: float
    mae: float
    rmse: float
    within_1f: float
    within_2f: float
    corr: float


def compute_skill(label: str, pairs: list[tuple[float, float]]) -> Skill | None:
    """pairs = (forecast_f, obs_f)."""
    if len(pairs) < 5:
        return None
    err = np.array([f - o for f, o in pairs], dtype=float)
    fc = np.array([f for f, _ in pairs], dtype=float)
    ob = np.array([o for _, o in pairs], dtype=float)
    bias = float(err.mean())
    mae = float(np.abs(err).mean())
    rmse = float(math.sqrt((err ** 2).mean()))
    within_1f = float((np.abs(err) <= 1.0).mean() * 100)
    within_2f = float((np.abs(err) <= 2.0).mean() * 100)
    corr = float(np.corrcoef(fc, ob)[0, 1]) if len(pairs) > 2 else float("nan")
    return Skill(label, len(pairs), bias, mae, rmse, within_1f, within_2f, corr)


def join_pairs(
    forecast: dict[date, float], obs: dict[date, float]
) -> list[tuple[date, float, float]]:
    days = sorted(set(forecast) & set(obs))
    return [(d, forecast[d], obs[d]) for d in days]


# ---------------------------------------------------------------------------
# Report + plots
# ---------------------------------------------------------------------------
def write_report(
    path: Path,
    start: date,
    end: date,
    skills_all: list[Skill],
    skills_metar: list[Skill],
    n_obs_all: int,
    n_obs_metar: int,
) -> None:
    lines = [
        "KAUS / AUS — HRRR Snapshot-Archiv vs. METAR/ASOS",
        f"Zeitraum: {start.isoformat()} → {end.isoformat()} (America/Chicago)",
        f"HRRR: Open-Meteo previous-runs · model={HRRR_MODEL} (HRRR-D0, HRRR-D-1)",
        f"Ältere Lags: model={PROXY_MODEL} (BM-D0…D-5) — reines HRRR liefert bei OM nur D0/D-1",
        "Obs: IEM ASOS station=AUS · all=alle Prints · metar53=nur :53",
        "",
        f"Obs-Tage (all): {n_obs_all}  |  Obs-Tage (:53): {n_obs_metar}",
        "",
        "=== Skill vs. ASOS daily max (alle Prints), °F ===",
        f"{'Snap':<10} {'n':>5} {'Bias':>7} {'MAE':>7} {'RMSE':>7} {'≤1°F%':>7} {'≤2°F%':>7} {'Corr':>7}",
    ]
    for s in skills_all:
        lines.append(
            f"{s.label:<10} {s.n:5d} {s.bias:+7.2f} {s.mae:7.2f} {s.rmse:7.2f} "
            f"{s.within_1f:6.1f}% {s.within_2f:6.1f}% {s.corr:7.3f}"
        )
    lines += [
        "",
        "=== Skill vs. :53-METAR daily max, °F ===",
        f"{'Snap':<10} {'n':>5} {'Bias':>7} {'MAE':>7} {'RMSE':>7} {'≤1°F%':>7} {'≤2°F%':>7} {'Corr':>7}",
    ]
    for s in skills_metar:
        lines.append(
            f"{s.label:<10} {s.n:5d} {s.bias:+7.2f} {s.mae:7.2f} {s.rmse:7.2f} "
            f"{s.within_1f:6.1f}% {s.within_2f:6.1f}% {s.corr:7.3f}"
        )
    lines += [
        "",
        "Hinweise:",
        "  - Positiver Bias = Forecast zu warm vs. Obs-Tagesmax.",
        "  - HRRR-D0/D-1 = NCEP-HRRR Previous-Runs (Tages-Lag, nicht 00/06/12z-Init).",
        "  - BM-* = Open-Meteo best_match Previous-Runs (Proxy für D-2+).",
        "  - Für Init-Stunden-Snapshots: NOMADS/AWS HRRR-GRIBs nötig.",
        "  - :53-METAR ≈ Settlement-näher; 'all' enthält auch 5-min/SPECI-Peaks.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def plot_skill(
    out: Path,
    start: date,
    end: date,
    joined: dict[str, list[tuple[date, float, float]]],
    skills: list[Skill],
    obs_title: str,
) -> None:
    fig, axes = plt.subplots(2, 2, figsize=(13, 9.5), dpi=140)
    fig.patch.set_facecolor("#F7F4EF")
    fig.suptitle(
        f"KAUS HRRR/BM-Snapshots vs {obs_title}\n{start} → {end}",
        fontsize=13,
        fontweight="bold",
    )

    ax = axes[0, 0]
    labels = [s.label for s in skills]
    x = np.arange(len(labels))
    ax.bar(x - 0.2, [s.mae for s in skills], 0.4, label="MAE", color="#2563EB")
    ax.bar(x + 0.2, [s.bias for s in skills], 0.4, label="Bias", color="#E67E22")
    ax.axhline(0, color="#6B7280", lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
    ax.set_ylabel("°F")
    ax.set_title("Fehler nach Snapshot-Alter")
    ax.legend(fontsize=8)
    ax.grid(True, axis="y", alpha=0.3)

    ax = axes[0, 1]
    ax.plot(labels, [s.within_1f for s in skills], "o-", color="#059669", label="≤1°F")
    ax.plot(labels, [s.within_2f for s in skills], "s-", color="#7C3AED", label="≤2°F")
    ax.set_ylim(0, 100)
    ax.set_ylabel("%")
    ax.set_title("Trefferquote Tagesmax")
    ax.tick_params(axis="x", rotation=30, labelsize=8)
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[1, 0]
    for label, color in (("HRRR-D0", "#E67E22"), ("HRRR-D-1", "#2563EB")):
        rows = joined.get(label) or []
        if not rows:
            continue
        ax.scatter(
            [o for _, _, o in rows],
            [f for _, f, _ in rows],
            s=12,
            alpha=0.35,
            color=color,
            label=label,
        )
    lims = [60, 115]
    ax.plot(lims, lims, "--", color="#6B7280", lw=1)
    ax.set_xlim(lims)
    ax.set_ylim(lims)
    ax.set_xlabel(f"Obs max °F ({obs_title})")
    ax.set_ylabel("Forecast max °F")
    ax.set_title("Scatter HRRR-D0 / HRRR-D-1")
    ax.legend(fontsize=8)
    ax.set_aspect("equal")
    ax.grid(True, alpha=0.3)

    ax = axes[1, 1]
    rows = joined.get("HRRR-D0") or []
    if rows:
        show = rows[-120:]
        days = [d for d, _, _ in show]
        err = [f - o for _, f, o in show]
        ax.axhline(0, color="#6B7280", lw=0.8)
        ax.axhline(2, color="#86EFAC", lw=0.7, ls=":")
        ax.axhline(-2, color="#86EFAC", lw=0.7, ls=":")
        ax.plot(days, err, color="#DC2626", lw=0.9)
        ax.set_title("HRRR-D0 − Obs (letzte ≤120 Tage)")
        ax.set_ylabel("Fehler °F")
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate()

    fig.tight_layout()
    fig.savefig(out, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)


def write_csv(
    path: Path,
    days: list[date],
    obs_all: dict[date, float],
    obs_metar: dict[date, float],
    forecasts: dict[str, dict[date, float]],
) -> None:
    labels = snapshot_order()
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["date", "obs_all_f", "obs_metar53_f"]
            + [f"fcst_{lab}_f" for lab in labels]
            + [f"err_{lab}_all_f" for lab in labels]
        )
        for d in days:
            oa = obs_all.get(d)
            om = obs_metar.get(d)
            fc = [forecasts[lab].get(d) for lab in labels]
            row: list[object] = [d.isoformat(), oa, om, *fc]
            if oa is not None:
                row.extend([(f - oa) if f is not None else None for f in fc])
            else:
                row.extend([None] * len(labels))
            w.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser(description="KAUS HRRR snapshots vs METAR")
    parser.add_argument("--days", type=int, default=365, help="Lookback days (default 365)")
    parser.add_argument(
        "--end",
        default=None,
        help="End date YYYY-MM-DD (default: yesterday)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("/opt/cursor/artifacts"),
        help="Output directory",
    )
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days - 1)
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== KAUS HRRR vs METAR  {start} → {end} ===", file=sys.stderr)
    print("Lade IEM ASOS …", file=sys.stderr)
    iem = fetch_iem_daily_max(start, end)
    obs_all = {d: v["all"] for d, v in iem.items()}
    obs_metar = {d: v["metar53"] for d, v in iem.items() if "metar53" in v}

    print("Lade HRRR + best_match previous-runs Snapshots …", file=sys.stderr)
    forecasts = fetch_hrrr_snapshot_daily_max(start, end)

    skills_all: list[Skill] = []
    skills_metar: list[Skill] = []
    joined_all: dict[str, list[tuple[date, float, float]]] = {}
    joined_metar: dict[str, list[tuple[date, float, float]]] = {}

    for label in snapshot_order():
        pairs_all = join_pairs(forecasts[label], obs_all)
        pairs_metar = join_pairs(forecasts[label], obs_metar)
        joined_all[label] = pairs_all
        joined_metar[label] = pairs_metar
        s_all = compute_skill(label, [(f, o) for _, f, o in pairs_all])
        s_met = compute_skill(label, [(f, o) for _, f, o in pairs_metar])
        if s_all:
            skills_all.append(s_all)
        if s_met:
            skills_metar.append(s_met)

    report = out_dir / "kaus_hrrr_metar_validation.txt"
    csv_path = out_dir / "kaus_hrrr_metar_daily.csv"
    plot_all = out_dir / "kaus_hrrr_metar_skill_all.png"
    plot_metar = out_dir / "kaus_hrrr_metar_skill_metar53.png"

    write_report(
        report, start, end, skills_all, skills_metar, len(obs_all), len(obs_metar)
    )
    all_days = sorted(set(obs_all) | set().union(*[set(s) for s in forecasts.values()]))
    write_csv(csv_path, all_days, obs_all, obs_metar, forecasts)
    plot_skill(plot_all, start, end, joined_all, skills_all, "ASOS all prints")
    plot_skill(plot_metar, start, end, joined_metar, skills_metar, ":53 METAR")

    print(report.read_text(encoding="utf-8"))
    print(f"Wrote {report}", file=sys.stderr)
    print(f"Wrote {csv_path}", file=sys.stderr)
    print(f"Wrote {plot_all}", file=sys.stderr)
    print(f"Wrote {plot_metar}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
