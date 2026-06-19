# 現地実カメラ動作テスト手順書（MQTT 受信）

> **対象読者:** 現地でのカメラ結合テストを担当するエンジニア。
> 設計の詳細は [docs/DESIGN_MQTT_RECEIVER.md](DESIGN_MQTT_RECEIVER.md) を参照。本書は「現地で上から順に実行する」ことに最適化している。

## 前提条件

- **broker（Mosquitto）とカウントアプリは同一 Raspberry Pi 上に同居する。**
- カメラ（Hanwha XNO-A6084R）は同一 LAN 内に設置済み。カメラの設定はクライアント／設置担当と協同で実施する。
- アプリの配置先は `/opt/parking`（systemd ユニットの `WorkingDirectory` に合わせる）。
- Python 3.11+（Raspberry Pi OS Bookworm 標準）。3.10 以下でも `requirements.txt` の `tomli` で代替できる（起動時に依存エラーが出なければ問題ない）。

---

## 0. 現地で必要な情報（事前にリスト化しておく）

現地作業を始める前に以下の情報を手元に揃えること。

| 情報 | 確認先 |
|------|--------|
| Pi に割り当てる固定 IP または DHCP 予約アドレス | クライアント（ネットワーク担当）に確認 |
| 駐車場の総台数（`total_spaces`） | クライアント |
| 満車しきい値・混雑しきい値（`full_at` / `crowded_at`） | クライアント |
| カメラ管理画面へのアクセス可否（ブラウザで開けるか・ログイン情報） | クライアント／設置担当 |
| Mosquitto に設定する MQTT ユーザー名・パスワード（自分で決める） | 現地で決定 |

---

## 1. 事前準備（事務所で済ませること）

### 1.1 Pi への SSH 有効化

Raspberry Pi Imager でイメージを焼く際に SSH と Wi-Fi（または有線）を有効化しておく。起動後:

```bash
# Pi にログイン確認
ssh pi@<PiのIPアドレス>
```

### 1.2 アプリ一式の転送

**方法A: git clone（Pi がインターネット接続可能な場合）**

```bash
# Pi 上で実行
sudo mkdir -p /opt/parking
sudo chown pi:pi /opt/parking
cd /opt/parking
git clone <リポジトリURL> .
```

**方法B: scp または USB（オフライン環境の場合）**

```bash
# 手元 PC からリポジトリ一式を転送
scp -r /path/to/parking-occupancy-tracker/* pi@<PiのIP>:/opt/parking/
```

---

## 2. アプリ配置

Pi 上で `/opt/parking` に移動して作業する。

```bash
cd /opt/parking

# Python 仮想環境を作成
python3 -m venv .venv

# 依存パッケージをインストール
.venv/bin/pip install -r requirements.txt
```

インストール完了後、`paho-mqtt` が含まれていることを確認:

```bash
.venv/bin/pip show paho-mqtt
```

---

## 3. Mosquitto 構築

### 3.1 インストール

```bash
sudo apt install -y mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto
```

### 3.2 設定ファイルの作成

> **重要:** Mosquitto の設定ファイルは**行内コメント（行末の `# ...`）が使えない**。コメントは必ず独立した行に書くこと（設計書 §9 のサンプルは行末コメント付きだが、以下では修正済みの形で記載している）。

**認証あり構成（推奨）の場合:**

```bash
sudo nano /etc/mosquitto/conf.d/parking.conf
```

以下の内容を入力:

```conf
# LAN 上のカメラから接続できるよう全 IF で待受
listener 1883 0.0.0.0
allow_anonymous false
password_file /etc/mosquitto/passwd
```

パスワードファイルを作成し、ユーザー `parking` を追加:

```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd parking
# プロンプトが表示されるのでパスワードを入力（2回）
```

Mosquitto を再起動して設定を反映:

```bash
sudo systemctl restart mosquitto
sudo systemctl status mosquitto
```

---

**代替: 認証なし構成（LAN クローズドで手早く試す場合）**

LAN が完全に閉じた環境で素早く疎通確認したい場合のみ使用する（セキュリティ上の理由から本番運用には使わないこと）:

```bash
sudo nano /etc/mosquitto/conf.d/parking.conf
```

```conf
# LAN 上のカメラから接続できるよう全 IF で待受
listener 1883 0.0.0.0
allow_anonymous true
```

```bash
sudo systemctl restart mosquitto
```

---

### 3.3 ループバック疎通確認

別ターミナルで subscriber を起動し、pub/sub が通ることを確認する。

**ターミナル 1（受信側）:**

```bash
# 認証あり
mosquitto_sub -h localhost -t 'test/#' -u parking -P <password> -v

# 認証なし
mosquitto_sub -h localhost -t 'test/#' -v
```

**ターミナル 2（送信側）:**

```bash
# 認証あり
mosquitto_pub -h localhost -t 'test/hello' -m 'ok' -u parking -P <password>

# 認証なし
mosquitto_pub -h localhost -t 'test/hello' -m 'ok'
```

ターミナル 1 に `test/hello ok` と表示されれば broker は正常動作している。

---

## 4. Pi の IP アドレス確認

```bash
hostname -I
```

表示された IP アドレス（例: `192.168.1.50`）が**カメラ側 MQTT プロファイルの接続先**になる。
アプリ側 `config.toml` の `host = "localhost"` はアプリ→broker（同一 Pi 内）の接続であり、これとは別物（[設計書 §5](DESIGN_MQTT_RECEIVER.md#5-設定ファイル差分-configtoml) 参照）。

**この IP をメモしておく（カメラ設定時に使用する）。**

---

## 5. config.toml 作成

```bash
cd /opt/parking
cp config.example.toml config.toml
```

`config.toml` を編集する。変更が必要な箇所のみを以下に示す:

```toml
[parking]
total_spaces = <駐車場の総台数>        # 例: 50

[thresholds]
crowded_at = <混雑判定の台数>          # 例: 40
full_at    = <満車判定の台数>          # 例: 50

[receiver]
type = "mqtt"                          # ← "http" から変更

[receiver.mqtt]
host = "localhost"                     # アプリ→broker（変更不要）
port = 1883                            # 変更不要
username = "parking"                   # broker に登録したユーザー名
password = "<password>"                # broker に登録したパスワード
# 認証なし構成の場合は username / password を空文字 "" のまま

[receiver.mqtt.rules]
bike     = "entry"                     # ← テスト用のまま残す（§6 のスモークテスト用）
test_out = "exit"                      # ← §6 で出庫(-1)も確認するため一時追加（§8.2 で削除）
```

> **注意:** `config.toml` は `.gitignore` 済み。認証情報が含まれるためリポジトリにコミットしないこと。

---

## 6. アプリ手動起動＋擬似イベントでスモークテスト（カメラ接続前に必ず実施）

> カメラ結合テストの前に、broker とアプリの連携を単独で確認する。

### 6.1 アプリ起動

```bash
cd /opt/parking
PYTHONPATH=src .venv/bin/python -m parking config.toml
```

> **重要:** `PYTHONPATH=src` は必須。`.venv/bin/activate` するだけでは `parking` パッケージが見つからずエラーになる。

起動後のログに以下の2行が出ることを確認（文言は実際のログそのまま）:

```
INFO parking.receivers.mqtt: MqttReceiver 起動: broker=localhost:1883 topic=... （broker 未接続でも接続を待機します）
INFO parking.receivers.mqtt: MQTT 接続成功: localhost:1883 を購読 topic='+/onvif-ej/OpenApp/WiseAI/LineCrossing/#' (QoS 1)
```

「MQTT 接続成功」が出ないまま heartbeat が `connected=False` を示す場合は §10 のトラブルシューティングを参照。

### 6.2 擬似イベントの投入（別ターミナルで実行）

```bash
# ① 入庫（State=true）→ current +1 になること
mosquitto_pub -h localhost \
  -t 'AA:BB/onvif-ej/OpenApp/WiseAI/LineCrossing/&vs-0/bike' \
  -m '{"UtcTime":"2026-05-29T00:00:00Z","Source":{"RuleName":"bike"},"Data":{"State":"true","ObjectId":"1","Action":"Right"}}' \
  -u parking -P <password>

# ② イベント解除（State=false）→ カウント変化なし（無視されること）
mosquitto_pub -h localhost \
  -t 'AA:BB/onvif-ej/OpenApp/WiseAI/LineCrossing/&vs-0/bike' \
  -m '{"UtcTime":"2026-05-29T00:00:04Z","Source":{"RuleName":"bike"},"Data":{"State":"false","ObjectId":"","Action":""}}' \
  -u parking -P <password>

# ③ 出庫線（State=true）→ current -1 になること
mosquitto_pub -h localhost \
  -t 'AA:BB/onvif-ej/OpenApp/WiseAI/LineCrossing/&vs-0/test_out' \
  -m '{"Source":{"RuleName":"test_out"},"Data":{"State":"true","ObjectId":"2"}}' \
  -u parking -P <password>

# ④ 未登録 RuleName → WARNING ログで無視されること（落ちないこと）
mosquitto_pub -h localhost \
  -t 'AA:BB/onvif-ej/OpenApp/WiseAI/LineCrossing/&vs-0/unknown' \
  -m '{"Source":{"RuleName":"unknown"},"Data":{"State":"true"}}' \
  -u parking -P <password>
```

> 認証なし構成の場合は `-u parking -P <password>` を省く。

### 6.3 確認観点

| 確認項目 | 期待する動作 |
|----------|-------------|
| ① の直後 | `入庫検出: current=1/...` のログが出る |
| ② の直後 | カウント変化なし（`State:"false"` は無視される） |
| ③ の直後 | `出庫検出: current=0/...` のログが出る |
| ④ の直後 | `WARNING ... 未登録の RuleName 'unknown'。rules テーブルに無いため無視します` が出てアプリが継続動作する |
| heartbeat | 60 秒ごとに `MQTT heartbeat: connected=True, msgs=N, counted=M, last_msg=...` が出る |

全項目を確認したらアプリを `Ctrl-C` で停止する。

---

## 7. カメラ側設定（クライアント／設置担当と協同で実施）

詳細は [設計書 §8](DESIGN_MQTT_RECEIVER.md#8-カメラ側設定クライアント設置担当が実施) を参照。以下の3点を設定する。

### 7.1 仮想線を2本作成（WiseAI）

- 入庫線・出庫線をそれぞれ作成し、**線名を決める**（例: `car_in` / `car_out`）。
  - この線名が MQTT トピック末尾（`Source.RuleName`）になり、`config.toml` の `[receiver.mqtt.rules]` キーと一致させる。
- **各線の対象物フィルタを「車両（car）」に限定する。** 人・自転車で発火する設定では誤カウントになる。

### 7.2 イベントルールを2本作成

- 入庫線用・出庫線用それぞれにイベントルールを作成し、アクションで **MQTT を有効化**する。
- > **注意:** 片方のルールしか MQTT を有効化しないと、その方向のイベントが届かず**カウントが片側欠落**する。必ず両方のルールで MQTT アクションを有効にすること。

### 7.3 MQTT プロファイルの設定

カメラの MQTT 設定画面で:

- **接続先ホスト:** §4 で確認した Pi の LAN 内 IP アドレス
- **ポート:** `1883`
- **ユーザー名 / パスワード:** §3.2 で Mosquitto に登録したもの（認証なしの場合は空欄）

---

## 8. 実カメラ結合テスト

### 8.1 broker への到達確認

アプリを起動する前に、カメラからのメッセージが broker に届いているかを確認する。

```bash
# 全トピックを購読して生メッセージを表示
mosquitto_sub -h localhost -t '#' -v -u parking -P <password>
```

車を仮想線付近で動かして、以下のようなメッセージが表示されることを確認する:

```
E4:30:22:CA:38:7A/onvif-ej/OpenApp/WiseAI/LineCrossing/&vs-0/car_in {"UtcTime":"...","Source":{"RuleName":"car_in"},"Data":{"State":"true",...}}
```

> **ここで実際のトピックと RuleName（仮想線名）をメモする。** 次の手順で `config.toml` に設定する。

### 8.2 config.toml の rules を実線名で更新

```bash
nano /opt/parking/config.toml
```

`[receiver.mqtt.rules]` セクションを実際の線名に書き換える（テスト用の `bike` / `test_out` 行は削除）:

```toml
[receiver.mqtt.rules]
car_in  = "entry"   # ← 実際の入庫線名に変更
car_out = "exit"    # ← 実際の出庫線名に変更
```

### 8.3 アプリを起動して結合テスト

```bash
cd /opt/parking
PYTHONPATH=src .venv/bin/python -m parking config.toml
```

実際の車または歩行者によるテストを実施し、以下を確認する:

| 確認項目 | 期待する動作 |
|----------|-------------|
| 入庫線を車が通過 | `入庫検出: current=N` ログが出る |
| 出庫線を車が通過 | `出庫検出: current=N-1` ログが出る |
| 歩行者・自転車の通過 | カウント変化なし（car 限定フィルタが効いている） |

### 8.4 設計書 §13 に基づくチェックリスト

- [ ] **入口線の誤発火なし:** 入庫時に出口線（`car_out`）が発火していないこと（`mosquitto_sub -t '#'` で確認）。
- [ ] **出口線の誤発火なし:** 出庫時に入口線（`car_in`）が発火していないこと。
- [ ] **スロットル影響の確認:** 「実行時間: 60」「アラーム出力: 5s」の設定が MQTT 発行間隔のスロットルになっていないか。連続入庫で取りこぼしがあれば `min_event_interval` の調整またはカメラ側の設定変更を検討する（[設計書 §13](DESIGN_MQTT_RECEIVER.md#13-未解決事項実装をブロックしない--テスト時に確定) 参照）。
- [ ] **対象物フィルタの確認:** 人・自転車に反応しないこと（car 限定設定の効果確認）。
- [ ] **QoS 変更の可否確認:** カメラ側の publish QoS を 1 に変更できるか確認（任意。[設計書 §3.1](DESIGN_MQTT_RECEIVER.md#31-配信品質qos--retainの制約-重要) 参照。既定 QoS 0 は既知の制約として受容可）。

---

## 9. systemd サービス化

### 9.1 ユニットファイルのコピーとコメント解除

```bash
sudo cp /opt/parking/systemd/parking.service /etc/systemd/system/parking.service
sudo nano /etc/systemd/system/parking.service
```

`[Unit]` セクションの `Requires=mosquitto.service` のコメントを**解除する**（MQTT 運用では Mosquitto が必要なため）:

```ini
[Unit]
Description=Parking Occupancy Tracker
After=network-online.target light-controller.service mosquitto.service
Wants=network-online.target
# ↓ MQTT 運用のため以下のコメントを解除済み
Requires=mosquitto.service
```

> `Requires=mosquitto.service` が有効な状態では、Mosquitto が起動していないと parking サービスも起動しない。paho-mqtt の自動再接続があるため起動順の前後は致命的ではないが、依存関係を明示することで運用時のトラブルを防ぐ。

### 9.2 サービスの有効化と起動

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now parking.service
```

ログを確認:

```bash
sudo journalctl -u parking -f
```

`MQTT 接続成功: localhost:1883 を購読 ...` が出ることを確認する。

### 9.3 Pi 再起動テスト

```bash
sudo reboot
```

再起動後に以下を確認:

```bash
# サービスが自動起動していること
sudo systemctl status parking.service

# parking_state.json から台数が復元されていること
sudo journalctl -u parking -n 50 --no-pager | grep -E "復元|current"
```

---

## 10. トラブルシューティング

問題が発生した場合はまずログで切り分ける:

```bash
# サービスログ（直近 50 行）
sudo journalctl -u parking -n 50 --no-pager

# アプリログファイル
tail -f /opt/parking/parking.log
```

| 症状 | ログの手がかり | 対処 |
|------|---------------|------|
| (a) MQTT 接続が認証で拒否される | `ERROR ... MQTT 接続が認証で拒否されました ... username/password を確認してください` | broker の `/etc/mosquitto/passwd` に登録したユーザー名・パスワードと `config.toml` の `[receiver.mqtt]` `username`/`password` を照合する。不一致なら `sudo mosquitto_passwd /etc/mosquitto/passwd parking` でパスワードを更新し、broker を restart |
| (b) heartbeat で `connected=False` が続く | `MQTT heartbeat: connected=False` | Mosquitto の起動状態を確認: `sudo systemctl status mosquitto`。停止していれば `sudo systemctl start mosquitto`。起動中なら `config.toml` の `host`/`port` が `localhost`/`1883` になっているか確認 |
| (c) heartbeat は `connected=True` だが `last_msg=未受信` | `MQTT heartbeat: connected=True, ..., last_msg=未受信` | broker にメッセージが届いていないか、届いているが rules 不一致の可能性。`mosquitto_sub -h localhost -t '#' -v -u parking -P <password>` で broker に届いているか確認。**届いていない → カメラ側**（MQTT プロファイルの接続先 IP・ポート・認証情報、イベントルールの MQTT アクション有効化を再確認）。**届いている → アプリ側**（`[receiver.mqtt.rules]` のキーと実際の RuleName を照合） |
| (d) `未登録の RuleName` WARNING が出てカウントされない | `WARNING ... 未登録の RuleName 'car_in'。rules テーブルに無いため無視します` | `config.toml` の `[receiver.mqtt.rules]` キーと実際の線名（§8.1 でメモした RuleName）が一致していない。config.toml を修正してアプリを再起動 |
| (e) カウントが2重になる | `入庫検出` が1通過で2回ログに出る | `config.toml` の `[receiver.mqtt]` で `min_event_interval` を設定して連続カウントを抑制する（例: `min_event_interval = 3.0`）。またはカメラ側のイベントルール設定を確認 |

---

## 11. テスト完了時に持ち帰って確定する値のチェックリスト

現地テスト終了後、以下の値を確認・記録して持ち帰ること（[設計書 §13](DESIGN_MQTT_RECEIVER.md#13-未解決事項実装をブロックしない--テスト時に確定) 参照）。

- [ ] **broker 認証情報:** 本番運用用のユーザー名・パスワードを決定したか
- [ ] **本番の仮想線名（入庫/出庫）:** `[receiver.mqtt.rules]` に設定する実際の線名（`RuleName`）
- [ ] **誤発火の有無:** 入庫時に出口線が発火しないか、出庫時に入口線が発火しないか
- [ ] **スロットル影響の有無:** 「実行時間 60 / アラーム出力 5s」による取りこぼしは発生したか。発生した場合の `min_event_interval` の推奨値
- [ ] **QoS 変更の可否:** カメラ側 publish QoS を 1 に設定できるか（取りこぼし軽減のため。既定 QoS 0 は既知制約として受容可）
