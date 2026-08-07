#!/usr/bin/env python3
"""KAUS: untertägige HRRR-Inits (AWS/Google) vs. IEM-METAR — Pilot 90 Tage.

Pro Kalendertag (America/Chicago) und Init (06/12/15/18z UTC):
  HRRR-sfc TMP:2m über die Nachmittags-/Peak-Stunden → Forecast-Tagesmax °F
gegen IEM ASOS AUS Tagesmax (:53 und alle Prints).

  MPLBACKEND=Agg python3 validate_hrrr_inits_kaus.py
  MPLBACKEND=Agg python3 validate_hrrr_inits_kaus.py --days 90 --workers 4

Resume: schreibt fortlaufend `kaus_hrrr_inits_daily.csv` und überspringt fertige Zellen.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

USER_AGENT = "weather/1.0 (KAUS HRRR init validation)"
LAT, LON = 30.1945, -97.6699
LON360 = LON % 360
CT = ZoneInfo("America/Chicago")
INIT_HOURS = (6, 12, 15, 18)
# UTC hours that typically contain the CT daily max (summer/Austin)
PEAK_UTC_HOURS = tuple(range(15, 24)) + (0, 1)  # 15z–01z ≈ 10–20 CT
OUT_DEFAULT = Path("/opt/cursor/artifacts")


def http_get(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: Exception | None = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code != 429:
                raise
            time.sleep(5 * (attempt + 1))
    assert last is not None
    raise last


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def month_chunks(start: date, end: date, size: int = 40):
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=size - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


# ---------------------------------------------------------------------------
# Obs
# ---------------------------------------------------------------------------
def fetch_iem_daily_max(start: date, end: date) -> dict[date, dict[str, float]]:
    daily_all: dict[date, float] = {}
    daily_metar: dict[date, float] = {}
    for c0, c1 in month_chunks(start, end, 40):
        query = urllib.parse.urlencode(
            {
                "station": "AUS",
                "data": "tmpf",
                "tz": "America/Chicago",
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
        time.sleep(1.5)
        text = http_get(url, timeout=300).decode()
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
            d = ts.date()
            if not (start <= d <= end):
                continue
            daily_all[d] = val if d not in daily_all else max(daily_all[d], val)
            if ts.minute == 53:
                daily_metar[d] = (
                    val if d not in daily_metar else max(daily_metar[d], val)
                )
    out = {}
    for d, vmax in daily_all.items():
        out[d] = {"all": vmax}
        if d in daily_metar:
            out[d]["metar53"] = daily_metar[d]
    print(f"  IEM days={len(out)}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# HRRR point extract
# ---------------------------------------------------------------------------
def nearest_t2m_c(ds, lat: float = LAT, lon360: float = LON360) -> float:
    dist = (ds.latitude - lat) ** 2 + (ds.longitude - lon360) ** 2
    idx = np.unravel_index(np.argmin(dist.values), dist.shape)
    kelvin = float(ds["t2m"].values[idx])
    return kelvin - 273.15


def max_fxx_for_init(init_hour: int) -> int:
    # 00/06/12/18 → 48h; other hours → 18h
    return 48 if init_hour % 6 == 0 else 18


def valid_hours_for_ct_day(day: date) -> list[datetime]:
    """UTC datetimes (hour resolution) that fall on CT calendar day and peak window."""
    start_ct = datetime(day.year, day.month, day.day, 0, 0, tzinfo=CT)
    end_ct = start_ct + timedelta(days=1)
    out: list[datetime] = []
    cur = start_ct.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end_utc = end_ct.astimezone(timezone.utc)
    while cur < end_utc:
        if cur.hour in PEAK_UTC_HOURS or True:
            # keep all hours of CT day for correct daily max; peak filter optional
            out.append(cur.replace(tzinfo=None))  # naive UTC for Herbie
        cur += timedelta(hours=1)
    return out


def hrrr_daily_max_for_init(day: date, init_hour: int) -> float | None:
    """Max T2m °C over CT day from one HRRR init (UTC hour on `day` or day-1 if needed)."""
    from herbie import Herbie

    # Init time: use the init on the UTC morning of the CT day.
    # For early CT hours before init, those fxx won't exist — fine for max.
    init = datetime(day.year, day.month, day.day, init_hour)  # naive UTC
    # If CT day starts before this init (e.g. 06z), morning hours missing — OK for max.
    max_f = max_fxx_for_init(init_hour)
    vals: list[float] = []
    for valid in valid_hours_for_ct_day(day):
        fxx = int((valid - init).total_seconds() // 3600)
        if fxx < 0 or fxx > max_f:
            continue
        # Only pull hours that can affect daily max: CT 12–18
        valid_ct = valid.replace(tzinfo=timezone.utc).astimezone(CT)
        # Summer daily max almost always 12–18 CT (keeps request count tractable).
        if not (12 <= valid_ct.hour <= 18):
            continue
        try:
            H = Herbie(
                init,
                model="hrrr",
                product="sfc",
                fxx=fxx,
                priority=["aws", "google", "nomads"],
                verbose=False,
            )
            ds = H.xarray("TMP:2 m")
            if isinstance(ds, list):
                ds = ds[0]
            vals.append(nearest_t2m_c(ds))
        except Exception as exc:  # noqa: BLE001
            # missing cycle / lead
            continue
    if not vals:
        return None
    return max(vals)


def hrrr_job(day: date, init_hour: int) -> tuple[date, int, float | None, str | None]:
    try:
        tmax_c = hrrr_daily_max_for_init(day, init_hour)
        return day, init_hour, tmax_c, None
    except Exception as exc:  # noqa: BLE001
        return day, init_hour, None, str(exc)


# ---------------------------------------------------------------------------
# Metrics / IO
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
    if len(pairs) < 5:
        return None
    err = np.array([f - o for f, o in pairs], dtype=float)
    fc = np.array([p[0] for p in pairs], dtype=float)
    ob = np.array([p[1] for p in pairs], dtype=float)
    return Skill(
        label,
        len(pairs),
        float(err.mean()),
        float(np.abs(err).mean()),
        float(math.sqrt((err**2).mean())),
        float((np.abs(err) <= 1.0).mean() * 100),
        float((np.abs(err) <= 2.0).mean() * 100),
        float(np.corrcoef(fc, ob)[0, 1]),
    )


def load_progress(path: Path) -> dict[tuple[str, int], float]:
    """key (date_iso, init_hour) -> forecast max °F."""
    out: dict[tuple[str, int], float] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as fh:
        r = csv.DictReader(fh)
        for row in r:
            for h in INIT_HOURS:
                key = f"hrrr_{h:02d}z_f"
                if row.get(key):
                    try:
                        out[(row["date"], h)] = float(row[key])
                    except ValueError:
                        pass
    return out


def write_daily_csv(
    path: Path,
    days: list[date],
    obs: dict[date, dict[str, float]],
    fcst: dict[tuple[date, int], float],
) -> None:
    fields = (
        ["date", "obs_all_f", "obs_metar53_f"]
        + [f"hrrr_{h:02d}z_f" for h in INIT_HOURS]
        + [f"err_{h:02d}z_metar53_f" for h in INIT_HOURS]
    )
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for d in days:
            row: dict[str, object] = {
                "date": d.isoformat(),
                "obs_all_f": obs.get(d, {}).get("all"),
                "obs_metar53_f": obs.get(d, {}).get("metar53"),
            }
            om = obs.get(d, {}).get("metar53")
            for h in INIT_HOURS:
                f = fcst.get((d, h))
                row[f"hrrr_{h:02d}z_f"] = None if f is None else round(f, 2)
                if f is not None and om is not None:
                    row[f"err_{h:02d}z_metar53_f"] = round(f - om, 2)
            w.writerow(row)


def plot_and_report(
    out_dir: Path,
    start: date,
    end: date,
    days: list[date],
    obs: dict[date, dict[str, float]],
    fcst: dict[tuple[date, int], float],
) -> None:
    skills: list[Skill] = []
    joined: dict[str, list[tuple[date, float, float]]] = {}
    for h in INIT_HOURS:
        label = f"{h:02d}z"
        pairs: list[tuple[date, float, float]] = []
        for d in days:
            f = fcst.get((d, h))
            o = obs.get(d, {}).get("metar53")
            if f is None or o is None:
                continue
            pairs.append((d, f, o))
        joined[label] = pairs
        s = compute_skill(label, [(f, o) for _, f, o in pairs])
        if s:
            skills.append(s)

    lines = [
        "KAUS — HRRR untertägige Inits (AWS/Google) vs. :53-METAR",
        f"Zeitraum: {start} → {end} (CT Kalendertag)",
        f"Inits UTC: {', '.join(f'{h:02d}z' for h in INIT_HOURS)}",
        "Forecast-Max = max TMP:2m über CT 12–18 Uhr (HRRR sfc, nearest grid)",
        "Obs: IEM ASOS AUS :53-METAR Tagesmax °F",
        "",
        f"{'Init':<8} {'n':>5} {'Bias':>7} {'MAE':>7} {'RMSE':>7} {'≤1°F%':>7} {'≤2°F%':>7} {'Corr':>7}",
    ]
    for s in skills:
        lines.append(
            f"{s.label:<8} {s.n:5d} {s.bias:+7.2f} {s.mae:7.2f} {s.rmse:7.2f} "
            f"{s.within_1f:6.1f}% {s.within_2f:6.1f}% {s.corr:7.3f}"
        )
    lines += [
        "",
        "Hinweise:",
        "  - Positiver Bias = HRRR zu warm vs. METAR-Max.",
        "  - Spätere Inits sehen mehr vom Tag (kürzerer Lead) → meist bessere MAE.",
        "  - 15z nur bis f18; 06/12/18z bis f48.",
    ]
    report = out_dir / "kaus_hrrr_inits_validation.txt"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    fig, axes = plt.subplots(2, 2, figsize=(12.5, 9.2), dpi=140)
    fig.patch.set_facecolor("#F7F4EF")
    fig.suptitle(
        f"KAUS HRRR Inits vs :53-METAR\n{start} → {end}",
        fontsize=13,
        fontweight="bold",
    )
    labels = [s.label for s in skills]
    x = np.arange(len(labels))

    ax = axes[0, 0]
    ax.bar(x - 0.2, [s.mae for s in skills], 0.4, label="MAE", color="#2563EB")
    ax.bar(x + 0.2, [s.bias for s in skills], 0.4, label="Bias", color="#E67E22")
    ax.axhline(0, color="#6B7280", lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.set_ylabel("°F")
    ax.set_title("Fehler nach Init")
    ax.legend(fontsize=8)
    ax.grid(True, axis="y", alpha=0.3)

    ax = axes[0, 1]
    ax.plot(labels, [s.within_1f for s in skills], "o-", color="#059669", label="≤1°F")
    ax.plot(labels, [s.within_2f for s in skills], "s-", color="#7C3AED", label="≤2°F")
    ax.set_ylim(0, 100)
    ax.set_ylabel("%")
    ax.set_title("Trefferquote")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[1, 0]
    colors = {"06z": "#93C5FD", "12z": "#2563EB", "15z": "#E67E22", "18z": "#DC2626"}
    for lab, rows in joined.items():
        if not rows:
            continue
        ax.scatter(
            [o for _, _, o in rows],
            [f for _, f, _ in rows],
            s=10,
            alpha=0.35,
            color=colors.get(lab, "#6B7280"),
            label=lab,
        )
    ax.plot([70, 110], [70, 110], "--", color="#6B7280", lw=1)
    ax.set_xlabel("METAR max °F")
    ax.set_ylabel("HRRR max °F")
    ax.set_title("Scatter nach Init")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)

    ax = axes[1, 1]
    # latest init error series
    rows = joined.get("15z") or joined.get("18z") or []
    if rows:
        show = rows[-60:]
        ax.axhline(0, color="#6B7280", lw=0.8)
        ax.plot(
            [d for d, _, _ in show],
            [f - o for _, f, o in show],
            color="#DC2626",
            lw=0.9,
        )
        ax.set_title("15z/18z − METAR (letzte ≤60 Tage)")
        ax.set_ylabel("Fehler °F")
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate()

    fig.tight_layout()
    plot = out_dir / "kaus_hrrr_inits_skill.png"
    fig.savefig(plot, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(report.read_text(encoding="utf-8"))
    print(f"Wrote {report}", file=sys.stderr)
    print(f"Wrote {plot}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--end", default=None, help="YYYY-MM-DD (default yesterday)")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--out-dir", type=Path, default=OUT_DEFAULT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute even if progress CSV has values",
    )
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days - 1)
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "kaus_hrrr_inits_daily.csv"

    print(f"=== HRRR inits KAUS {start} → {end} ===", file=sys.stderr)
    obs = fetch_iem_daily_max(start, end)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    days = [d for d in days if d in obs]

    progress = {} if args.force else load_progress(csv_path)
    # convert progress keys to (date, hour)
    done: dict[tuple[date, int], float] = {}
    for (ds, h), val in progress.items():
        done[(date.fromisoformat(ds), h)] = val

    jobs: list[tuple[date, int]] = []
    for d in days:
        for h in INIT_HOURS:
            if (d, h) not in done:
                jobs.append((d, h))
    print(f"Jobs to run: {len(jobs)} (cached {len(done)})", file=sys.stderr)

    fcst: dict[tuple[date, int], float] = dict(done)
    if jobs:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(hrrr_job, d, h): (d, h) for d, h in jobs}
            n_done = 0
            for fut in as_completed(futs):
                d, h, tmax_c, err = fut.result()
                n_done += 1
                if tmax_c is not None:
                    fcst[(d, h)] = c_to_f(tmax_c)
                if n_done % 10 == 0 or n_done == len(jobs):
                    print(
                        f"  progress {n_done}/{len(jobs)} "
                        f"last={d} {h:02d}z "
                        f"{'OK '+format(c_to_f(tmax_c),'.1f')+'F' if tmax_c is not None else 'MISS '+str(err)}",
                        file=sys.stderr,
                    )
                    write_daily_csv(csv_path, days, obs, fcst)

    write_daily_csv(csv_path, days, obs, fcst)
    plot_and_report(out_dir, start, end, days, obs, fcst)
    print(f"Wrote {csv_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
