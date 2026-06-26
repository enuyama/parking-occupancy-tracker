from __future__ import annotations

import dataclasses
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

    @property
    def full_at(self) -> int:
        """現在の満車閾値。実行中に set_full_at で変化しうる。"""
        return self._thresholds.full_at

    @property
    def crowded_at(self) -> int:
        """現在の混雑閾値。実行中に set_crowded_at で変化しうる。"""
        return self._thresholds.crowded_at

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

    def set_full_at(self, value: int) -> CountResult:
        """満車閾値 full_at を実行中に変更する（crowded_at は据え置き）。

        GUI からの +/- 調整用。範囲（crowded_at 以上・total 以下）を外れる
        場合は変更せず現状維持で accepted=False を返す。
        """
        if not (self._thresholds.crowded_at <= value <= self._total):
            if value < self._thresholds.crowded_at:
                reason = f"crowded_at={self._thresholds.crowded_at} を下回る"
            else:
                reason = f"total={self._total} を上回る"
            logger.warning(
                "full_at 変更を拒否: 指定値 %d が範囲外（%s）。"
                "許容範囲は crowded_at(%d) <= full_at <= total(%d)。"
                "現状（full_at=%d）を維持します。",
                value,
                reason,
                self._thresholds.crowded_at,
                self._total,
                self._thresholds.full_at,
            )
            return CountResult(
                accepted=False,
                current=self._current,
                status=self._status,
                status_changed=False,
            )
        old_full_at = self._thresholds.full_at
        self._thresholds = dataclasses.replace(self._thresholds, full_at=value)
        new_status = self._compute_status(self._current)
        changed = new_status != self._status
        logger.debug(
            "full_at 変更: %d -> %d (crowded_at=%d total=%d) "
            "current=%d status %s%s",
            old_full_at,
            value,
            self._thresholds.crowded_at,
            self._total,
            self._current,
            new_status.value,
            " [変化]" if changed else "",
        )
        self._status = new_status
        return CountResult(
            accepted=True,
            current=self._current,
            status=new_status,
            status_changed=changed,
        )

    def set_crowded_at(self, value: int) -> CountResult:
        """混雑閾値 crowded_at を実行中に変更する（full_at は据え置き）。

        GUI からの +/- 調整用。範囲（1 以上・full_at 以下）を外れる
        場合は変更せず現状維持で accepted=False を返す。
        """
        if not (1 <= value <= self._thresholds.full_at):
            if value < 1:
                reason = "下限1 を下回る"
            else:
                reason = f"full_at={self._thresholds.full_at} を上回る"
            logger.warning(
                "crowded_at 変更を拒否: 指定値 %d が範囲外（%s）。"
                "許容範囲は 1 <= crowded_at <= full_at(%d)。"
                "現状（crowded_at=%d）を維持します。",
                value,
                reason,
                self._thresholds.full_at,
                self._thresholds.crowded_at,
            )
            return CountResult(
                accepted=False,
                current=self._current,
                status=self._status,
                status_changed=False,
            )
        old_crowded_at = self._thresholds.crowded_at
        self._thresholds = dataclasses.replace(self._thresholds, crowded_at=value)
        new_status = self._compute_status(self._current)
        changed = new_status != self._status
        logger.debug(
            "crowded_at 変更: %d -> %d (full_at=%d total=%d) "
            "current=%d status %s%s",
            old_crowded_at,
            value,
            self._thresholds.full_at,
            self._total,
            self._current,
            new_status.value,
            " [変化]" if changed else "",
        )
        self._status = new_status
        return CountResult(
            accepted=True,
            current=self._current,
            status=new_status,
            status_changed=changed,
        )

    def _compute_status(self, current: int) -> OccupancyStatus:
        if current >= self._thresholds.full_at:
            return OccupancyStatus.FULL
        if current >= self._thresholds.crowded_at:
            return OccupancyStatus.CROWDED
        return OccupancyStatus.EMPTY
