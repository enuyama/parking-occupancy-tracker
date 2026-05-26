# 受信層設計書: LinkBase 経由 HTTP 受信

> **位置付け:** REQUIREMENTS.md §5.1 / §6.1 / §7 / §10 で「カメラ OC → Pi GPIO 直結」としていた受信方式を、
> 「カメラ OC → LinkBase (満空灯制御装置) → localhost HTTP → 本アプリ」に変更する。本書はその差分設計のみを扱う。
> REQUIREMENTS.md の他セクション（カウント・永続化・満空混判定など）は変更なし。

最終更新: 2026-05-26（LinkBase 実ソース確認を反映）

## 1. 背景

- 同一 Pi 上で LinkBase（Pi-protect / 満空灯制御装置）が動作しており、こちらが既にカメラ側 OC をGPIO で受け、HTTP で他アプリに通知できる「通知モード4（全ポート通知モード）」を持つ。
- カメラ OC → 本アプリ Pi GPIO への直結配線・フォトカプラ実装を新規に組むより、既存 LinkBase をそのまま使う方が配線・運用が単純（屋外配線・電気仕様詰めの大半をスキップ可能）。
- 参考実装: `tbbox-playlist-switcher`（同じ LinkBase 通知を FastAPI で受けてTBBOX を切り替えるアプリ）。プロトコル詳細はそちらの `docs/DESIGN_HTTP_SWITCH.md` と一致。

## 2. アーキテクチャ

### 2.1 変更前（REQUIREMENTS.md 当初案）
```
[XNO-A6084R] ─IO1/IO2 (OCパルス)──> [本アプリ Pi GPIO]
```

### 2.2 変更後（本設計 / LinkBase 実ソース確認済み）
```
[XNO-A6084R]                       同一 Pi 内
   │ IO1 (入庫 OCパルス)            ┌──────────────────────────────────┐
   │ IO2 (出庫 OCパルス)            │ [LinkBase (light-controller)]    │
   ├──────────────────────────────> │  MCP23017(I2C) 入力ピン8-11 監視  │
                                    │  状態が変化した時のみ送信        │
                                    │  Mode=4 (全桁そのまま送信)       │
                                    │       │                          │
                                    │       ▼ HTTP GET                 │
                                    │  /api/control?alert=…&id=SIM_ID  │
                                    │       │                          │
                                    │       ▼                          │
                                    │ [本アプリ (parking)]             │
                                    │  HTTPReceiver (FastAPI:8080)     │
                                    │   → エッジ検出                   │
                                    │   → counter.record_entry/exit    │
                                    │   → store / status               │
                                    └──────────────────────────────────┘
```

- LinkBase は Pi にI2C接続した **MCP23017（GPIO拡張IC, `dtoverlay=mcp23017,addr=0x20`）** の入力ピン8〜11を読む。**Pi本体の40ピンGPIOではない。**
- 物理配線（カメラ OC → MCP23017 入力端子）は外部業者の責務。本アプリから見える入力は HTTP のみ。
- LinkBase 自身は別の FastAPI を **port 80** で持ち、逆方向（HTTP受信→リレー出力で表示灯駆動）も担う。本アプリは port 8080 を使うので競合しない。

## 3. プロトコル

LinkBase → 本アプリの HTTP リクエスト仕様は `tbbox-playlist-switcher/docs/DESIGN_HTTP_SWITCH.md` §3 と同一。

```
GET /api/control?alert=<8桁>&id=<SIMカードID>
```

- `alert` は 8 桁、上位 4 桁が SW1〜SW4 の状態、下位 4 桁は `9999` 固定。
- 各桁は `0` / `1` / `9` のいずれか。`9` は「状態非表示」。
- alert 桁 ↔ MCP23017 入力ピンの対応（LinkBase `main.py` の `_convert_input` で確定。`INPUT_PORT=[8,9,10,11]` を `reversed` して `1-i` 反転）:

  | alert 桁 | 名称 | MCP23017 入力ピン | 値 |
  | --- | --- | --- | --- |
  | 1 桁目 | SW1 | pin 11 (GPB3) | `1 - v11` |
  | 2 桁目 | SW2 | pin 10 (GPB2) | `1 - v10` |
  | 3 桁目 | SW3 | pin 9 (GPB1) | `1 - v9` |
  | 4 桁目 | SW4 | pin 8 (GPB0) | `1 - v8` |

- 値の極性: `1-i` の反転が入ることはソースで確定。ただし「接点クローズ＝alert `0` か `1` か」の**絶対極性は、カメラOC→MCP入力の配線・プルアップ依存で未確定**。
  本設計は参考実装(tbbox)の観測（接点クローズ→`0`）に合わせ `active_value="0"` を既定とするが、**実機で1回観測して確定する**（§13）。

レスポンス:

| ステータス | ボディ | 用途 |
| --- | --- | --- |
| 200 | `{"status":"ok","entries":0,"exits":0,"current":12,"occupancy":"EMPTY"}` | 正常 |
| 400 | `{"detail":"<error_code>"}` | パラメータ異常 |
| 500 | `{"detail":"Internal_error: ..."}` | 内部エラー |

`entries` / `exits` は今回のリクエストで検出したエッジ数（通常 0 か 1）。デバッグ用途。

## 4. 受信ロジック設計

### 4.1 ポート割り当て（config で可変）

`config.toml` の `[receiver.http]` で SW 位置と用途を結びつける。

```toml
[receiver]
type = "http"   # "http" | "gpio" | "dummy"

[receiver.http]
host = "127.0.0.1"
port = 8080
entry_switch = 1   # SW番号 (1..4) — 入庫として扱う桁
exit_switch  = 2   # SW番号 (1..4) — 出庫として扱う桁
active_value = "0" # alert文字で ACTIVE を意味する値。LinkBase 反転仕様に合わせて "0"
                   # 反転無しの構成に切り替えたい場合は "1" に変更
```

未使用 SW（上記例では SW3/SW4）は **無視**。`9`（状態非表示）も無視。

### 4.2 エッジ検出

本アプリは内部で「直近に観測した entry/exit SW の状態」を保持し、リクエスト到着毎に **0→ACTIVE への遷移（立ち上がりエッジ）** を検出する。

```python
# 疑似コード
def on_request(alert: str) -> tuple[int, int]:
    sw_entry_now = alert[entry_switch_index]   # '0' / '1' / '9'
    sw_exit_now  = alert[exit_switch_index]

    entries = exits = 0
    if sw_entry_now != "9":
        if sw_entry_now == ACTIVE and last_entry != ACTIVE:
            entries = 1
        last_entry = sw_entry_now
    if sw_exit_now != "9":
        if sw_exit_now == ACTIVE and last_exit != ACTIVE:
            exits = 1
        last_exit = sw_exit_now
    return entries, exits
```

ポイント:
- 立ち下がり（ACTIVE→非ACTIVE）はカウントしない。
- `9` は前回状態を上書きしない（= 前回状態を維持する keep_previous 動作）。
- 起動直後の初期状態は「非ACTIVE」とみなす。これは「Pi 起動直後にカメラ側が ACTIVE 維持中だった場合に偽の +1 を打たないため」の安全側設定。
- entries と exits を同じリクエスト内で同時に 1 ずつ立てる可能性は許容（複合イベント）。順序は entry → exit の順で counter に反映する（恣意的だが一貫していればよい）。

### 4.3 デバウンス・最小間隔（LinkBase 実挙動を反映）

LinkBase 実ソース（`main.py` の `Input.run`）の挙動:
- 入力状態が**変化した時だけ** HTTP 送信する（`if new_input == tmp: continue`）。常時 polling 送信ではない。
- 送信後 **`DI_WAIT=1秒` 停止**する。監視ループ自体も `MONITOR_INTERVAL=0.2s` 間隔。

このため:
- 同じ ACTIVE 状態が連続して届くことは通常ない（変化時のみ）。エッジ検出の二重カウントは構造的に起きにくい。
- 一方で **パルスが短いと送信後1秒の停止中に閉→開が収まり、1台分を取りこぼす**。
  → カメラのアラーム出力パルス幅・車の間隔は **1.5秒以上**を推奨（[INTEGRATION_GUIDE.md](INTEGRATION_GUIDE.md) §10 参照）。

`min_event_interval`（秒, 既定 `0.5`）は接点チャタリング対策の保険。エッジ検出後この時間内の再エッジを無視する。
LinkBase が既に >1秒間隔に律速するため、通常は干渉しない。

```toml
[receiver.http]
min_event_interval = 0.5
```

### 4.4 状態の永続性

`last_entry` / `last_exit` の状態は **メモリ保持のみ**。Pi 再起動時はリセットされる（§4.2 のとおり初回は非ACTIVE想定）。

理由: カメラのアラーム出力は数百 ms のパルスであり、Pi 再起動を跨いで「ACTIVE 維持中」のまま観測される可能性は低い。永続化するメリットより、起動時の偽カウント排除を優先する。

### 4.5 並行性

FastAPI は通常 1 リクエスト/スレッドで処理するが、`last_entry` / `last_exit` / `counter` を共有するため `threading.Lock` を 1 本持ち、受信ハンドラ全体をロックする。LinkBase からの送信は同一クライアント・低頻度（送信後1秒停止のため概ね 1 回/秒以下）なので競合の心配は薄いが、明示的にシリアライズする。

## 5. エンドポイント仕様

| パス | メソッド | 用途 |
| --- | --- | --- |
| `/api/control` | GET | LinkBase からの状態通知。Query: `alert`(必須), `id`(任意) |
| `/health` | GET | 死活確認。`{"status":"healthy","current":N,"occupancy":"..."}` |
| `/state` | GET | 現在の台数・ステータスの参照用（運用デバッグ）。後で Web 画面検討時に拡張 |

### 5.1 バリデーション（`/api/control`）

参考実装 §8.1 と同じ:

| 条件 | HTTP | detail |
| --- | --- | --- |
| `alert` が空 | 400 | `Parameter_not_found` |
| `alert` が 8 桁でない | 400 | `Invalid_parameter_length` |
| `alert` が `0/1/9` 以外を含む | 400 | `Parameter_contains_invalid_value` |
| 全桁が `9` | 200 | `{"status":"ok","message":"all_nines"}` （ノーオペ） |
| 下位 4 桁が `9999` でない | 200 警告ログ | エラーにはしない（参考実装に倣う） |

## 6. 設定ファイル差分 (config.toml)

REQUIREMENTS.md §10.3 の案に対する差分:

```toml
[parking]
total_spaces = 100

[thresholds]
crowded_at = 80          # 絶対値判定（割合判定から変更済み）
full_at    = 100

[receiver]
type = "http"   # ★ デフォルトを "gpio" から "http" に変更

[receiver.http]            # ★ 新規セクション
host = "127.0.0.1"          # localhost のみ受け付け。外部公開しない
port = 8080
entry_switch = 1
exit_switch  = 2
active_value = "0"
min_event_interval = 0.5

[receiver.gpio]             # 互換のため残置。実機で使う場合のみ参照
entry_pin   = 17
exit_pin    = 27
pull_up     = true
bounce_time = 0.05
min_interval = 0.2

[storage]
state_file = "parking_state.json"   # SQLite から JSON ファイルに変更済み

[logging]
level = "INFO"
file  = "parking.log"
```

`host = "127.0.0.1"` で **外部からのリクエストは受けない**（同一 Pi の LinkBase のみが叩く想定）。本アプリは認証機構を持たないので、ネットワーク到達性で防御する。

## 7. ファイル構成差分

REQUIREMENTS.md §10.2 のディレクトリ構成に対する差分:

```
src/parking/
├─ receivers/
│  ├─ base.py            # 変更なし
│  ├─ gpio.py            # 残置・利用は configurable
│  ├─ http.py            # ★ 新規: FastAPI + uvicorn 実装
│  └─ dummy.py           # 変更なし
```

`receivers/http.py` の責務:
- FastAPI アプリの組み立て（ルート登録）。
- `start()` で uvicorn をバックグラウンドスレッドで起動。`stop()` で graceful shutdown。
- `on_entry` / `on_exit` コールバックを **エッジ検出後** に呼ぶ（counter は base 設計のまま）。

### 7.1 依存追加

`requirements.txt`:
```
gpiozero      # 既存
fastapi>=0.110
uvicorn[standard]>=0.27
```

## 8. アプリ配線（app.py 差分）

REQUIREMENTS.md §10.7 の手順はそのまま。Step 4 で `config.receiver.type == "http"` のとき `HttpReceiver` を生成する。残りは同一。

`HttpReceiver.start()` はノンブロッキングで返り、内部スレッドで uvicorn を回す。メインスレッドは SIGTERM 待ちで sleep / `signal.pause()`。

## 9. LinkBase 側の設定

LinkBase の `/opt/light/config.json` を以下に設定する（クライアント／設置担当が現地で実施。参考実装の `tbbox-playlist-switcher/docs/DESIGN_HTTP_SWITCH.md` §13.2 と同じ手順）:

```json
{
  "GET_URL": "http://127.0.0.1:8080/api/control",
  "SIM_ID": "unused",
  "Mode": "4",
  "MONITOR_INTERVAL": 0.2
}
```

- `Mode: "4"` = 全桁そのまま送信（`main.py` の `_mask_by_mode` でマスクされない）。既存機は表示灯用途で `Mode="2"` のことがあるので **`"4"` に変更**する。
- `SIM_ID` は `main.py` が `config['SIM_ID']` で参照するため **キー必須**（消すと KeyError で起動不可）。値は `id=` に乗るだけで本アプリは無視するので任意。
- カメラの IO1 → MCP23017 pin11（SW1）、IO2 → pin10（SW2）が本設計のデフォルト。割当を変える場合は `entry_switch` / `exit_switch` を合わせる。

## 10. systemd

`systemd/parking.service` の起動依存関係に LinkBase を追加する（同一 Pi 上に LinkBase の systemd ユニットがある前提）。

```ini
[Unit]
Description=Parking Occupancy Tracker
After=network-online.target light-controller.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/parking
ExecStart=/usr/bin/python3 -m parking
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=multi-user.target
```

`light-controller.service` が LinkBase 側のユニット名（実機ソース `etc/systemd/system/light-controller.service` で確認）。本アプリが先に起動しても LinkBase からの GET が来ないだけで害はないので、`Requires=` ではなく `After=` のみで十分。

## 11. 動作確認

### 11.1 単体（LinkBase なし）
```bash
# 入庫 + 出庫 立ち上がりを順に再現
curl "http://127.0.0.1:8080/api/control?alert=01999999&id=test"   # SW1=0(ACTIVE), 他=9
curl "http://127.0.0.1:8080/api/control?alert=11999999&id=test"   # SW1=1(非ACTIVE) → 立ち下がり、無視
curl "http://127.0.0.1:8080/api/control?alert=01999999&id=test"   # 再度 ACTIVE → 入庫 +1
curl "http://127.0.0.1:8080/api/control?alert=10999999&id=test"   # SW2=0(ACTIVE) → 出庫 -1
curl "http://127.0.0.1:8080/health"
```

- `active_value="0"` 設定下での想定。
- 連続して同じ ACTIVE 状態（`01999999`を2回続け）を投げてもエッジは 1 回しか立たないことを確認。

### 11.2 結合（LinkBase あり）
1. LinkBase config.json を §9 のとおりに書き換え、`systemctl restart light`。
2. カメラの IO1（入庫）テスト出力 → 本アプリログに「入庫検出 / current=N+1」が出る。
3. IO2（出庫）テスト出力 → 「出庫検出 / current=N-1」。
4. Pi 再起動 → `parking_state.json` から `current` が復元されることを確認（既存仕様）。

## 12. 旧 GPIO 直結方針との関係

- §6.1 のフォトカプラ・電圧整合・OC 許容 V/A の議論は **LinkBase 経由ではスキップ可能**（LinkBase 側が既に処理している）。
- `receivers/gpio.py` は将来 LinkBase を介さず直結に戻す可能性を残すために保持するが、フェーズ1 の実装・実機投入では使わない。

## 13. LinkBase 実ソース確認の結果（2026-05-26）

LinkBase 実ソース（`opt/light/{main.py,peripheral.py,http_request.py,config.json}`, `boot/config.txt`）を確認。

**確定した事項:**
- 信号入力は **MCP23017（I2C GPIO拡張IC）の入力ピン8〜11**。Pi本体GPIOではない。
- alert桁↔ピン対応と `1-i` 反転は `_convert_input` で確定（§3 の表）。
- **送信は状態変化時のみ**（常時 polling ではない）＋送信後 `DI_WAIT=1秒` 停止（§4.3）。
- `Mode="4"` でマスク無し全桁送信。既存機は `Mode="2"`。
- `SIM_ID` はキー必須・値は任意。送信は `?alert=...&id=SIM_ID`。
- LinkBase 自身は port 80 で FastAPI を持つ（逆方向の表示灯出力用）。本アプリは 8080。

**未解決（実装ブロックしない / 実機で確定）:**
- MCP23017 ピン番号 ↔ IOボード物理端子ラベル(In1〜4等) の対応。
- 極性の絶対値（接点クローズ＝alert `0`/`1` どちらか）。`1-i` 反転は確定だがカメラOC側の配線次第 → 実機で1回観測し `active_value` を確定。
- カメラのアラーム出力パルス幅（取りこぼし防止に 1.5秒以上を推奨）。
