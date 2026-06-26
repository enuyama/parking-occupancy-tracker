from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class OccupancyStatus(str, Enum):
    FULL = "FULL"
    CROWDED = "CROWDED"
    EMPTY = "EMPTY"


@dataclass(frozen=True)
class State:
    current_count: int
    status: OccupancyStatus
    updated_at: datetime
    # GUI から実行中に調整される満車閾値。旧フォーマットの JSON には
    # キーが無いため、その場合は None（呼び出し側が config 値を採用する）。
    full_at: int | None = None
    # GUI から実行中に調整される混雑閾値。full_at と同様、旧フォーマットの
    # JSON にはキーが無いため、その場合は None（呼び出し側が config 値を採用する）。
    crowded_at: int | None = None
