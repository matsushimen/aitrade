---
name: aitrade-validation
description: Validate and reproduce the aitrade BTC/JPY data collection, CSV export, and baseline backtest. Use when checking bitbank data, investigating candle gaps or simulated fills, or preparing comparable backtest results.
---

# aitrade の動作確認

1. リポジトリのルートで [README.md](../../../../README.md) と対象スクリプトを読み、Python 3.11 以上を使う。公開APIを呼べるか確認し、取得できない場合はエラーを記録する。既存の `data/` に重要なファイルがある場合は、別の作業ツリーで実行する。
2. 次のコマンドを POSIX シェルで実行する。直近の完了日を日本時間で選ぶ。調査対象日が指定されていれば、その日付を使う。

```sh
set -eu
trade_date="$(python3 -c 'from datetime import datetime, timedelta; from zoneinfo import ZoneInfo; print((datetime.now(ZoneInfo("Asia/Tokyo")).date() - timedelta(days=1)).strftime("%Y%m%d"))')"
python3 scripts/collect_bitbank.py --date "$trade_date"
python3 scripts/export_candles.py --start "$trade_date" --end "$trade_date" --output "data/btc_jpy_5min_$trade_date.csv"
python3 scripts/backtest_baseline.py "data/btc_jpy_5min_$trade_date.csv" --fee-bps 15 --slippage-bps 5
```

3. 保存されたAPI応答とCSVを照合する。指定日の日本時間00:00から23:55までの5分足288本がそろい、時刻・OHLCVが対応していることを確認する。欠損がある場合は対象時刻と取得元の応答を調べる。欠損を検査から除外したり、値を補って通過させたりしない。
4. レポートの開始残高が10,000円で、開始残高と終了残高の差が純損益および取引損益の合計と一致することを確認する。取引があれば、確定足でのシグナル、次足の始値、売買両側のコスト、手仕舞い時刻、損切り条件を代表例で照合する。取引ゼロはあり得るため、その場合は約定計算まで確認済みとは扱わず、別の完了日または期間を追加する。
5. 対象日、足の本数、片道手数料・スリッページ、取引数、純損益、最大ドローダウン、未確認事項を報告する。例の 15 bps と 5 bps は動作確認用の仮定。1日分の結果は動作確認に使い、戦略の評価には別期間のコスト込み成績を使う。
