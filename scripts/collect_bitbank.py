#!/usr/bin/env python3
"""Collect bitbank BTC/JPY 5-minute candles into SQLite."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE_URL = "https://public.bitbank.cc"
PAIR = "btc_jpy"
TIMEFRAME = "5min"
BAR_MS = 5 * 60 * 1000
JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parents[1]


def parse_date(value: str) -> date:
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Date must use YYYYMMDD format."
        ) from exc

    if parsed.strftime("%Y%m%d") != value:
        raise argparse.ArgumentTypeError("Date must use YYYYMMDD format.")
    return parsed


def fetch_json(url: str, retries: int = 3) -> dict:
    request = Request(
        url,
        headers={"User-Agent": "aitrade-data-collector/0.1"},
    )

    for attempt in range(retries):
        try:
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))

        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == retries - 1:
                raise

            retry_after = exc.headers.get("Retry-After")
            delay = (
                int(retry_after)
                if retry_after and retry_after.isdigit()
                else 2**attempt
            )
            time.sleep(delay)

        except (URLError, TimeoutError):
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)

    raise RuntimeError("API request failed after retries")


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
            exchange TEXT NOT NULL,
            pair TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            open_time_ms INTEGER NOT NULL,
            open TEXT NOT NULL,
            high TEXT NOT NULL,
            low TEXT NOT NULL,
            close TEXT NOT NULL,
            volume TEXT NOT NULL,
            fetched_at_utc TEXT NOT NULL,
            PRIMARY KEY (exchange, pair, timeframe, open_time_ms)
        )
        """
    )
    connection.commit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect bitbank BTC/JPY 5-minute candles."
    )
    parser.add_argument(
        "--date",
        type=parse_date,
        help="JST date to collect in YYYYMMDD format. Defaults to today.",
    )
    args = parser.parse_args()

    target_date = args.date or datetime.now(JST).date()
    date_text = target_date.strftime("%Y%m%d")
    url = f"{BASE_URL}/{PAIR}/candlestick/{TIMEFRAME}/{date_text}"

    raw_dir = ROOT / "data" / "raw" / "bitbank" / PAIR / TIMEFRAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / f"{date_text}.json"

    db_path = ROOT / "data" / "aitrade.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        payload = fetch_json(url)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"Failed to fetch API data: {exc}", file=sys.stderr)
        return 1

    if payload.get("success") != 1:
        print(f"API returned an error: {payload}", file=sys.stderr)
        return 1

    raw_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    fetched_at = datetime.now(timezone.utc).isoformat()
    now_ms = int(time.time() * 1000)
    candle_groups = payload.get("data", {}).get("candlestick", [])

    rows: list[tuple] = []
    for group in candle_groups:
        if group.get("type") != TIMEFRAME:
            continue

        for item in group.get("ohlcv", []):
            if len(item) != 6:
                continue

            open_price, high, low, close, volume, timestamp = item
            open_time_ms = int(timestamp)

            # Do not store a candle that has not closed yet.
            if open_time_ms + BAR_MS > now_ms:
                continue

            rows.append(
                (
                    "bitbank",
                    PAIR,
                    TIMEFRAME,
                    open_time_ms,
                    str(open_price),
                    str(high),
                    str(low),
                    str(close),
                    str(volume),
                    fetched_at,
                )
            )

    with sqlite3.connect(db_path) as connection:
        ensure_schema(connection)
        connection.executemany(
            """
            INSERT INTO candles (
                exchange, pair, timeframe, open_time_ms,
                open, high, low, close, volume, fetched_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (exchange, pair, timeframe, open_time_ms)
            DO UPDATE SET
                open = excluded.open,
                high = excluded.high,
                low = excluded.low,
                close = excluded.close,
                volume = excluded.volume,
                fetched_at_utc = excluded.fetched_at_utc
            """,
            rows,
        )
        connection.commit()

    print(
        f"{date_text}: saved {len(rows)} closed candles.\n"
        f"Raw response: {raw_path.relative_to(ROOT)}\n"
        f"SQLite DB:    {db_path.relative_to(ROOT)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
