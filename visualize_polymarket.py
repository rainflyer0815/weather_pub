#!/usr/bin/env python3
"""Visualisiert den Polymarket-Wettverlauf für München-Temperatur heute."""

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

USER_AGENT = "weather/1.0 (Munich Airport polymarket chart)"
BERLIN = ZoneInfo("Europe/Berlin")
GAMMA_API_URL = "https://gamma-api.polymarket.com/events"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"
OUTPUT_PATH = Path("/opt/cursor/artifacts/polymarket_munich_afternoon.png")

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
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def polymarket_slug(target_date: datetime) -> str:
    month = MONTH_NAMES[target_date.month]
    return (
        f"highest-temperature-in-munich-on-{month}-"
        f"{target_date.day}-{target_date.year}"
    )


def get_top_outcome_tokens(limit: int = 3) -> dict[str, str]:
    today = datetime.now(BERLIN)
    slug = polymarket_slug(today)
    query = urllib.parse.urlencode({"slug": slug})
    event = fetch_json(f"{GAMMA_API_URL}?{query}")[0]

    markets = []
    for market in event.get("markets", []):
        prices = json.loads(market.get("outcomePrices", "[0, 1]"))
        markets.append(
            {
                "label": market.get("groupItemTitle", "?"),
                "price": float(prices[0]),
                "token": json.loads(market.get("clobTokenIds", "[]"))[0],
            }
        )

    markets.sort(key=lambda item: item["price"], reverse=True)
    return {market["label"]: market["token"] for market in markets[:limit]}


def fetch_price_history(token_id: str, start_ts: int, end_ts: int) -> list[dict]:
    query = urllib.parse.urlencode(
        {
            "market": token_id,
            "startTs": start_ts,
            "endTs": end_ts,
            "fidelity": 5,
        }
    )
    payload = fetch_json(f"{CLOB_HISTORY_URL}?{query}")
    return payload.get("history", [])


def plot_afternoon_history(output_path: Path = OUTPUT_PATH) -> Path:
    now = datetime.now(BERLIN)
    afternoon_start = now.replace(hour=12, minute=0, second=0, microsecond=0)
    start_ts = int(afternoon_start.timestamp())
    end_ts = int(now.timestamp())

    tokens = get_top_outcome_tokens(limit=3)
    histories = {
        label: fetch_price_history(token_id, start_ts, end_ts)
        for label, token_id in tokens.items()
    }

    fig, ax = plt.subplots(figsize=(11, 6))
    colors = {"30°C": "#e4572e", "29°C": "#4c78a8", "31°C": "#54a24b"}

    for label, history in histories.items():
        if not history:
            continue

        times = [datetime.fromtimestamp(point["t"], BERLIN) for point in history]
        prices = [point["p"] * 100 for point in history]
        ax.plot(
            times,
            prices,
            marker="o",
            markersize=3,
            linewidth=2,
            label=label,
            color=colors.get(label),
        )

    ax.set_title(
        "Polymarket Wettverlauf – Höchsttemperatur München\n"
        f"{now.strftime('%d.%m.%Y')} Nachmittag (12:00–{now.strftime('%H:%M')} Ortszeit)",
        fontsize=13,
        fontweight="bold",
    )
    ax.set_xlabel("Uhrzeit (Europe/Berlin)")
    ax.set_ylabel("Wahrscheinlichkeit (Yes-Preis in %)")
    ax.set_ylim(0, 100)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", title="Outcomes")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=BERLIN))
    fig.autofmt_xdate()
    plt.tight_layout()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return output_path


def main() -> None:
    try:
        output = plot_afternoon_history()
        print(f"Chart gespeichert: {output}")
    except Exception as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
