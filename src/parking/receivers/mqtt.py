from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from ..config import MqttReceiverConfig
from .base import EventCallback

logger = logging.getLogger(__name__)


class MqttReceiver:
    """カメラの WiseAI 仮想線交差イベントを MQTT broker 経由で受信し、
    Data.State が count_state のメッセージだけを採用して on_entry / on_exit を呼ぶ受信層。

    プロトコル・設計詳細は docs/DESIGN_MQTT_RECEIVER.md（特に §4）。

    要点:
    - paho-mqtt 2.x（CallbackAPIVersion.VERSION2）を使う。
    - connect_async() + loop_start() でノンブロッキング起動（broker 未起動でも start() は失敗しない）。
    - 切断・接続失敗は自動再接続（指数バックオフ）に任せ、再接続時の購読復帰のため
      subscribe は必ず on_connect 内で行う。
    - 受信ループは1メッセージの異常で止めない（全体 try/except で WARNING して破棄）。
    """

    def __init__(
        self,
        config: MqttReceiverConfig,
        on_entry: EventCallback,
        on_exit: EventCallback,
    ) -> None:
        self._cfg = config
        self._on_entry = on_entry
        self._on_exit = on_exit

        # カウント反映・内部状態を保護する1本のロック。
        # paho-mqtt のコールバックはネットワークスレッドから呼ばれるため必須。
        self._lock = threading.Lock()

        # 連続カウント抑制（min_event_interval）の前回採用時刻を RuleName ごとに持つ。
        # 線（仮想線）単位にすることで別入口の同時通過を取りこぼさない（§4.4）。
        self._last_count_at: dict[str, float] = {}

        # 統計・死活監視用カウンタ（ハートビートで出力する）。
        self._connected: bool = False
        self._msg_total: int = 0          # 受信した総メッセージ数
        self._counted_total: int = 0      # 採用（カウント反映）した総数
        self._last_message_at: datetime | None = None  # 直近のメッセージ受信時刻(UTC)

        # ハートビートスレッドとその停止イベント。
        self._hb_thread: threading.Thread | None = None
        self._hb_stop = threading.Event()

        self._client: mqtt.Client | None = None
        self._started = False
        # stop() による意図的切断かどうか。意図的なら _on_disconnect で WARNING を出さない。
        self._stopping = False

    # ------------------------------------------------------------------
    # 共通ヘルパ
    # ------------------------------------------------------------------
    @staticmethod
    def _payload_text(payload, limit: int = 1000) -> str:
        """ログ用にペイロードを安全に文字列化し、長すぎる場合は切り詰める。"""
        if isinstance(payload, (bytes, bytearray)):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = str(payload)
        if len(text) > limit:
            return f"{text[:limit]}...(切り詰め, 全{len(text)}バイト)"
        return text

    # ------------------------------------------------------------------
    # paho-mqtt コールバック
    # ------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        """接続結果のハンドリング。成功時はここで subscribe して再接続時の購読復帰を保証する。"""
        # paho-mqtt 2.x の reason_code は ReasonCode オブジェクト。is_failure で成否判定できる。
        if not getattr(reason_code, "is_failure", False):
            with self._lock:
                self._connected = True
            logger.info(
                "MQTT 接続成功: %s:%d を購読 topic=%r (QoS 1)",
                self._cfg.host,
                self._cfg.port,
                self._cfg.topic,
            )
            # 再接続時にも購読が復帰するよう、必ず on_connect 内で subscribe する。
            client.subscribe(self._cfg.topic, qos=1)
            return

        # 失敗時。認証系の拒否は設定ミスに即気づけるよう ERROR で目立たせる。
        text = str(reason_code).lower()
        if "not authorized" in text or "bad user" in text or "password" in text:
            logger.error(
                "MQTT 接続が認証で拒否されました (reason=%s)。"
                "receiver.mqtt の username/password を確認してください。",
                reason_code,
            )
        else:
            logger.warning("MQTT 接続に失敗しました (reason=%s)。自動再接続を継続します。", reason_code)

    def _on_disconnect(self, client, userdata, *args) -> None:
        """切断時。接続フラグを倒す。再接続は paho-mqtt の自動再接続に任せる。

        paho-mqtt 2.x はコールバック署名が版差で揺れるため可変長で受ける。
        """
        with self._lock:
            self._connected = False
        if self._stopping:
            logger.info("MQTT を切断しました（stop() による正常切断）。")
            return
        # VERSION2 の署名は (client, userdata, disconnect_flags, reason_code, properties)。
        # 版差に備えて可変長で受けているため、reason_code は位置でゆるく拾う。
        reason = args[1] if len(args) >= 2 else (args[0] if args else None)
        logger.warning("MQTT が切断されました (reason=%s)。自動再接続を試みます。", reason)

    def _on_subscribe(self, client, userdata, mid, reason_code_list, properties=None) -> None:
        """SUBACK 受信時。購読が実際に成立したか・付与 QoS を確認できるよう記録する。"""
        logger.debug("MQTT SUBACK 受信: mid=%s 付与結果=%s", mid, reason_code_list)

    def _on_message(self, client, userdata, message) -> None:
        """受信メッセージの処理。1メッセージの異常で受信ループを止めないよう全体を try/except で囲う。"""
        try:
            now_utc = datetime.now(timezone.utc)
            with self._lock:
                self._msg_total += 1
                self._last_message_at = now_utc

            # A. 全受信メッセージの生トレース（採用/破棄に関わらず全メッセージを記録）。
            # 全メッセージで発火するホットパスのため、DEBUG 無効時はペイロードのデコード自体を避ける
            # （%-style は %s 置換を遅延するが、引数式 _payload_text(...) の評価は遅延しないため）。
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("MQTT受信(raw): topic=%r payload=%s", message.topic, self._payload_text(message.payload))

            try:
                msg = json.loads(message.payload)
            except (ValueError, TypeError) as e:
                logger.warning(
                    "MQTT メッセージの JSON 解析に失敗 (topic=%r): %s。payload=%s。破棄します。",
                    message.topic,
                    e,
                    self._payload_text(message.payload),
                )
                return
            if not isinstance(msg, dict):
                logger.warning(
                    "MQTT メッセージが JSON オブジェクトではありません (topic=%r)。payload=%s。破棄します。",
                    message.topic,
                    self._payload_text(message.payload),
                )
                return

            data = msg.get("Data") or {}
            state = str(data.get("State", "")).lower()
            if state != self._cfg.count_state:
                # 既定 "true" 以外（イベント解除 "false" など）は採用しない。
                logger.debug(
                    "State=%r が count_state=%r と不一致のため無視 (topic=%r)",
                    state,
                    self._cfg.count_state,
                    message.topic,
                )
                return

            source = msg.get("Source") or {}
            rule_name = source.get("RuleName")
            direction = self._cfg.rules.get(rule_name)

            # B. 入庫/出庫の実データを DEBUG 出力。
            if direction in ("entry", "exit"):
                event_info = {
                    "topic": message.topic,
                    "RuleName": rule_name,
                    "State": data.get("State"),  # lower 化前の元の値
                    "ObjectId": data.get("ObjectId"),
                    "Action": data.get("Action"),
                    "UtcTime": msg.get("UtcTime"),
                }
                if direction == "entry":
                    self._fire(rule_name, "入庫", self._on_entry, event_info)
                else:
                    self._fire(rule_name, "出庫", self._on_exit, event_info)
            else:
                logger.warning(
                    "未登録の RuleName %r。rules テーブルに無いため無視します (topic=%r)",
                    rule_name,
                    message.topic,
                )
        except Exception:
            # 想定外スキーマ・予期しない例外でも受信ループを止めない。
            try:
                payload_text = self._payload_text(message.payload)
            except Exception:
                payload_text = "(取得不可)"
            logger.exception(
                "MQTT メッセージ処理中に予期しない例外。topic=%r payload=%s 当該メッセージを破棄して継続します。",
                message.topic,
                payload_text,
            )

    def _fire(self, rule_name: str, label: str, callback: EventCallback, event_info: dict) -> None:
        """min_event_interval(RuleName 単位)で抑制しつつカウントコールバックを呼ぶ。ロックで保護する。"""
        with self._lock:
            now = time.monotonic()
            last = self._last_count_at.get(rule_name, 0.0)
            elapsed = now - last
            if self._cfg.min_event_interval > 0 and last > 0 and elapsed < self._cfg.min_event_interval:
                logger.info(
                    "%s(線=%r): min_event_interval(%.3fs)以内(%.3fs)のため無視。"
                    "連続通過を取りこぼす場合は値を下げる/カメラ側のパルス間隔を見直す。data=%s",
                    label,
                    rule_name,
                    self._cfg.min_event_interval,
                    elapsed,
                    event_info,
                )
                return
            self._last_count_at[rule_name] = now
            self._counted_total += 1
            logger.debug("%s検出(線=%r): カウント反映。data=%s", label, rule_name, event_info)
            try:
                callback()
            except Exception:
                logger.exception(
                    "%s コールバックで例外（カウントが反映されていない可能性, 線=%r）", label, rule_name
                )

    # ------------------------------------------------------------------
    # ハートビート（死活監視, §4.6）
    # ------------------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        interval = self._cfg.heartbeat_interval
        # Event.wait(interval) で待つことで stop() に即応する。
        while not self._hb_stop.wait(interval):
            with self._lock:
                connected = self._connected
                msgs = self._msg_total
                counted = self._counted_total
                last = self._last_message_at
            last_str = last.isoformat() if last is not None else "未受信"
            logger.info(
                "MQTT heartbeat: connected=%s, msgs=%d, counted=%d, last_msg=%s",
                connected,
                msgs,
                counted,
                last_str,
            )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._stopping = False
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._cfg.client_id,
        )
        if self._cfg.username:
            # password は username 指定時のみ設定（空 password は None 扱いにせず空文字を渡す）。
            client.username_pw_set(self._cfg.username, self._cfg.password)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.on_subscribe = self._on_subscribe

        # 切断・接続失敗時の自動再接続を指数バックオフで行う（LAN 断・broker 再起動に耐える）。
        client.reconnect_delay_set(min_delay=1, max_delay=60)

        self._client = client

        # connect_async + loop_start で「繋がるまで再試行して待つ」ノンブロッキング起動にする。
        # broker が未起動でも start() は失敗せず、起動次第 on_connect で接続成功ログが出る。
        client.connect_async(self._cfg.host, self._cfg.port, keepalive=self._cfg.keepalive)
        client.loop_start()

        # ハートビートスレッド（heartbeat_interval=0 なら起動しない＝無効）。
        if self._cfg.heartbeat_interval > 0:
            self._hb_stop.clear()
            self._hb_thread = threading.Thread(
                target=self._heartbeat_loop, name="mqtt-heartbeat", daemon=True
            )
            self._hb_thread.start()

        self._started = True
        logger.info(
            "MqttReceiver 起動: broker=%s:%d topic=%r count_state=%r rules=%d件 "
            "min_interval=%.3fs heartbeat=%.1fs（broker 未接続でも接続を待機します）",
            self._cfg.host,
            self._cfg.port,
            self._cfg.topic,
            self._cfg.count_state,
            len(self._cfg.rules),
            self._cfg.min_event_interval,
            self._cfg.heartbeat_interval,
        )
        # F. 起動時に rules マッピングを DEBUG 出力。
        logger.debug("MQTT rules マッピング(RuleName→方向): %s", self._cfg.rules)

    def stop(self) -> None:
        # 二重呼び出し・未 start() でも安全に通る。
        self._stopping = True
        # ① ハートビートスレッドへ停止フラグを立てて join。
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=5.0)
            if self._hb_thread.is_alive():
                logger.warning("MQTT ハートビートスレッドが5秒以内に停止しませんでした。")
            self._hb_thread = None

        # ② loop_stop() でネットワークスレッドを止めてから disconnect()。
        #    順序を守らないと自動再接続スレッドが残って再接続し続ける。
        if self._client is not None:
            try:
                self._client.loop_stop()
            except Exception:
                logger.exception("MQTT loop_stop() で例外。")
            try:
                self._client.disconnect()
            except Exception:
                logger.exception("MQTT disconnect() で例外。")
            self._client = None

        self._started = False
        logger.info("MqttReceiver 停止")
