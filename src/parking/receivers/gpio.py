from __future__ import annotations

import logging
import threading
import time

from ..config import GpioReceiverConfig
from .base import EventCallback

logger = logging.getLogger(__name__)


class GpioReceiver:
    """gpiozero.Button による GPIO 接点入力受信層。

    フェーズ1の主実装は HttpReceiver。これは将来 LinkBase を介さず
    カメラ OC を直接 Pi GPIO に取り込む構成に戻す場合の代替実装。
    """

    def __init__(
        self,
        config: GpioReceiverConfig,
        on_entry: EventCallback,
        on_exit: EventCallback,
    ) -> None:
        self._cfg = config
        self._on_entry = on_entry
        self._on_exit = on_exit
        self._last_entry_at = 0.0
        self._last_exit_at = 0.0
        self._lock = threading.Lock()
        self._entry_btn = None
        self._exit_btn = None

    def start(self) -> None:
        # gpiozero は環境（pin factory）に応じて実機/モックで動作する。
        try:
            from gpiozero import Button  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                "gpiozero が見つかりません。実機では `pip install gpiozero` が必要です。"
                "開発機で試す場合は config の receiver.type を 'dummy' か 'http' にしてください。"
            ) from e

        try:
            self._entry_btn = Button(
                self._cfg.entry_pin,
                pull_up=self._cfg.pull_up,
                bounce_time=self._cfg.bounce_time,
            )
            self._exit_btn = Button(
                self._cfg.exit_pin,
                pull_up=self._cfg.pull_up,
                bounce_time=self._cfg.bounce_time,
            )
        except Exception as e:
            raise RuntimeError(
                f"GPIO ピンの初期化に失敗 (entry_pin={self._cfg.entry_pin}, "
                f"exit_pin={self._cfg.exit_pin}): {e}。ピン番号の競合・権限・実機環境を確認してください。"
            ) from e

        self._entry_btn.when_pressed = self._handle_entry
        self._exit_btn.when_pressed = self._handle_exit
        logger.info(
            "GpioReceiver 起動: entry_pin=%d exit_pin=%d pull_up=%s bounce_time=%.3fs",
            self._cfg.entry_pin,
            self._cfg.exit_pin,
            self._cfg.pull_up,
            self._cfg.bounce_time,
        )

    def _handle_entry(self) -> None:
        with self._lock:
            now = time.monotonic()
            if (now - self._last_entry_at) < self._cfg.min_interval:
                return
            self._last_entry_at = now
        try:
            self._on_entry()
        except Exception:
            logger.exception("on_entry コールバックで例外")

    def _handle_exit(self) -> None:
        with self._lock:
            now = time.monotonic()
            if (now - self._last_exit_at) < self._cfg.min_interval:
                return
            self._last_exit_at = now
        try:
            self._on_exit()
        except Exception:
            logger.exception("on_exit コールバックで例外")

    def stop(self) -> None:
        for btn in (self._entry_btn, self._exit_btn):
            if btn is not None:
                try:
                    btn.close()
                except Exception:
                    logger.exception("Button close 失敗")
        logger.info("GpioReceiver 停止")
