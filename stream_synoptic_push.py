#!/usr/bin/env python3
"""Synoptic-Push-Stream (WebSocket) empfangen und in MariaDB speichern.

Läuft parallel zum Poll-Kanal (poll_synoptic_5min.py), schreibt aber in die
EIGENE Tabelle synoptic_push_obs (siehe schema_synoptic_push.sql). So bleiben
Push- und Poll-Latenz direkt vergleichbar und die Kanäle stören sich nicht.

Ablauf:
  1. Session-ID des letzten Laufs aus synoptic_push_state lesen.
  2. WebSocket verbinden – bevorzugt als Resume (lückenlos, bis 3 Tage),
     sonst neue Session mit rewind als Backfill.
  3. Eingehende data-Messages puffern und alle paar Sekunden per Upsert
     in die DB schreiben (received_at_utc mit Millisekunden).
  4. Nach --max-runtime Minuten sauber beenden; die Session-ID bleibt in der
     DB, der nächste Workflow-Lauf setzt dort fort.

Telegram: Ist TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID gesetzt, wird zusätzlich zum
Datenbankeintrag eine Nachricht geschickt – aber nur für Echtzeit-Werte (kein
Backfill/Resume-Nachlauf) und nur, wenn sich der Wert einer Station geändert
hat (sonst würde KLGA1M 60 Nachrichten pro Stunde erzeugen).

Konfiguration über Umgebungsvariablen oder .env.db:
  SYNOPTIC_TOKEN       API-Token (Pflicht)
  SYNOPTIC_STATION     Stations-ID(s), kommagetrennt, Standard: KLGA,KLGA1M
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
  TELEGRAM_BOT_TOKEN   optional – Telegram-Benachrichtigungen
  TELEGRAM_CHAT_ID     optional

Nutzung:
  python3 stream_synoptic_push.py                     # Dauerlauf (340 Min)
  python3 stream_synoptic_push.py --max-runtime 5     # kurzer Testlauf
  python3 stream_synoptic_push.py --dry-run           # nicht in DB schreiben
  python3 stream_synoptic_push.py --telegram-all      # jeden Echtzeit-Wert melden
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PUSH_SERVER = "wss://push.synopticdata.com/feed"
TELEGRAM_API = "https://api.telegram.org"
# Werte, die älter ankommen, sind Backfill (rewind/Resume) und lösen keine
# Telegram-Nachricht aus.
REALTIME_MAX_LAG_MINUTES = 15
DEFAULT_STATIONS = "KLGA,KLGA1M"
DEFAULT_VARS = "air_temp"
DEFAULT_MAX_RUNTIME_MINUTES = 340
DEFAULT_REWIND_MINUTES = 120
FLUSH_INTERVAL_SECONDS = 10
SOCKET_TIMEOUT_SECONDS = 30
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env.db"

CREATE_OBS_SQL = """
CREATE TABLE IF NOT EXISTS synoptic_push_obs (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    station VARCHAR(10) NOT NULL,
    sensor VARCHAR(40) NOT NULL,
    sensor_set TINYINT UNSIGNED NOT NULL DEFAULT 1,
    observed_at_utc DATETIME NOT NULL,
    value_num DECIMAL(9,2) NULL,
    qc_flags VARCHAR(255) NULL,
    received_at_utc DATETIME(3) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_push_obs (station, sensor, sensor_set, observed_at_utc),
    KEY idx_push_station_day (station, observed_at_utc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

CREATE_STATE_SQL = """
CREATE TABLE IF NOT EXISTS synoptic_push_state (
    stream_key VARCHAR(120) NOT NULL,
    session_id VARCHAR(64) NULL,
    updated_at_utc DATETIME NOT NULL,
    PRIMARY KEY (stream_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

# Erster Insert gewinnt bei received_at_utc (echte Push-Latenz); nachgelieferte
# Werte (z. B. via Resume/rewind) aktualisieren nur den Messwert, falls er
# vorher NULL war.
INSERT_SQL = """
INSERT INTO synoptic_push_obs (
    station, sensor, sensor_set, observed_at_utc, value_num, qc_flags, received_at_utc
) VALUES (%s, %s, %s, %s, %s, %s, %s)
ON DUPLICATE KEY UPDATE
    value_num = COALESCE(value_num, VALUES(value_num)),
    qc_flags = COALESCE(qc_flags, VALUES(qc_flags))
"""


@dataclass(frozen=True)
class PushRow:
    station: str
    sensor: str
    sensor_set: int
    observed_at: datetime
    value: float | None
    qc_flags: str | None
    received_at: datetime


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def connect_db():
    try:
        import pymysql
    except ImportError as error:
        raise RuntimeError("pymysql fehlt. Installiere mit: pip install pymysql") from error

    config = {
        "DB_HOST": os.environ.get("DB_HOST", "").strip(),
        "DB_USER": os.environ.get("DB_USER", "").strip(),
        "DB_PASSWORD": os.environ.get("DB_PASSWORD", "").strip(),
        "DB_NAME": os.environ.get("DB_NAME", "").strip(),
    }
    missing = [name for name, value in config.items() if not value]
    if missing:
        raise RuntimeError(f"Fehlende DB-Konfiguration: {', '.join(missing)}")

    return pymysql.connect(
        host=config["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "3306")),
        user=config["DB_USER"],
        password=config["DB_PASSWORD"],
        database=config["DB_NAME"],
        charset="utf8mb4",
        autocommit=False,
    )


def ensure_tables() -> None:
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(CREATE_OBS_SQL)
            cursor.execute(CREATE_STATE_SQL)
        connection.commit()
    finally:
        connection.close()


def load_session_id(stream_key: str) -> str | None:
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT session_id FROM synoptic_push_state WHERE stream_key = %s",
                (stream_key,),
            )
            row = cursor.fetchone()
            return row[0] if row and row[0] else None
    finally:
        connection.close()


def save_session_id(stream_key: str, session_id: str | None) -> None:
    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO synoptic_push_state (stream_key, session_id, updated_at_utc) "
                "VALUES (%s, %s, UTC_TIMESTAMP()) "
                "ON DUPLICATE KEY UPDATE session_id = VALUES(session_id), "
                "updated_at_utc = VALUES(updated_at_utc)",
                (stream_key, session_id),
            )
        connection.commit()
    finally:
        connection.close()


def parse_observation_time(raw: str) -> datetime:
    text = str(raw).strip()
    if text.isdigit() and len(text) == 12:  # Doku-Format yyyymmddhhmm
        return datetime.strptime(text, "%Y%m%d%H%M")
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def parse_data_message(payload: dict) -> list[PushRow]:
    received_at = datetime.now(timezone.utc).replace(tzinfo=None)
    rows: list[PushRow] = []
    for entry in payload.get("data", []):
        sensor = str(entry.get("sensor", ""))
        station = str(entry.get("stid", "")).upper()      
        if sensor != "air_temp":
            continue
        if station not in {"KLGA", "KLGA1M"}:
            continue
     
        try:
            observed_at = parse_observation_time(entry["date"])
        except (KeyError, ValueError) as error:
            print(f"Übersprungen (Datum unlesbar): {entry} ({error})", file=sys.stderr)
            continue
        value = entry.get("value")
        qc = entry.get("qc") or []
        rows.append(
            PushRow(
                station=str(entry.get("stid", "")).upper()[:10],
                sensor=str(entry.get("sensor", ""))[:40],
                sensor_set=int(entry.get("set", 1)),
                observed_at=observed_at,
                value=float(value) if value is not None else None,
                qc_flags=(",".join(str(flag) for flag in qc)[:255] or None),
                received_at=received_at,
            )
        )
    return rows


def flush_rows(buffer: list[PushRow], dry_run: bool) -> int:
    if not buffer:
        return 0
    if dry_run:
        for row in buffer:
            print(f"[dry-run] {row.station} {row.sensor} {row.observed_at:%Y-%m-%d %H:%M}Z {row.value}")
        count = len(buffer)
        buffer.clear()
        return count

    connection = connect_db()
    try:
        with connection.cursor() as cursor:
            for row in buffer:
                cursor.execute(
                    INSERT_SQL,
                    (
                        row.station,
                        row.sensor,
                        row.sensor_set,
                        row.observed_at,
                        row.value,
                        row.qc_flags,
                        row.received_at,
                    ),
                )
        connection.commit()
    finally:
        connection.close()

    count = len(buffer)
    buffer.clear()
    return count


class TelegramNotifier:
    """Meldet Echtzeit-Werte per Telegram – standardmäßig nur bei Wertänderung."""

    def __init__(self, notify_all: bool, dry_run: bool) -> None:
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.notify_all = notify_all
        self.dry_run = dry_run
        self.last_value: dict[tuple[str, str], float | None] = {}
        if self.enabled:
            mode = "alle Echtzeit-Werte" if notify_all else "nur Wertänderungen"
            print(f"Telegram aktiv ({mode}).")
        else:
            print("Telegram nicht konfiguriert (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID fehlen) – nur DB.")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def collect(self, rows: list[PushRow]) -> None:
        if not self.enabled:
            return
        lines: list[str] = []
        for row in sorted(rows, key=lambda r: (r.station, r.observed_at)):
            lag_minutes = (row.received_at - row.observed_at).total_seconds() / 60
            is_realtime = 0 <= lag_minutes <= REALTIME_MAX_LAG_MINUTES
            key = (row.station, row.sensor)
            previous = self.last_value.get(key)
            if is_realtime:
                self.last_value[key] = row.value
            if not is_realtime or row.value is None:
                continue
            if not self.notify_all and previous is not None and row.value == previous:
                continue
            fahrenheit = row.value * 9 / 5 + 32
            line = f"{row.station} {row.observed_at:%H:%M}Z: {row.value:.1f} °C = {fahrenheit:.1f} °F"
            if previous is not None and row.value != previous:
                arrow = "↑" if row.value > previous else "↓"
                line += f" ({arrow} von {previous:.1f} °C)"
            elif previous is None:
                line += " (erster Echtzeit-Wert)"
            lines.append(line)
        if lines:
            self._send("📡 Synoptic Push\n" + "\n".join(lines))

    def _send(self, text: str) -> None:
        if self.dry_run:
            print(f"[dry-run] Telegram:\n{text}")
            return
        payload = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{TELEGRAM_API}/bot{self.token}/sendMessage",
            data=payload,
            headers={"User-Agent": "weather/1.0 (Synoptic push streamer)"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
            if not body.get("ok"):
                print(f"Telegram-API-Fehler: {body}", file=sys.stderr)
        except Exception as error:
            # Telegram-Ausfälle dürfen den DB-Pfad nie stören.
            print(f"Telegram-Sendefehler: {error}", file=sys.stderr)


def open_stream(token: str, stations: str, variables: str, session_id: str | None, rewind: int):
    """Verbindet den WebSocket. Liefert (ws, neue_session_id, resumed)."""
    from websocket import create_connection

    attempts: list[tuple[str, bool]] = []
    if session_id:
        attempts.append((f"{PUSH_SERVER}/{token}/{session_id}", True))
    query = f"stid={stations}&vars={variables}&rewind={rewind}"
    attempts.append((f"{PUSH_SERVER}/{token}/?{query}", False))

    last_error: Exception | None = None
    for url, resumed in attempts:
        try:
            ws = create_connection(url, timeout=SOCKET_TIMEOUT_SECONDS)
            auth = json.loads(ws.recv())
            if auth.get("type") == "auth" and auth.get("code") == "success":
                mode = "Resume" if resumed else "Neu"
                print(f"Verbunden ({mode}): Session {auth.get('session')} – {auth.get('messages')}")
                return ws, auth.get("session"), resumed
            print(f"Auth fehlgeschlagen ({url.split('?')[0]}...): {auth.get('messages')}", file=sys.stderr)
            ws.close()
        except Exception as error:
            last_error = error
            print(f"Verbindungsfehler: {error}", file=sys.stderr)
    raise RuntimeError(f"Keine Verbindung zum Push-Stream möglich: {last_error}")


def stream(
    token: str,
    stations: str,
    variables: str,
    max_runtime_minutes: int,
    rewind: int,
    dry_run: bool,
    notifier: TelegramNotifier,
) -> int:
    from websocket import WebSocketTimeoutException

    stream_key = f"{stations}|{variables}"
    session_id = None if dry_run else load_session_id(stream_key)
    deadline = time.monotonic() + max_runtime_minutes * 60

    total_rows = 0
    buffer: list[PushRow] = []
    last_flush = time.monotonic()
    last_heartbeat = time.monotonic()

    ws = None
    reconnect_delay = 5
    try:
        while time.monotonic() < deadline:
            if ws is None:
                try:
                    ws, session_id, _ = open_stream(token, stations, variables, session_id, rewind)
                    if not dry_run:
                        save_session_id(stream_key, session_id)
                    reconnect_delay = 5
                except Exception as error:
                    print(f"Reconnect in {reconnect_delay}s: {error}", file=sys.stderr)
                    time.sleep(min(reconnect_delay, max(0, deadline - time.monotonic())))
                    reconnect_delay = min(reconnect_delay * 2, 120)
                    continue

            try:
                message = ws.recv()
                payload = json.loads(message)
                msg_type = payload.get("type")
                if msg_type == "data":
                    rows = parse_data_message(payload)
                    buffer.extend(rows)
                    notifier.collect(rows)
                elif msg_type == "metadata":
                    stations_meta = [s.get("stid") for s in payload.get("stations", [])]
                    print(f"Metadata: Stationen {stations_meta}, Units {payload.get('units')}")
                elif msg_type == "auth":
                    print(f"Auth-Nachricht im Stream: {payload}")
            except WebSocketTimeoutException:
                # Kein neues Paket innerhalb des Socket-Timeouts – Verbindung
                # mit Ping am Leben halten und weiter warten.
                try:
                    ws.ping()
                except Exception:
                    ws = None
            except Exception as error:
                print(f"Stream-Fehler, verbinde neu: {error}", file=sys.stderr)
                try:
                    ws.close()
                except Exception:
                    pass
                ws = None

            now = time.monotonic()
            if buffer and now - last_flush >= FLUSH_INTERVAL_SECONDS:
                try:
                    stored = flush_rows(buffer, dry_run)
                    total_rows += stored
                    last_flush = now
                    latest = datetime.now(timezone.utc)
                    print(f"[{latest:%H:%M:%S}Z] {stored} Werte gespeichert (gesamt {total_rows}).")
                except Exception as error:
                    print(f"DB-Fehler beim Flush (Puffer bleibt erhalten): {error}", file=sys.stderr)
                    last_flush = now
            if now - last_heartbeat >= 300:
                remaining = int((deadline - now) / 60)
                print(f"Heartbeat: {total_rows} Werte gespeichert, noch ~{remaining} Min. Laufzeit.")
                last_heartbeat = now
    finally:
        if buffer:
            try:
                total_rows += flush_rows(buffer, dry_run)
            except Exception as error:
                print(f"DB-Fehler beim finalen Flush: {error}", file=sys.stderr)
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    print(f"Fertig: {total_rows} Werte gespeichert. Session {session_id} bleibt für Resume gespeichert.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stations", default=None, help="Stations-IDs, kommagetrennt (Standard: env SYNOPTIC_STATION)")
    parser.add_argument("--vars", default=DEFAULT_VARS, help=f"Sensorvariablen (Standard: {DEFAULT_VARS})")
    parser.add_argument(
        "--max-runtime",
        type=int,
        default=DEFAULT_MAX_RUNTIME_MINUTES,
        help=f"Laufzeit in Minuten (Standard: {DEFAULT_MAX_RUNTIME_MINUTES}, passt in GHA-Job-Limit)",
    )
    parser.add_argument(
        "--rewind",
        type=int,
        default=DEFAULT_REWIND_MINUTES,
        help="Backfill in Minuten, falls kein Resume möglich ist",
    )
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nicht in DB schreiben")
    parser.add_argument(
        "--telegram-all",
        action="store_true",
        help="Jeden Echtzeit-Wert melden statt nur Wertänderungen",
    )
    args = parser.parse_args()

    load_env_file(ENV_FILE)

    token = os.environ.get("SYNOPTIC_TOKEN", "").strip()
    if not token:
        print("SYNOPTIC_TOKEN fehlt (Umgebungsvariable oder .env.db).", file=sys.stderr)
        return 2

    stations = (args.stations or os.environ.get("SYNOPTIC_STATION", DEFAULT_STATIONS)).strip().upper()

    if not args.dry_run:
        ensure_tables()

    notifier = TelegramNotifier(notify_all=args.telegram_all, dry_run=args.dry_run)
    return stream(token, stations, args.vars, args.max_runtime, args.rewind, args.dry_run, notifier)


if __name__ == "__main__":
    raise SystemExit(main())
