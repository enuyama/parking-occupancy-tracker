from __future__ import annotations

try:
    import tomllib  # Python 3.11+ 標準
except ModuleNotFoundError:  # Python 3.10 以下では tomli バックポートを使う
    import tomli as tomllib  # type: ignore[no-redef]
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """config.toml の内容・構文が不正なときに送出する。

    メッセージには「どの項目が・どういう値で・なぜ駄目か」を必ず含める。
    """


@dataclass(frozen=True)
class ParkingConfig:
    total_spaces: int


@dataclass(frozen=True)
class ThresholdsConfig:
    # 現在台数の絶対値で判定する。
    # current >= full_at    -> FULL
    # current >= crowded_at -> CROWDED
    # それ以外               -> EMPTY
    crowded_at: int
    full_at: int


@dataclass(frozen=True)
class HttpReceiverConfig:
    host: str
    port: int
    entry_switch: int
    exit_switch: int
    active_value: str
    min_event_interval: float


@dataclass(frozen=True)
class GpioReceiverConfig:
    entry_pin: int
    exit_pin: int
    pull_up: bool
    bounce_time: float
    min_interval: float


@dataclass(frozen=True)
class MqttReceiverConfig:
    host: str
    port: int
    username: str
    password: str
    client_id: str
    keepalive: int
    topic: str
    count_state: str
    min_event_interval: float
    heartbeat_interval: float
    rules: dict[str, str]


@dataclass(frozen=True)
class ReceiverConfig:
    type: Literal["http", "gpio", "dummy", "mqtt"]
    http: HttpReceiverConfig | None
    gpio: GpioReceiverConfig | None
    mqtt: MqttReceiverConfig | None


@dataclass(frozen=True)
class StorageConfig:
    state_file: str


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    file: str


@dataclass(frozen=True)
class Config:
    parking: ParkingConfig
    thresholds: ThresholdsConfig
    receiver: ReceiverConfig
    storage: StorageConfig
    logging: LoggingConfig

    def summary(self) -> str:
        """起動ログに出す1行サマリ。設定の取り違えを早期発見するため。"""
        r = self.receiver
        if r.type == "http" and r.http is not None:
            rcv = (
                f"http(host={r.http.host}:{r.http.port}, "
                f"entry=SW{r.http.entry_switch}, exit=SW{r.http.exit_switch}, "
                f"active={r.http.active_value!r}, "
                f"min_interval={r.http.min_event_interval}s)"
            )
        elif r.type == "gpio" and r.gpio is not None:
            rcv = (
                f"gpio(entry_pin={r.gpio.entry_pin}, exit_pin={r.gpio.exit_pin}, "
                f"pull_up={r.gpio.pull_up})"
            )
        elif r.type == "mqtt" and r.mqtt is not None:
            # password は絶対に出さない。
            rcv = (
                f"mqtt(host={r.mqtt.host}:{r.mqtt.port}, "
                f"client_id={r.mqtt.client_id!r}, topic={r.mqtt.topic!r}, "
                f"count_state={r.mqtt.count_state!r}, rules={len(r.mqtt.rules)}件, "
                f"min_interval={r.mqtt.min_event_interval}s, "
                f"heartbeat={r.mqtt.heartbeat_interval}s)"
            )
        else:
            rcv = r.type
        return (
            f"total_spaces={self.parking.total_spaces}, "
            f"crowded_at={self.thresholds.crowded_at}, full_at={self.thresholds.full_at}, "
            f"receiver={rcv}, state_file={self.storage.state_file}, "
            f"log_level={self.logging.level}, log_file={self.logging.file}"
        )


# --- 取り出しヘルパ（項目名つきの明確なエラーを出す） --------------------


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    if name not in raw:
        raise ConfigError(f"[{name}] セクションがありません。config.example.toml を参照してください。")
    val = raw[name]
    if not isinstance(val, dict):
        raise ConfigError(f"[{name}] はテーブル（セクション）である必要があります（実際: {type(val).__name__}）。")
    return val


def _require(d: dict[str, Any], section: str, key: str) -> Any:
    if key not in d:
        raise ConfigError(f"{section}.{key} がありません。config.example.toml を参照してください。")
    return d[key]


def _as_int(d: dict[str, Any], section: str, key: str, default: int | None = None) -> int:
    if key not in d:
        if default is not None:
            return default
        raise ConfigError(f"{section}.{key} がありません。config.example.toml を参照してください。")
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise ConfigError(f"{section}.{key} は整数で指定してください（実際: {v!r}）。")
    return v


def _as_float(d: dict[str, Any], section: str, key: str, default: float | None = None) -> float:
    if key not in d:
        if default is not None:
            return default
        raise ConfigError(f"{section}.{key} がありません。")
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ConfigError(f"{section}.{key} は数値で指定してください（実際: {v!r}）。")
    return float(v)


def _as_str(d: dict[str, Any], section: str, key: str, default: str | None = None) -> str:
    if key not in d:
        if default is not None:
            return default
        raise ConfigError(f"{section}.{key} がありません。config.example.toml を参照してください。")
    v = d[key]
    if not isinstance(v, str):
        raise ConfigError(f"{section}.{key} は文字列で指定してください（実際: {v!r}）。")
    return v


def _as_bool(d: dict[str, Any], section: str, key: str) -> bool:
    v = _require(d, section, key)
    if not isinstance(v, bool):
        raise ConfigError(f"{section}.{key} は true/false で指定してください（実際: {v!r}）。")
    return v


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} が見つかりません。config.example.toml をコピーして作成してください。"
        )

    try:
        with path.open("rb") as f:
            raw: dict[str, Any] = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} の TOML 構文エラー: {e}") from e
    except OSError as e:
        raise ConfigError(f"{path} を読み込めません: {e}") from e

    # [parking]
    parking_raw = _section(raw, "parking")
    total_spaces = _as_int(parking_raw, "parking", "total_spaces")
    if total_spaces <= 0:
        raise ConfigError(f"parking.total_spaces は 1 以上にしてください（実際: {total_spaces}）。")
    parking = ParkingConfig(total_spaces=total_spaces)

    # [thresholds]
    th_raw = _section(raw, "thresholds")
    crowded_at = _as_int(th_raw, "thresholds", "crowded_at")
    full_at = _as_int(th_raw, "thresholds", "full_at")
    if not (0 < crowded_at <= full_at <= total_spaces):
        raise ConfigError(
            "thresholds は 0 < crowded_at <= full_at <= parking.total_spaces を満たす必要があります"
            f"（実際: crowded_at={crowded_at}, full_at={full_at}, total_spaces={total_spaces}）。"
        )
    thresholds = ThresholdsConfig(crowded_at=crowded_at, full_at=full_at)

    # [receiver]
    rcv_raw = _section(raw, "receiver")
    rtype = _as_str(rcv_raw, "receiver", "type")
    if rtype not in ("http", "gpio", "dummy", "mqtt"):
        raise ConfigError(
            f'receiver.type は "http" / "gpio" / "dummy" / "mqtt" のいずれか（実際: {rtype!r}）。'
        )

    http_cfg: HttpReceiverConfig | None = None
    if "http" in rcv_raw:
        h = rcv_raw["http"]
        if not isinstance(h, dict):
            raise ConfigError("[receiver.http] はテーブルである必要があります。")
        entry_switch = _as_int(h, "receiver.http", "entry_switch")
        exit_switch = _as_int(h, "receiver.http", "exit_switch")
        active_value = _as_str(h, "receiver.http", "active_value")
        http_cfg = HttpReceiverConfig(
            host=_as_str(h, "receiver.http", "host"),
            port=_as_int(h, "receiver.http", "port"),
            entry_switch=entry_switch,
            exit_switch=exit_switch,
            active_value=active_value,
            min_event_interval=_as_float(h, "receiver.http", "min_event_interval", default=0.5),
        )
        if entry_switch not in (1, 2, 3, 4):
            raise ConfigError(f"receiver.http.entry_switch は 1..4（実際: {entry_switch}）。")
        if exit_switch not in (1, 2, 3, 4):
            raise ConfigError(f"receiver.http.exit_switch は 1..4（実際: {exit_switch}）。")
        if entry_switch == exit_switch:
            raise ConfigError(
                f"receiver.http.entry_switch と exit_switch は別の値にしてください（両方 {entry_switch}）。"
            )
        if active_value not in ("0", "1"):
            raise ConfigError(f'receiver.http.active_value は "0" または "1"（実際: {active_value!r}）。')
        if http_cfg.min_event_interval < 0:
            raise ConfigError(
                f"receiver.http.min_event_interval は 0 以上（実際: {http_cfg.min_event_interval}）。"
            )

    gpio_cfg: GpioReceiverConfig | None = None
    if "gpio" in rcv_raw:
        g = rcv_raw["gpio"]
        if not isinstance(g, dict):
            raise ConfigError("[receiver.gpio] はテーブルである必要があります。")
        gpio_cfg = GpioReceiverConfig(
            entry_pin=_as_int(g, "receiver.gpio", "entry_pin"),
            exit_pin=_as_int(g, "receiver.gpio", "exit_pin"),
            pull_up=_as_bool(g, "receiver.gpio", "pull_up"),
            bounce_time=_as_float(g, "receiver.gpio", "bounce_time"),
            min_interval=_as_float(g, "receiver.gpio", "min_interval"),
        )

    mqtt_cfg: MqttReceiverConfig | None = None
    if "mqtt" in rcv_raw:
        m = rcv_raw["mqtt"]
        if not isinstance(m, dict):
            raise ConfigError("[receiver.mqtt] はテーブルである必要があります。")

        # [receiver.mqtt.rules] テーブル（RuleName → "entry"/"exit"）。
        rules_raw = m.get("rules", {})
        if not isinstance(rules_raw, dict):
            raise ConfigError("[receiver.mqtt.rules] はテーブルである必要があります。")
        rules: dict[str, str] = {}
        for rule_name, direction in rules_raw.items():
            if not isinstance(direction, str) or direction not in ("entry", "exit"):
                raise ConfigError(
                    f"receiver.mqtt.rules.{rule_name} は \"entry\" または \"exit\" のいずれか"
                    f"（実際: {direction!r}）。"
                )
            rules[rule_name] = direction
        if not rules:
            logger.warning(
                "receiver.mqtt.rules が空です。カウント対象の仮想線が無いため、"
                "受信してもカウントされません（起動は継続）。"
            )

        # count_state はパース時に .lower() 正規化して保持（on_message 側の state.lower() と確実に一致させる）。
        count_state = _as_str(m, "receiver.mqtt", "count_state", default="true").lower()
        if not count_state:
            raise ConfigError("receiver.mqtt.count_state は空文字にできません（既定 \"true\"）。")

        topic = _as_str(m, "receiver.mqtt", "topic", default="+/onvif-ej/OpenApp/WiseAI/LineCrossing/#")
        if not topic:
            raise ConfigError("receiver.mqtt.topic は空文字にできません。")

        username = _as_str(m, "receiver.mqtt", "username", default="")
        password = _as_str(m, "receiver.mqtt", "password", default="")
        if not username and password:
            logger.warning(
                "receiver.mqtt.username が空ですが password が指定されています。"
                "設定ミスの可能性があります（認証なしなら両方空に）。"
            )

        mqtt_cfg = MqttReceiverConfig(
            host=_as_str(m, "receiver.mqtt", "host", default="localhost"),
            port=_as_int(m, "receiver.mqtt", "port", default=1883),
            username=username,
            password=password,
            client_id=_as_str(m, "receiver.mqtt", "client_id", default="parking-tracker"),
            keepalive=_as_int(m, "receiver.mqtt", "keepalive", default=60),
            topic=topic,
            count_state=count_state,
            min_event_interval=_as_float(m, "receiver.mqtt", "min_event_interval", default=0.0),
            heartbeat_interval=_as_float(m, "receiver.mqtt", "heartbeat_interval", default=60.0),
            rules=rules,
        )
        if not (1 <= mqtt_cfg.port <= 65535):
            raise ConfigError(f"receiver.mqtt.port は 1..65535（実際: {mqtt_cfg.port}）。")
        if mqtt_cfg.min_event_interval < 0:
            raise ConfigError(
                f"receiver.mqtt.min_event_interval は 0 以上（実際: {mqtt_cfg.min_event_interval}）。"
            )
        if mqtt_cfg.heartbeat_interval < 0:
            raise ConfigError(
                f"receiver.mqtt.heartbeat_interval は 0 以上（実際: {mqtt_cfg.heartbeat_interval}）。"
            )

    # 選択した受信層に対応するセクションが存在するか
    if rtype == "http" and http_cfg is None:
        raise ConfigError('receiver.type="http" ですが [receiver.http] セクションがありません。')
    if rtype == "gpio" and gpio_cfg is None:
        raise ConfigError('receiver.type="gpio" ですが [receiver.gpio] セクションがありません。')
    if rtype == "mqtt" and mqtt_cfg is None:
        raise ConfigError('receiver.type="mqtt" ですが [receiver.mqtt] セクションがありません。')

    receiver = ReceiverConfig(type=rtype, http=http_cfg, gpio=gpio_cfg, mqtt=mqtt_cfg)

    # [storage]
    storage_raw = _section(raw, "storage")
    storage = StorageConfig(state_file=_as_str(storage_raw, "storage", "state_file"))

    # [logging]
    log_raw = _section(raw, "logging")
    level = _as_str(log_raw, "logging", "level").upper()
    valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
    if level not in valid_levels:
        raise ConfigError(f"logging.level は {valid_levels} のいずれか（実際: {level!r}）。")
    logging_cfg = LoggingConfig(level=level, file=_as_str(log_raw, "logging", "file"))

    return Config(
        parking=parking,
        thresholds=thresholds,
        receiver=receiver,
        storage=storage,
        logging=logging_cfg,
    )
