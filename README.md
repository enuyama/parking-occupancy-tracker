# Parking Occupancy Tracker

駐車場の入庫/出庫をカメラ (Hanwha Vision XNO-A6084R) 経由で検知し、Raspberry Pi 上で現在台数と満空混ステータスを保持するエッジシステム。

詳細仕様は [REQUIREMENTS.md](REQUIREMENTS.md) と [docs/DESIGN_HTTP_RECEIVER.md](docs/DESIGN_HTTP_RECEIVER.md) を参照。

## アーキ概要 (フェーズ1)

### MQTT 経路（現行・主経路、2026-06-24 実機検証済み）

```
[カメラ XNO-A6084R] --LAN--> [Mosquitto broker (同一Pi)] ---> [本アプリ MqttReceiver]
  WiseAI 仮想線交差                  :1883                         │
  (entry / exit)                                                    └── counter / status
```

- 受信方式は `receiver.type = "mqtt"` が現行デフォルト。
- カメラは MQTT クライアント接続を broker に向けるだけで、全 ONVIF/WiseAI イベント（LineCrossing 含む）を自動 publish する。
- カメラ側設定: (1) MQTT クライアント接続（接続先 = Pi の LAN 内 IP:1883）、(2) WiseAI 仮想線2本（`entry` / `exit`、対象物 = 車両）。
- 詳細は [docs/DESIGN_MQTT_RECEIVER.md](docs/DESIGN_MQTT_RECEIVER.md) を参照。

### 接点経路（中止中）

```
[カメラ XNO-A6084R] --OC--> [LinkBase (同一Pi)] --HTTP--> [本アプリ HttpReceiver]
```

- LinkBase 経由 HTTP 受信（`GET /api/control?alert=...`）。現在は使用していない。

---

- 開発時は `receiver.type = "dummy"` で stdin から入庫/出庫を打ち込んで検証可能。

## セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.toml config.toml
# config.toml を環境に合わせて編集
```

カメラ MQTT 経由の現地テスト手順は [docs/SETUP_FIELD_TEST_MQTT.md](docs/SETUP_FIELD_TEST_MQTT.md) を参照。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

- `tests/test_counter.py` — カウント増減・クランプ・しきい値判定
- `tests/test_config.py` — 設定ロードとバリデーション
- `tests/test_store.py` — JSON 永続化（保存・復元・破損時）
- `tests/test_http_receiver.py` — HTTP 受信のエッジ検出（最重要）
- `tests/test_app.py` — config→counter→store の配線と再起動復元

## 起動

```bash
python -m parking config.toml
```

引数を省略するとカレントディレクトリの `config.toml` を読む。

## GUI（Raspberry Pi タッチパネル）

`[gui].enabled = true` にすると、起動時に満空混ステータスと手動操作パネルを表示する Tkinter ウィンドウが立ち上がる。MQTT 自動カウントと**同時稼働**し、画面から手動補正できる。

```toml
[gui]
enabled = true           # false で従来のヘッドレス動作
fullscreen = false       # 本番タッチパネルでは true 推奨（Esc で解除）
poll_interval_ms = 200   # 画面の状態更新間隔(ms)。50..5000
```

画面は管理室の係員が操作する前提で、**最も頻度の高い「現在の台数」を主役**に据えた 1 画面構成:

- **ステータス（最上段）**: 満車／混雑／空車を語＋色で表示（満=赤・混=橙・空=緑）。下に `現在 / 満車台数`（例 `20 / 19`）を併記し妥当性を確認しやすくする。
- **現在の台数（主役・大）**: 大きな水色の `[－] 数値 [＋]`。タップで1台ずつ、**長押しすると連打**（押しっぱなしでまとめて補正）。0〜total_spaces でガード。
- **満車台数 / 混雑台数（下部・小さめ）**: それぞれ `[－] 数値 [＋]`（長押し連打可）。`1 <= crowded_at <= full_at <= total_spaces` を常に満たすようガード。
- **限界フィードバック**: 上限/下限に達して増減できないときは数値を**赤く点滅**させ、「押しても無反応」を防ぐ。押下は即時に画面へ反映。

調整した `full_at` / `crowded_at` と現在台数は `parking_state.json` に保存され、再起動後も復元される。

### 依存と起動上の注意

- Tkinter は Python 標準。Raspberry Pi OS では未導入の場合 `sudo apt install python3-tk` が必要。
- **日本語フォントが必要**。未導入だと「満車」「現在の台数」等が □（豆腐）になる。`sudo apt install fonts-noto-cjk` を入れること。
- X（デスクトップ）環境で起動すること。SSH やヘッドレス起動時は `DISPLAY` を指定する（例: `DISPLAY=:0 python -m parking config.toml`）。
- GUI モジュールの読み込み・初期化に失敗した場合は、警告ログを出してヘッドレス動作（受信のみ）にフォールバックする。
- systemd で自動起動する場合は X セッション配下で起動し、`Environment=DISPLAY=:0` 等を設定する。

> 補足: macOS の Aqua 版 Tkinter は実マウスのクリック処理が特殊で、mac ローカルではボタンがうまく反応しないことがある（本番ラズパイ=Linux/X11 には無関係）。GUI の動作確認は下記ツールで Linux/X11 上で行うこと。

### GUI を Linux/X11（ラズパイ相当）で確認する

Docker があれば、本番ラズパイと同じ Linux/X11 環境で GUI を検証・プレビューできる（macOS の挙動に惑わされない）。

```bash
# 実 OS クリック（単発/ダブルクリック/高速連打）を自動送出して動作を自動検証
bash tools/verify_gui_linux.sh

# GUI をブラウザに出して自分のマウスで操作する（noVNC）。
# 起動後 ブラウザで http://localhost:6080/vnc.html を開き [Connect]（パスワード不要）
bash tools/preview_gui_linux.sh
```

## 動作確認

### dummy 受信で手動操作

`config.toml`:

```toml
[receiver]
type = "dummy"
```

```bash
python -m parking config.toml
# stdin に i (入庫) / o (出庫) / q (終了) を1行ずつ入力
```

### HTTP 受信を curl で確認

`config.toml`:

```toml
[receiver]
type = "http"

[receiver.http]
host = "127.0.0.1"
port = 8080
entry_switch = 1
exit_switch  = 2
active_value = "1"   # LinkBase公式仕様: "1"=入力あり。反転構成なら "0"
```

別ターミナルから（`active_value="1"` の場合）:

```bash
# SW1=1(ACTIVE)=入庫立ち上がり -> +1
curl "http://127.0.0.1:8080/api/control?alert=10999999&id=test"

# SW1=0 に戻す (立ち下がりは無視)
curl "http://127.0.0.1:8080/api/control?alert=00999999&id=test"

# 再度 ACTIVE -> +1
curl "http://127.0.0.1:8080/api/control?alert=10999999&id=test"

# SW2=1(ACTIVE)=出庫 -> -1
curl "http://127.0.0.1:8080/api/control?alert=01999999&id=test"

# 現在状態
curl "http://127.0.0.1:8080/health"
curl "http://127.0.0.1:8080/state"
```

### 永続化テスト

1. 何回か入庫を打って `current_count` を非ゼロにする。
2. アプリを Ctrl-C で停止。
3. 再起動すると直前の `current_count` から再開することを確認。

## LinkBase 側設定

LinkBase の `/opt/light/config.json`:

```json
{
  "GET_URL": "http://127.0.0.1:8080/api/control",
  "SIM_ID": "<任意>",
  "Mode": "4",
  "MONITOR_INTERVAL": 0.2
}
```

詳細・物理ポートと SW 番号の対応は `docs/DESIGN_HTTP_RECEIVER.md` を参照。

## systemd サービス化

```bash
sudo cp systemd/parking.service /etc/systemd/system/parking.service
# WorkingDirectory / ExecStart のパスを環境に合わせて編集
sudo systemctl daemon-reload
sudo systemctl enable --now parking.service
sudo journalctl -u parking -f
```

## ディレクトリ構成

```
parking-occupancy-tracker/
├─ REQUIREMENTS.md
├─ README.md
├─ requirements.txt
├─ config.example.toml
├─ src/parking/
│  ├─ __main__.py
│  ├─ app.py
│  ├─ config.py
│  ├─ counter.py
│  ├─ gui.py     # Raspberry Pi タッチパネル GUI (Tkinter)
│  ├─ models.py
│  ├─ store.py
│  └─ receivers/
│     ├─ base.py
│     ├─ http.py     # フェーズ1 主実装
│     ├─ gpio.py     # 代替 (将来 LinkBase を介さない構成用)
│     └─ dummy.py    # 開発用
├─ systemd/parking.service
└─ docs/
   ├─ DESIGN_HTTP_RECEIVER.md
   └─ (カメラ仕様書 PDF)
```
