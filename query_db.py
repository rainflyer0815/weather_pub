#!/usr/bin/env python3
"""Client für die generische Kasserver DB-Query-API (db_query_api.php)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / ".env.db.api"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def query_api(sql: str, params: list | None = None) -> dict:
    url = os.environ.get("DB_API_URL", "").strip()
    api_key = os.environ.get("DB_API_KEY", "").strip()
    if not url or not api_key:
        raise RuntimeError(
            f"DB_API_URL und DB_API_KEY setzen (z. B. in {ENV_FILE.name})."
        )

    payload = json.dumps({"sql": sql, "params": params or []}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "User-Agent": "weather/1.0 (db query client)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(details)
        except json.JSONDecodeError:
            body = {"ok": False, "error": details}
        raise RuntimeError(f"API HTTP {error.code}: {body}") from error

    if not body.get("ok"):
        raise RuntimeError(f"API-Fehler: {body}")
    return body


def format_table(columns: list[str], rows: list[dict]) -> str:
    if not rows:
        return "(keine Zeilen)"
    widths = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            widths[col] = max(widths[col], len(str(row.get(col, ""))))
    header = " | ".join(col.ljust(widths[col]) for col in columns)
    sep = "-+-".join("-" * widths[col] for col in columns)
    lines = [header, sep]
    for row in rows:
        lines.append(" | ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sql", nargs="?", help="SQL-Abfrage")
    parser.add_argument("--file", type=Path, help="SQL aus Datei lesen")
    parser.add_argument("--json", action="store_true", help="Roh-JSON ausgeben")
    args = parser.parse_args()

    load_env_file(ENV_FILE)

    if args.file:
        sql = args.file.read_text(encoding="utf-8")
    elif args.sql:
        sql = args.sql
    else:
        sql = sys.stdin.read()

    sql = sql.strip()
    if not sql:
        print("Keine SQL-Abfrage angegeben.", file=sys.stderr)
        return 1

    try:
        result = query_api(sql)
    except Exception as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"{result['count']} Zeile(n), {result['elapsed_ms']} ms", end="")
    if result.get("truncated"):
        print(" [truncated]", end="")
    print()
    print(format_table(result.get("columns", []), result.get("rows", [])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
