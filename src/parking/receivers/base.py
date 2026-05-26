from __future__ import annotations

from typing import Callable, Protocol


EventCallback = Callable[[], None]


class EventReceiver(Protocol):
    """入庫/出庫イベントの抽象受信層。

    実装は start()/stop() で起動・停止し、内部でイベント検出時に
    コンストラクタで受け取った on_entry / on_exit コールバックを呼ぶ。
    """

    def start(self) -> None: ...
    def stop(self) -> None: ...
