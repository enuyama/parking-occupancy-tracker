from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import ThresholdsConfig
from .models import OccupancyStatus

logger = logging.getLogger(__name__)


@dataclass
class CountResult:
    accepted: bool
    current: int
    status: OccupancyStatus
    status_changed: bool


class OccupancyCounter:
    """現在台数の保持・増減・満空混判定を行う純ロジック（I/O なし）"""

    def __init__(
        self,
        total_spaces: int,
        thresholds: ThresholdsConfig,
        initial_count: int = 0,
    ) -> None:
        if total_spaces <= 0:
            raise ValueError("total_spaces > 0")
        self._total = total_spaces
        self._thresholds = thresholds
        self._current = max(0, min(initial_count, total_spaces))
        self._status = self._compute_status(self._current)

    @property
    def total_spaces(self) -> int:
        return self._total

    @property
    def current(self) -> int:
        return self._current

    @property
    def status(self) -> OccupancyStatus:
        return self._status

    def record_entry(self) -> CountResult:
        return self._apply(+1)

    def record_exit(self) -> CountResult:
        return self._apply(-1)

    def _apply(self, delta: int) -> CountResult:
        new = self._current + delta
        direction = "入庫" if delta > 0 else "出庫"
        if new < 0 or new > self._total:
            limit = "下限0" if new < 0 else f"上限{self._total}"
            logger.warning(
                "範囲外イベントを無視: %s を試みたが %s を超える "
                "(current=%d delta=%+d total=%d)。"
                "カメラ/配線の誤検知か、起動時の初期台数ズレの可能性。",
                direction,
                limit,
                self._current,
                delta,
                self._total,
            )
            return CountResult(
                accepted=False,
                current=self._current,
                status=self._status,
                status_changed=False,
            )
        new_status = self._compute_status(new)
        changed = new_status != self._status
        logger.debug(
            "カウント更新: %s current %d -> %d (total=%d) status %s%s",
            direction,
            self._current,
            new,
            self._total,
            new_status.value,
            " [変化]" if changed else "",
        )
        self._current = new
        self._status = new_status
        return CountResult(
            accepted=True,
            current=new,
            status=new_status,
            status_changed=changed,
        )

    def _compute_status(self, current: int) -> OccupancyStatus:
        if current >= self._thresholds.full_at:
            return OccupancyStatus.FULL
        if current >= self._thresholds.crowded_at:
            return OccupancyStatus.CROWDED
        return OccupancyStatus.EMPTY
