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

- Raw API responses: `data/raw/bitbank/btc_jpy/5min/YYYYMMDD.json`
  (filenames use the API's UTC dates). Each requested JST day requires the
  previous and same UTC date; only candles within the requested JST day are
  inserted into SQLite. Existing raw files and database rows remain compatible;
  rerun collection for previously collected JST days to fill their missing hours.
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

## Run the baseline backtest

```sh
python3 scripts/backtest_baseline.py data/btc_jpy_5min.csv --fee-bps 15 --slippage-bps 5
```

The command prints a JSON report with the net P&L, drawdown measured at candle
closes, and individual simulated trades. The example cost inputs are
illustrative and are not bitbank's fee schedule. Both are per side; one basis
point is 0.01%. Use the slippage input for estimated execution friction,
including spread if you have no bid/ask history.

This baseline compares the 12- and 36-candle simple moving averages. Signals
from a closed candle can execute at the next candle's open. It models one
long spot position, exits by the 23:55 JST bar open, and starts with JPY 10,000.
Position size is capped at JPY 5,000 including entry costs, and is sized to
target at most JPY 100 of modeled loss at a 2% stop. New entries are disabled
once realized daily losses reach JPY 200. Gaps can cause fills beyond a stop,
so the configured limits do not guarantee maximum losses.

The simulator uses OHLCV and fixed costs; it does not model order book depth,
partial fills or exchange minimum order sizes. A stop touched inside a bar
is modeled at the stop price plus adverse slippage unless the bar opened
through the stop, in which case it uses the open plus adverse slippage.

## Safety

This repository is for research and paper trading. The collector uses public market data only. It does not submit orders or access exchange account credentials.

## Roadmap implementation

Trading conditions, stage status and limitations are documented in
[docs/trading-plan.md](docs/trading-plan.md). Validated Decimal settings live in
[config/baseline.json](config/baseline.json). All examples below use simulated
funds; the cost assumptions are not a verified exchange fee schedule.

Use configuration (existing explicit fee/slippage CLI flags still override it):

```sh
python3 scripts/backtest_baseline.py data/btc_jpy_5min.csv --config config/baseline.json
```

Export separate chronological development and holdout periods, then evaluate:

```sh
python3 scripts/evaluate.py --development data/development.csv --holdout data/holdout.csv --config config/baseline.json > data/evaluation.json
```

Generate a classification with an **OpenAI-compatible Chat Completions endpoint**.
Configure the provider key in the execution environment; never commit it or put
its value in command arguments. Specify the provider's model and API base URL,
including its version path:

```sh
python3 scripts/llm_assist.py --db data/aitrade.sqlite3 --base-url https://llm.example/v1 --api-key-env LLM_API_KEY --model provider-model --response-format json_object --output data/llm_decisions.jsonl
python3 scripts/evaluate.py --development data/development.csv --holdout data/holdout.csv --config config/baseline.json --decisions data/llm_decisions.jsonl > data/llm_comparison.json
```

Replace the example URL and model with your provider's values. `--base-url`
defaults to `OPENAI_BASE_URL`, then `https://api.openai.com/v1` for compatibility
with existing commands. `--api-key-env` defaults to `OPENAI_API_KEY`; a custom
variable never falls back to another key. Use `--no-auth` for a local endpoint
that needs no authentication. HTTP is supported for local deployments; use
HTTPS for remote connections.

Choose `--response-format json_schema` (default) for Structured Outputs,
`json_object` for JSON mode, or `text` to omit the API's `response_format` field.
Every mode still requires the same strict local JSON validation. Unsupported
modes fail explicitly; the client does not silently retry in a different mode.

Classification must be run periodically while collecting data; the command above
runs once. Decisions generated today cannot be applied to yesterday's trades.
Missing, stale or risk-off decisions block new entries in assisted mode. Historical
knowledge in a model also prevents treating retrospective generation as unseen
market research. Collect decisions prospectively before evaluating their effect.

Persistent candle replay (completed days only; restart safely with the same input):

```sh
python3 scripts/paper_trade.py --csv data/btc_jpy_5min.csv --config config/baseline.json --state data/paper_replay.json
```

Optional delayed-candle polling from a JST start date:

```sh
python3 scripts/paper_trade.py --start 20261105 --collect --watch --config config/baseline.json --state data/paper_delayed.json
```

Real-time paper fills at observed public bid/ask (one poll by default; `--watch`
keeps polling; no exchange credentials or real orders):

```sh
python3 scripts/paper_quotes.py --collect --watch --config config/baseline.json --state data/paper_quotes.json
python3 scripts/paper_quotes.py --state data/paper_quotes.json --config config/baseline.json --halt
python3 scripts/paper_quotes.py --state data/paper_quotes.json --replay
```

To enable the LLM entry veto, add `--decisions data/llm_decisions.jsonl` to the
quote command and use a **new** state file. Keep collection and classification
running to supply fresh decisions. A stop request persists even if the quote
request fails; liquidation needs a fresh quote. Ctrl-C exits the process and
preserves state; it does not request liquidation. Run the halt command to do that.

Validation:

```sh
python3 -m unittest discover -s tests -v
```

## CI/CD

[![CI](https://github.com/matsushimen/aitrade/actions/workflows/ci.yml/badge.svg)](https://github.com/matsushimen/aitrade/actions/workflows/ci.yml)

Pull requests and main pushes run offline tests on Python 3.11–3.14, static
checks and configuration validation. Successful main builds deliver a source
archive with a checksum; version tags prepare a draft GitHub Release.
See [CI/CD operations](docs/ci-cd.md) for local checks, release commands and
branch protection setup. No trading process is deployed automatically.
