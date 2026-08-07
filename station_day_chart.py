#!/usr/bin/env python3
"""Tagesverlauf-Template: Ensemble · MADIS · METAR/SPECI · Wolken/Wind · Polymarket.

Wiederverwendbares Layout für Polymarket Daily-High Charts (Klassik-Stil).
Zukünftige Bild-Updates sollen dieses Modul nutzen statt One-Off-Skripte.

Beispiel:
  python3 station_day_chart.py --station KAUS
  python3 station_day_chart.py --station KAUS --out /opt/cursor/artifacts/kaus.png
  python3 station_day_chart.py --list

Layout (3 Panels, beiger Hintergrund):
  1) Temperatur: ICON-Ensemble + Mittel, Open-Meteo best_match (orange),
     MADIS 5-min (grau, steps), METAR Quadrate / SPECI Rauten (lila),
     Peakfenster 14–17 lokal, Jetzt-Linie, HRRR-Niederschlag unten
  2) Meta: Wolken-Icons + Windpfeile (stündlich, Ortszeit)
  3) Polymarket Yes-% Balken (CLOB-Mid wenn erreichbar)

Zeitzone: immer Stations-Ortszeit (Kalendertag 00:00–24:00), kein Vortag.
"""

from __future__ import annotations

import argparse
import json
import math
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, DrawingArea
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle

from poll_madis_hfmetar import USER_AGENT, fetch_madis_observations

# ---------------------------------------------------------------------------
# Visuelles Template (Klassik — nicht ändern ohne Layout-Review)
# ---------------------------------------------------------------------------
COL_BG = "#F7F4EF"
COL_MEAN = "#111827"
COL_HRRR = "#E67E22"
COL_MADIS = "#6B7280"
COL_METAR = "#7C3AED"
COL_SPECI = "#C026D3"
COL_PEAK = "#FDE68A"
COL_NOW = "#111827"
COL_RAIN = "#60A5FA"
COL_GRID = "#E5E7EB"
ENS_COLORS = ("#FCA5A5", "#86EFAC", "#93C5FD", "#C4B5FD", "#FCD34D")
PM_COLORS = ("#059669", "#0D9488", "#64748B", "#94A3B8")

PEAK_START_HOUR = 14
PEAK_END_HOUR = 17
DEFAULT_OUT_DIR = Path("/opt/cursor/artifacts")


@dataclass(frozen=True)
class Station:
    """Station für Tagesverlauf + Polymarket-Slug."""

    stid: str
    city: str
    lat: float
    lon: float
    tz_name: str
    pm_city: str  # Slug-Fragment: highest-temperature-in-{pm_city}-on-...
    label_tz: str  # Kurzlabel für Achse, z.B. CT / ET

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)


# Bekannte Stationen — bei Bedarf erweitern
STATIONS: dict[str, Station] = {
    "KAUS": Station(
        "KAUS", "Austin", 30.1945, -97.6699,
        "America/Chicago", "austin", "CT",
    ),
    "KLGA": Station(
        "KLGA", "New York", 40.7772, -73.8726,
        "America/New_York", "nyc", "ET",
    ),
    "KDFW": Station(
        "KDFW", "Dallas", 32.8998, -97.0403,
        "America/Chicago", "dallas", "CT",
    ),
    "KHOU": Station(
        "KHOU", "Houston", 29.6454, -95.2789,
        "America/Chicago", "houston", "CT",
    ),
    "KATL": Station(
        "KATL", "Atlanta", 33.6407, -84.4277,
        "America/New_York", "atlanta", "ET",
    ),
}


def http_json(url: str, timeout: int = 45):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def parse_local(ts: str, tz: ZoneInfo) -> datetime:
    """Open-Meteo-Zeitstempel in Stations-Ortszeit (naive)."""
    t = datetime.fromisoformat(ts)
    if t.tzinfo is not None:
        return t.astimezone(tz).replace(tzinfo=None)
    return t


def to_local_naive(t: datetime, tz: ZoneInfo) -> datetime:
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(tz).replace(tzinfo=None)


def wind_color(kmh: float | None) -> str:
    if kmh is None:
        return "#9CA3AF"
    if kmh < 10:
        return "#22C55E"
    if kmh < 20:
        return "#84CC16"
    if kmh < 30:
        return "#EAB308"
    if kmh < 40:
        return "#F97316"
    if kmh < 50:
        return "#EF4444"
    return "#111827"


def make_cloud_da(cover: float | None) -> DrawingArea:
    """Sonnen-/Wolken-Glyph in Display-Pixeln (keine Zeitachsen-Verzerrung)."""
    da = DrawingArea(20, 14, 0, 0)
    if cover is None:
        return da
    if cover < 15:
        da.add_artist(Circle((10, 7), 5.5, fc="#F5C542", ec="#B45309", lw=0.4))
    elif cover < 50:
        da.add_artist(Circle((6, 9), 3.8, fc="#F5C542", ec="#B45309", lw=0.3))
        da.add_artist(Circle((11, 6.5), 5.0, fc="#D0D5DD", ec="#9CA3AF", lw=0.3))
        da.add_artist(Circle((15, 7.5), 4.0, fc="#B8C0CC", ec="#9CA3AF", lw=0.3))
    elif cover < 85:
        da.add_artist(Circle((7, 7), 5.0, fc="#9AA3B2", ec="#6B7280", lw=0.3))
        da.add_artist(Circle((13, 8), 4.8, fc="#7D8696", ec="#6B7280", lw=0.3))
        da.add_artist(Circle((10, 5.5), 4.4, fc="#8B93A3", ec="#6B7280", lw=0.3))
    else:
        da.add_artist(Circle((7, 7), 5.2, fc="#5B6472", ec="#374151", lw=0.3))
        da.add_artist(Circle((13, 8), 5.0, fc="#3F4754", ec="#374151", lw=0.3))
        da.add_artist(Circle((10, 5.5), 4.6, fc="#4A5260", ec="#374151", lw=0.3))
    return da


def fmt_obs(pt: tuple[datetime, float] | None) -> str:
    if not pt:
        return "—"
    t, v = pt
    return f"{v:.1f}°C / {c_to_f(v):.0f}°F @ {t:%H:%M}"


def pm_slug(station: Station, day: date) -> str:
    month = day.strftime("%B").lower()
    return f"highest-temperature-in-{station.pm_city}-on-{month}-{day.day}-{day.year}"


def pm_label(question: str, day: date) -> str:
    if "between" in question:
        return (
            question.split("between ")[-1]
            .replace(f" on {day.strftime('%B')} {day.day}?", "")
            .replace("°F", "")
            .strip()
        )
    if "below" in question.lower():
        return question.split("be ")[-1].split(" on ")[0].replace("°F", "").strip()
    if "or higher" in question.lower() or "or above" in question.lower():
        return question.split("be ")[-1].split(" on ")[0].replace("°F", "").strip()
    return question[-40:]


# ---------------------------------------------------------------------------
# Daten
# ---------------------------------------------------------------------------
@dataclass
class DayData:
    station: Station
    day: date
    now_local: datetime
    ens_members: list[list[tuple[datetime, float]]]
    ens_mean: list[tuple[datetime, float]]
    hrrr: list[tuple[datetime, float]]
    madis: list[tuple[datetime, float]]
    metar: list[tuple[datetime, float]]
    speci: list[tuple[datetime, float]]
    wx_rows: list[dict]  # minutely_15 in Ortszeit
    meta_hours: list[dict]  # 24 Stunden-Slots
    pm: list[dict]


def fetch_day_data(station: Station, day: date | None = None) -> DayData:
    tz = station.tz
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz).replace(tzinfo=None)
    day = day or now_utc.astimezone(tz).date()
    xl0 = datetime(day.year, day.month, day.day, 0, 0, 0)
    xl1 = xl0 + timedelta(days=1)
    day_iso = day.isoformat()

    def keep_day(pairs):
        return [p for p in pairs if xl0 <= p[0] < xl1]

    # ICON Ensemble
    ens_raw = http_json(
        "https://ensemble-api.open-meteo.com/v1/ensemble?"
        + urllib.parse.urlencode(
            {
                "latitude": station.lat,
                "longitude": station.lon,
                "timezone": station.tz_name,
                "models": "icon_seamless",
                "hourly": "temperature_2m",
                "start_date": day_iso,
                "end_date": day_iso,
            }
        )
    )
    hourly = ens_raw["hourly"]
    ens_times = [parse_local(t, tz) for t in hourly["time"]]
    ens_members: list[list[tuple[datetime, float]]] = []
    for key in [k for k in hourly if k.startswith("temperature_2m")]:
        pairs = keep_day(
            [(t, float(v)) for t, v in zip(ens_times, hourly[key]) if v is not None]
        )
        if pairs:
            ens_members.append(pairs)
    ens_mean: list[tuple[datetime, float]] = []
    if ens_members:
        by_h: dict[datetime, list[float]] = {}
        for series in ens_members:
            for t, v in series:
                by_h.setdefault(t, []).append(v)
        ens_mean = [(t, sum(vs) / len(vs)) for t, vs in sorted(by_h.items())]

    # Open-Meteo best_match + minutely
    om = http_json(
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode(
            {
                "latitude": station.lat,
                "longitude": station.lon,
                "timezone": station.tz_name,
                "models": "best_match",
                "hourly": "temperature_2m",
                "minutely_15": (
                    "precipitation,precipitation_probability,"
                    "cloud_cover,wind_speed_10m,wind_direction_10m"
                ),
                "start_date": day_iso,
                "end_date": day_iso,
            }
        )
    )
    hrrr = keep_day(
        [
            (parse_local(t, tz), float(v))
            for t, v in zip(om["hourly"]["time"], om["hourly"]["temperature_2m"])
            if v is not None
        ]
    )
    wx = om["minutely_15"]
    wx_rows: list[dict] = []
    for i, ts in enumerate(wx["time"]):
        t = parse_local(ts, tz)
        if not (xl0 <= t < xl1):
            continue
        wx_rows.append(
            {
                "t": t,
                "precip": float(wx["precipitation"][i] or 0),
                "pop": float(wx["precipitation_probability"][i] or 0),
                "cloud": (
                    float(wx["cloud_cover"][i])
                    if wx["cloud_cover"][i] is not None
                    else None
                ),
                "wspd": (
                    float(wx["wind_speed_10m"][i])
                    if wx["wind_speed_10m"][i] is not None
                    else None
                ),
                "wdir": (
                    float(wx["wind_direction_10m"][i])
                    if wx["wind_direction_10m"][i] is not None
                    else None
                ),
            }
        )

    meta_hours: list[dict] = []
    for hour in range(24):
        target = xl0 + timedelta(hours=hour)
        exact = next((r for r in wx_rows if r["t"] == target), None)
        if exact:
            meta_hours.append({**exact, "t": target})
            continue
        same = [r for r in wx_rows if r["t"].hour == hour]
        if same:
            best = min(same, key=lambda r: abs((r["t"] - target).total_seconds()))
            meta_hours.append({**best, "t": target})

    # MADIS
    madis_raw: list[tuple[datetime, float]] = []
    for obs in fetch_madis_observations({station.stid}, hours_back=20):
        if obs.air_temp_c is None:
            continue
        t = to_local_naive(obs.observed_at, tz)
        if xl0 <= t < xl1:
            madis_raw.append((t, float(obs.air_temp_c)))
    ded: dict[datetime, float] = {}
    for t, v in madis_raw:
        ded[t.replace(second=0, microsecond=0)] = v
    madis = sorted(ded.items())

    # METAR / SPECI
    metar: list[tuple[datetime, float]] = []
    speci: list[tuple[datetime, float]] = []
    awc = http_json(
        f"https://aviationweather.gov/api/data/metar?ids={station.stid}&format=json&hours=30"
    )
    for row in awc:
        raw = row.get("rawOb") or ""
        if "MADISHF" in raw.upper():
            continue
        ot, temp = row.get("obsTime"), row.get("temp")
        if ot is None or temp is None:
            continue
        t = to_local_naive(datetime.fromtimestamp(ot, tz=timezone.utc), tz)
        if not (xl0 <= t < xl1):
            continue
        pt = (t, float(temp))
        if raw.upper().startswith("SPECI"):
            speci.append(pt)
        else:
            metar.append(pt)
    metar.sort()
    speci.sort()

    # Polymarket
    pm: list[dict] = []
    try:
        events = http_json(
            f"https://gamma-api.polymarket.com/events?slug={pm_slug(station, day)}"
        )
        markets = (events[0].get("markets") or []) if events else []
        if markets and isinstance(markets[0], str):
            markets = [
                http_json(f"https://gamma-api.polymarket.com/markets/{mid}")
                for mid in markets
            ]
        for m in markets:
            q = m.get("question") or ""
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            p = float(prices[0]) if prices else float(m.get("lastTradePrice") or 0)
            tokens = m.get("clobTokenIds")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            pm.append(
                {
                    "label": pm_label(q, day),
                    "yes": p * 100,
                    "token": tokens[0] if tokens else None,
                }
            )
    except Exception as exc:  # noqa: BLE001
        print(f"Polymarket warn: {exc}")

    def clob_mid(token: str) -> float | None:
        try:
            book = http_json(
                f"https://clob.polymarket.com/book?token_id={token}", timeout=12
            )
            bids = [float(x["price"]) for x in (book.get("bids") or [])]
            asks = [float(x["price"]) for x in (book.get("asks") or [])]
            if not bids or not asks:
                return None
            return (max(bids) + min(asks)) / 2 * 100
        except Exception:  # noqa: BLE001
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {
            pool.submit(clob_mid, r["token"]): r for r in pm if r.get("token")
        }
        for fut in as_completed(futs):
            row = futs[fut]
            mid = fut.result()
            if mid is not None:
                row["yes"] = mid
    pm.sort(key=lambda x: -float(x["yes"]))

    return DayData(
        station=station,
        day=day,
        now_local=now_local,
        ens_members=ens_members,
        ens_mean=ens_mean,
        hrrr=hrrr,
        madis=madis,
        metar=metar,
        speci=speci,
        wx_rows=wx_rows,
        meta_hours=meta_hours,
        pm=pm,
    )


# ---------------------------------------------------------------------------
# Render (Template-Layout)
# ---------------------------------------------------------------------------
def render_station_day_chart(
    data: DayData,
    out: Path,
    *,
    also: Path | None = None,
) -> Path:
    """Zeichnet das Klassik-3-Panel-Template und speichert PNG."""
    station = data.station
    day = data.day
    xl0 = datetime(day.year, day.month, day.day, 0, 0, 0)
    xl1 = xl0 + timedelta(days=1)
    peak0 = xl0 + timedelta(hours=PEAK_START_HOUR)
    peak1 = xl0 + timedelta(hours=PEAK_END_HOUR)
    now_local = data.now_local
    cet = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Berlin"))

    all_official = sorted(data.metar + data.speci)
    madis_last = data.madis[-1] if data.madis else None
    madis_max = max(data.madis, key=lambda x: x[1]) if data.madis else None
    metar_max = max(all_official, key=lambda x: x[1]) if all_official else None
    ens_peak = max(data.ens_mean, key=lambda x: x[1]) if data.ens_mean else None
    rain_sum = sum(r["precip"] for r in data.wx_rows)

    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "figure.facecolor": COL_BG,
            "axes.facecolor": "white",
            "axes.edgecolor": "#D1D5DB",
            "grid.color": COL_GRID,
        }
    )
    fig = plt.figure(figsize=(16, 11.4), dpi=140, facecolor=COL_BG)
    gs = fig.add_gridspec(
        3,
        1,
        height_ratios=[6.6, 1.45, 2.15],
        hspace=0.12,
        left=0.07,
        right=0.93,
        top=0.90,
        bottom=0.07,
    )
    ax = fig.add_subplot(gs[0])
    axm = fig.add_subplot(gs[1], sharex=ax)
    axp = fig.add_subplot(gs[2], sharex=ax)

    # --- Panel 1: Temperatur ---
    ax.axvspan(peak0, peak1, color=COL_PEAK, alpha=0.45, zorder=0, clip_on=True)
    ax.axvline(peak0, color="#D97706", ls="--", lw=0.9, alpha=0.55)
    ax.axvline(peak1, color="#D97706", ls="--", lw=0.9, alpha=0.55)

    for i, series in enumerate(data.ens_members):
        ax.plot(
            [t for t, _ in series],
            [v for _, v in series],
            color=ENS_COLORS[i % len(ENS_COLORS)],
            lw=0.5,
            alpha=0.18,
            zorder=2,
            clip_on=True,
        )
    if data.ens_mean:
        ax.plot(
            [t for t, _ in data.ens_mean],
            [v for _, v in data.ens_mean],
            color=COL_MEAN,
            lw=2.6,
            ls="--",
            zorder=4,
            clip_on=True,
        )
    if data.hrrr:
        ax.plot(
            [t for t, _ in data.hrrr],
            [v for _, v in data.hrrr],
            color=COL_HRRR,
            lw=2.2,
            zorder=5,
            clip_on=True,
        )
    if data.madis:
        ax.plot(
            [t for t, _ in data.madis],
            [v for _, v in data.madis],
            color=COL_MADIS,
            lw=1.5,
            zorder=6,
            drawstyle="steps-post",
            clip_on=True,
        )
    if all_official:
        ax.plot(
            [t for t, _ in all_official],
            [v for _, v in all_official],
            color=COL_METAR,
            lw=1.4,
            zorder=7,
            alpha=0.85,
            clip_on=True,
        )
    if data.metar:
        ax.scatter(
            [t for t, _ in data.metar],
            [v for _, v in data.metar],
            s=48,
            marker="s",
            color=COL_METAR,
            zorder=9,
            edgecolors="white",
            linewidths=0.4,
            clip_on=True,
        )
    if data.speci:
        ax.scatter(
            [t for t, _ in data.speci],
            [v for _, v in data.speci],
            s=55,
            marker="D",
            color=COL_SPECI,
            zorder=9,
            edgecolors="white",
            linewidths=0.4,
            clip_on=True,
        )

    ax_r = ax.twinx()
    ptimes = [r["t"] for r in data.wx_rows]
    precip = [r["precip"] for r in data.wx_rows]
    pop = [r["pop"] for r in data.wx_rows]
    centered = [t - timedelta(minutes=6) for t in ptimes]
    ax_r.bar(
        centered,
        precip,
        width=timedelta(minutes=12),
        color=COL_RAIN,
        alpha=0.40,
        zorder=1,
        align="edge",
        clip_on=True,
    )
    rmax = max(precip) if precip else 0
    ax_r.set_ylim(0, max(4.0, rmax * 5 if rmax > 0 else 4.0))
    ax_r.set_ylabel("Niederschlag mm/15min", color="#1D4ED8", fontsize=8)
    ax_r.tick_params(axis="y", labelsize=7, colors="#1D4ED8")
    for pt, pr, po in zip(ptimes, precip, pop):
        if pr >= 0.3 or (pr > 0 and po >= 20):
            ax_r.text(
                pt,
                pr + 0.05,
                f"{pr:.1f}\n{po:.0f}%",
                ha="center",
                va="bottom",
                fontsize=6.5,
                color="#1E40AF",
                linespacing=0.9,
                clip_on=True,
            )

    if xl0 <= now_local < xl1:
        ax.axvline(now_local, color=COL_NOW, lw=1.4, ls="-.", zorder=8)
        ax.text(
            now_local,
            0.02,
            "Jetzt",
            transform=ax.get_xaxis_transform(),
            color=COL_NOW,
            fontsize=8,
            fontweight="bold",
            ha="center",
            va="bottom",
            bbox=dict(
                boxstyle="round,pad=0.2", fc="white", ec="#9CA3AF", alpha=0.92
            ),
            zorder=12,
        )

    ax.set_ylabel("Temperatur (°C)", fontsize=10)
    ax2 = ax.secondary_yaxis("right", functions=(c_to_f, f_to_c))
    ax2.spines["right"].set_position(("axes", 1.08))
    ax2.set_ylabel("Temperatur (°F)", fontsize=10)
    ax.grid(True, alpha=0.55, lw=0.6)
    ax.set_xlabel(f"Ortszeit {station.label_tz} ({station.city})", fontsize=9)

    all_t = (
        [v for _, v in data.madis]
        + [v for _, v in all_official]
        + [v for _, v in data.hrrr]
        + [v for _, v in data.ens_mean]
    )
    ax.set_ylim(
        (min(all_t) - 1.5) if all_t else 20,
        (max(all_t) + 2.0) if all_t else 42,
    )

    box = (
        "Letzte Obs / Peaks\n"
        f"MADIS last   {fmt_obs(madis_last)}\n"
        f"MADIS max    {fmt_obs(madis_max)}\n"
        f"METAR-Max    {fmt_obs(metar_max)}\n"
        f"Ens-Peak     {fmt_obs(ens_peak)}\n"
        f"Regen Σ      {rain_sum:.1f} mm"
    )
    ax.text(
        0.995,
        0.97,
        box,
        transform=ax.transAxes,
        ha="right",
        va="top",
        fontsize=8,
        family="DejaVu Sans Mono",
        bbox=dict(
            boxstyle="round,pad=0.45", fc="white", ec="#D1D5DB", alpha=0.95
        ),
        zorder=15,
    )
    ax.legend(
        handles=[
            Rectangle(
                (0, 0),
                1,
                1,
                fc=COL_PEAK,
                alpha=0.55,
                ec="#D97706",
                label=f"typ. Peakfenster {PEAK_START_HOUR}–{PEAK_END_HOUR} {station.label_tz}",
            ),
            Line2D([0], [0], color=COL_NOW, lw=1.4, ls="-.", label="Jetzt"),
            Line2D(
                [0], [0], color=COL_MEAN, lw=2.4, ls="--", label="Ensemble-Mittel (ICON)"
            ),
            Line2D(
                [0],
                [0],
                color=COL_HRRR,
                lw=2.2,
                label="Open-Meteo best_match (HRRR)",
            ),
            Line2D([0], [0], color=COL_MADIS, lw=1.5, label="MADIS 5-min"),
            Line2D(
                [0],
                [0],
                color=COL_METAR,
                lw=1.4,
                marker="s",
                markersize=7,
                label="METAR",
            ),
            Line2D(
                [0],
                [0],
                color=COL_SPECI,
                lw=0,
                marker="D",
                markersize=7,
                label="SPECI",
            ),
            Rectangle((0, 0), 1, 1, fc=COL_RAIN, alpha=0.45, label="HRRR precip mm"),
        ],
        loc="upper left",
        fontsize=7.5,
        framealpha=0.95,
        edgecolor="#D1D5DB",
    )

    months_de = [
        "Jan", "Feb", "Mär", "Apr", "Mai", "Jun",
        "Jul", "Aug", "Sep", "Okt", "Nov", "Dez",
    ]
    fig.suptitle(
        f"{station.stid} {station.city} {day.day}. {months_de[day.month - 1]} {day.year}"
        f" — Tagesverlauf  |  Stand {cet:%H:%M} CET / {now_local:%H:%M} {station.label_tz}",
        fontsize=13,
        fontweight="bold",
        y=0.965,
        color="#1F2937",
    )
    ax.set_title(
        f"Settlement = WU ← METAR/SPECI   ·   MADIS ≠ Settlement"
        f"   ·   nur {station.label_tz}-Tag 00:00–24:00",
        fontsize=8.5,
        color="#6B7280",
        pad=6,
    )
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{x:.1f}"))

    # --- Panel 2: Wolken / Wind ---
    axm.set_ylim(0, 2)
    axm.set_yticks([0.55, 1.45])
    axm.set_yticklabels(["Wind", "Wolken"], fontsize=8)
    axm.grid(True, axis="x", which="major", alpha=0.35)
    axm.xaxis.set_major_locator(mdates.HourLocator(interval=2))
    axm.xaxis.set_minor_locator(mdates.HourLocator(interval=1))
    axm.grid(True, axis="x", which="minor", alpha=0.15)
    axm.tick_params(labelbottom=False)
    for spine in axm.spines.values():
        spine.set_color("#D1D5DB")
    axm.plot([xl0, xl1 - timedelta(minutes=1)], [-1, -1], alpha=0)

    for row in data.meta_hours:
        t = row["t"]
        xnum = mdates.date2num(t)
        axm.add_artist(
            AnnotationBbox(
                make_cloud_da(row["cloud"]),
                (xnum, 1.45),
                xycoords="data",
                frameon=False,
                box_alignment=(0.5, 0.5),
                pad=0,
                annotation_clip=True,
            )
        )
        spd, direction = row["wspd"], row["wdir"]
        if spd is None or direction is None:
            continue
        to_rad = math.radians(direction + 180)
        dx = math.sin(to_rad) * 0.012
        dy = math.cos(to_rad) * 0.28
        col = wind_color(spd)
        axm.annotate(
            "",
            xy=(xnum + dx, 0.55 + dy),
            xytext=(xnum - dx, 0.55 - dy),
            arrowprops=dict(arrowstyle="-|>", color=col, lw=1.7, mutation_scale=11),
            zorder=5,
            annotation_clip=True,
        )
        axm.text(
            xnum,
            0.10,
            f"{spd:.0f}",
            ha="center",
            va="bottom",
            fontsize=6.5,
            color=col,
            fontweight="bold",
            clip_on=True,
        )
    axm.text(
        1.005,
        0.5,
        "km/h",
        transform=axm.transAxes,
        ha="left",
        va="center",
        fontsize=7,
        color="#6B7280",
    )

    # --- Panel 3: Polymarket ---
    axp.plot([xl0, xl1 - timedelta(minutes=1)], [-1, -1], alpha=0)
    axp.set_ylim(0, 1)
    axp.set_ylabel("Polymarket", fontsize=9)
    axp.set_xlabel(f"Ortszeit {station.label_tz} ({station.city})", fontsize=9)
    axp.set_yticks([])
    axp.grid(True, axis="x", alpha=0.35)
    axp.set_title(
        f"Polymarket {station.city} High — Stand {now_local:%H:%M} {station.label_tz}",
        fontsize=10,
        loc="left",
        pad=4,
    )
    items = [r for r in data.pm if r["yes"] >= 0.3][:8] or data.pm[:6]
    n = len(items)
    if n:
        total_h, gap = 0.82, 0.012
        bh = (total_h - gap * (n - 1)) / n
        y0 = 0.09
        xmin, xmax = mdates.date2num(xl0), mdates.date2num(xl1)
        span = xmax - xmin
        for i, row in enumerate(items):
            y = y0 + (n - 1 - i) * (bh + gap)
            width = span * (row["yes"] / 100.0) * 0.92
            color = PM_COLORS[min(i, len(PM_COLORS) - 1)]
            axp.add_patch(
                FancyBboxPatch(
                    (xmin + span * 0.01, y),
                    max(width, span * 0.008),
                    bh,
                    boxstyle="round,pad=0.002,rounding_size=0.001",
                    facecolor=color,
                    edgecolor="white",
                    alpha=0.88,
                    linewidth=0.5,
                    clip_on=True,
                )
            )
            axp.text(
                xmin + span * 0.015,
                y + bh / 2,
                f"{row['label']}  {row['yes']:.1f}%",
                va="center",
                ha="left",
                fontsize=8,
                color="white",
                fontweight="bold",
            )
    axp.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    axp.xaxis.set_major_locator(mdates.HourLocator(interval=2))

    for a in (ax, axm, axp, ax_r):
        a.set_xlim(xl0, xl1)
        a.set_autoscalex_on(False)

    leg_ax = fig.add_axes([0.78, 0.335, 0.175, 0.04])
    leg_ax.set_facecolor(COL_BG)
    leg_ax.axis("off")
    leg_ax.set_xlim(0, 6)
    leg_ax.set_ylim(0, 1)
    for i, (spd, lab) in enumerate(
        [(5, "<10"), (15, "10–20"), (25, "20–30"), (35, "30–40"), (45, "40–50"), (55, "≥50")]
    ):
        leg_ax.annotate(
            "",
            xy=(i + 0.55, 0.58),
            xytext=(i + 0.15, 0.58),
            arrowprops=dict(
                arrowstyle="-|>", color=wind_color(spd), lw=1.4, mutation_scale=9
            ),
        )
        leg_ax.text(i + 0.35, 0.12, lab, ha="center", fontsize=5.5, color="#4B5563")
    leg_ax.set_title("Windstärke km/h", fontsize=6.5, pad=1, color="#6B7280")

    fig.text(
        0.5,
        0.015,
        f"Nur {station.label_tz}-Kalendertag 00:00–24:00  ·  "
        "Settlement = WU ← METAR/SPECI  ·  MADIS ≠ Settlement",
        ha="center",
        fontsize=8,
        color="#6B7280",
    )

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, bbox_inches="tight", facecolor=COL_BG)
    if also:
        also = Path(also)
        also.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(also, bbox_inches="tight", facecolor=COL_BG)
    plt.close(fig)
    return out


def resolve_station(key: str) -> Station:
    k = key.strip().upper()
    if k in STATIONS:
        return STATIONS[k]
    for st in STATIONS.values():
        if st.city.lower() == key.strip().lower() or st.pm_city == key.strip().lower():
            return st
    known = ", ".join(sorted(STATIONS))
    raise SystemExit(f"Unbekannte Station {key!r}. Bekannt: {known}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tagesverlauf-Template (Ensemble/MADIS/METAR/PM)"
    )
    parser.add_argument(
        "--station",
        "-s",
        default="KAUS",
        help="ICAO oder Stadt (Default: KAUS)",
    )
    parser.add_argument(
        "--day",
        help="Kalendertag YYYY-MM-DD in Stationszeitzone (Default: heute)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Ausgabe-PNG (Default: /opt/cursor/artifacts/{stid}_{day}_fullday.png)",
    )
    parser.add_argument(
        "--also",
        type=Path,
        help="Zusätzliche Kopie (z.B. austin_fullday.png)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Bekannte Stationen auflisten",
    )
    args = parser.parse_args()

    if args.list:
        for stid, st in sorted(STATIONS.items()):
            print(f"  {stid:5s}  {st.city:12s}  {st.tz_name}  pm={st.pm_city}")
        return 0

    station = resolve_station(args.station)
    day = date.fromisoformat(args.day) if args.day else None
    data = fetch_day_data(station, day)
    day = data.day
    out = args.out or (
        DEFAULT_OUT_DIR / f"{station.stid.lower()}_{day.isoformat()}_fullday.png"
    )
    also = args.also
    if also is None and station.stid == "KAUS":
        also = DEFAULT_OUT_DIR / "austin_fullday.png"

    path = render_station_day_chart(data, out, also=also)
    madis_last = data.madis[-1] if data.madis else None
    official = sorted(data.metar + data.speci)
    metar_max = max(official, key=lambda x: x[1]) if official else None
    print(f"Saved {path}")
    if also:
        print(f"Saved {also}")
    print(
        f"{station.stid} {day} | Stand {data.now_local:%H:%M} {station.label_tz} | "
        f"MADIS={fmt_obs(madis_last)} | METAR-Max={fmt_obs(metar_max)} | "
        f"PM top={data.pm[0]['label']} {data.pm[0]['yes']:.1f}%" if data.pm else "PM=—"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
