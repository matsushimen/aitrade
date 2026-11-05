#!/usr/bin/env python3
"""Export a complete range of bitbank BTC/JPY candles for backtesting."""

import argparse
import csv
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import NamedTemporaryFile

from collect_bitbank import BAR_MS, JST, PAIR, ROOT, TIMEFRAME, parse_date

COLUMNS = ("open_time_ms", "open", "high", "low", "close", "volume")


def read_candles(db_path: Path, start_ms: int, end_ms: int) -> list[tuple]:
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute(
            """
            SELECT open_time_ms, open, high, low, close, volume
            FROM candles
            WHERE exchange = ? AND pair = ? AND timeframe = ?
              AND open_time_ms >= ? AND open_time_ms < ?
            ORDER BY open_time_ms
            """,
            ("bitbank", PAIR, TIMEFRAME, start_ms, end_ms),
        ).fetchall()


def format_jst(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, JST).isoformat()


def validate_candles(rows: list[tuple], start_ms: int, end_ms: int) -> None:
    expected_ms = start_ms
    for timestamp, *values in rows:
        if not isinstance(timestamp, int) or timestamp % BAR_MS:
            raise ValueError(f"Invalid 5-minute timestamp: {timestamp!r}")
        if timestamp != expected_ms:
            raise ValueError(
                f"Missing candle at {format_jst(expected_ms)}; "
                f"next candle is {format_jst(timestamp)}."
            )

        try:
            open_price, high, low, close, volume = map(Decimal, values)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid OHLCV at {format_jst(timestamp)}.") from exc

        if (
            not all(value.is_finite() for value in (open_price, high, low, close, volume))
            or not (0 < low <= min(open_price, close) <= max(open_price, close) <= high)
            or volume < 0
        ):
            raise ValueError(f"Invalid OHLCV at {format_jst(timestamp)}.")
        expected_ms += BAR_MS

    if expected_ms != end_ms:
        raise ValueError(f"Missing candle at {format_jst(expected_ms)}.")


def write_csv(output: Path, rows: list[tuple]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="", dir=output.parent, delete=False
    ) as stream:
        temporary_path = Path(stream.name)
        try:
            writer = csv.writer(stream)
            writer.writerow(COLUMNS)
            writer.writerows(rows)
            stream.close()
            temporary_path.replace(output)
        finally:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, type=parse_date, help="First JST date (YYYYMMDD).")
    parser.add_argument("--end", required=True, type=parse_date, help="Last JST date (inclusive).")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "aitrade.sqlite3")
    parser.add_argument("--output", required=True, type=Path, help="Destination CSV file.")
    args = parser.parse_args()

    if args.start > args.end:
        parser.error("--start must be on or before --end.")
    if args.end >= datetime.now(JST).date():
        parser.error("--end must be before today's JST date.")
    if args.output.resolve() == args.db.resolve():
        parser.error("--output must not point to the source database.")

    start = datetime.combine(args.start, time.min, JST)
    end = datetime.combine(args.end + timedelta(days=1), time.min, JST)
    start_ms = int(start.timestamp()) * 1000
    end_ms = int(end.timestamp()) * 1000
    try:
        rows = read_candles(args.db, start_ms, end_ms)
        validate_candles(rows, start_ms, end_ms)
        write_csv(args.output, rows)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1

    print(f"Exported {len(rows)} candles to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
