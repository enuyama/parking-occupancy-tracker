# 結合手順書: カメラの接点 → アプリのカウント増減を確認するまで

> カメラ(XNO-A6084R)が車を検知して出す接点信号を、Raspberry Pi → LinkBase → 本アプリ と流し、
> 最終的に**アプリの現在台数が +1 / -1 されること**を確認するまでの手順をまとめる。
> 関連: [REQUIREMENTS.md](../REQUIREMENTS.md) §0/§6.1/§8.1、[DESIGN_HTTP_RECEIVER.md](DESIGN_HTTP_RECEIVER.md)、[SPECIFICATION.md](SPECIFICATION.md)

最終更新: 2026-05-26

---

## 0. 全体像

```
[カメラ XNO-A6084R]
   │ IO1(オレンジ)=入庫 / IO2(茶)=出庫 … オープンコレクタのパルス出力
   │ GND(黒)
   ▼  ← フォトカプラで絶縁・サージ対策。GNDは共通化（外部業者の配線スコープ）
[LinkBase IOボードの接点入力端子（MCP23017 の入力 8〜11）]
   │            ※ Pi本体の40ピンGPIOではない。Pi は MCP23017 と I2C で接続
   ▼  ← LinkBase ソフトが入力を監視し、状態が変化した時だけ送信
[LinkBase（同一Pi上のソフト, Mode4）]
   │ GET /api/control?alert=XXXX9999&id=... （localhost）
   ▼
[本アプリ parking]
   │ 立ち上がりエッジを検出 → record_entry / record_exit
   ▼
現在台数 +1 / -1 → parking_state.json 保存 → ログ出力
```

> LinkBase の実ソース（`opt/light/main.py` ほか）で確認済み。LinkBase は Pi にI2C接続した
> **MCP23017（GPIO拡張IC）の入力ピン8〜11** を読み、**状態が変化した時のみ** HTTP GET を送る。

**重要な役割分担:**
- カメラ→LinkBase IOボード入力端子 の物理配線・フォトカプラ … **外部業者（ハード）**
- カメラのAIイベント→アラーム出力の紐付け … **カメラ Webviewer 設定（設置担当）**
- 接点入力監視→HTTP送信 … **LinkBase ソフト（設定のみ）**
- HTTP受信→カウント … **本アプリ（実装済み）**

本アプリは GPIO に触れない（MCP23017 を読むのは LinkBase）。アプリから見える入力は HTTP だけ。

---

## 1. 事前に用意するもの

- Raspberry Pi（LinkBase ソフトと本アプリが同居）
- カメラ XNO-A6084R（PoE給電・ネットワーク接続済み、Webviewer にアクセスできる状態）
- カメラ付属のオーディオ/アラームケーブル（オレンジ=IO1 / 茶=IO2 / 黒=GND）
- フォトカプラ＋電流制限抵抗（外部業者手配）
- 本アプリ一式（`git clone` 済み、Python 3.9+。3.11未満でも tomli 併用で動作）

---

## 2. ステップ1: 配線（カメラ → LinkBase IOボード入力端子）

カメラのアラーム出力(OC)を、**LinkBase 基板の接点入力端子 IN（CN4）** に接続する。
**Pi本体の40ピンGPIOではない**（接点を読むのは基板上のIC、Pi はI2C接続）。

LinkBase公式仕様 §6 で **alert の桁番号 = 接点入力ポート番号（IN1〜IN4）** と確定:

| 用途 | カメラ線 | alert桁 | LinkBase端子 |
|---|---|---|---|
| 入庫 | IO1（オレンジ） | 1桁目 | IN1（CN4） |
| 出庫 | IO2（茶） | 2桁目 | IN2（CN4） |
| （未使用） | — | 3桁目 | IN3 |
| （未使用） | — | 4桁目 | IN4 |
| GND | 黒 | — | CN4 の COM（基板GNDに接続済み） |

- 接点は **IN と COM の間**に入れる（仕様書 §3.2: COMは基板GNDに接続済み）。
- 間に**フォトカプラ**を入れる（屋外・絶縁・サージ対策。REQUIREMENTS §6.1）。カメラとIOボードの**GNDを共通化**。

> ⚠️ **極性（入力ありが alert `1` か `0` か）は実機で確認。**
> 公式仕様 §6 では **`1`＝入力あり / `0`＝入力なし** なので、カメラ発火→入力あり→`1` が既定（`active_value="1"`）。
> ただしフォトカプラの組み方で反転しうるため、**ステップ4で実測して確定**する。

---

## 3. ステップ2: カメラ側設定（Webviewer）

カメラの AI 分析イベントを、アラーム出力に紐付ける。

1. Webviewer にログイン。
2. IO1 / IO2 を **「出力」** に設定（2 configurable I/O ports。マニュアル p.33）。
3. AIエンジンの分析イベントを設定:
   - 「仮想線（交差・方向）」または「車両カウント」で、**入庫方向の通過 → IO1**、**出庫方向の通過 → IO2** に割り当てる。
4. アラーム出力のパルス幅（持続時間）を確認・設定。
   - **重要**: LinkBase は接点の状態が変化した時だけ送信し、**1回送信するごとに約1秒停止する**（`main.py` の `DI_WAIT=1`）。
     パルスが短いと、この停止中に「閉→開」が起きて**1台分を丸ごと取りこぼす**。
   - 安全のため **パルス幅・車の間隔とも 1.5秒以上**を目安に設定する（0.2秒では不足）。
   - 取れた値はステップ5の `min_event_interval` 調整に反映。

> 入庫/出庫の「線の引き方・進入方向」はカメラ設置側の責務（本アプリのスコープ外）。

---

## 4. ステップ3: LinkBase側設定

LinkBase の `/opt/light/config.json` を設定する（参考: `tbbox-playlist-switcher/DEPLOY.md` 付録）。

```json
{
    "GET_URL": "http://127.0.0.1:8080/api/control",
    "SIM_ID": "unused",
    "Mode": "4",
    "MONITOR_INTERVAL": 0.2
}
```

| キー | 設定値 | 意味 |
|---|---|---|
| `GET_URL` | `http://127.0.0.1:8080/api/control` | 本アプリのエンドポイント（同一Pi=localhost） |
| `Mode` | `"4"` | 全ポート通知モード（接点状態をHTTPで送る） |
| `MONITOR_INTERVAL` | `0.2` | GPIO監視周期(秒) |
| `SIM_ID` | 任意の文字列（ダミー可） | HTTPの `id=` に乗るだけ。**本アプリは無視する**ので値は何でもよい |

> **SIM_ID について（ソース確認済み）:** 送信先が localhost のため、本アプリの動作には一切関係しない（`id=` は読み捨て）。
> ただし `main.py` が `SIM_ID = config['SIM_ID']` で参照しており、**キーごと削除すると KeyError で LinkBase が起動しない**。
> → **キーは必ず残す**。値は任意（`"unused"` 等のダミーで可）。

> **Mode に注意:** 公式の出荷デフォルトは `"4"`（全ポート通知）。ただし表示灯用途で `Mode="2"`/`"3"`（変化してない側を`9`でマスク）に変更されている個体があるので確認する。
> 駐車場用途では **`Mode="4"`** にする。`MONITOR_INTERVAL` は複数接点同時変化時に必要なら調整（公式 §8）。

### LinkBase への接続（config.json 編集方法）

config.json は LinkBase 機器内（`/opt/light/config.json`）にあるので、SSH で入って編集する（公式 §7・§9）:

- 接続: SORACOM Napter のオンデマンドリモートアクセスで**デバイス側ポート22**を開け、SSH。
- ログイン: ユーザー `mtx` / パスワード `MechaTracks`（公式 §9.1）。
- 編集後の反映: `sudo systemctl restart light-controller.service`（公式 §9.2）。
- 一時停止: `sudo systemctl stop light-controller.service`。

---

## 5. ステップ4: 極性・ピン対応の実測確認（最重要）

ステップ1の⚠️を潰す。**アプリを起動した状態で**、既知の接点を手でON/OFFしながら届く `alert` を観測する。

1. 本アプリをデバッグログで起動（ステップ6参照、`logging.level = "DEBUG"` にしておくと `受信: alert=...` が見える）。
2. 入庫側の接点（カメラIO1相当）を手動でON/OFF、またはカメラの前で実際に入庫方向に通過させる。
3. ログに出る `alert` を確認:
   - どの桁が変化するか → entry_switch / exit_switch の桁番号（=IN番号）
   - **入力あり（カメラ発火）のとき**その桁が `0` か `1` か → `active_value`（公式仕様では `1`=入力あり）
4. 出庫側でも同様に確認。

この結果でステップ6のアプリ config を確定させる。

---

## 6. ステップ5: アプリのconfig設定

`config.toml`（無ければ `cp config.example.toml config.toml`）の受信層を実測値に合わせる。

```toml
[receiver]
type = "http"

[receiver.http]
host = "127.0.0.1"
port = 8080
entry_switch = 1        # 入庫の桁番号(=IN番号, 1..4)。ステップ4で確認
exit_switch  = 2        # 出庫の桁番号
active_value = "1"      # 入力あり(カメラ発火)のとき alert が "1" なら "1"（公式仕様の既定）。反転構成なら "0"
min_event_interval = 0.5  # カメラのパルス幅に合わせて調整
```

総台数・しきい値も実値に:

```toml
[parking]
total_spaces = 100

[thresholds]
crowded_at = 80
full_at    = 100
```

---

## 7. ステップ6: アプリ起動

起動方法は2つ。**動作確認は 7-1 の手動起動**、**本番常駐は 7-2 の systemd** を使う。

引数で config ファイルのパスを渡す（省略時はカレントの `config.toml`）。

### 7-1. 手動起動（動作確認・デバッグ用）

```bash
cd <プロジェクトルート>

# 初回のみ: 仮想環境と依存
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 初回のみ: 設定ファイル用意（ステップ5に従って編集）
cp config.example.toml config.toml

# 起動（フォアグラウンド。Ctrl-C で停止）
PYTHONPATH=src python -m parking config.toml
```

- `src/` をパッケージ探索路に入れるため **`PYTHONPATH=src`** が必要。
- 実測確認（ステップ4）の前は `config.toml` の `[logging] level = "DEBUG"` にしておくと、届いた `alert` と判定理由が全部出る。

起動ログ例（正常時）:
```
... 設定を読み込みました (config.toml): total_spaces=100, crowded_at=80, full_at=100, receiver=http(host=127.0.0.1:8080, entry=SW1, exit=SW2, active='0', ...)
... 状態ファイル parking_state.json が無いため、新規（0台）で開始します。
... HttpReceiver 起動: http://127.0.0.1:8080/api/control (entry=SW1, exit=SW2, active='1', min_interval=0.500s)
... 起動完了: total=100 current=0 status=EMPTY。イベント待機中。
```

起動に失敗した場合は `[FATAL] ...` が stderr に出る（設定エラーは終了コード2、ポート競合等は1）。

### 7-2. systemd 常駐（本番・自動起動）

Pi 再起動後も自動で立ち上がるようにする。アプリを `/opt/parking` に配置する前提（変える場合は service ファイルの各パスを合わせる）。

```bash
# 1) 配置
sudo mkdir -p /opt/parking
sudo cp -r <プロジェクトルート>/* /opt/parking/
cd /opt/parking

# 2) 仮想環境と依存
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt

# 3) 設定ファイル（ステップ5に従って編集）
sudo cp config.example.toml config.toml
sudo nano config.toml

# 4) サービス登録
sudo cp systemd/parking.service /etc/systemd/system/parking.service
sudo systemctl daemon-reload
sudo systemctl enable --now parking.service

# 5) 状態・ログ確認
systemctl status parking.service
journalctl -u parking.service -f
```

サービス定義（`systemd/parking.service`）の要点:
- `Environment=PYTHONPATH=/opt/parking/src` … `-m parking` を解決するため。
- `ExecStart=/opt/parking/.venv/bin/python -m parking /opt/parking/config.toml`
- `After=...light-controller.service` … LinkBase（`light-controller.service`）の後に起動。
- `Restart=on-failure` … 異常終了時に自動再起動。

操作コマンド:
```bash
sudo systemctl restart parking.service   # 再起動（config変更後など）
sudo systemctl stop parking.service      # 停止
sudo systemctl disable parking.service   # 自動起動を無効化
```

---

## 8. ステップ7: カウント増減の確認

### 8-1. 実機（カメラ）で確認

1. カメラの前を**入庫方向**に通過（またはIO1接点をON/OFF）。
2. アプリのログに以下が出ることを確認:
   ```
   入庫検出: current=1 status=EMPTY
   ```
3. **出庫方向**に通過（またはIO2接点をON/OFF）:
   ```
   出庫検出: current=0 status=EMPTY
   ```
4. 現在値を確認:
   ```bash
   curl http://127.0.0.1:8080/state
   # {"current":0,"total":100,"occupancy":"EMPTY"}

   cat parking_state.json
   # {"current_count":0,"status":"EMPTY","updated_at":"..."}
   ```

### 8-2. 確認できるポイント

- 入庫で `current` が +1、出庫で -1 されるか。
- 1台の通過で **1だけ**増えるか（パルス維持中に多重カウントされないか）。
  - 多重カウントされる場合 → `min_event_interval` を上げる、カメラのパルス幅を調整。
- 80台で `status` が CROWDED、100台で FULL になるか（しきい値）。
- 0台で出庫イベントが来ても負にならない（ログに範囲外WARNINGが出て値は動かない）。

### 8-3. 再起動復元の確認

1. 何回か入庫して `current` を非ゼロにする。
2. アプリを Ctrl-C で停止 → 再起動。
3. 起動ログに `状態を復元: current=N` が出て、停止前の値から再開すればOK。

---

## 9. LinkBaseなしで先に確認したい場合（切り分け用）

実機・LinkBaseが揃う前に、アプリ単体の動作を curl で確認できる（LinkBaseの代わりに手でHTTPを叩く）。

```bash
# （active_value="1" の場合。入力あり=1 で立ち上がりを作る）
# 入庫の立ち上がり（SW1: 0→1）
curl "http://127.0.0.1:8080/api/control?alert=00999999&id=t"   # 非ACTIVE(入力なし)
curl "http://127.0.0.1:8080/api/control?alert=10999999&id=t"   # SW1=1(入力あり) → +1
# 連続ACTIVEは増えない
curl "http://127.0.0.1:8080/api/control?alert=10999999&id=t"   # 1のまま → 変化なし
# 出庫（SW2: 0→1）
curl "http://127.0.0.1:8080/api/control?alert=00999999&id=t"   # 戻す
curl "http://127.0.0.1:8080/api/control?alert=01999999&id=t"   # SW2=1 → -1

curl "http://127.0.0.1:8080/state"
```

これが期待通りなら「HTTP受信→カウント」は正常。残るは「カメラ→Pi→LinkBase→HTTP」の物理・設定だけに切り分けられる。

---

## 10. トラブルシューティング

| 症状 | 切り分け・対処 |
|---|---|
| アプリにHTTPが来ない | `curl http://127.0.0.1:8080/health` が返るか。LinkBase の `GET_URL`・`Mode:4`・再起動を確認。ポート8080衝突確認（`netstat -tulpn \| grep 8080`） |
| HTTPは来るがカウントされない | ログの `受信: alert=...`（DEBUG）で桁と値を確認。`entry_switch`/`exit_switch`/`active_value` がズレていないか（ステップ4の実測と照合） |
| 1台で複数カウントされる | 接点のチャタリング。`min_event_interval` を上げる、カメラのアラーム出力持続時間を調整 |
| 速い/連続通過で台数を取りこぼす | パルスが短く、LinkBaseの送信後1秒停止(`DI_WAIT`)中に閉→開が収まっている。カメラのパルス幅・車間隔を1.5秒以上に |
| カウントが増えるが減らない（逆も） | 入庫/出庫の桁割当が逆。`entry_switch`/`exit_switch` を入れ替え、または配線(IO1/IO2)を確認 |
| 全く逆（ONで増えない/OFFで増える） | `active_value` の極性が逆。`"0"`↔`"1"` を切替 |
| 起動時に勝手に+1される | 起動直後カメラがACTIVE維持中だった可能性。仕様上は非ACTIVE初期化だが、配線・極性を再確認 |

---

## 11. チェックリスト（現地作業用）

- [ ] カメラ IO1/IO2/GND を フォトカプラ経由で LinkBase IOボード入力端子(MCP23017 8〜11)に配線、GND共通化
- [ ] カメラ Webviewer: IO1/IO2を出力に、AIイベントを紐付け、パルス幅設定
- [ ] LinkBase `config.json`: GET_URL / Mode=4 / MONITOR_INTERVAL=0.2、再起動
- [ ] ステップ4: alert を実測し、桁対応・極性を確定
- [ ] アプリ `config.toml`: entry_switch / exit_switch / active_value / total_spaces / しきい値
- [ ] アプリ起動、`/health` 応答確認
- [ ] 入庫で +1、出庫で -1、1通過=1カウントを確認
- [ ] しきい値（混/満）の切替確認
- [ ] 再起動で current が復元することを確認
