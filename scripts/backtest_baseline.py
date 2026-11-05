#!/usr/bin/env python3
"""Run a BTC/JPY spot baseline on complete, exported 5-minute candles."""

import argparse
import csv
import json
import sys
from collections import deque
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from collect_bitbank import BAR_MS, JST
from trading_config import TradingConfig
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


def run_backtest(rows: list[tuple], fee: Decimal, slippage: Decimal,
                 *, config: TradingConfig | None = None,
                 decisions: dict | None = None, halted: bool = False,
                 halt_from_ms: int | None = None) -> dict:
    from llm_assist import select_decision
    config = config or TradingConfig()
    config = replace(config, fee_bps=fee * BASIS_POINTS, slippage_bps=slippage * BASIS_POINTS)
    if not rows:
        raise ValueError("No candles to simulate.")
    half_spread = config.spread_bps / BASIS_POINTS / 2
    buy_factor = (1 + half_spread) * (1 + slippage) * (1 + fee)
    sell_factor = (1 - half_spread) * (1 - slippage) * (1 - fee)
    closes = deque(maxlen=config.slow_window)
    previous_spread = None
    signal = 0
    cash = config.starting_cash_jpy
    quantity = ZERO
    entry_cost = ZERO
    stop_price = ZERO
    entry_timestamp = None
    session_date = None
    session_pnl = ZERO
    peak_equity = config.starting_cash_jpy
    max_drawdown = ZERO
    trades = []
    events = []
    last_equity = cash

    for timestamp, open_, _, low, close, _ in rows:
        bar_time = datetime.fromtimestamp(timestamp / 1000, JST)
        if bar_time.date() != session_date:
            session_date = bar_time.date()
            session_pnl = ZERO

        open_price = Decimal(open_)
        low_price = Decimal(low)
        close_price = Decimal(close)
        at_session_end = bar_time.time() >= FLATTEN_TIME
        is_halted = halted or (halt_from_ms is not None and timestamp >= halt_from_ms)
        decision = None if decisions is None else select_decision(decisions, timestamp)
        allowed = decisions is None or (decision is not None and decision["regime"] != "risk_off")
        if signal == 1 and quantity == 0:
            events.append({"bar_jst": format_jst(timestamp), "event": "entry_decision",
                           "signal_bar_jst": format_jst(timestamp - BAR_MS),
                           "reason": "halted" if is_halted else "session_end" if at_session_end else
                           "daily_loss" if session_pnl <= -config.daily_loss_jpy else
                           "llm_missing_or_risk_off" if not allowed else "cross_up",
                           "llm": decision})
        if (
            not is_halted and allowed
            and quantity == 0
            and signal == 1
            and not at_session_end
            and session_pnl > -config.daily_loss_jpy
        ):
            unit_cost = open_price * buy_factor
            stop_price = open_price * (1 - config.stop_fraction)
            unit_risk = unit_cost - stop_price * sell_factor
            available_risk = min(config.trade_risk_jpy, config.daily_loss_jpy + session_pnl)
            # Round down so Decimal division cannot put the order over its cap.
            raw_quantity = min(min(cash, config.max_position_jpy) / unit_cost, available_risk / unit_risk)
            quantity = raw_quantity.quantize(Decimal("0.00000001"), rounding="ROUND_DOWN")
            if quantity:
                entry_cost = quantity * unit_cost
                entry_timestamp = timestamp
                cash -= entry_cost
                events.append({"bar_jst": format_jst(timestamp), "event": "fill_buy",
                               "quantity_btc": str(quantity), "reference_price_jpy": str(open_price),
                               "cash_flow_jpy": str(-entry_cost), "stop_price_jpy": str(stop_price),
                               "reason": "cross_up", "signal_bar_jst": format_jst(timestamp - BAR_MS)})

        if quantity:
            exit_price = None
            if is_halted:
                exit_price, reason = open_price, "manual_halt"
            elif open_price <= stop_price:
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
                events.append({"bar_jst": format_jst(timestamp), "event": "fill_sell",
                               "quantity_btc": str(quantity), "reference_price_jpy": str(exit_price),
                               "cash_flow_jpy": str(proceeds), "pnl_jpy": str(pnl), "reason": reason})
                quantity = ZERO

        equity = cash + quantity * close_price * sell_factor
        last_equity = equity
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, peak_equity - equity)

        # Use this candle's close only to produce a signal for the next open.
        closes.append(close_price)
        signal = 0
        if len(closes) == config.slow_window:
            slow_total = sum(closes)
            fast_total = sum(list(closes)[-config.fast_window:])
            spread = fast_total * config.slow_window - slow_total * config.fast_window
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
        "starting_cash_jpy": str(config.starting_cash_jpy),
        "ending_cash_jpy": str(cash),
        "net_pnl_jpy": str(last_equity - config.starting_cash_jpy),
        "max_close_drawdown_jpy": str(max_drawdown),
        "config": config.to_dict(),
        "spread_bps_round_trip": str(config.spread_bps),
        "equity_jpy": str(last_equity),
        "open_quantity_btc": str(quantity),
        "unrealized_pnl_jpy": str(last_equity - cash - (entry_cost if quantity else ZERO)),
        "realized_pnl_jpy": str(sum((Decimal(t["pnl_jpy"]) for t in trades), ZERO)),
        "events": events,
        "trade_count": len(trades),
        "trades": trades,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--fee-bps", type=cost_bps)
    parser.add_argument("--slippage-bps", type=cost_bps)
    args = parser.parse_args()

    try:
        rows = load_candles(args.csv)
        config = TradingConfig.load(args.config)
        fee_bps = args.fee_bps if args.fee_bps is not None else config.fee_bps
        slippage_bps = args.slippage_bps if args.slippage_bps is not None else config.slippage_bps
        result = run_backtest(rows, fee_bps / BASIS_POINTS, slippage_bps / BASIS_POINTS, config=config)
    except (OSError, ValueError, OverflowError, csv.Error) as exc:
        print(f"Backtest failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
