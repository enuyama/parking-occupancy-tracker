# 実装仕様書 (as-built / フェーズ1)

> 本書は **現時点の実装が実際にどう動くか** を記述したもの。
> 「何を作るか・なぜそうするか」は [REQUIREMENTS.md](../REQUIREMENTS.md) と
> [DESIGN_HTTP_RECEIVER.md](DESIGN_HTTP_RECEIVER.md) / [DESIGN_MQTT_RECEIVER.md](DESIGN_MQTT_RECEIVER.md) を参照。
> 実装を変更したら本書も追従させること。

最終更新: 2026-06-24 / 対象: フェーズ1（カメラ信号受信 → カウント → 状態保持）

---

## 1. システム概要

駐車場の入庫/出庫を検知して現在台数をカウントし、満空混ステータスを判定・永続化する Raspberry Pi 上のエッジアプリ。

- 入力: 受信層は `receiver.type` で排他選択する。
  - **MQTT（現行・実機検証済み）**: 同一 Pi 上の Mosquitto broker 経由でカメラ（Hanwha XNO-A6084R）の WiseAI 仮想線交差イベントを受信。
  - HTTP（接点経路・**開発中止中(paused)・実機未検証**）: 同一 Pi 上の LinkBase（満空灯制御装置）からの HTTP リクエストを受ける経路。実装済みだが現在は使用しない。
- 出力（フェーズ1）: ローカルの状態ファイル＋ログ。サイネージ出力はフェーズ2（未実装）。
- 言語: Python 3.9+（3.11未満は `tomllib` が無いため `tomli` バックポートを使用。requirements.txt で自動導入）。外部依存は `paho-mqtt`（MQTT 使用時）/ FastAPI / uvicorn（HTTP 使用時）/ gpiozero（GPIO 使用時のみ）。

### データフロー（MQTT 経路・現行）

```
[カメラ XNO-A6084R] --MQTT publish--> [Mosquitto broker :1883] --subscribe--> [本アプリ]
   (ONVIF/WiseAI イベントを                (同一 Pi 上に同居)                    │
    自動 publish、LineCrossing 含む)                                            ▼
                                                          MqttReceiver (State=true を採用)
                                                                                 │ on_entry / on_exit
                                                                                 ▼
                              OccupancyCounter (増減・クランプ・満空混判定)
                                                                                 │
                                                                                 ▼
                              Store (parking_state.json に保存)
                                                                                 │
                                                                                 ▼
                              logging (parking.log に履歴追記)
```

カメラは MQTT クライアント接続を broker に向けるだけで、全 ONVIF/WiseAI イベント（LineCrossing 含む）を自動 publish する。アプリ→broker は `localhost`、カメラ→broker は Pi の LAN 内 IP に接続する。カメラ設定・broker 設定は本アプリのスコープ外。本アプリの入力境界は MQTT トピックの購読。

### データフロー（HTTP 接点経路・**中止中**）

> 以下は接点（LinkBase/GPIO）経路の図。実装は残っているが開発中止中・実機未検証。

```
[カメラ XNO-A6084R] --OC--> [LinkBase] --HTTP GET--> [本アプリ]
                                                         │
                          GET /api/control?alert=...     │
                                                         ▼
                              HttpReceiver (エッジ検出)
                                                         │ on_entry / on_exit
                                                         ▼
                              OccupancyCounter (増減・クランプ・満空混判定)
                                                         ...（以降は共通）
```

カメラ→LinkBase の物理結線・カメラ設定は本アプリのスコープ外。この経路の入力境界は HTTP。

---

## 2. モジュール構成

| ファイル | 責務 |
| --- | --- |
| `src/parking/__main__.py` | エントリポイント。`python -m parking [config.toml]` |
| `src/parking/app.py` | 配線。config ロード → store 復元 → counter 構築 → receiver 起動 → シグナル待ち |
| `src/parking/config.py` | `config.toml` のロードと dataclass 化・バリデーション |
| `src/parking/models.py` | `OccupancyStatus`(Enum: FULL/CROWDED/EMPTY)、`State` |
| `src/parking/counter.py` | 現在台数の保持・増減・クランプ・満空混判定（純ロジック、I/O なし） |
| `src/parking/store.py` | 状態の JSON ファイル永続化（原子的書き込み） |
| `src/parking/receivers/base.py` | 受信層の Protocol（`start()` / `stop()`） |
| `src/parking/receivers/mqtt.py` | **現行の主実装**: paho-mqtt で broker を購読し `Data.State == count_state` を検出 → `RuleName` で入庫/出庫に振り分けてコールバック |
| `src/parking/receivers/http.py` | 接点経路の実装（**中止中**）: FastAPI でHTTP受信 → エッジ検出 → コールバック |
| `src/parking/receivers/gpio.py` | 代替: gpiozero による GPIO 接点入力（フェーズ1では未使用） |
| `src/parking/receivers/dummy.py` | 開発用: stdin から `i`/`o` を読んでイベント発火 |

---

## 3. HTTP API（HttpReceiver）

`receiver.type = "http"` のとき、`[receiver.http].host:port`（デフォルト `127.0.0.1:8080`）で待ち受ける。

### 3.1 `GET /api/control`

LinkBase からの状態通知を受ける主エンドポイント。

| クエリ | 必須 | 説明 |
| --- | --- | --- |
| `alert` | ○ | 8桁。上位4桁が SW1〜SW4 状態、各桁 `0`/`1`/`9` |
| `id` | × | SIMカードID等。ログ用途のみ |

**レスポンス:**

| 条件 | HTTP | ボディ |
| --- | --- | --- |
| 正常 | 200 | `{"status":"ok","entries":<0/1>,"exits":<0/1>, ...state}` |
| 全桁が `9` | 200 | `{"status":"ok","message":"all_nines"}`（ノーオペ） |
| `alert` なし | 400 | `{"detail":"Parameter_not_found"}` |
| `alert` が8桁でない | 400 | `{"detail":"Invalid_parameter_length"}` |
| `alert` が `0/1/9` 以外を含む | 400 | `{"detail":"Parameter_contains_invalid_value"}` |

- `entries` / `exits` は当該リクエストで検出したエッジ数（0 または 1）。
- 下位4桁が `9999` でない場合はエラーにせず WARNING ログのみ。
- `...state` 部分は state_provider があれば `current` / `total` / `occupancy` がマージされる。

### 3.2 `GET /health`

`{"status":"healthy","current":N,"total":M,"occupancy":"..."}`（state は state_provider 由来）。

### 3.3 `GET /state`

`{"current":N,"total":M,"occupancy":"..."}` のみを返す（運用デバッグ用）。

---

## 4. エッジ検出アルゴリズム（http.py）

LinkBase は接点の**状態が変化した時だけ**送信する（常時ポーリング送信ではない。実ソース確認済み、[DESIGN_HTTP_RECEIVER.md](DESIGN_HTTP_RECEIVER.md) §4.3）。
本アプリは「ACTIVE になった瞬間（立ち上がり）」のみを 1 イベントとして数える設計で、LinkBase が将来どちらの送信方式でも正しく動く。

### 4.1 定義

- `entry_switch` / `exit_switch`: alert の何桁目（=LinkBaseの接点入力ポートIN番号）を入庫/出庫として見るか（1〜4、`config` 指定）。
- `active_value`: alert 文字で ACTIVE(入力あり) を意味する値（`"0"` または `"1"`）。LinkBase公式仕様 §6 で `1`=入力あり のため既定は `"1"`（フォトカプラ反転構成なら `"0"`、実機で確定）。
- 内部に `last_entry` / `last_exit`（前回観測した文字）を保持。**起動直後は非ACTIVE で初期化**（起動時の偽カウント防止）。

### 4.2 1リクエストの処理（entry / exit で同一ロジック）

対象桁の文字 `sw` について:

1. `sw == "9"` → **何もしない**（前回状態を上書きもしない＝維持）。
2. `sw == active_value` かつ `last != active_value`（立ち上がり）かつ
   前回エッジ確定から `min_event_interval` 秒以上経過 → **イベント発火**、エッジ時刻を更新。
3. `last = sw` に更新。

立ち下がり（ACTIVE→非ACTIVE）と ACTIVE 維持はイベントにならない。
entry と exit は独立に判定され、同一リクエストで両方発火しうる（その場合 entry → exit の順でコールバック）。

### 4.3 並行性

受信ハンドラ全体を `threading.Lock` 1本で直列化。コールバック（counter 更新）もロック内で呼ぶ。

### 4.4 既知のトレードオフ

`min_event_interval`（既定0.5秒）以内の「立ち下がり→再立ち上がり」は、正当な2台目でも抑制される。
単一ゲートで0.5秒以内の連続入庫は物理的に稀という前提。実機のパルス幅実測後に値を詰める（未確定）。

---

## 4A. MQTT受信（mqtt.py） — 現行の主実装

`receiver.type = "mqtt"` のとき有効。カメラ（Hanwha XNO-A6084R）の WiseAI 仮想線交差（LineCrossing）イベントを、同一 Pi 上の Mosquitto broker 経由で受信する受信層。**実機結合検証済み（2026-06-24）**。設計詳細は [DESIGN_MQTT_RECEIVER.md](DESIGN_MQTT_RECEIVER.md)。

### 4A.1 トピックとペイロード（実測）

- カメラが publish するトピック: `<カメラMAC>/onvif-ej/OpenApp/WiseAI/LineCrossing/&vs-0/<線名>`
- アプリの購読トピック（既定）: `+/onvif-ej/OpenApp/WiseAI/LineCrossing/#`
- ペイロード（実測例）:

```json
{
  "UtcTime": "...",
  "Source": { "VideoSourceToken": "vs-0", "RuleName": "entry" },
  "Data": { "State": "true", "ObjectId": "3147", "Action": "Left" }
}
```

- 1 通過につき `State:"true"` が 1 発 → 数秒後に `State:"false"` が 1 発、の 2 メッセージが来る。アプリは **`"true"` のみ採用**し、1 通過 = 1 カウントとする。
- 対象物（車/人）の選別は**カメラ側**（仮想線の対象物フィルタ）で行う。アプリは `ObjectType` を見ない。

### 4A.2 接続・購読

- paho-mqtt 2.x（`CallbackAPIVersion.VERSION2`）を使用。
- `connect_async()` + `loop_start()` でノンブロッキング起動。**broker 未起動でも `start()` は失敗しない**（起動次第 `on_connect` で接続成功ログ）。
- subscribe は必ず `on_connect` 内で実行（QoS 1）。再接続時にも購読が復帰することを保証するため。
- 認証あり（既定ユーザー `parking`）。`username` 指定時のみ `password` を設定。認証拒否は ERROR ログで目立たせる。

### 4A.3 State フィルタと RuleName 振り分け

1. 受信ペイロードを JSON 解析（失敗・非オブジェクトは WARNING で破棄、payload を添付）。
2. `Data.State` を小文字化して `count_state`（既定 `"true"`、config ロード時に小文字正規化）と比較。一致しないもの（`"false"` 等）は採用しない。
3. `Source.RuleName` を `rules` テーブル（`RuleName → "entry"/"exit"`）で引き、`entry` → `on_entry`、`exit` → `on_exit` を呼ぶ。**未登録の `RuleName` は WARNING を出して無視**。
4. 線名は `entry`（入庫）/ `exit`（出庫）が確定値。

### 4A.4 連続カウント抑制（min_event_interval、線単位）

- `min_event_interval`（既定 `0.0`）は **`RuleName`（線）ごと**に前回採用時刻を保持して抑制する。別入口の同時通過を取りこぼさないため線単位とする。
- 実測では 1 通過 = `"true"` 1 発のため、既定 `0.0`（抑制なし）で十分。

### 4A.5 自動再接続・ハートビート

- 切断・接続失敗は paho-mqtt の自動再接続（指数バックオフ `min_delay=1`〜`max_delay=60` 秒）に任せる。LAN 断・broker 再起動に耐える。
- ハートビートスレッド（既定 `heartbeat_interval=60` 秒、`0` で無効）が `connected` / 受信総数 / 採用総数 / 直近受信時刻を定期 INFO 出力。

### 4A.6 graceful stop

- `stop()` はハートビートスレッドを停止 join → `loop_stop()`（ネットワークスレッド停止）→ `disconnect()` の順。順序を守らないと自動再接続スレッドが残り再接続し続けるため。二重呼び出し・未 `start()` でも安全。

### 4A.7 並行性

- paho-mqtt のコールバックはネットワークスレッドから呼ばれるため、カウント反映・内部状態（統計カウンタ・線ごとの前回採用時刻）を `threading.Lock` 1 本で保護する。
- 1 メッセージの異常で受信ループを止めない（全体を try/except で囲い WARNING/exception して破棄・継続）。

---

## 5. カウントと満空混判定（counter.py）

### 5.1 カウント

- `record_entry()`: `current + 1`。`current + 1 > total_spaces` なら**拒否**（`accepted=False`、値は変えず WARNING ログ）。
- `record_exit()`: `current - 1`。`current - 1 < 0` なら**拒否**（同上）。
- 初期値 `initial_count` は `0..total_spaces` にクランプして保持。

### 5.2 満空混判定（絶対値）

現在台数の絶対値で判定する（割合ではない）:

```
current >= full_at      -> FULL
current >= crowded_at    -> CROWDED   （かつ < full_at）
それ以外                  -> EMPTY
```

制約: `0 < crowded_at <= full_at <= total_spaces`（config ロード時に検証）。

例: `total=100, crowded_at=80, full_at=100` → 79台=空 / 80台=混 / 100台=満。

### 5.3 戻り値 `CountResult`

`accepted`（範囲内か）/ `current` / `status` / `status_changed`（直前ステータスから変化したか）。

---

## 6. 永続化（store.py）

### 6.1 形式

単一の JSON ファイル（`[storage].state_file`、既定 `parking_state.json`）に最新状態のみ:

```json
{
  "current_count": 12,
  "status": "CROWDED",
  "updated_at": "2026-05-26T07:34:21.123456+00:00"
}
```

- `updated_at` は UTC ISO8601。
- 書き込みは同一ディレクトリの一時ファイル → `os.replace` で原子的に置換（電源断時の半端書き込み防止）。
- 親ディレクトリが無ければ自動作成。

### 6.2 復元

起動時 `restore()`:
- ファイルが無い → `None`（→ counter は0台で開始）。
- JSON 破損 / 必須キー欠落 → ERROR ログを出して `None`（0台で開始）。
- 正常 → `current_count` を counter の初期値に流し込む。

### 6.3 履歴

入出庫・ステータス変化の履歴は永続化対象外。`logging.file`（parking.log）に追記される。

---

## 7. 設定ファイル（config.toml）

`config.example.toml` がサンプル。実ファイル `config.toml` は `.gitignore` 対象。
起動引数で別パス指定可（`python -m parking <path>`、省略時 `config.toml`）。

```toml
[parking]
total_spaces = 100            # 物理的な総台数（>0）

[thresholds]
crowded_at = 80               # この台数以上で混
full_at    = 100              # この台数以上で満。0 < crowded_at <= full_at <= total_spaces

[receiver]
type = "mqtt"                 # "mqtt"(現行) | "http"(中止中) | "gpio" | "dummy"

[receiver.mqtt]               # type="mqtt" のときのみ参照（現行・実機検証済み）
host = "localhost"            # アプリ→broker は localhost（同一 Pi 同居）。省略時 "localhost"
port = 1883                   # 省略時 1883
username = "parking"          # broker 認証ユーザー。省略時 ""（認証なし）
password = "..."              # username 指定時のみ設定。コードに直書きしない（実ファイルで設定）
client_id = "parking-tracker" # 省略時 "parking-tracker"
keepalive = 60                # 省略時 60
topic = "+/onvif-ej/OpenApp/WiseAI/LineCrossing/#"  # 購読トピック。省略時この値
count_state = "true"          # この State のみ採用。小文字正規化される。省略時 "true"
min_event_interval = 0.0      # 連続カウント抑制(秒、線単位)。省略時 0.0（抑制なしで十分）
heartbeat_interval = 60.0     # 死活ログ間隔(秒)。0 で無効。省略時 60.0

[receiver.mqtt.rules]         # RuleName(仮想線名) → "entry"/"exit"
entry = "entry"               # 入庫線
exit  = "exit"                # 出庫線

[receiver.http]               # type="http" のときのみ参照（接点経路・中止中）
host = "127.0.0.1"            # localhost のみ受付（外部公開しない）
port = 8080
entry_switch = 1              # 入庫として見る SW番号 (1..4)
exit_switch  = 2              # 出庫として見る SW番号 (1..4、entry と別値)
active_value = "0"            # ACTIVE を意味する文字 ("0" | "1")
min_event_interval = 0.5      # エッジ抑制間隔(秒)。省略時 0.5

[receiver.gpio]               # type="gpio" のときのみ参照（フェーズ1未使用）
entry_pin    = 17
exit_pin     = 27
pull_up      = true
bounce_time  = 0.05
min_interval = 0.2

[storage]
state_file = "parking_state.json"

[logging]
level = "INFO"                # ルートロガーのレベル
file  = "parking.log"         # ローテーション: 5MB × 3世代
```

バリデーションは config ロード時に実施し、違反は `ValueError` / `FileNotFoundError` で起動失敗。

---

## 8. 受信層の種別

| type | 実装 | 用途 |
| --- | --- | --- |
| `mqtt` | MqttReceiver | **本番・現行（実機検証済み）**。同一 Pi 上の Mosquitto broker 経由でカメラの WiseAI 仮想線交差イベントを購読し `State=true` を採用 |
| `http` | HttpReceiver | 接点経路（**中止中・実機未検証**）。LinkBase からの HTTP を受ける構成用。実装は残存 |
| `gpio` | GpioReceiver | 代替。LinkBase を介さずカメラ OC を Pi GPIO 直結する構成用。`when_pressed` で発火、`min_interval` で連発抑制。実機が無くても gpiozero の mock pin factory で起動可 |
| `dummy` | DummyReceiver | 開発。stdin に `i`(入庫)/`o`(出庫)/`q`(終了) を1行ずつ |

いずれも `start()`（ノンブロッキング）/ `stop()` を持ち、検出時に `on_entry` / `on_exit` を呼ぶ。

---

## 9. 起動・終了ライフサイクル

1. config ロード（無ければ stderr に案内して終了コード2）。
2. logging 初期化（stdout + ローテーションファイル）。
3. Store 構築 → `restore()` → counter 構築 → 復元直後の状態を1度保存。
4. `receiver.type` に応じた受信層を構築・`start()`。
5. SIGTERM / SIGINT を待つ（`threading.Event`）。
6. シグナル受信で `receiver.stop()` → `store.close()` → 終了。

systemd ユニット `systemd/parking.service` は `After=...light-controller.service`（LinkBase）で起動順を後ろにする。`Restart=on-failure`。

---

## 10. ログ

- 形式: `%(asctime)s %(levelname)s %(name)s: %(message)s`
- 出力先: 標準出力 ＋ `logging.file`（RotatingFileHandler 5MB×3）。
- 主なイベント: `入庫検出: current=N/total status=...` / `出庫検出: ...` / `ステータス変化: A -> B` / 範囲外や復元失敗の WARNING・ERROR。

### MQTT 受信（mqtt.py）の主なログ

- INFO `MqttReceiver 起動: broker=... topic=... count_state=... rules=N件 ...（broker 未接続でも接続を待機します）`
- INFO `MQTT 接続成功: <host>:<port> を購読 topic=... (QoS 1)`
- INFO `MQTT heartbeat: connected=.., msgs=.., counted=.., last_msg=..`
- DEBUG `MQTT rules マッピング(RuleName→方向): {...}` / `MQTT SUBACK 受信: ...` / `MQTT受信(raw): topic=.. payload=..` / `入庫検出(線='entry'): カウント反映。data={...'ObjectId':..}`
- WARNING `未登録の RuleName '...'。rules テーブルに無いため無視します (topic=..)` / JSON 解析失敗時は payload を添付。
- 接続失敗時: 認証拒否は ERROR、それ以外は WARNING（自動再接続を継続）。意図しない切断は WARNING、`stop()` による切断は INFO。

---

## 11. テスト

`pytest`（`pip install -r requirements-dev.txt`）。現在56件。

| ファイル | 対象 |
| --- | --- |
| `tests/test_counter.py` | 増減・クランプ・しきい値境界・status_changed |
| `tests/test_config.py` | ロードとバリデーション |
| `tests/test_store.py` | 保存/復元/破損時/原子的書き込み |
| `tests/test_http_receiver.py` | エッジ検出全パターン・極性・バリデーション・各エンドポイント |
| `tests/test_app.py` | 配線・範囲外・再起動復元・スナップショット |

---

## 12. 未実装 / 未検証（フェーズ1時点）

- **MQTT 実機結合（カメラ＋Mosquitto broker＋Pi）は検証済み（2026-06-24）。** トピック・ペイロード・1通過1カウント（`State:"true"`1発）を実測で確認済み。MQTT が現行・本番経路。
- **接点（HTTP + LinkBase）経路は実装済みだが開発中止中(paused)・実機未検証。** HTTP 到達後の挙動のみ検証済み。LinkBase の送信形式・送信契機（=状態変化時のみ）・極性（`1`=入力あり）は公式仕様＋ソースで確認済みだが、`active_value` の最終極性・物理ポート対応（`entry_switch`/`exit_switch`）・`min_event_interval` 適正値は未確定（実機検証が中止中のため）。
- フェーズ2（サイネージ出力・手動補正ボタン・Web画面）は未着手。
