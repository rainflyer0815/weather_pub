#!/usr/bin/env python3
"""Erzeugt alle Wetter- und Peakzeit-Charts nach /opt/cursor/artifacts/."""

import shutil
import sys
from pathlib import Path

from visualize_peak_time import plot_peak_time_by_day, plot_peak_time_by_week
from visualize_temperature import plot_temperature

ARTIFACT_DIR = Path("/opt/cursor/artifacts")
WORKSPACE = Path(__file__).resolve().parent

CHARTS = (
    "temperature_munich.png",
    "peak_time_by_week.png",
    "peak_time_by_day.png",
)


def main() -> None:
    try:
        plot_temperature()
        plot_peak_time_by_week()
        plot_peak_time_by_day()

        ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
        for name in CHARTS:
            source = WORKSPACE / name
            target = ARTIFACT_DIR / name
            if source.exists():
                shutil.copy2(source, target)

        print("Charts erzeugt:")
        for name in CHARTS:
            path = ARTIFACT_DIR / name
            print(f"  {path} ({path.stat().st_size // 1024} KB)")
    except Exception as error:
        print(f"Fehler: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
