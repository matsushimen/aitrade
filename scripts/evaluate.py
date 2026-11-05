"""Evaluate disjoint chronological development/holdout data at identical costs."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

from backtest_baseline import BASIS_POINTS, load_candles, run_backtest
from llm_assist import load_decisions
from trading_config import TradingConfig


def evaluate(development, holdout, config, decisions=None):
    if development[-1][0] >= holdout[0][0]:
        raise ValueError('Development must end before holdout starts; overlap is forbidden.')
    def run(rows, gate=None):
        return run_backtest(rows, config.fee_bps / BASIS_POINTS,
                            config.slippage_bps / BASIS_POINTS, config=config, decisions=gate)
    result = {'protocol': 'Separate initial cash and SMA warmup per period; no automatic tuning.',
              'development': {'baseline': run(development)},
              'holdout': {'baseline': run(holdout)},
              'llm_comparison_status': 'not_run_no_decisions'}
    if decisions is not None:
        for name, rows in (('development', development), ('holdout', holdout)):
            result[name]['llm_assisted'] = run(rows, decisions)
        result['llm_comparison_status'] = 'recorded_decisions_only; causal receipt times enforced; missing/stale blocks entry'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--development', type=Path, required=True)
    parser.add_argument('--holdout', type=Path, required=True)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--decisions', type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(load_candles(args.development), load_candles(args.holdout),
                          TradingConfig.load(args.config),
                          load_decisions(args.decisions) if args.decisions else None)
        result['provenance'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in (args.development, args.holdout, args.config, args.decisions) if p}
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except (OSError, ValueError) as exc:
        print(f'Evaluation failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
