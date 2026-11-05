#!/usr/bin/env python3
"""Run a BTC/JPY spot baseline on complete, exported 5-minute candles."""

import argparse
import csv
import json
import sys
from collections import deque
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from collect_bitbank import JST
from export_candles import COLUMNS, format_jst, validate_candles

STARTING_CASH = Decimal("10000")
MAX_POSITION = Decimal("5000")
TRADE_RISK = Decimal("100")
DAILY_LOSS = Decimal("200")
STOP_DISTANCE = Decimal("0.02")
FAST_WINDOW = 12
SLOW_WINDOW = 36
FLATTEN_TIME = time(23, 55)
BASIS_POINTS = Decimal("10000")
ZERO = Decimal("0")


def cost_bps(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("Expected a number in basis points.") from exc
    if not amount.is_finite() or not 0 <= amount < BASIS_POINTS:
        raise argparse.ArgumentTypeError("Expected basis points in [0, 10000).")
    return amount


def load_candles(path: Path) -> list[tuple]:
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        if tuple(next(reader, ())) != COLUMNS:
            raise ValueError("Expected the CSV header produced by export_candles.py.")
        rows = []
        for row in reader:
            if len(row) != len(COLUMNS):
                raise ValueError(f"Wrong number of columns at CSV line {reader.line_num}.")
            rows.append((int(row[0]), *row[1:]))

    if not rows:
        raise ValueError("No candles in the CSV.")
    first_day = datetime.fromtimestamp(rows[0][0] / 1000, JST).date()
    last_day = datetime.fromtimestamp(rows[-1][0] / 1000, JST).date()
    start = datetime.combine(first_day, time.min, JST)
    end = datetime.combine(last_day + timedelta(days=1), time.min, JST)
    validate_candles(rows, int(start.timestamp()) * 1000, int(end.timestamp()) * 1000)
    return rows


def run_backtest(rows: list[tuple], fee: Decimal, slippage: Decimal) -> dict:
    buy_factor = (1 + slippage) * (1 + fee)
    sell_factor = (1 - slippage) * (1 - fee)
    closes = deque(maxlen=SLOW_WINDOW)
    previous_spread = None
    signal = 0
    cash = STARTING_CASH
    quantity = ZERO
    entry_cost = ZERO
    stop_price = ZERO
    entry_timestamp = None
    session_date = None
    session_pnl = ZERO
    peak_equity = STARTING_CASH
    max_drawdown = ZERO
    trades = []

    for timestamp, open_, _, low, close, _ in rows:
        bar_time = datetime.fromtimestamp(timestamp / 1000, JST)
        if bar_time.date() != session_date:
            session_date = bar_time.date()
            session_pnl = ZERO

        open_price = Decimal(open_)
        low_price = Decimal(low)
        close_price = Decimal(close)
        at_session_end = bar_time.time() >= FLATTEN_TIME
        if (
            quantity == 0
            and signal == 1
            and not at_session_end
            and session_pnl > -DAILY_LOSS
        ):
            unit_cost = open_price * buy_factor
            stop_price = open_price * (1 - STOP_DISTANCE)
            unit_risk = unit_cost - stop_price * sell_factor
            available_risk = min(TRADE_RISK, DAILY_LOSS + session_pnl)
            # Round down so Decimal division cannot put the order over its cap.
            raw_quantity = min(min(cash, MAX_POSITION) / unit_cost, available_risk / unit_risk)
            quantity = raw_quantity.quantize(Decimal("0.00000001"), rounding="ROUND_DOWN")
            if quantity:
                entry_cost = quantity * unit_cost
                entry_timestamp = timestamp
                cash -= entry_cost

        if quantity:
            exit_price = None
            if open_price <= stop_price:
                exit_price, reason = open_price, "stop_at_open"
            elif at_session_end:
                exit_price, reason = open_price, "session_end"
            elif signal == -1:
                exit_price, reason = open_price, "cross_down"
            elif low_price <= stop_price:
                exit_price, reason = stop_price, "stop_intrabar"

            if exit_price is not None:
                proceeds = quantity * exit_price * sell_factor
                pnl = proceeds - entry_cost
                cash += proceeds
                session_pnl += pnl
                trades.append({
                    "entry_bar_jst": format_jst(entry_timestamp),
                    "exit_bar_jst": format_jst(timestamp),
                    "exit_reason": reason,
                    "pnl_jpy": str(pnl),
                })
                quantity = ZERO

        equity = cash + quantity * close_price * sell_factor
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, peak_equity - equity)

        # Use this candle's close only to produce a signal for the next open.
        closes.append(close_price)
        signal = 0
        if len(closes) == SLOW_WINDOW:
            slow_total = sum(closes)
            fast_total = sum(list(closes)[-FAST_WINDOW:])
            spread = fast_total * SLOW_WINDOW - slow_total * FAST_WINDOW
            if previous_spread is not None:
                if previous_spread <= 0 < spread:
                    signal = 1
                elif previous_spread >= 0 > spread:
                    signal = -1
            previous_spread = spread

    return {
        "first_bar_jst": format_jst(rows[0][0]),
        "last_bar_jst": format_jst(rows[-1][0]),
        "candle_count": len(rows),
        "fee_bps_per_side": str(fee * BASIS_POINTS),
        "slippage_bps_per_side": str(slippage * BASIS_POINTS),
        "starting_cash_jpy": str(STARTING_CASH),
        "ending_cash_jpy": str(cash),
        "net_pnl_jpy": str(cash - STARTING_CASH),
        "max_close_drawdown_jpy": str(max_drawdown),
        "trade_count": len(trades),
        "trades": trades,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--fee-bps", type=cost_bps, required=True)
    parser.add_argument("--slippage-bps", type=cost_bps, required=True)
    args = parser.parse_args()

    try:
        rows = load_candles(args.csv)
        result = run_backtest(rows, args.fee_bps / BASIS_POINTS, args.slippage_bps / BASIS_POINTS)
    except (OSError, ValueError, OverflowError, csv.Error) as exc:
        print(f"Backtest failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
