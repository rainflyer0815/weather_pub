#!/usr/bin/env python3
"""Interaktives / live-aktualisierendes KLGA-Diagramm (MADIS + METAR/SPECI + PM).

Einmalig HTML erzeugen:
  python3 live_klga_chart.py
  python3 live_klga_chart.py --out /tmp/klga_live.html

Live-Server (Wetter alle --interval s, Polymarket/CLOB alle --pm-interval s):
  python3 live_klga_chart.py --serve --port 8765 --pm-interval 2
  → http://127.0.0.1:8765/

Datenquellen: NOAA MADIS HFMETAR, aviationweather.gov, Open-Meteo HRRR,
Polymarket Gamma + CLOB (Yes-% = Orderbuch-Mid, BUY/SELL = Imbalance).
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


def fetch_yes_book_signal(
    token_id: str, near_cents: float = 0.05, threshold: float = 0.15
) -> dict | None:
    """BUY/SELL + Mid aus Orderbuch (±near_cents um Mid)."""
    try:
        book = http_json(f"https://clob.polymarket.com/book?token_id={token_id}", timeout=12)
    except Exception:  # noqa: BLE001
        return None
    bids = [(float(x["price"]), float(x["size"])) for x in (book.get("bids") or [])]
    asks = [(float(x["price"]), float(x["size"])) for x in (book.get("asks") or [])]
    if not bids or not asks:
        return None
    best_bid = max(bids, key=lambda x: x[0])[0]
    best_ask = min(asks, key=lambda x: x[0])[0]
    mid = (best_bid + best_ask) / 2.0
    bid_near = sum(s for p, s in bids if p >= mid - near_cents)
    ask_near = sum(s for p, s in asks if p <= mid + near_cents)
    total = bid_near + ask_near
    imbalance = ((bid_near - ask_near) / total) if total > 0 else 0.0
    if imbalance >= threshold:
        signal = "BUY"
    elif imbalance <= -threshold:
        signal = "SELL"
    else:
        signal = "FLAT"
    last_trade = book.get("last_trade_price")
    try:
        last_trade_f = float(last_trade) if last_trade is not None else None
    except (TypeError, ValueError):
        last_trade_f = None
    # Live-Yes: Mid, sonst Last-Trade
    live = mid if mid > 0 else last_trade_f
    return {
        "signal": signal,
        "imbalance": round(imbalance, 3),
        "best_bid": round(best_bid, 3),
        "best_ask": round(best_ask, 3),
        "mid": round(mid, 4),
        "last_trade": last_trade_f,
        "yes_live": round(live * 100, 1) if live is not None else None,
    }


def fetch_pm_markets(day: date) -> tuple[list[dict], str | None]:
    """Gamma-Markets inkl. Yes-Token (für Server-seitiges CLOB-Polling)."""
    month = day.strftime("%B").lower()
    slug = f"highest-temperature-in-nyc-on-{month}-{day.day}-{day.year}"
    try:
        events = http_json(f"https://gamma-api.polymarket.com/events?slug={slug}", timeout=20)
        markets = (events[0].get("markets") or []) if events else []
        if markets and isinstance(markets[0], str):
            markets = [
                http_json(f"https://gamma-api.polymarket.com/markets/{mid}", timeout=20)
                for mid in markets
            ]
        pm: list[dict] = []
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
                    "signal": None,
                    "imbalance": None,
                }
            )
        pm.sort(key=lambda x: -x["yes"])
        return pm, None
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)


def apply_clob_live(rows: list[dict], max_books: int = 8) -> list[dict]:
    """Yes-% aus CLOB-Mid + BUY/SELL parallel aktualisieren. Token bleiben erhalten."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out = [dict(r) for r in rows]
    jobs = [
        (i, r.get("yes_token"))
        for i, r in enumerate(out[:max_books])
        if r.get("yes_token")
    ]
    if not jobs:
        return out

    def _one(token: str):
        return fetch_yes_book_signal(token)

    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
        fut = {pool.submit(_one, tok): i for i, tok in jobs}
        for f in as_completed(fut):
            i = fut[f]
            try:
                book = f.result()
            except Exception:  # noqa: BLE001
                book = None
            if not book:
                continue
            if book.get("yes_live") is not None:
                out[i]["yes"] = book["yes_live"]
            out[i]["signal"] = book.get("signal")
            out[i]["imbalance"] = book.get("imbalance")
    out.sort(key=lambda x: -float(x.get("yes") or 0))
    return out


def public_pm(rows: list[dict]) -> list[dict]:
    """Client-Payload ohne Token-IDs."""
    clean = []
    for r in rows[:10]:
        clean.append(
            {
                "label": r.get("label"),
                "yes": r.get("yes"),
                "signal": r.get("signal"),
                "imbalance": r.get("imbalance"),
            }
        )
    return clean


def _now_stamps() -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    return now, now.astimezone(ET)


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

    pm_meta, pm_error = fetch_pm_markets(day)
    pm_meta = apply_clob_live(pm_meta)
    pm = public_pm(pm_meta)

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

    return {
        "generated_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_et": now_et.strftime("%Y-%m-%d %H:%M ET"),
        "generated_at_cet": now.astimezone(CET).strftime("%Y-%m-%d %H:%M CET"),
        "pm_live_et": now_et.strftime("%H:%M:%S ET"),
        "day": day.isoformat(),
        "now_et": iso_et(now_et),
        "madis": {"t": madis_t, "c": madis_c},
        "metar": {"t": metar_t, "c": metar_c},
        "speci": {"t": speci_t, "c": speci_c},
        "hrrr": {"t": om_t, "c": om_c},
        "ensemble": {"t": ens_t, "c": ens_c},
        "pm": pm,
        "_pm_meta": pm_meta,  # server-only; stripped before HTTP if present
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
  #pm { height: 360px; }
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
</main>
<footer>
  Quellen: NOAA MADIS HFMETAR, aviationweather.gov, Open-Meteo best_match (HRRR),
  Polymarket Gamma + CLOB (BUY/SELL = Orderbuch-Imbalance).
  Settlement = WU/METAR/SPECI, nicht MADIS. Auto-Refresh wenn unter <code>--serve</code>.
</footer>
<script>
const BOOT = __BOOT_JSON__;
const USE_API = __USE_API__;
const INTERVAL_MS = __INTERVAL_MS__;
const PM_INTERVAL_MS = __PM_INTERVAL_MS__;

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
  const sigColor = {BUY: "#16a34a", SELL: "#dc2626", FLAT: "#a8a29e"};
  const colors = pm.map(p => sigColor[p.signal] || "#d6d3d1");
  const texts = pm.map(p => {
    const pct = Number(p.yes).toFixed(1) + "%";
    return p.signal ? `${pct}  ${p.signal}` : pct;
  });
  const maxYes = Math.max(10, ...pm.map(p => Number(p.yes) || 0));
  const live = d.pm_live_et ? ` · live ${d.pm_live_et}` : "";
  const layout = {
    margin: {t: 40, r: 70, b: 40, l: 90},
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    title: {
      text: "Polymarket NYC High — Yes % (CLOB Mid)" + live,
      font: {size: 14}
    },
    xaxis: {
      title: "Yes %  ·  grün=BUY  rot=SELL  grau=FLAT",
      range: [0, maxYes * 1.35]
    },
    yaxis: {autorange: "reversed"},
    transition: {duration: 350, easing: "cubic-in-out"}
  };
  const trace = {
    type: "bar", orientation: "h",
    y: pm.map(p => p.label), x: pm.map(p => Number(p.yes) || 0),
    marker: {color: colors},
    text: texts,
    textposition: "outside",
    customdata: pm.map(p => {
      const imb = p.imbalance == null
        ? "—"
        : `${p.imbalance >= 0 ? "+" : ""}${Math.round(p.imbalance * 100)}%`;
      return [p.signal || "—", imb];
    }),
    hovertemplate:
      "%{y}<br>Yes %{x:.1f}% (CLOB Mid)<br>%{customdata[0]} · Imbalance %{customdata[1]}<extra></extra>"
  };
  Plotly.react("pm", [trace], layout, {responsive: true, displayModeBar: false});
}

function renderAll(d) {
  const pmBit = d.pm_live_et ? ` · PM ${d.pm_live_et}` : "";
  document.getElementById("subtitle").textContent =
    `${d.day} · Stand ${d.generated_at_cet} / ${d.generated_at_et}${pmBit}`;
  renderStats(d);
  renderTemp(d);
  renderPm(d);
}

let LAST = BOOT;

async function loadWeather() {
  const dot = document.getElementById("liveDot");
  const label = document.getElementById("liveLabel");
  try {
    let d = LAST;
    if (USE_API) {
      const res = await fetch("/api/snapshot?" + Date.now());
      if (!res.ok) throw new Error("HTTP " + res.status);
      d = await res.json();
    }
    LAST = d;
    renderAll(d);
    dot.classList.remove("off");
    label.textContent = USE_API
      ? `Live · Wetter ${Math.round(INTERVAL_MS/1000)}s · PM ${Math.round(PM_INTERVAL_MS/1000)}s`
      : "Snapshot (statisch)";
  } catch (e) {
    dot.classList.add("off");
    label.textContent = "Fehler: " + e.message;
  }
}

async function loadPm() {
  if (!USE_API) return;
  try {
    const res = await fetch("/api/pm?" + Date.now());
    if (!res.ok) throw new Error("HTTP " + res.status);
    const d = await res.json();
    LAST = {...LAST, pm: d.pm, pm_live_et: d.pm_live_et};
    const pmBit = d.pm_live_et ? ` · PM ${d.pm_live_et}` : "";
    document.getElementById("subtitle").textContent =
      `${LAST.day} · Stand ${LAST.generated_at_cet} / ${LAST.generated_at_et}${pmBit}`;
    renderPm(LAST);
    document.getElementById("liveDot").classList.remove("off");
  } catch (e) {
    document.getElementById("liveDot").classList.add("off");
  }
}

document.getElementById("refreshBtn").addEventListener("click", () => {
  loadWeather();
  loadPm();
});
loadWeather();
if (USE_API && INTERVAL_MS > 0) setInterval(loadWeather, INTERVAL_MS);
if (USE_API && PM_INTERVAL_MS > 0) setInterval(loadPm, PM_INTERVAL_MS);
</script>
</body>
</html>
"""


def _client_snapshot(snap: dict) -> dict:
    """HTTP-sichere Kopie ohne interne Token-Metadaten."""
    out = {k: v for k, v in snap.items() if not k.startswith("_")}
    return out


def render_html(
    snapshot: dict, *, use_api: bool, interval_ms: int, pm_interval_ms: int = 2000
) -> str:
    return (
        HTML_TEMPLATE.replace("__BOOT_JSON__", json.dumps(_client_snapshot(snapshot)))
        .replace("__USE_API__", "true" if use_api else "false")
        .replace("__INTERVAL_MS__", str(int(interval_ms)))
        .replace("__PM_INTERVAL_MS__", str(int(pm_interval_ms)))
    )


def write_html(
    path: Path,
    snapshot: dict,
    *,
    use_api: bool = False,
    interval_s: int = 60,
    pm_interval_s: float = 2.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_html(
            snapshot,
            use_api=use_api,
            interval_ms=interval_s * 1000,
            pm_interval_ms=int(pm_interval_s * 1000),
        ),
        encoding="utf-8",
    )
    return path


class _State:
    def __init__(self, interval_s: int, madis_hours: int, pm_interval_s: float):
        self.interval_s = interval_s
        self.madis_hours = madis_hours
        self.pm_interval_s = pm_interval_s
        self.lock = threading.Lock()
        self.snapshot = collect_snapshot(madis_hours=madis_hours)
        self.pm_meta = list(self.snapshot.pop("_pm_meta", []) or [])
        self.last_error: str | None = None
        self._gamma_every = max(1, int(15 / max(pm_interval_s, 0.5)))
        self._pm_ticks = 0

    def refresh(self) -> dict:
        try:
            snap = collect_snapshot(madis_hours=self.madis_hours)
            meta = list(snap.pop("_pm_meta", []) or [])
            with self.lock:
                self.snapshot = snap
                self.pm_meta = meta
                self.last_error = None
            return snap
        except Exception as exc:  # noqa: BLE001
            with self.lock:
                self.last_error = str(exc)
            raise

    def refresh_pm(self) -> dict:
        """Schnelles CLOB-Update; Gamma-Relist alle ~15s."""
        self._pm_ticks += 1
        day = datetime.now(ET).date()
        with self.lock:
            meta = [dict(r) for r in self.pm_meta]

        if self._pm_ticks == 1 or self._pm_ticks % self._gamma_every == 0:
            fresh, err = fetch_pm_markets(day)
            if fresh:
                # Token/Labels aus Gamma, Live-Yes kommt gleich aus CLOB
                by_label = {r["label"]: r for r in meta}
                for r in fresh:
                    old = by_label.get(r["label"])
                    if old and old.get("yes_token") and not r.get("yes_token"):
                        r["yes_token"] = old["yes_token"]
                meta = fresh
            elif err:
                print(f"[pm gamma] {err}", flush=True)

        live = apply_clob_live(meta)
        now, now_et = _now_stamps()
        stamp = now_et.strftime("%H:%M:%S ET")
        pub = public_pm(live)
        with self.lock:
            self.pm_meta = live
            self.snapshot["pm"] = pub
            self.snapshot["pm_live_et"] = stamp
            self.snapshot["errors"]["polymarket"] = None
        return {"pm": pub, "pm_live_et": stamp, "day": day.isoformat()}

    def get(self) -> dict:
        with self.lock:
            return _client_snapshot(self.snapshot)

    def get_pm(self) -> dict:
        with self.lock:
            return {
                "pm": list(self.snapshot.get("pm") or []),
                "pm_live_et": self.snapshot.get("pm_live_et"),
                "day": self.snapshot.get("day"),
            }


def serve(
    host: str, port: int, interval_s: int, madis_hours: int, pm_interval_s: float = 2.0
) -> None:
    state = _State(interval_s, madis_hours, pm_interval_s)

    def weather_refresher():
        while True:
            time.sleep(interval_s)
            try:
                state.refresh()
                print(f"[refresh weather] {state.get()['generated_at_et']}", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[refresh weather error] {exc}", flush=True)

    def pm_refresher():
        while True:
            time.sleep(pm_interval_s)
            try:
                out = state.refresh_pm()
                top = (out.get("pm") or [{}])[0]
                print(
                    f"[refresh pm] {out.get('pm_live_et')} "
                    f"{top.get('label')} {top.get('yes')}% {top.get('signal')}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[refresh pm error] {exc}", flush=True)

    threading.Thread(target=weather_refresher, daemon=True).start()
    threading.Thread(target=pm_refresher, daemon=True).start()

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
            if self.path.startswith("/api/pm"):
                try:
                    if "refresh=1" in self.path:
                        payload = state.refresh_pm()
                    else:
                        payload = state.get_pm()
                    body = json.dumps(payload).encode()
                    self._send(200, body, "application/json; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    body = json.dumps({"error": str(exc)}).encode()
                    self._send(500, body, "application/json; charset=utf-8")
                return
            if self.path.startswith("/api/snapshot"):
                try:
                    if "refresh=1" in self.path:
                        snap = state.refresh()
                        body = json.dumps(_client_snapshot(snap)).encode()
                    else:
                        body = json.dumps(state.get()).encode()
                    self._send(200, body, "application/json; charset=utf-8")
                except Exception as exc:  # noqa: BLE001
                    body = json.dumps({"error": str(exc)}).encode()
                    self._send(500, body, "application/json; charset=utf-8")
                return
            if self.path in ("/", "/index.html", "/klga_live.html"):
                html = render_html(
                    state.get(),
                    use_api=True,
                    interval_ms=interval_s * 1000,
                    pm_interval_ms=int(pm_interval_s * 1000),
                ).encode()
                self._send(200, html, "text/html; charset=utf-8")
                return
            self._send(404, b"not found", "text/plain; charset=utf-8")

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(
        f"Live dashboard: http://{host}:{port}/  "
        f"(weather {interval_s}s · polymarket {pm_interval_s}s)",
        flush=True,
    )
    httpd.serve_forever()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="HTML-Ausgabepfad")
    parser.add_argument("--serve", action="store_true", help="HTTP-Server mit Auto-Refresh")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--interval", type=int, default=60, help="Sekunden zwischen Wetter-Updates"
    )
    parser.add_argument(
        "--pm-interval",
        type=float,
        default=2.0,
        help="Sekunden zwischen Polymarket/CLOB-Updates (Balken + BUY/SELL)",
    )
    parser.add_argument("--madis-hours", type=int, default=8)
    args = parser.parse_args()

    if args.serve:
        serve(
            args.host,
            args.port,
            args.interval,
            args.madis_hours,
            pm_interval_s=args.pm_interval,
        )
        return 0

    snap = collect_snapshot(madis_hours=args.madis_hours)
    path = write_html(
        args.out,
        snap,
        use_api=False,
        interval_s=args.interval,
        pm_interval_s=args.pm_interval,
    )
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
