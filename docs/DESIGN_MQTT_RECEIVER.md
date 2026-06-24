# 受信層設計書: カメラ MQTT (LAN) 受信

> **位置付け:** 既存の接点ベース受信（カメラ OC → LinkBase → HTTP、`DESIGN_HTTP_RECEIVER.md`）に加え、
> 「カメラ → (LAN) MQTT broker → 本アプリ」という LAN 経由のカウント経路を**新規の受信層として追加**する。
> 本書はその差分設計のみを扱う。カウント・永続化・満空混判定（`counter.py` / `store.py`、および `app.py` の `Application` クラスのカウント処理）は**一切変更しない**。
> `app.py` で触るのは受信層ファクトリ `_build_receiver()` への分岐追加のみ（§7）。
>
> 既存の受信層 abstraction（`receivers/base.py` の `EventReceiver` Protocol）に `MqttReceiver` を1つ足し、
> `receiver.type = "mqtt"` で選択できるようにするだけ。HTTP / GPIO / dummy はそのまま残す。

最終更新: 2026-06-05

## 1. 背景・方針決定

- カメラ（Hanwha Vision XNO-A6084R）は WiseAI 仮想線交差イベントを **MQTT publish できる**（データシート Alarm Events「MQTT: publication」、対応プロトコル一覧「MQTT」）。同じイベントを HTTP/TCP でも送れるが、**クライアント指定により MQTT を採用**。
- 本番環境はカメラ・broker・本アプリすべて**同一 LAN 内（駐車場ローカル）で完結**する。インターネット公開は不要かつ禁止。
- MQTT は pub/sub モデルで、カメラ（publisher）とアプリ（subscriber）の間に必ず **broker（中継）が必要**。本構成では **broker（Mosquitto）を本アプリと同じ Pi に同居**させる。
  - カメラ → Pi の broker へ publish（LAN 経由、PiのLAN内IP:1883宛て）
  - 本アプリ → localhost の broker を subscribe
- 接点経路（LinkBase/HTTP）と MQTT 経路は、カメラ側では**同じイベントルールの別アクション**として分岐している（後述 §8）。本アプリ側ではどちらか一方を `receiver.type` で選んで使う。

### 1.1 なぜ受信層追加で済むか（別アプリ化しない理由）

`counter.py`（現在台数の増減・満空混判定、I/O なし純ロジック）と `store.py`（永続化）は受信方式から完全に独立している。受信方式は `EventReceiver` Protocol（`start()`/`stop()` で起動し、検出時に `on_entry`/`on_exit` コールバックを呼ぶ）で抽象化済み。
→ LAN カウントの追加は **`receivers/mqtt.py` を1枚足すだけ**で成立する。カウント実装を外部アプリに切り出す必要はない。

## 2. アーキテクチャ

### 2.1 既存（接点経路）
```
[XNO-A6084R] ─OCパルス─> [LinkBase] ─HTTP GET /api/control─> [本アプリ HttpReceiver]
```

### 2.2 本設計（MQTT 経路）
```
                          同一 Pi 内
[XNO-A6084R]            ┌─────────────────────────────────────────┐
  │ WiseAI 仮想線交差    │  [Mosquitto (broker)]  :1883             │
  │ (入庫線/出庫線)      │      ▲ publish              │ subscribe   │
  └──── LAN ────────────┼──────┘                      ▼            │
                        │                  [本アプリ (parking)]    │
                        │                   MqttReceiver           │
                        │                    → State="true" のみ   │
                        │                    → RuleName で方向判定  │
                        │                    → counter.record_*     │
                        │                    → store / status       │
                        └─────────────────────────────────────────┘
```

- カメラは LAN 上の別機器。broker への接続先は **Pi の LAN 内 IP**（`localhost` ではない。それはカメラ設定側＝§8）。
- 本アプリ → broker は同一 Pi なので `localhost:1883`。
- broker（Mosquitto）の導入・設定は §9。

## 3. プロトコル（実メッセージで確認済み）

Node-RED で broker を購読して取得した実サンプル（2026-05-27、テスト用の仮想線 `bike`）:

**トピック**
```
E4:30:22:CA:38:7A/onvif-ej/OpenApp/WiseAI/LineCrossing/&vs-0/bike
└─ カメラMAC ────┘                                    └vs┘ └線名┘
```

**ペイロード（JSON）**
```json
{
  "UtcTime": "2026-05-27T09:05:18.543Z",
  "Source": { "VideoSourceToken": "vs-0", "RuleName": "bike" },
  "Data":   { "State": "true", "ObjectId": "2080", "Action": "Right" }
}
```

確認できた事実と設計上の扱い:

| 項目 | 内容 | 設計上の扱い |
| --- | --- | --- |
| トピック先頭 | カメラの MAC アドレス | 複数カメラ対応のため**ワイルドカード購読**（§4.1） |
| トピック末尾 | 仮想線の名前（= `Source.RuleName`） | **入庫/出庫の判別キー**（§4.3） |
| `Data.State` | `"true"` = 交差発生 / `"false"` = イベント解除 | **`"true"` のときだけカウント**。`"false"` は無視（§4.2） |
| `Data.ObjectId` | 追跡対象の個体 ID（trueのみ値あり） | 重複除去に利用可（任意, §4.4） |
| `Data.Action` | 交差方向（`Right`/`Left`） | **方式A（2線）では未使用**。将来 1線方向判別に拡張する余地として保持 |
| `UtcTime` | イベント時刻 | ログ用途 |

> 1回の通過で `State:"true"` → 数秒後 `State:"false"` の**2メッセージ**が届く。`"true"` のみ採用することで「1通過 = 1カウント」になる。これは HTTP 受信層の立ち上がりエッジ検出と同じ考え方。
>
> **実サンプル2例目（同テスト、1台の自転車が右方向に1回通過。クライアント提供 `docs/ハンファからのMQTT.xlsx`）**: 上記 `true`（`ObjectId:"2080"` / `Action:"Right"` / UtcTime `09:05:18.543Z`）の **約3.6秒後** に `State:"false"`（`ObjectId:""` / `Action:""` と空）が1発だけ届いた。すなわち「1通過 = true 1発 → 数秒後 false 1発」「値を持つのは `true` のみ・`false` は解除通知」という前提を実データで再確認済み。`true` のみ採用する設計で 1通過=1カウントになる（重複 `true` は観測されておらず、`min_event_interval` 既定 `0.0` で問題ない傍証）。

### 3.1 配信品質（QoS / retain）の制約 ※重要

実サンプルのメタは **`qos: 0` / `retain: false`**。これは設計上の重要な制約を意味する:

- **broker はメッセージをバッファしない**。本アプリ（subscriber）が停止・再起動中、または broker 未接続の間にカメラが publish したイベントは**失われる**（再送されない）。
- すなわち MQTT を採用しても、この設定では「取りこぼしに強い」という MQTT 一般の利点は得られず、**HTTP 直送と同様の fire-and-forget**。アプリのダウンタイム＝その間のカウント欠落になる。
- 緩和したい場合の選択肢（いずれもカメラ側 publish QoS 次第で効果が決まる。実効 QoS は publisher と subscriber の低い方）:
  - 本アプリの subscribe を QoS 1 にしても、カメラが QoS 0 で publish する限り再送は効かない。
  - 真に取りこぼしを防ぐにはカメラ側を QoS 1 に設定変更してもらう必要がある（可否はカメラ設定 §8 で要確認）。
- フェーズ1の割り切り: 入出庫の僅かな欠落は `parking_state.json` の現在値が累積保持するため致命的ではない（HTTP 経路と同じ前提）。ズレが蓄積したら運用で台数補正する。本制約は**既知事項として受容**する。

## 4. 受信ロジック設計

### 4.1 接続・購読

- ライブラリ: **paho-mqtt 2.x**。
  - 2.x はコールバック API が変更されているため `mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)` で生成する。コールバック署名は `on_connect(client, userdata, flags, reason_code, properties)` / `on_message(client, userdata, message)`（本書の疑似コードは要点のみ抜粋した簡略形）。
- `start()` で broker に接続し、`topic`（既定はワイルドカード）を **QoS 1 で subscribe**（subscribe 自体の取りこぼし防止。ただし実効配信品質は §3.1 のとおり publisher 側に律速される）。接続・購読はバックグラウンドスレッド（`loop_start()`）で回し、ノンブロッキングで返す。
- **初回接続は `connect_async()` を使い、broker が未起動でも `start()` を失敗させない**。HTTP 受信層は bind 失敗で即例外終了するが、MQTT は broker 起動順に依存するため「繋がるまで再試行して待つ」方針にする（systemd の起動順前後に強くする）。接続成功は `on_connect` のログで確認する。
- 切断・接続失敗時は paho-mqtt の自動再接続に任せる（`reconnect_delay_set` で指数バックオフ）。LAN 断・broker 再起動に耐える。再接続後は購読が復帰するよう `on_connect` 内で subscribe する。
- **認証失敗の早期検知（重要）**: `connect_async()` + 自動再接続だと、**認証情報の誤り（broker が拒否）でも無言で無限リトライ**になり、現場で「繋がらない理由」が分からなくなる。`on_connect` の `reason_code` を判定し:
  - 成功（`reason_code` が success）→ INFO ログ（接続先・購読トピック）＋ subscribe 復帰。
  - 認証系の拒否（`not authorized` / `bad username or password` 等の reason_code）→ **ERROR ログで「認証情報を確認せよ」と明示**し、設定ミスに即気づけるようにする（リトライ自体は継続するが、ログで目立たせる）。
  - その他の失敗 → WARNING で reason_code を残す。
- 購読トピック例（全カメラ・全仮想線を購読）:
  ```
  +/onvif-ej/OpenApp/WiseAI/LineCrossing/#
  ```
  `+` = MAC 1階層ワイルドカード、`#` = 以降全階層。カメラが増えても設定変更不要。

### 4.2 State フィルタ（カウントの肝）

受信した JSON をパースし、`Data.State` が **`count_state`（既定 `"true"`）と一致するメッセージだけ**を処理する。それ以外（`"false"` 等）は DEBUG ログのみで破棄。

```python
# 疑似コード（キー欠損で落とさないよう .get() で防御。実装は §7 のとおり全体 try/except）
def on_message(payload: bytes):
    msg = json.loads(payload)                      # 不正JSONは except で WARNING
    data = msg.get("Data") or {}
    state = str(data.get("State", "")).lower()
    if state != count_state:                       # 既定 "true" 以外は無視（イベント解除など）
        return
    rule = (msg.get("Source") or {}).get("RuleName")
    direction = rules.get(rule)                     # §4.3
    ...
```

### 4.3 入庫/出庫の判別（RuleName → 方向マッピング）

出入口は1つだが**仮想線を2本引き**、入庫で踏む線・出庫で踏む線を分ける（方式A）。判別はトピック末尾＝`Source.RuleName` で行う。対応は**設定ファイルの `[receiver.mqtt.rules]` テーブルで外出し**する。

```python
direction = rules.get(rule_name)      # "entry" / "exit" / None
if direction == "entry":
    on_entry()
elif direction == "exit":
    on_exit()
else:
    logger.warning("未登録のRuleName %r。rules テーブルに無いため無視", rule_name)
    # テスト用の bike など、対象外の線が来ても安全にスキップ
```

- **本番の仮想線名は `entry`（入庫）/ `exit`（出庫）に確定済み（2026-06-24 実機確認）**。`[receiver.mqtt.rules]` への設定値は確定している。
- 未登録の RuleName は WARNING で無視（他用途の線や自動配信される ONVIF イベントが流れ込んでも誤カウントしない）。
- **本番は1カメラ前提（クライアント確定）**。`rules` のキーは `RuleName` のみでよく、カメラ MAC でのスコープは不要（ワイルドカード購読の `+` は将来カメラ追加に備えた保険）。将来複数カメラで同名線を区別する必要が出たら `MAC/RuleName` 単位への拡張を検討する（現時点では実装しない）。

### 4.4 重複除去（任意・保険）

- `min_event_interval`（秒, 既定 `0.0`=無効）: **同一 RuleName（仮想線）単位**で前回カウントからこの秒数以内の再カウントを無視する保険。チャタリング・同一線の二重発火対策。`last_*_at` は**線（RuleName）ごと**に持つ（例 `dict[str, float]`）。
  - HTTP 受信層は方向（entry/exit）単位だが、MQTT は**線ごと**にすることで「別入口の同時通過」を取りこぼさない。本番は1カメラ・入庫線/出庫線が各1本なので方向単位と実質同じ挙動だが、将来同一方向に複数線を引いても安全な**線単位**を採用する。
- `ObjectId` による重複除去（任意）: 直近に処理した `ObjectId` を短時間記憶し、同一 ID の再 `true` を無視する余地を残す。既定は無効（実機で重複が観測されたら有効化）。

### 4.5 並行性

paho-mqtt のコールバックはネットワークスレッドから呼ばれる。`counter` 更新と内部状態（`last_*_at` 等）を保護するため `threading.Lock` を1本持ち、`on_message` のカウント反映区間をロックする。`on_entry`/`on_exit` は HTTP 受信層と同じく counter 側でも逐次化される前提。

### 4.6 ハートビート（死活監視）

MQTT モードは HTTP サーバを持たないため、`/health`・`/state` のような外部から叩ける生存確認の口が無い。運用で「アプリが生きているか・broker に繋がっているか・最後にイベントを受けたのはいつか」をログから追えるよう、**定期ハートビートログ**を出す。

- `start()` 時にデーモンスレッドを1本立て、`heartbeat_interval`（秒, 既定 `60.0`、`0` で無効）ごとに INFO で1行出力する。内容:
  - broker 接続状態（`on_connect`/`on_disconnect` で更新する内部フラグ `connected`）。
  - 受信した総メッセージ数・採用（カウント）した総数。
  - **直近にメッセージを受信した時刻**（`last_message_at`。一度も受けていなければ「未受信」）。
  - 例: `MQTT heartbeat: connected=True, msgs=1234, counted=600, last_msg=2026-06-05T01:23:45Z`
- これにより「broker は繋がっているがイベントが全く来ない（カメラ側ルール未設定/対象物フィルタ過剰）」状態と「broker 未接続」状態をログだけで切り分けられる。
- 監視システムがある場合はこの行を grep して `connected=False` や `last_msg` 停滞でアラートできる。HTTP 口を別途立てる重い実装はフェーズ1ではしない（必要になったら後付け）。
- `stop()` でハートビートスレッドも停止する（§7 の graceful 停止に含める）。

## 5. 設定ファイル差分 (config.toml)

```toml
[receiver]
type = "mqtt"   # ★ "http" | "gpio" | "dummy" | "mqtt"

[receiver.mqtt]                 # ★ 新規セクション
host = "localhost"              # アプリ→broker の接続先。Pi 同居なので localhost
port = 1883
username = ""                   # 認証なしなら空文字。broker に合わせる
password = ""
client_id = "parking-tracker"
keepalive = 60
topic = "+/onvif-ej/OpenApp/WiseAI/LineCrossing/#"   # 購読トピック（ワイルドカード可）
count_state = "true"            # Data.State がこの値のときだけカウント（大文字小文字は無視）
min_event_interval = 0.0        # 連続カウント抑制（秒）。0=無効。抑制キーの粒度は §4.4
heartbeat_interval = 60.0       # 死活ログを出す間隔（秒）。0=無効（§4.6）

[receiver.mqtt.rules]           # RuleName(=仮想線名) → 方向 ("entry"/"exit")
# 現地確定値（2026-06-24実機確認）— WiseAI 仮想線名と一致させる
entry = "entry"
exit  = "exit"
```

- `host = "localhost"` は **アプリが broker に繋ぐ先**。カメラが broker に繋ぐ先（Pi の LAN 内 IP）はカメラ側設定であり別物（§8）。
- 認証を付けない LAN クローズド構成なら `username`/`password` は空でよい（broker 側 `allow_anonymous true`）。付ける場合は §9 と合わせる。

## 6. config.py 差分

- `ReceiverConfig.type` の `Literal` に `"mqtt"` を追加。
- `MqttReceiverConfig` dataclass を追加（host:str, port:int, username:str, password:str, client_id:str, keepalive:int, topic:str, count_state:str, min_event_interval:float, heartbeat_interval:float, rules:dict[str,str]）。
- `[receiver.mqtt]` のパース・バリデーションを `load_config` に追加:
  - `type="mqtt"` なら `[receiver.mqtt]` 必須。
  - `rules` の各値は `"entry"`/`"exit"` のいずれかであること（それ以外は ConfigError）。
  - `rules` が空なら「カウント対象の線が無い」旨を **WARNING**（エラーにはしない＝起動はできる）。
  - `port` は 1..65535、`min_event_interval >= 0`、`heartbeat_interval >= 0`（既定 `60.0`、`0` で無効）。
  - `count_state` は空文字でないこと（既定 `"true"`）。**パース時に `count_state.lower()` で正規化して保持**し、`on_message` 側の `state.lower()` と確実に一致させる（`"True"`/`"TRUE"` 等の表記揺れで永久に一致しない事故を防ぐ）。`topic` は空文字でないこと。
  - `username` が空で `password` が非空、の片側だけ指定はおそらく設定ミスなので WARNING。
- `ReceiverConfig` に `mqtt: MqttReceiverConfig | None` フィールド追加、`summary()` に mqtt 分岐追加。

## 7. ファイル構成・依存差分

```
src/parking/receivers/
├─ base.py     # 変更なし（EventReceiver Protocol をそのまま実装）
├─ http.py     # 変更なし
├─ gpio.py     # 変更なし
├─ dummy.py    # 変更なし
└─ mqtt.py     # ★ 新規: paho-mqtt で購読しエッジ(State=true)を検出
```

`receivers/mqtt.py` の責務:
- paho-mqtt クライアント組み立て・接続・購読（`start()`）、graceful 切断（`stop()`）。
- メッセージ受信 → JSON パース → State フィルタ → RuleName 振り分け → `on_entry`/`on_exit`。
- 不正 JSON・想定外スキーマは例外を握りつぶして WARNING（1メッセージの異常で受信ループを止めない）。
- ハートビートスレッドの起動・停止（§4.6）。

`stop()` の graceful 停止手順（HTTP 受信層の `stop()` と同等の堅牢さを担保）:
1. ハートビートスレッドへ停止フラグ（`threading.Event`）を立て、`join(timeout=...)`。
2. paho-mqtt は **`loop_stop()` でネットワークスレッドを止めてから `disconnect()`** の順で呼ぶ（自動再接続スレッドが残って再接続し続けないようにする）。
3. ネットワーク/ハートビート両スレッドが期限内に止まらなければ WARNING ログ（HTTP の `stop()` が join タイムアウトで警告するのと同じ）。
4. `stop()` は二重呼び出し・未 `start()` 状態でも安全（None チェック）にする。

`app.py` の `_build_receiver()` に分岐追加（既存パターン踏襲）:
```python
if rtype == "mqtt":
    if cfg.receiver.mqtt is None:
        raise ValueError("receiver.type=mqtt だが [receiver.mqtt] が無い")
    from .receivers.mqtt import MqttReceiver
    return MqttReceiver(config=cfg.receiver.mqtt, on_entry=on_entry, on_exit=on_exit)
```
`app.py` 本体・`counter.py`・`store.py` は無変更。

### 7.1 依存追加

`requirements.txt`:
```
paho-mqtt>=2.0
```

## 8. カメラ側設定（クライアント／設置担当が実施）

**実機確認済み（2026-06-24）**: Hanwha XNO-A6084R は **MQTT クライアント接続を broker に向けるだけで、設定されているすべての ONVIF / WiseAI イベント（LineCrossing 含む）を自動 publish する**。現地キャプチャで motion / blur / relay 等、明示的にルール化していないイベントまで大量に自動配信されることを実測で確認済み。

この挙動から:
- **LineCrossing カウントに必要なカメラ側設定は以下の2つだけ**:
  1. **MQTT クライアント接続設定**: 接続先 = **Pi の LAN 内 IP : 1883**、認証を付けたなら username/password も。
  2. **WiseAI 仮想線2本の定義（`entry` / `exit`、対象物フィルタ = 車両）**: この線名が `Source.RuleName` ＝トピック末尾になり、`[receiver.mqtt.rules]` のキーと一致する。
- **「イベントルール作成 ＋ MQTT アクション有効化 ＋ カスタム MQTT 発行プロファイル」は LineCrossing カウントには不要**。実際に設定すると同一通過で二重 publish になるため、設定しないこと。
- ~~片方しか MQTT を有効化しないとカウント片側欠落する~~: 自動配信が前提のため該当しない。

本番で必要な設定（確定値）:

1. **MQTT クライアント接続設定**（カメラの MQTT 設定画面）。
   - 接続先 = **Pi の LAN 内 IP : 1883**、認証あり構成なら username/password も一致させる。
2. **WiseAI 仮想線を2本定義**（対象物フィルタ = 車両 car）。
   - 入庫線名: **`entry`**、出庫線名: **`exit`**（現地確定値。`[receiver.mqtt.rules]` のキーと一致）。
   - 対象物選別はカメラ側で実施する。LineCrossing ペイロードに ObjectType は含まれないため、アプリ側での絞り込みは不可。

確認・調整事項（テスト時）:
- イベントルール画面の「実行時間: 60」「アラーム出力: 5s」が **MQTT 発行間隔のスロットルになっていないか**（連続入庫の取りこぼし確認）。

## 9. broker（Mosquitto）構築（Pi 上）

```bash
sudo apt install -y mosquitto mosquitto-clients
sudo systemctl enable --now mosquitto
```

`/etc/mosquitto/conf.d/parking.conf`:
```conf
listener 1883 0.0.0.0       # LAN 上のカメラから接続できるよう全 IF で待受
# --- 認証あり構成（推奨） ---
allow_anonymous false
password_file /etc/mosquitto/passwd
# --- 認証なし LAN クローズド構成にするなら上2行を消し allow_anonymous true ---
```

認証を付ける場合:
```bash
sudo mosquitto_passwd -c /etc/mosquitto/passwd parking   # ユーザー作成
sudo systemctl restart mosquitto
```

- デフォルトの Mosquitto は localhost のみ待受のことがあるため、**カメラ（別機器）から繋ぐには `listener 1883 0.0.0.0` が必須**。
- **インターネットへポート開放しない**（1883 は平文・常時スキャン対象）。Pi で `ufw` 利用時は LAN サブネット限定で開ける:
  ```bash
  sudo ufw allow from <LANサブネット>/24 to any port 1883 proto tcp
  ```
- TLS(8883) は LAN クローズドなら必須ではない（必要なら後付け）。
- broker の認証情報は本アプリ側では `config.toml`（`.gitignore` 済み・リポジトリに含めない）に置く。`config.example.toml` には空のプレースホルダのみ記載し、実値はコミットしない。

## 10. systemd

`systemd/parking.service` の依存に Mosquitto を追加（同一 Pi 上にある前提）。

```ini
[Unit]
Description=Parking Occupancy Tracker
After=network-online.target mosquitto.service
Wants=network-online.target
Requires=mosquitto.service

[Service]
Type=simple
WorkingDirectory=/opt/parking
ExecStart=/usr/bin/python3 -m parking
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=multi-user.target
```

`mosquitto.service` が無いと subscribe 先が無いため、HTTP 経路（LinkBase は `After=` のみで可）と違い **`Requires=` で起動を要求**する。paho-mqtt 自動再接続があるので起動順の前後は致命的ではないが、依存は明示する。

## 11. 動作確認

### 11.1 単体（カメラなし・mosquitto_pub で擬似 publish）

`receiver.type="mqtt"`、`rules` に `bike="entry"` を設定した状態で:

```bash
# 入庫（State=true）→ +1
mosquitto_pub -h localhost -t 'AA:BB/onvif-ej/OpenApp/WiseAI/LineCrossing/&vs-0/bike' \
  -m '{"UtcTime":"2026-05-29T00:00:00Z","Source":{"RuleName":"bike"},"Data":{"State":"true","ObjectId":"1","Action":"Right"}}'

# イベント解除（State=false）→ 無視（カウント変化なし）
mosquitto_pub -h localhost -t 'AA:BB/onvif-ej/OpenApp/WiseAI/LineCrossing/&vs-0/bike' \
  -m '{"UtcTime":"2026-05-29T00:00:04Z","Source":{"RuleName":"bike"},"Data":{"State":"false","ObjectId":"","Action":""}}'

# 出庫線（rules に exit="exit" を登録した上で）→ -1
mosquitto_pub -h localhost -t 'AA:BB/onvif-ej/OpenApp/WiseAI/LineCrossing/&vs-0/exit' \
  -m '{"Source":{"RuleName":"exit"},"Data":{"State":"true","ObjectId":"2"}}'

# 未登録 RuleName → WARNING で無視されること
mosquitto_pub -h localhost -t '.../unknown' \
  -m '{"Source":{"RuleName":"unknown"},"Data":{"State":"true"}}'
```

確認:
- `State:"true"` のみカウントされ、`"false"` は無視されること。
- RuleName に応じて入庫/出庫が正しく振り分くこと。
- 未登録 RuleName・不正 JSON で**落ちず**ログ警告で継続すること。
- broker 再起動後に自動再接続して受信再開すること。

### 11.2 結合（クライアント環境・実機カメラ）
1. Mosquitto を §9 で構築、カメラ MQTT プロファイルを Pi broker に向ける（§8）。
2. `[receiver.mqtt]` の host/port/認証/`rules` を実環境に合わせて確定（broker IP はこの段階で確認）。
3. 入庫線を実際に横切る → ログに「入庫検出 / current=N+1」。
4. 出庫線 → 「出庫検出 / current=N-1」。
5. Pi 再起動 → `parking_state.json` から復元（既存仕様）。

## 12. 既存（接点）経路との関係

- MQTT 経路と接点（HTTP/LinkBase）経路は `receiver.type` で**排他選択**。同時併用はしない（前提: 1駐車場はどちらか一方）。
- カメラ側では同一イベントルールから接点出力と MQTT の両アクションを出せるため、将来どちらの経路にも切替可能。本アプリ側は受信層を差し替えるだけ。
- 信頼性特性は経路で異なる: HTTP/接点は LinkBase が「変化時のみ送信＋1秒停止」で律速、MQTT は §3.1 のとおり QoS 0 で fire-and-forget。いずれも取りこぼし時は現在値の累積保持でカバーする前提は共通。

## 12.1 関連ドキュメントへの反映（実装時のフォローアップ）

- `REQUIREMENTS.md` §0（現在地・意思決定ログ）に「LAN/MQTT 受信層を追加（クライアント指定）」を1行追記する。
- `SPECIFICATION.md`（as-built）は**実装完了後**に MQTT 経路を追記する（本書は設計、SPECIFICATION は実装済み挙動の記述という役割分担）。
- ~~`config.example.toml` に `[receiver.mqtt]` / `[receiver.mqtt.rules]` のコメント付きサンプルを追加する（§5 の内容）。~~ **済（2026-06-05、本設計確定と同時に反映）**。

## 13. 未解決事項（実装をブロックしない / テスト時に確定）

### 解決済み（2026-06-24 実機確認）

- ~~**broker の IP・ポート・認証情報**~~: → 解決。Pi の LAN 内 IP:1883 + 認証で確立済み。`[receiver.mqtt]` に設定済み。
- ~~**本番の仮想線名（入庫/出庫）**~~: → 解決。`entry`（入庫）/ `exit`（出庫）に確定。`[receiver.mqtt.rules]` に反映済み。
- ~~**対象物フィルタ**~~: → 解決。カメラ側 WiseAI 仮想線の対象物フィルタを車両に設定することで対応。LineCrossing ペイロードに ObjectType は含まれないためアプリ側での絞り込みは不可・不要（カメラ側完結）。
- ~~**min_event_interval の値**~~: → 解決。実測で「1通過 = true 1発のみ」を確認。`min_event_interval = 0.0`（無効）で十分。
- ~~**1ゲート2線の方向フィルタ**~~: → 解決。実機動作で入庫線/出庫線が独立して発火することを確認。誤発火なし。

### 未解決（引き続き要確認）

- **「実行時間60」「アラーム出力5s」の影響**: MQTT 発行間隔のスロットルになっていないか実機確認。なっていれば `min_event_interval` で吸収 or カメラ側調整。
- **重複除去の要否**: 同一 ObjectId の多重 `true` が実機で出るなら `ObjectId` 重複除去を有効化（現状は未観測のため既定無効のまま）。
- **配信品質（§3.1）**: 取りこぼしを許容できない要件なら、カメラ側 publish QoS を 1 にできるか確認。既定（QoS 0）では既知の制約として受容。
