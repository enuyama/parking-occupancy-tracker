# Parking Occupancy Tracker

駐車場の入庫/出庫をカメラ (Hanwha Vision XNO-A6084R) 経由で検知し、Raspberry Pi 上で現在台数と満空混ステータスを保持するエッジシステム。

詳細仕様は [REQUIREMENTS.md](REQUIREMENTS.md) と [docs/DESIGN_HTTP_RECEIVER.md](docs/DESIGN_HTTP_RECEIVER.md) を参照。

## アーキ概要 (フェーズ1)

```
[カメラ XNO-A6084R] --OC--> [LinkBase (同一Pi)] --HTTP--> [本アプリ] --SQLite-->
                                                              │
                                                              └── counter / status
```

- 受信方式は **LinkBase 経由 HTTP** (`GET /api/control?alert=...`) がデフォルト。
- 開発時は `receiver.type = "dummy"` で stdin から入庫/出庫を打ち込んで検証可能。

## セットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.toml config.toml
# config.toml を環境に合わせて編集
```

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
active_value = "0"
```

別ターミナルから:

```bash
# SW1=0(ACTIVE)=入庫立ち上がり -> +1
curl "http://127.0.0.1:8080/api/control?alert=01999999&id=test"

# SW1=1 に戻す (立ち下がりは無視)
curl "http://127.0.0.1:8080/api/control?alert=11999999&id=test"

# 再度 ACTIVE -> +1
curl "http://127.0.0.1:8080/api/control?alert=01999999&id=test"

# SW2=0(ACTIVE)=出庫 -> -1
curl "http://127.0.0.1:8080/api/control?alert=10999999&id=test"

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
