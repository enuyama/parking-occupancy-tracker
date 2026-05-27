# 実装仕様書 (as-built / フェーズ1)

> 本書は **現時点の実装が実際にどう動くか** を記述したもの。
> 「何を作るか・なぜそうするか」は [REQUIREMENTS.md](../REQUIREMENTS.md) と
> [DESIGN_HTTP_RECEIVER.md](DESIGN_HTTP_RECEIVER.md) を参照。
> 実装を変更したら本書も追従させること。

最終更新: 2026-05-26 / 対象: フェーズ1（カメラ信号受信 → カウント → 状態保持）

---

## 1. システム概要

駐車場の入庫/出庫を検知して現在台数をカウントし、満空混ステータスを判定・永続化する Raspberry Pi 上のエッジアプリ。

- 入力: 同一 Pi 上の LinkBase（満空灯制御装置）からの HTTP リクエスト。
- 出力（フェーズ1）: ローカルの状態ファイル＋ログ。サイネージ出力はフェーズ2（未実装）。
- 言語: Python 3.9+（3.11未満は `tomllib` が無いため `tomli` バックポートを使用。requirements.txt で自動導入）。外部依存は FastAPI / uvicorn / gpiozero（GPIO 使用時のみ）。

### データフロー

```
[カメラ XNO-A6084R] --OC--> [LinkBase] --HTTP GET--> [本アプリ]
                                                         │
                          GET /api/control?alert=...     │
                                                         ▼
                              HttpReceiver (エッジ検出)
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

カメラ→LinkBase の物理結線・カメラ設定は本アプリのスコープ外。本アプリの入力境界は HTTP。

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
| `src/parking/receivers/http.py` | **主実装**: FastAPI でHTTP受信 → エッジ検出 → コールバック |
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
type = "http"                 # "http" | "gpio" | "dummy"

[receiver.http]
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
| `http` | HttpReceiver | 本番。LinkBase からの HTTP を受ける |
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
- 主なイベント: `入庫検出: current=N status=...` / `出庫検出: ...` / `ステータス変化: A -> B` / 範囲外や復元失敗の WARNING・ERROR。

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

- **実機結合（カメラ＋LinkBase＋Pi）は未検証。** HTTP 到達後の挙動のみ検証済み。
- LinkBase の送信形式・送信契機（=状態変化時のみ）・極性（`1`=入力あり）は公式仕様＋ソースで確認済み。`active_value` の最終極性（フォトカプラ反転の有無）のみ実機観測で確定。
- 物理ポートと入庫/出庫の対応（`entry_switch`/`exit_switch`）は実配線で確定。
- `min_event_interval` の適正値はカメラのパルス幅実測後に確定。
- フェーズ2（サイネージ出力・手動補正ボタン・LinkBase以外の連携・Web画面）は未着手。
