# Garuda Linuxへの移行

ユーザー権限のsystemdで、データ収集・LLM分類・仮想取引を分離して動かします。
この手順は実注文を開始しません。移行先PCで実行してください。

## 1. 導入と動作確認

Garudaの通常の更新手順（`garuda-update`）でシステムを更新してから、必要なパッケージを入れます。
Arch系では`pacman -Sy`だけを使った部分更新を避けてください。

```sh
sudo pacman -S --needed git python python-pip tzdata ca-certificates
python --version
git clone https://github.com/matsushimen/aitrade.git "$HOME/aitrade"
cd "$HOME/aitrade"
python -m venv .venv
.venv/bin/python -m unittest discover -s tests -v
```

Python 3.11以上が必要です。systemd設定は`~/aitrade`への配置を前提にしています。
この移行用PRが未マージの場合は、clone後に次を実行して移行用ブランチを使います。

```sh
git switch --track origin/deploy/garuda-linux
```

実行時にpipパッケージは不要です。既存のソースディレクトリがある場合はcloneで上書きせず、
変更内容を確認してから更新してください。venvやAPIキーはこのクラウド環境からコピーしません。

## 2. 最初はLLMなしで起動

```sh
cd "$HOME/aitrade"
.venv/bin/python scripts/linux_service.py collect
mkdir -p "$HOME/.config/systemd/user"
cp deploy/systemd/aitrade-* "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now aitrade-collect.timer
systemctl --user enable --now aitrade-paper@paper.service
systemctl --user status aitrade-paper@paper.service
journalctl --user -u aitrade-paper@paper.service -n 50 --no-pager
systemctl --user list-timers 'aitrade-*'
```

収集は5分境界の10秒後、paperは15秒間隔でquoteを観測します。
直近のSMA窓とLLM入力に必要なJST日を取得するため、日付をまたいだ起動にも対応します。
通信失敗などでpaperプロセスが終了すると30秒後に再起動します。
収集と分類はoneshotサービスのため、失敗した場合は次のタイマー実行で再試行します。

状態は`data/paper_baseline.json`、DBは`data/aitrade.sqlite3`に保存されます。
欠損や改訂を検出した際はログを確認し、原因を解消してください。
状態ファイルを削除してエラーを隠すと、仮想残高やポジションの引き継ぎが失われます。

## 3. OpenAI互換LLMの設定

```sh
install -d -m 700 "$HOME/.config/aitrade"
install -m 600 deploy/llm.env.example "$HOME/.config/aitrade/llm.env"
${EDITOR:-nano} "$HOME/.config/aitrade/llm.env"
```

接続先URL、モデル名、APIキーを設定してください。接続先は`/v1`などのバージョンパスまで含めます。
Structured Outputsに対応しないサーバーでは`json_object`または`text`を指定します。
認証不要の場合のみ`AITRADE_LLM_NO_AUTH=1`にし、キーは空欄にします。
環境ファイルはsystemd形式です。`export`は付けず、値に空白がある場合は引用符で囲みます。
ファイルをリポジトリにコピーしたり、キーをコマンド引数に書いたりしないでください。

```sh
systemctl --user start aitrade-classify.service
journalctl --user -u aitrade-classify.service -n 30 --no-pager
systemctl --user enable --now aitrade-classify.timer
systemctl --user enable --now aitrade-paper@paper-llm.service
```

分類は5分境界の40秒後に実行します。LLM入力と応答受信時刻をそのまま記録し、過去にはさかのぼりません。
LLM併用paperは`data/paper_llm.json`、分類は`data/llm_decisions.jsonl`に保存します。
キーを読むのは分類サービスだけです。paperサービスにはキーを渡しません。
同一足を手動実行とタイマーが重ねて分類した場合は、重複API呼び出しを避けるため既存記録を検出して終了します。

LLMあり/なしは同じデータ・初期条件で将来期間に比較します。
起動時刻が異なれば実時間の観測価格も異なるため、単純な残高差だけで効果を判断せず、
蓄積後に同じ期間のCSVと分類ログで`evaluate.py`を実行してください。
LLM呼び出しは提供元の料金がかかります。分類タイマーを無効化すれば呼び出しも止まります。

## 4. ログイン前の起動・スリープ

ログアウト後やPC起動直後もユーザーサービスを使う場合は、必要に応じてlingerを有効にします。

```sh
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

**lingerはサスペンドや電源断を防ぎません。** 継続観測する時間帯はGaruda/KDEの電源管理で
自動サスペンドを見直してください。ディスクをログイン時に解除する構成などでは、ホームへのアクセスも必要です。
停止中のquoteやLLM判断は再現できず、再開後にその時間の約定や分類を作り直しません。
PCの時刻同期が有効であることも`timedatectl status`で確認してください。

## 5. 停止・仮想ポジションの手仕舞い

プロセス停止だけでは仮想保有を売却しません。
まず自動再起動を止め、その後に永続停止要求と次の新鮮なbidでの仮想売却を実行します。

```sh
systemctl --user stop aitrade-paper@paper.service aitrade-paper@paper-llm.service
cd "$HOME/aitrade"
.venv/bin/python scripts/linux_service.py halt
.venv/bin/python scripts/linux_service.py halt-llm
```

開始していない方式のhaltは不要です。通信断やデータ欠損で売却できなくても停止要求は保存されます。
ログと状態を確認し、復旧後に同じhaltを再実行してください。
停止済み状態では再起動しても新規買いを再開しません。新しい実験は、サービスを停止した状態で
既存stateを別名に退避し、初期条件と開始日時を記録してから始めてください。
収集・LLM呼び出しも止める場合は次を実行します。

```sh
systemctl --user disable --now aitrade-classify.timer aitrade-collect.timer
systemctl --user stop aitrade-classify.service aitrade-collect.service
systemctl --user disable aitrade-paper@paper.service aitrade-paper@paper-llm.service
```

## 6. データ引き継ぎ・更新

GitHubにあるのはソースだけで、既存`data/`は含まれません。新規paperを開始するなら、手順2で
必要な足を取得すれば足ります。クラウド環境の研究用データ・レポートも必要なら別途転送します。
既存の仮想ポジションを引き継ぐ場合は、元環境の全プロセスを止めてから`data/`をコピーし、
同じ設定ファイルを使ってください。二つの環境を同じ実験の続きとして同時に動かさないでください。
SQLiteをコピーするときは書き込みを止め、WALを使用している場合はそのファイルも含めるか、
SQLiteのbackup APIでスナップショットを作ってください。状態ファイルは途中で編集しません。

更新時はpaperとタイマー・実行中のoneshotを停止し、`git pull --ff-only`、テスト、必要なら
unitの再コピーと`daemon-reload`を行ってから再開します。
GarudaのPython更新でvenvが使えなくなった場合は、プロセス停止後に`.venv`を作り直してください。
古い仮想ポジションがあるままstateを削除することは避けてください。

継続運用ではjournalと`data/`のディスク使用量も監視してください。
この設定はユーザーPCでの起動と再起動を整えるもので、常時稼働・約定・利益を保証するものではありません。
