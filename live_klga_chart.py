#!/usr/bin/env python3
"""Interaktives / live-aktualisierendes KLGA-Diagramm (MADIS + METAR/SPECI + PM).

Einmalig HTML erzeugen:
  python3 live_klga_chart.py
  python3 live_klga_chart.py --out /tmp/klga_live.html

Live-Server (Browser auto-refresh alle --interval Sekunden):
  python3 live_klga_chart.py --serve --port 8765
  → http://127.0.0.1:8765/

Datenquellen: NOAA MADIS HFMETAR, aviationweather.gov, Open-Meteo HRRR,
Polymarket Gamma API. Ensemble-Kurve optional aus festem Tagesprofil (Screenshot).
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from poll_madis_hfmetar import USER_AGENT, fetch_madis_observations

ET = timezone(timedelta(hours=-4))
CET = timezone(timedelta(hours=2))
DEFAULT_OUT = Path("/opt/cursor/artifacts/klga_live.html")

# Digitalisiertes Ensemble-Mittel (Morgen-Screenshot 6. Aug 2026), stündlich °C
ENSEMBLE_C_BY_HOUR = [
    24.4, 24.0, 23.6, 23.2, 22.9, 22.7, 22.8, 23.5,
    24.8, 26.2, 27.8, 29.2, 30.3, 31.1, 31.5, 31.6,
    31.4, 30.8, 29.8, 28.6, 27.4, 26.5, 25.8, 25.2,
]


def http_json(url: str, timeout: int = 45):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def iso_et(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ET).strftime("%Y-%m-%dT%H:%M:%S")


def _pm_label(question: str, day: date) -> str:
    if "between" in question:
        return (
            question.split("between ")[-1]
            .replace(f" on {day.strftime('%B')} {day.day}?", "")
            .replace("°F", "")
            .strip()
        )
    if "below" in question.lower():
        return "79 or below"
    return question[-28:]


def fetch_yes_book_pressure(token_id: str, near_cents: float = 0.05) -> dict | None:
    """Orderbuch-Druck für YES-Token: Imbalance >0 = Bid/Kauf-Druck, <0 = Ask/Verkauf-Druck."""
    try:
        book = http_json(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=20)
    except Exception:  # noqa: BLE001
        return None
    bids = [(float(x["price"]), float(x["size"])) for x in (book.get("bids") or [])]
    asks = [(float(x["price"]), float(x["size"])) for x in (book.get("asks") or [])]
    if not bids or not asks:
        return None
    best_bid = max(bids, key=lambda x: x[0])
    best_ask = min(asks, key=lambda x: x[0])
    mid = (best_bid[0] + best_ask[0]) / 2.0
    spread = best_ask[0] - best_bid[0]
    bid_near = sum(s for p, s in bids if p >= mid - near_cents)
    ask_near = sum(s for p, s in asks if p <= mid + near_cents)
    total = bid_near + ask_near
    imbalance = ((bid_near - ask_near) / total) if total > 0 else 0.0
    # Top-of-book notional (price * size) within 10¢ of touch
    bid_notional = sum(p * s for p, s in bids if p >= best_bid[0] - 0.10)
    ask_notional = sum(p * s for p, s in asks if p <= best_ask[0] + 0.10)
    last_trade = book.get("last_trade_price")
    try:
        last_trade_f = float(last_trade) if last_trade is not None else None
    except (TypeError, ValueError):
        last_trade_f = None
    hist_t, hist_p = [], []
    momentum = None
    try:
        hist = http_json(
            f"https://clob.polymarket.com/prices-history?market={token_id}"
            f"&interval=1h&fidelity=1",
            timeout=20,
        )
        series = hist.get("history") or []
        for pt in series:
            hist_t.append(
                datetime.fromtimestamp(pt["t"], tz=timezone.utc)
                .astimezone(ET)
                .strftime("%Y-%m-%dT%H:%M:%S")
            )
            hist_p.append(round(float(pt["p"]) * 100, 2))
        if len(series) >= 2:
            momentum = round((float(series[-1]["p"]) - float(series[0]["p"])) * 100, 2)
    except Exception:  # noqa: BLE001
        pass
    return {
        "best_bid": round(best_bid[0], 3),
        "best_ask": round(best_ask[0], 3),
        "mid": round(mid, 3),
        "spread": round(spread, 3),
        "bid_depth_near": round(bid_near, 1),
        "ask_depth_near": round(ask_near, 1),
        "bid_notional_10c": round(bid_notional, 1),
        "ask_notional_10c": round(ask_notional, 1),
        "imbalance": round(imbalance, 3),
        "last_trade": last_trade_f,
        "momentum_1h_pp": momentum,
        "price_hist": {"t": hist_t, "p": hist_p},
    }


def collect_snapshot(day: date | None = None, madis_hours: int = 8) -> dict:
    day = day or datetime.now(ET).date()
    day_start = datetime(day.year, day.month, day.day, tzinfo=ET)
    now = datetime.now(timezone.utc)
    now_et = now.astimezone(ET)

    madis_rows = []
    for obs in fetch_madis_observations({"KLGA"}, hours_back=madis_hours):
        t = obs.observed_at
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        te = t.astimezone(ET)
        if te.date() != day or obs.air_temp_c is None:
            continue
        madis_rows.append((te, round(float(obs.air_temp_c), 1)))
    madis_rows.sort(key=lambda r: r[0])
    # dedupe by minute
    madis_dedup = {}
    for te, c in madis_rows:
        madis_dedup[te] = c
    madis_t = [iso_et(te) for te in sorted(madis_dedup)]
    madis_c = [madis_dedup[te] for te in sorted(madis_dedup)]

    metar_t, metar_c, speci_t, speci_c = [], [], [], []
    ms_pairs = []
    try:
        awc = http_json(
            "https://aviationweather.gov/api/data/metar?ids=KLGA&format=json&hours=18"
        )
        for row in awc:
            raw = row.get("rawOb") or ""
            if "MADISHF" in raw.upper():
                continue
            ot = row.get("obsTime")
            temp = row.get("temp")
            if ot is None or temp is None:
                continue
            te = datetime.fromtimestamp(ot, tz=timezone.utc).astimezone(ET)
            if te.date() != day:
                continue
            c = float(temp)
            stamp = iso_et(te)
            if raw.upper().startswith("SPECI"):
                kind = "SPECI"
            elif te.minute == 51:
                kind = "METAR"
            elif "AO2" in raw:
                kind = "SPECI"
            else:
                continue
            if kind == "METAR":
                metar_t.append(stamp)
                metar_c.append(round(c, 1))
            else:
                speci_t.append(stamp)
                speci_c.append(round(c, 1))
            ms_pairs.append((te, c, kind))
    except Exception as exc:  # noqa: BLE001
        awc_error = str(exc)
    else:
        awc_error = None

    om_t, om_c = [], []
    try:
        om = http_json(
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude=40.7772&longitude=-73.8726&hourly=temperature_2m"
            f"&timezone=America%2FNew_York&start_date={day.isoformat()}"
            f"&end_date={day.isoformat()}&models=best_match"
        )
        for ts, temp in zip(om["hourly"]["time"], om["hourly"]["temperature_2m"]):
            if temp is None:
                continue
            om_t.append(ts)
            om_c.append(round(float(temp), 1))
    except Exception as exc:  # noqa: BLE001
        om_error = str(exc)
    else:
        om_error = None

    pm = []
    try:
        month = day.strftime("%B").lower()
        slug = f"highest-temperature-in-nyc-on-{month}-{day.day}-{day.year}"
        events = http_json(f"https://gamma-api.polymarket.com/events?slug={slug}")
        markets = (events[0].get("markets") or []) if events else []
        if markets and isinstance(markets[0], str):
            markets = [
                http_json(f"https://gamma-api.polymarket.com/markets/{mid}")
                for mid in markets
            ]
        for m in markets:
            q = m.get("question") or m.get("groupItemTitle") or "?"
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            p = float(prices[0]) if prices else float(m.get("lastTradePrice") or 0)
            tokens = m.get("clobTokenIds")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            yes_token = tokens[0] if tokens else None
            pm.append(
                {
                    "label": _pm_label(q, day),
                    "yes": round(p * 100, 1),
                    "yes_token": yes_token,
                }
            )
        pm.sort(key=lambda x: -x["yes"])
        # Orderbuch-Druck für die Top-Buckets (CLOB public book)
        for row in pm[:5]:
            tok = row.get("yes_token")
            if not tok:
                row["pressure"] = None
                continue
            row["pressure"] = fetch_yes_book_pressure(tok)
    except Exception as exc:  # noqa: BLE001
        pm_error = str(exc)
    else:
        pm_error = None

    ens_t = [
        iso_et(day_start + timedelta(hours=h)) for h in range(24)
    ]
    ens_c = list(ENSEMBLE_C_BY_HOUR)

    ms_max = max(ms_pairs, key=lambda r: r[1]) if ms_pairs else None
    madis_max_c = max(madis_c) if madis_c else None
    madis_max_t = None
    if madis_max_c is not None:
        # last occurrence of max
        for te_iso, c in zip(reversed(madis_t), reversed(madis_c)):
            if c == madis_max_c:
                madis_max_t = te_iso
                break

    # Live-Druck-Historie: Ringpuffer je Bucket (für Serve-Mode)
    pressure_hist = getattr(collect_snapshot, "_pressure_hist", {})
    stamp = now_et.strftime("%Y-%m-%dT%H:%M:%S")
    for row in pm[:5]:
        label = row["label"]
        pr = row.get("pressure") or {}
        series = pressure_hist.setdefault(label, {"t": [], "imb": [], "mid": []})
        series["t"].append(stamp)
        series["imb"].append(pr.get("imbalance"))
        series["mid"].append(round(pr["mid"] * 100, 1) if pr.get("mid") is not None else None)
        # keep last ~90 samples (~1.5h @ 60s)
        for k in ("t", "imb", "mid"):
            series[k] = series[k][-90:]
    collect_snapshot._pressure_hist = pressure_hist

    return {
        "generated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_et": now_et.strftime("%Y-%m-%d %H:%M ET"),
        "generated_at_cet": now.astimezone(CET).strftime("%Y-%m-%d %H:%M CET"),
        "day": day.isoformat(),
        "now_et": iso_et(now_et),
        "madis": {"t": madis_t, "c": madis_c},
        "metar": {"t": metar_t, "c": metar_c},
        "speci": {"t": speci_t, "c": speci_c},
        "hrrr": {"t": om_t, "c": om_c},
        "ensemble": {"t": ens_t, "c": ens_c},
        "pm": pm[:10],
        "pressure_hist": pressure_hist,
        "stats": {
            "metar_max_f": round(c_to_f(ms_max[1]), 1) if ms_max else None,
            "metar_max_et": ms_max[0].strftime("%H:%M") if ms_max else None,
            "metar_max_kind": ms_max[2] if ms_max else None,
            "madis_max_f": round(c_to_f(madis_max_c), 1) if madis_max_c is not None else None,
            "madis_max_et": madis_max_t[11:16] if madis_max_t else None,
            "last_madis_f": round(c_to_f(madis_c[-1]), 1) if madis_c else None,
            "last_madis_et": madis_t[-1][11:16] if madis_t else None,
        },
        "errors": {
            "awc": awc_error,
            "open_meteo": om_error,
            "polymarket": pm_error,
        },
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>KLGA Live — MADIS / METAR / Polymarket</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  :root {
    --bg: #f4f1ea;
    --card: #fffdf8;
    --ink: #1c1917;
    --muted: #78716c;
    --line: #e7e5e4;
    --accent: #0f766e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: "Segoe UI", system-ui, sans-serif;
    background: radial-gradient(1200px 600px at 10% -10%, #dbeafe 0%, transparent 55%),
                radial-gradient(900px 500px at 100% 0%, #ffedd5 0%, transparent 50%),
                var(--bg);
    color: var(--ink);
  }
  header {
    display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline;
    justify-content: space-between; padding: 16px 20px 8px;
  }
  h1 { font-size: 1.25rem; margin: 0; letter-spacing: -0.02em; }
  .meta { color: var(--muted); font-size: 0.9rem; }
  .pill {
    display: inline-flex; gap: 8px; align-items: center; flex-wrap: wrap;
    padding: 6px 10px; border-radius: 999px; background: var(--card);
    border: 1px solid var(--line); font-size: 0.85rem;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #16a34a; }
  .dot.off { background: #a8a29e; }
  main { padding: 0 12px 24px; display: grid; gap: 12px; }
  .card {
    background: var(--card); border: 1px solid var(--line);
    border-radius: 14px; padding: 8px; box-shadow: 0 1px 0 rgba(0,0,0,.04);
  }
  #temp { height: 520px; }
  #pm { height: 320px; }
  #pressureImb { height: 300px; }
  #pressureDepth { height: 280px; }
  #pressurePrice { height: 280px; }
  .press-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
  }
  @media (max-width: 900px) { .press-grid { grid-template-columns: 1fr; } }
  .card h2 {
    margin: 4px 8px 0; font-size: 0.95rem; font-weight: 600;
  }
  .card .hint {
    margin: 2px 8px 6px; color: var(--muted); font-size: 0.78rem;
  }
  .stats {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 8px; padding: 4px 8px 12px;
  }
  .stat {
    background: #fafaf9; border: 1px solid var(--line); border-radius: 10px;
    padding: 10px 12px;
  }
  .stat b { display: block; font-size: 1.05rem; margin-top: 2px; }
  .stat span { color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: .04em; }
  footer { padding: 0 20px 24px; color: var(--muted); font-size: 0.8rem; }
  button {
    border: 1px solid var(--line); background: white; border-radius: 8px;
    padding: 6px 10px; cursor: pointer; font: inherit;
  }
  button:hover { border-color: var(--accent); color: var(--accent); }
</style>
</head>
<body>
<header>
  <div>
    <h1>KLGA Live — Ensemble · MADIS · METAR/SPECI · Polymarket</h1>
    <div class="meta" id="subtitle">lädt…</div>
  </div>
  <div class="pill">
    <span class="dot" id="liveDot"></span>
    <span id="liveLabel">Live</span>
    <button type="button" id="refreshBtn">Jetzt aktualisieren</button>
  </div>
</header>
<main>
  <section class="card">
    <div class="stats" id="stats"></div>
    <div id="temp"></div>
  </section>
  <section class="card">
    <div id="pm"></div>
  </section>
  <section class="card">
    <h2>Polymarket Buy / Sell Druck</h2>
    <p class="hint">
      Orderbuch-Imbalance (±5¢ um Mid): positiv = Bid/Kauf-Druck, negativ = Ask/Verkauf-Druck.
      Depth = Shares nahe Touch (±10¢). Momentum = Mid-Veränderung letzte Stunde (pp).
    </p>
    <div id="pressureImb"></div>
    <div class="press-grid">
      <div id="pressureDepth"></div>
      <div id="pressurePrice"></div>
    </div>
  </section>
</main>
<footer>
  Quellen: NOAA MADIS HFMETAR, aviationweather.gov, Open-Meteo best_match (HRRR),
  Polymarket Gamma + CLOB Orderbuch.
  Settlement = WU/METAR/SPECI, nicht MADIS. Auto-Refresh wenn unter <code>--serve</code>.
</footer>
<script>
const BOOT = __BOOT_JSON__;
const USE_API = __USE_API__;
const INTERVAL_MS = __INTERVAL_MS__;

function fFromC(c) { return c * 9/5 + 32; }

function renderStats(d) {
  const s = d.stats || {};
  const cells = [
    ["METAR/SPECI-Max", s.metar_max_f != null ? `${s.metar_max_f}°F @ ${s.metar_max_et}` : "—"],
    ["MADIS-Max", s.madis_max_f != null ? `${s.madis_max_f}°F @ ${s.madis_max_et}` : "—"],
    ["Letzte MADIS", s.last_madis_f != null ? `${s.last_madis_f}°F @ ${s.last_madis_et}` : "—"],
    ["Stand", d.generated_at_cet || d.generated_at_et || "—"],
  ];
  document.getElementById("stats").innerHTML = cells.map(([k,v]) =>
    `<div class="stat"><span>${k}</span><b>${v}</b></div>`
  ).join("");
}

function tempTraces(d) {
  const traces = [];
  if (d.ensemble?.t?.length) {
    traces.push({
      x: d.ensemble.t, y: d.ensemble.c, name: "Ensemble (~mean)",
      mode: "lines", line: {dash: "dash", width: 2, color: "#2563eb"}
    });
  }
  if (d.hrrr?.t?.length) {
    traces.push({
      x: d.hrrr.t, y: d.hrrr.c, name: "Open-Meteo HRRR",
      mode: "lines", line: {width: 2, color: "#ea580c"}
    });
  }
  if (d.madis?.t?.length) {
    traces.push({
      x: d.madis.t, y: d.madis.c, name: "MADIS 5-min",
      mode: "lines", line: {width: 1.5, color: "#78716c"},
      hovertemplate: "%{x|%H:%M}  %{y:.1f}°C / %{customdata:.1f}°F<extra>MADIS</extra>",
      customdata: d.madis.c.map(fFromC)
    });
  }
  const msT = [...(d.metar?.t||[]), ...(d.speci?.t||[])];
  const msC = [...(d.metar?.c||[]), ...(d.speci?.c||[])];
  // sort by time for line
  const pairs = msT.map((t,i)=>({t,c:msC[i]})).sort((a,b)=>a.t.localeCompare(b.t));
  if (pairs.length) {
    traces.push({
      x: pairs.map(p=>p.t), y: pairs.map(p=>p.c), name: "METAR/SPECI",
      mode: "lines", line: {width: 2.5, color: "#6d28d9"},
      hovertemplate: "%{x|%H:%M}  %{y:.1f}°C / %{customdata:.1f}°F<extra>MS</extra>",
      customdata: pairs.map(p=>fFromC(p.c))
    });
  }
  if (d.metar?.t?.length) {
    traces.push({
      x: d.metar.t, y: d.metar.c, name: "METAR",
      mode: "markers", marker: {symbol: "square", size: 9, color: "#6d28d9"}
    });
  }
  if (d.speci?.t?.length) {
    traces.push({
      x: d.speci.t, y: d.speci.c, name: "SPECI",
      mode: "markers", marker: {symbol: "diamond", size: 10, color: "#db2777"}
    });
  }
  if (d.now_et) {
    traces.push({
      x: [d.now_et, d.now_et], y: [20, 40], name: "Jetzt",
      mode: "lines", line: {dash: "dot", width: 1.5, color: "#111"},
      hoverinfo: "skip"
    });
  }
  // 14:51 marker for the day
  if (d.day) {
    const m1451 = d.day + "T14:51:00";
    traces.push({
      x: [m1451, m1451], y: [20, 40], name: "14:51",
      mode: "lines", line: {dash: "dot", width: 1.5, color: "#dc2626"},
      hoverinfo: "skip"
    });
  }
  return traces;
}

function renderTemp(d) {
  const layout = {
    margin: {t: 30, r: 50, b: 40, l: 50},
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    legend: {orientation: "h", y: 1.12},
    xaxis: {title: "Ortszeit ET", type: "date"},
    yaxis: {title: "°C", side: "left"},
    yaxis2: {
      title: "°F", overlaying: "y", side: "right",
      tickvals: [70,75,80,85,90,95].map(f => (f-32)*5/9),
      ticktext: ["70","75","80","85","90","95"]
    },
    shapes: [{
      type: "rect", xref: "x", yref: "paper",
      x0: d.day + "T14:00:00", x1: d.day + "T16:00:00",
      y0: 0, y1: 1, fillcolor: "rgba(37,99,235,0.08)", line: {width: 0}
    }],
    hovermode: "x unified"
  };
  // attach yaxis2 to HRRR silently by duplicating? skip — secondary ticks via tickvals mapping
  Plotly.react("temp", tempTraces(d), layout, {responsive: true, displayModeBar: true});
}

function renderPm(d) {
  const pm = (d.pm || []).slice(0, 8);
  const layout = {
    margin: {t: 36, r: 40, b: 40, l: 90},
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    title: {text: "Polymarket NYC High — Yes %", font: {size: 14}},
    xaxis: {title: "Yes %", range: [0, Math.max(40, ...pm.map(p=>p.yes)) * 1.25]},
    yaxis: {autorange: "reversed"}
  };
  const colors = pm.map(p => p.yes >= 30 ? "#16a34a" : p.yes >= 15 ? "#ea580c" : "#a8a29e");
  Plotly.react("pm", [{
    type: "bar", orientation: "h",
    y: pm.map(p => p.label), x: pm.map(p => p.yes),
    marker: {color: colors},
    text: pm.map(p => p.yes.toFixed(1) + "%"),
    textposition: "outside",
    hovertemplate: "%{y}: %{x:.1f}%<extra></extra>"
  }], layout, {responsive: true, displayModeBar: false});
}

function topPressure(d) {
  return (d.pm || []).filter(p => p.pressure).slice(0, 5);
}

function renderPressure(d) {
  const rows = topPressure(d);
  const base = {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    margin: {t: 36, r: 24, b: 40, l: 90}
  };
  if (!rows.length) {
    Plotly.react("pressureImb", [], {...base, title: {text: "Kein Orderbuch (CLOB)", font:{size:13}}});
    Plotly.react("pressureDepth", [], base);
    Plotly.react("pressurePrice", [], base);
    return;
  }
  const labels = rows.map(r => r.label);
  const imb = rows.map(r => +(r.pressure.imbalance * 100).toFixed(1));
  const mom = rows.map(r => r.pressure.momentum_1h_pp);
  const bidD = rows.map(r => r.pressure.bid_depth_near);
  const askD = rows.map(r => r.pressure.ask_depth_near);
  const colors = imb.map(v => v > 8 ? "#16a34a" : v < -8 ? "#dc2626" : "#a8a29e");

  Plotly.react("pressureImb", [{
    type: "bar", orientation: "h",
    y: labels, x: imb, marker: {color: colors},
    customdata: rows.map(r => [
      (r.pressure.best_bid*100).toFixed(1),
      (r.pressure.best_ask*100).toFixed(1),
      (r.pressure.spread*100).toFixed(1),
      r.pressure.momentum_1h_pp
    ]),
    text: imb.map((v,i) => {
      const m = mom[i];
      const ms = m == null ? "" : ` · 1h ${m>=0?"+":""}${m}pp`;
      return `${v>0?"+":""}${v}%${ms}`;
    }),
    textposition: "outside",
    hovertemplate:
      "%{y}<br>Imbalance %{x:.1f}%<br>Bid %{customdata[0]}¢ / Ask %{customdata[1]}¢" +
      "<br>Spread %{customdata[2]}¢<br>1h Mom %{customdata[3]}pp<extra></extra>"
  }], {
    ...base,
    title: {text: "Orderbuch-Imbalance (Kauf ← → Verkauf)", font: {size: 13}},
    xaxis: {title: "Imbalance % (Bid−Ask)/(Bid+Ask)", range: [-100, 100], zeroline: true},
    yaxis: {autorange: "reversed"},
    shapes: [{
      type: "line", x0: 0, x1: 0, y0: -0.5, y1: labels.length - 0.5,
      line: {color: "#78716c", width: 1, dash: "dot"}
    }]
  }, {responsive: true, displayModeBar: false});

  Plotly.react("pressureDepth", [
    {
      type: "bar", orientation: "h", name: "Bid-Depth (±5¢)",
      y: labels, x: bidD, marker: {color: "#16a34a"},
      hovertemplate: "%{y} Bid %{x:.0f}<extra></extra>"
    },
    {
      type: "bar", orientation: "h", name: "Ask-Depth (±5¢)",
      y: labels, x: askD.map(v => -v), marker: {color: "#dc2626"},
      hovertemplate: "%{y} Ask %{customdata:.0f}<extra></extra>",
      customdata: askD
    }
  ], {
    ...base,
    title: {text: "Near-Touch Depth (Shares)", font: {size: 13}},
    barmode: "relative",
    xaxis: {title: "← Ask  |  Bid →"},
    yaxis: {autorange: "reversed"},
    legend: {orientation: "h", y: 1.15}
  }, {responsive: true, displayModeBar: false});

  // Preis-Sparklines: CLOB 1h history + Live-Ringpuffer wenn vorhanden
  const priceTraces = [];
  const hist = d.pressure_hist || {};
  rows.forEach((r, i) => {
    const ph = r.pressure.price_hist || {};
    if (ph.t && ph.t.length) {
      priceTraces.push({
        x: ph.t, y: ph.p, name: r.label + " (1h)",
        mode: "lines", line: {width: 2},
        hovertemplate: "%{y:.1f}¢<extra>" + r.label + "</extra>"
      });
    }
    const live = hist[r.label];
    if (live && live.t && live.t.length > 1) {
      priceTraces.push({
        x: live.t, y: live.mid, name: r.label + " (live mid)",
        mode: "lines+markers", line: {width: 1.5, dash: "dot"},
        marker: {size: 4},
        hovertemplate: "%{y:.1f}¢ mid<extra>" + r.label + "</extra>"
      });
    }
  });
  Plotly.react("pressurePrice", priceTraces, {
    ...base,
    title: {text: "Yes-Preis (¢) — 1h History / Live-Mid", font: {size: 13}},
    xaxis: {type: "date", title: "ET"},
    yaxis: {title: "¢"},
    legend: {orientation: "h", y: 1.18, font: {size: 10}},
    margin: {t: 40, r: 20, b: 40, l: 45}
  }, {responsive: true, displayModeBar: false});
}

function renderAll(d) {
  document.getElementById("subtitle").textContent =
    `${d.day} · Stand ${d.generated_at_cet} / ${d.generated_at_et}`;
  renderStats(d);
  renderTemp(d);
  renderPm(d);
  renderPressure(d);
}

async function load() {
  const dot = document.getElementById("liveDot");
  const label = document.getElementById("liveLabel");
  try {
    let d = BOOT;
    if (USE_API) {
      const res = await fetch("/api/snapshot?" + Date.now());
      if (!res.ok) throw new Error("HTTP " + res.status);
      d = await res.json();
    }
    renderAll(d);
    dot.classList.remove("off");
    label.textContent = USE_API ? "Live · Auto-Refresh" : "Snapshot (statisch)";
  } catch (e) {
    dot.classList.add("off");
    label.textContent = "Fehler: " + e.message;
  }
}

document.getElementById("refreshBtn").addEventListener("click", load);
load();
if (USE_API && INTERVAL_MS > 0) setInterval(load, INTERVAL_MS);
</script>
</body>
</html>
"""


def render_html(snapshot: dict, *, use_api: bool, interval_ms: int) -> str:
    return (
        HTML_TEMPLATE.replace("__BOOT_JSON__", json.dumps(snapshot))
        .replace("__USE_API__", "true" if use_api else "false")
        .replace("__INTERVAL_MS__", str(int(interval_ms)))
    )


def write_html(path: Path, snapshot: dict, *, use_api: bool = False, interval_s: int = 60) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_html(snapshot, use_api=use_api, interval_ms=interval_s * 1000),
        encoding="utf-8",
    )
    return path


class _State:
    def __init__(self, interval_s: int, madis_hours: int):
        self.interval_s = interval_s
        self.madis_hours = madis_hours
        self.lock = threading.Lock()
        self.snapshot = collect_snapshot(madis_hours=madis_hours)
        self.last_error: str | None = None

    def refresh(self) -> dict:
        try:
            snap = collect_snapshot(madis_hours=self.madis_hours)
            with self.lock:
                self.snapshot = snap
                self.last_error = None
            return snap
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.last_error = str(exc)
            raise

    def get(self) -> dict:
        with self.lock:
            return dict(self.snapshot)


def serve(host: str, port: int, interval_s: int, madis_hours: int) -> None:
    state = _State(interval_s, madis_hours)

    def refresher():
        while True:
            time.sleep(interval_s)
            try:
                state.refresh()
                print(f"[refresh] {state.get()['generated_at_et']}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[refresh error] {exc}", flush=True)

    threading.Thread(target=refresher, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            print(f"[http] {self.address_string()} {fmt % args}", flush=True)

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/snapshot"):
                try:
                    # light: return cached; optional ?refresh=1 forces
                    if "refresh=1" in self.path:
                        snap = state.refresh()
                    else:
                        snap = state.get()
                    body = json.dumps(snap).encode()
                    self._send(200, body, "application/json; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    body = json.dumps({"error": str(exc)}).encode()
                    self._send(500, body, "application/json; charset=utf-8")
                return
            if self.path in ("/", "/index.html", "/klga_live.html"):
                html = render_html(
                    state.get(), use_api=True, interval_ms=interval_s * 1000
                ).encode()
                self._send(200, html, "text/html; charset=utf-8")
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Live dashboard: http://{host}:{port}/  (refresh every {interval_s}s)", flush=True)
    httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="HTML-Ausgabepfad")
    parser.add_argument("--serve", action="store_true", help="HTTP-Server mit Auto-Refresh")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--interval", type=int, default=60, help="Sekunden zwischen Live-Updates")
    parser.add_argument("--madis-hours", type=int, default=8)
    args = parser.parse_args()

    if args.serve:
        serve(args.host, args.port, args.interval, args.madis_hours)
        return 0

    snap = collect_snapshot(madis_hours=args.madis_hours)
    path = write_html(args.out, snap, use_api=False, interval_s=args.interval)
    print(f"wrote {path}")
    print(f"stand {snap['generated_at_cet']} / {snap['generated_at_et']}")
    st = snap["stats"]
    print(
        f"METAR-Max {st['metar_max_f']}F @ {st['metar_max_et']} | "
        f"MADIS last {st['last_madis_f']}F @ {st['last_madis_et']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
