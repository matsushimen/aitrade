"""Persistent delayed-candle paper simulation, replay or polling public data.

Fills use completed OHLCV and are recorded after the candle closes. This is not
an executable quote simulator: next-open prices were not available at receipt.
"""
import argparse
import csv
import fcntl
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, time as day_time, timedelta
from pathlib import Path

from backtest_baseline import BASIS_POINTS, load_candles, run_backtest
from collect_bitbank import BAR_MS, JST, ROOT, parse_date
from export_candles import read_candles, validate_candles
from trading_config import TradingConfig
from storage import atomic_json, require_state


def advance(path, rows, config, *, halt=False, now_ms=None, start_ms=None):
    """Atomically advance a locked immutable candle journal; retry is idempotent."""
    now_ms = int(time.time()*1000) if now_ms is None else now_ms
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix+'.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else None
        if state is not None:
            require_state(state, 'delayed_completed_ohlcv',
                          ('config', 'start_ms', 'candles', 'halt_from_ms', 'observed_at_ms', 'report'))
        if state and state['config'] != config.to_dict():
            raise ValueError('Configuration changed; use a new paper state.')
        if state and start_ms is not None and state['start_ms'] != start_ms:
            raise ValueError('Start date changed; use a new paper state.')
        normalized = [list(row) for row in rows]
        if state and normalized[:len(state['candles'])] != state['candles']:
            raise ValueError('Previously observed candles changed or disappeared; paper state was preserved.')
        if not rows:
            raise ValueError('No closed candles; no state change.')
        start_ms = start_ms if start_ms is not None else rows[0][0]
        validate_candles(rows, start_ms, rows[-1][0]+BAR_MS)
        if rows[-1][0]+BAR_MS > now_ms:
            raise ValueError('Unclosed candle; paper state was preserved.')
        halt_from = state['halt_from_ms'] if state else None
        # Stop requests affect only bars not yet processed, never rewrite fills.
        if halt and halt_from is None:
            halt_from = (state['candles'][-1][0]+BAR_MS) if state else rows[0][0]
        report = run_backtest(rows, config.fee_bps/BASIS_POINTS,
                              config.slippage_bps/BASIS_POINTS, config=config,
                              halt_from_ms=halt_from)
        for event in report['events']:
            event['fill_model'] = 'delayed_completed_ohlcv'
        # Save the first observation time; replaying cannot revise audit times.
        old_times = state['observed_at_ms'] if state else {}
        observed = {str(row[0]): old_times.get(str(row[0]), now_ms) for row in rows}
        for event in report['events']:
            timestamp = int(datetime.fromisoformat(event['bar_jst']).timestamp())*1000
            event['recorded_at_ms'] = observed[str(timestamp)]
        next_state = {'version': 1, 'mode': 'delayed_completed_ohlcv',
                      'start_ms': start_ms, 'config': config.to_dict(),
                      'halt_from_ms': halt_from, 'candles': normalized,
                      'observed_at_ms': observed, 'report': report,
                      'status': 'halt_requested_waiting_for_next_bar' if halt_from is not None and rows[-1][0] < halt_from
                                else 'halted' if halt_from is not None else 'running'}
        atomic_json(path, next_state)
        return next_state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--csv', type=Path, help='Replay complete exported CSV without network.')
    parser.add_argument('--start', type=parse_date, help='First JST day for SQLite polling.')
    parser.add_argument('--db', type=Path, default=ROOT/'data/aitrade.sqlite3')
    parser.add_argument('--until-ms', type=int, help='Replay cutoff, exclusive; requires --csv.')
    parser.add_argument('--collect', action='store_true', help='Refresh public candles before each poll.')
    parser.add_argument('--watch', action='store_true', help='Poll until interrupted; otherwise run once.')
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--halt', action='store_true', help='Persistently prohibit entries and exit on next unprocessed bar.')
    args = parser.parse_args()
    if bool(args.csv) == bool(args.start) or args.interval < 1:
        parser.error('Choose either --csv or --start; interval must be positive.')
    if args.until_ms is not None and not args.csv:
        parser.error('--until-ms requires --csv.')
    protected = [args.db, args.csv, args.config]
    if any(p and args.state.resolve() == p.resolve() for p in protected):
        parser.error('State must not overwrite source data or configuration.')
    try:
        config = TradingConfig.load(args.config)
        while True:
            now_ms = int(time.time()*1000)
            if args.csv:
                rows = load_candles(args.csv)
                rows = [row for row in rows if args.until_ms is None or row[0] < args.until_ms]
                start_ms = None
            else:
                start = datetime.combine(args.start, day_time.min, JST)
                start_ms = int(start.timestamp())*1000
                if args.collect:
                    # Refetch overlapping days for safe restart and complete gap detection.
                    day = args.start
                    while day <= datetime.now(JST).date():
                        subprocess.run([sys.executable, str(ROOT/'scripts/collect_bitbank.py'), '--date', day.strftime('%Y%m%d')], check=True)
                        day += timedelta(days=1)
                    now_ms = int(time.time()*1000)
                end_ms = now_ms//BAR_MS*BAR_MS
                rows = read_candles(args.db, start_ms, end_ms)
                validate_candles(rows, start_ms, end_ms)
                if rows and now_ms-(rows[-1][0]+2*BAR_MS) > config.max_feed_age_seconds*1000:
                    raise ValueError('Feed is stale; no new paper events committed.')
            state = advance(args.state, rows, config, halt=args.halt, now_ms=now_ms, start_ms=start_ms)
            print(json.dumps({'status': state['status'], 'candle_count': len(rows),
                              'equity_jpy': state['report']['equity_jpy'],
                              'trade_count': state['report']['trade_count'], 'mode': state['mode']}), flush=True)
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, csv.Error, sqlite3.Error, subprocess.CalledProcessError) as exc:
        print(f'Paper stopped without advancing state: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
