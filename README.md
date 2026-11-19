作成日時：2026-11-19 16:15（日本時間）／更新日時：2026-11-19 19:30（日本時間）

# bitFlyer 高速bot（公式APIのみ・自作クライアント）

bitFlyer の公式 API（HTTP API と Realtime API）だけを使い、外部ライブラリを一切使わず Python 標準ライブラリだけで書いた売買プログラムです。非公式の API・非公開のエンドポイント・他人のライブラリは使っておらず、参考にもしていません（公式ドキュメントだけを見て作成）。

---

## 最初に：「3日で10万円を100倍」について

**このプログラムは、3日で100倍を目標にした設計にはしていません。** どんなプログラムでも狙って出せる数字ではないためです。`python run.py target` で次の計算をそのまま確かめられます。

| 条件 | 結果 |
|---|---|
| 100倍に必要な増え方 | 1時間あたり +6.6% を 72 時間、休みなく続ける |
| 全額をレバレッジ2倍で3日持ち続ける | BTC が3日で約 50.5 倍にならないと届かない |
| 1回 0.05% 取る売買を1日500回、**全部勝った**場合 | 約 4.48 倍 |
| 勝率55%・1回±0.05%・1日2000回（レバレッジ2倍で毎回全額） | 3日後の中央値 約 1.82 倍。100倍には勝率 88.4% 以上が必要（確率は 10^-686） |

- bitFlyer Crypto CFD のレバレッジは個人だと最大2倍（証拠金率50%〜）です。
- 勝率50%は当てずっぽうと同じです。自分のデータで何%当たるかは `analyze` で測れます。
- 記録データがあれば、`target --data data/` で「未来を完全に知っていた場合（実在しない）の上限」も計算します。

---

## できること

- **自作の API クライアント**（`bfbot/rest.py`, `bfbot/ws.py`）
  - HTTP API: 認証（HMAC-SHA256）、公式の呼び出し上限より手前で止める制限、HTTPS 接続の使い回し
  - Realtime API: WebSocket（RFC 6455）を socket と ssl だけで実装、JSON-RPC 2.0、認証、自動再接続（待ち時間を倍々に伸ばす）
- **高速化**
  - 板と約定を受信した瞬間に判断（この環境での計測では1件あたり中央値 約0.05ミリ秒）
  - 発注は別スレッドで送り、受信処理を止めない
  - 注文イベント（約定・取消）は別の WebSocket 接続で受け取る
  - TCP_NODELAY、HTTPS の接続を張りっぱなしにして毎回の暗号化の準備を省く
  - 受信の遅れ・処理時間・発注の往復時間をログに出す
- **傾向分析**（`bfbot/features.py`, `bfbot/analyze.py`）
  - 短期トレンド（速い指数移動平均と遅い指数移動平均の差）、長めの傾向（1分と15分）、約定の買い/売りの偏り、板の厚みの偏り、注文の流れ（Order Flow Imbalance、Cont, Kukanov, Stoikov 2014）
  - それぞれが「その後の値動きをどれだけ当てるか」を、**スプレッド（売値と買値の差）を払った後の損益**で測る。前半・後半に分けて、たまたまかどうかも見る
- **模擬売買**: 遅延を入れ、約定は自分に不利な側で判定（指値は自分の価格より向こう側で約定が起きた時だけ約定扱い）
- **安全装置**（下に一覧）
- **本番は3重ロック**（全部外さないと発注しない）

## 必要なもの

- Python 3.9 以上（追加のインストールは不要）
- 本番だけ: bitFlyer の API キー

## 使い方（この順番で）

```bash
cp config.example.json config.json        # 設定（必要なら編集）

python run.py record --hours 6            # 1. 板と約定を記録（data/ に保存。長いほど良い）
python run.py analyze data/               # 2. 傾向分析（コスト込みでプラスの組み合わせがあるか）
python run.py backtest data/              # 3. 記録データで売買を再現
python run.py target --data data/ \
  --p-win 0.53 --win-bps 4 --loss-bps 4 --trades-per-day 300   # 4. 3の結果を入れて100倍を検証
python run.py paper --minutes 120         # 5. 実データで模擬売買（お金は動かない）
python run.py check                       # 6. APIキーの確認（読み取りだけ）
```

`analyze` の「まとめ」に、前半・後半ともプラスの組み合わせが出なければ、その設定で本番に出すのはやめてください。

動作確認だけなら、合成データ（作り物の相場）でも全部動かせます:

```bash
python run.py synth --minutes 60
python run.py analyze data_synth/
python run.py backtest data_synth/
```

合成データの損益には意味がありません（わざと予測しやすい相場にしてあります）。

### 模擬相場で「儲かるか」を比べる

```bash
python run.py simulate          # 予測できるクセの強さ別に、3日間 × 3通りの損益を比べる
python run.py simulate --all    # とても強い・極端なクセのケースも入れる
```

相場に「予測できるクセ」がどれくらいあれば bot が黒字になるかを見るためのものです。実際の相場にクセがどれくらいあるかは、記録した実データで `analyze` を使って測ります。

## GitHub 上での「儲かるか」シミュレーションと結果のページ

結果のページ: **https://githitosh.github.io/coin1/**

GitHub Actions の「実データで儲かるか」が次を自動で行います（お金は動かしません。API キーも使いません）。

1. bitFlyer 公式 Realtime API から板と約定を記録（既定 60 分）
2. 傾向分析（各指標がスプレッドを払った後でも値動きを当てるか）
3. 設定の候補を記録の**前半**だけで比べて選び、選ぶのに使っていない**後半**で成績を確かめる
4. 「3日で100倍」の計算と、結果のページ（`gh-pages` ブランチ）の更新

動かし方:

- Actions タブ →「実データで儲かるか」→ **Run workflow**（記録する分数を 5〜300 で指定）
- スマホからは `simulate-request.txt` を書き換えてコミットしても始まります

初回だけ: リポジトリの **Settings → Pages** で、Branch を **gh-pages**・**/(root)** にして Save すると、ページが公開されます。

記録したデータは、Actions の各実行ページから 7 日間ダウンロードできます（`python run.py analyze` などで手元でも調べられます）。本番の発注は GitHub 上では動かしません（キーを置かないため）。

## 主な設定（config.json）

| 項目 | 既定値 | 意味 |
|---|---|---|
| `product_code` | `FX_BTC_JPY` | Crypto CFD（売買手数料無料・売りから入れる）。`BTC_JPY` は現物（手数料あり、売りからは入れない） |
| `strategy.size` | 0.001 | 1回の数量（BTC）。最小発注数量は 0.001 |
| `strategy.entry_threshold` | 0.45 | 合成スコアがこれを超えたら入る |
| `strategy.take_profit_bps` / `stop_loss_bps` | 8 / 6 | 利確・損切り（bps＝0.01%） |
| `strategy.max_hold_sec` | 45 | 最長の保有秒数 |
| `strategy.order_mode` | `taker` | `taker`＝即時約定（IOC：残りは取消）、`maker`＝板に置いて待つ（GTC：取り消すまで有効） |
| `strategy.trend_filter` | true | 長めの傾向に逆らう向きには入らない |
| `risk.max_position_btc` | 0.002 | 建玉の上限 |
| `risk.max_daily_loss_jpy` | 3000 | 1日の損失がこれに達したら停止して決済 |
| `risk.max_drawdown_jpy` | 5000 | 最高値からの下落がこれに達したら停止して決済 |
| `risk.max_orders_per_min` | 20 | 1分あたりの注文数の上限 |

既定値は出発点です。`analyze` と `backtest` の結果を見て決めてください。

## 本番（実際のお金が動きます）

次の3つが全部そろった時だけ発注します。

1. `config.json` に `"live": {"enabled": true}`
2. 環境変数 `BF_LIVE=1`
3. コマンドに `--i-understand-real-money`

```bash
export BF_API_KEY=...
export BF_API_SECRET=...
BF_LIVE=1 python run.py live --i-understand-real-money --minutes 30
```

- API キーの権限は「残高・建玉・注文の参照」「新規注文・取消」「注文のイベントを受信」だけにし、**出金の権限は付けない**でください。キーはファイルに書かず環境変数で渡します。
- 起動時に建玉や未約定の注文が残っていると起動しません（手で片付けてから）。
- 終了時（Ctrl+C・時間切れ・停止）は全注文を取り消し、建玉を決済します。
- 本番は今のところ `FX_BTC_JPY` だけに対応しています（現物は手数料が BTC で引かれ、建玉の管理が違うため）。

## 安全装置

- 1日の損失上限・最高値からの下落上限 → 新規停止＋決済
- 連敗が続いたら一定時間休む
- 注文の間隔と1分あたりの回数を制限（公式の上限 5分300回よりかなり低く）
- 板のデータが遅れたら新規に入らない。データが止まったまま建玉が残ったら HTTP API で決済
- 取引所の状態（`getboardstate`）が NORMAL / BUSY / VERY BUSY かつ RUNNING の時だけ新規に入る
- API エラーが続いたら停止
- 発注の通信が途中で切れて受付の有無が分からない時は、全注文を取り消して取引所の建玉に合わせる（二重発注を防ぐため、発注は自動で再送しない）
- 同時に出す注文は1本だけ。決済は必ず建玉と同じ数量（Crypto CFD は建玉を超えた分が新規建てになるため）

## 使っている公式 API

- HTTP API（`https://api.bitflyer.com/v1/`）: `getboardstate`, `getticker`, `getboard`, `getexecutions`, `gethealth`, `getmarkets`, `me/getcollateral`, `me/getbalance`, `me/getpositions`, `me/getchildorders`, `me/getexecutions`, `me/sendchildorder`, `me/cancelchildorder`, `me/cancelallchildorders`
- Realtime API（`wss://ws.lightstream.bitflyer.com/json-rpc`）: `lightning_board_snapshot_{product}`, `lightning_board_{product}`, `lightning_executions_{product}`, `child_order_events`

## 守っていること

- 約定させる意思のない注文で相場を動かす行為（見せ玉など）は法律で禁止されています。このプログラムが出す注文はすべて約定を目的にしたものです。
- bitFlyer は「注文を繰り返してシステムに負荷をかけている」と判断した場合に API の利用を制限することがあると公式に書いています。注文頻度の上限はそのためでもあります。
- Realtime API は「接続と切断を高頻度で繰り返す」と接続を制限されることがあるため、再接続の間隔を伸ばしていく作りにしています。

## 注意

- 投資の助言ではありません。過去データやペーパーでの成績は将来を保証しません。
- 手数料・レバレッジポイント（日本時間18時までに決済しなかった建玉にかかる。買建玉は 0.04%/日）・ファンディングレートなどの条件は変わることがあります。使う前に bitFlyer の公式ページで確認してください。
- GET のクエリを署名に含める扱いは公式ドキュメントに明記がないため、HTTP のリクエスト行と同じ「パス＋クエリ」で署名しています。`python run.py check` で実際の鍵で確認できます。
- 一番の遅れは通信です。動かす場所での `check` の往復時間と、ログの「受信遅れ」を見てください。

## テスト

```bash
python -m unittest discover -s tests -t .
```

ローカルの偽取引所（bitFlyer には接続しない）で、署名の検証・発注・約定イベント・応答より先に届くイベント・通信断時の照合・終了時の決済・損益の一致まで確かめます（29件）。

## ファイル構成

```
run.py               コマンド
config.example.json  設定の見本
bfbot/rest.py        HTTP API クライアント（自作）
bfbot/ws.py          WebSocket と Realtime API クライアント（自作）
bfbot/book.py        手元の板
bfbot/features.py    傾向分析の指標
bfbot/strategy.py    売買の判断
bfbot/broker.py      模擬売買と本番発注
bfbot/risk.py        安全装置
bfbot/engine.py      全体の流れ（ペーパー・本番・バックテスト）
bfbot/recorder.py    記録
bfbot/analyze.py     傾向分析レポート
bfbot/target.py      「3日で100倍」の検証
bfbot/synth.py       動作確認用の合成データ
tests/               テスト（偽取引所を含む）
```
