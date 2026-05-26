from __future__ import annotations

import logging
import sys
import threading

from .base import EventCallback

logger = logging.getLogger(__name__)


class DummyReceiver:
    """stdin から i (entry) / o (exit) を1文字ずつ読んでイベント発火する。

    開発・E2E 動作確認用。
    """

    def __init__(self, on_entry: EventCallback, on_exit: EventCallback) -> None:
        self._on_entry = on_entry
        self._on_exit = on_exit
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        logger.info("DummyReceiver 起動: stdin に i=入庫 / o=出庫 / q=終了 を入力")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        for line in sys.stdin:
            if self._stop.is_set():
                break
            ch = line.strip().lower()
            if ch in ("i", "in", "entry"):
                self._on_entry()
            elif ch in ("o", "out", "exit"):
                self._on_exit()
            elif ch in ("q", "quit", "exit!"):
                logger.info("DummyReceiver: 終了入力")
                break
            else:
                logger.warning("DummyReceiver: 不明な入力 %r", ch)

    def stop(self) -> None:
        self._stop.set()
