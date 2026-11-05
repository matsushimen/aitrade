# aitrade

A small research project for BTC/JPY market-data collection and paper-trading experiments.

## Current scope

- Collect public BTC/JPY 5-minute candles from bitbank.
- Preserve the raw API response and store normalized candles in SQLite.
- Make collection repeatable and safe to rerun.
- No order placement, account credentials, or live trading code is included.

The paper-trading portfolio will start at JPY 10,000. This is separate from any exchange account or live funds.

## Requirements

- Python 3.11 or newer
- No third-party Python packages

## Collect candles

From the repository root:

```sh
python3 scripts/collect_bitbank.py --date 20261106
```

The date is JST in `YYYYMMDD` format. If omitted, the current JST date is used.

The collector saves:

- Raw API response: `data/raw/bitbank/btc_jpy/5min/YYYYMMDD.json`
- Normalized candles: `data/aitrade.sqlite3`

Rerunning for the same date updates matching timestamps instead of duplicating rows. A candle that has not yet closed is excluded.

## Export data for a backtest

Collect each day in the desired period, then export the stored candles:

```sh
python3 scripts/collect_bitbank.py --date 20261105
python3 scripts/export_candles.py --start 20261105 --end 20261105 --output data/btc_jpy_5min.csv
```

Both dates are inclusive JST calendar dates. Only completed days are accepted.
The CSV contains the candle start time in Unix milliseconds and OHLCV values,
sorted by timestamp. A complete day has 288 candles. If a timestamp is missing
or the stored OHLCV is invalid, the command exits with an error without
replacing the output file. Missing candles are not filled in.

## Safety

This repository is for research and paper trading. The collector uses public market data only. It does not submit orders or access exchange account credentials.
