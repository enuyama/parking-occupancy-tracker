from __future__ import annotations

import pytest

from parking.config import ThresholdsConfig
from parking.counter import OccupancyCounter
from parking.models import OccupancyStatus


def make_counter(total: int = 100, crowded: int = 80, full: int = 100, initial: int = 0):
    return OccupancyCounter(
        total_spaces=total,
        thresholds=ThresholdsConfig(crowded_at=crowded, full_at=full),
        initial_count=initial,
    )


def test_initial_state_empty():
    c = make_counter()
    assert c.current == 0
    assert c.status is OccupancyStatus.EMPTY


def test_entry_increments():
    c = make_counter()
    r = c.record_entry()
    assert r.accepted is True
    assert r.current == 1
    assert c.current == 1


def test_exit_decrements():
    c = make_counter(initial=5)
    r = c.record_exit()
    assert r.accepted is True
    assert r.current == 4
    assert c.current == 4


def test_exit_clamped_at_zero():
    c = make_counter(initial=0)
    r = c.record_exit()
    assert r.accepted is False
    assert r.current == 0
    assert c.current == 0


def test_entry_clamped_at_total():
    c = make_counter(total=3, crowded=2, full=3, initial=3)
    r = c.record_entry()
    assert r.accepted is False
    assert r.current == 3
    assert c.current == 3


def test_initial_count_clamped_into_range():
    over = make_counter(total=10, crowded=8, full=10, initial=999)
    assert over.current == 10
    under = make_counter(total=10, crowded=8, full=10, initial=-5)
    assert under.current == 0


@pytest.mark.parametrize(
    "current,expected",
    [
        (0, OccupancyStatus.EMPTY),
        (1, OccupancyStatus.EMPTY),
        (79, OccupancyStatus.EMPTY),
        (80, OccupancyStatus.CROWDED),
        (99, OccupancyStatus.CROWDED),
        (100, OccupancyStatus.FULL),
    ],
)
def test_status_thresholds_absolute(current, expected):
    c = make_counter(total=100, crowded=80, full=100, initial=current)
    assert c.status is expected


def test_status_changed_flag_on_boundary_cross():
    # 79 -> 80 で EMPTY -> CROWDED に変化
    c = make_counter(total=100, crowded=80, full=100, initial=79)
    r = c.record_entry()
    assert r.status is OccupancyStatus.CROWDED
    assert r.status_changed is True


def test_status_unchanged_within_band():
    c = make_counter(total=100, crowded=80, full=100, initial=10)
    r = c.record_entry()  # 10 -> 11, まだ EMPTY
    assert r.status is OccupancyStatus.EMPTY
    assert r.status_changed is False


def test_rejected_event_does_not_change_status_flag():
    c = make_counter(initial=0)
    r = c.record_exit()  # 範囲外
    assert r.accepted is False
    assert r.status_changed is False


def test_crowded_equals_full_threshold():
    # crowded_at == full_at の構成（混を飛ばして満になる）
    c = make_counter(total=10, crowded=10, full=10, initial=9)
    assert c.status is OccupancyStatus.EMPTY
    r = c.record_entry()
    assert r.current == 10
    assert r.status is OccupancyStatus.FULL


def test_invalid_total_raises():
    with pytest.raises(ValueError):
        OccupancyCounter(0, ThresholdsConfig(crowded_at=1, full_at=1))


def test_set_full_at_rejected_below_crowded_at():
    c = make_counter(total=100, crowded=80, full=100, initial=90)
    before_status = c.status
    r = c.set_full_at(79)  # crowded_at(80) 未満なので拒否
    assert r.accepted is False
    assert r.status_changed is False
    assert r.current == 90
    assert c.status is before_status
    # 拒否後も再計算が以前の full_at(100) で行われる: 90 は CROWDED のまま
    assert c.status is OccupancyStatus.CROWDED


def test_set_full_at_rejected_above_total():
    c = make_counter(total=100, crowded=80, full=100, initial=50)
    before_status = c.status
    r = c.set_full_at(101)  # total(100) 超過なので拒否
    assert r.accepted is False
    assert r.status_changed is False
    assert c.status is before_status


def test_set_full_at_accepted_at_boundaries():
    # 境界値 crowded_at と total は許容される
    c = make_counter(total=100, crowded=80, full=100, initial=0)
    assert c.set_full_at(80).accepted is True
    assert c.set_full_at(100).accepted is True


def test_set_full_at_lowering_makes_full():
    # current=85, full_at を 100 -> 85 に下げると CROWDED -> FULL に変化
    c = make_counter(total=100, crowded=80, full=100, initial=85)
    assert c.status is OccupancyStatus.CROWDED
    r = c.set_full_at(85)
    assert r.accepted is True
    assert r.status is OccupancyStatus.FULL
    assert r.status_changed is True
    assert r.current == 85  # current は変えない
    assert c.current == 85


def test_set_full_at_raising_drops_full_to_crowded():
    # current=90, full_at=90(FULL) を 100 に上げると FULL -> CROWDED に変化
    c = make_counter(total=100, crowded=80, full=90, initial=90)
    assert c.status is OccupancyStatus.FULL
    r = c.set_full_at(100)
    assert r.accepted is True
    assert r.status is OccupancyStatus.CROWDED
    assert r.status_changed is True
    assert c.current == 90


def test_set_full_at_no_status_change_when_within_band():
    # current=50（EMPTY）。full_at をどう動かしても EMPTY のまま
    c = make_counter(total=100, crowded=80, full=100, initial=50)
    r = c.set_full_at(90)
    assert r.accepted is True
    assert r.status is OccupancyStatus.EMPTY
    assert r.status_changed is False


def test_set_crowded_at_rejected_below_one():
    c = make_counter(total=100, crowded=80, full=100, initial=70)
    before_status = c.status
    r = c.set_crowded_at(0)  # 下限1 未満なので拒否
    assert r.accepted is False
    assert r.status_changed is False
    assert r.current == 70
    assert c.status is before_status
    assert c.crowded_at == 80  # 変更されない


def test_set_crowded_at_rejected_above_full_at():
    c = make_counter(total=100, crowded=80, full=100, initial=50)
    before_status = c.status
    r = c.set_crowded_at(101)  # full_at(100) 超過なので拒否
    assert r.accepted is False
    assert r.status_changed is False
    assert c.status is before_status
    assert c.crowded_at == 80  # 変更されない


def test_set_crowded_at_accepted_at_boundaries():
    # 境界値 1 と full_at は許容される
    c = make_counter(total=100, crowded=80, full=100, initial=0)
    assert c.set_crowded_at(1).accepted is True
    assert c.set_crowded_at(100).accepted is True


def test_set_crowded_at_lowering_makes_crowded():
    # current=70（EMPTY）。crowded_at を 80 -> 70 に下げると EMPTY -> CROWDED に変化
    c = make_counter(total=100, crowded=80, full=100, initial=70)
    assert c.status is OccupancyStatus.EMPTY
    r = c.set_crowded_at(70)
    assert r.accepted is True
    assert r.status is OccupancyStatus.CROWDED
    assert r.status_changed is True
    assert r.current == 70  # current は変えない
    assert c.current == 70


def test_set_crowded_at_raising_drops_crowded_to_empty():
    # current=80（CROWDED）。crowded_at を 80 -> 90 に上げると CROWDED -> EMPTY に変化
    c = make_counter(total=100, crowded=80, full=100, initial=80)
    assert c.status is OccupancyStatus.CROWDED
    r = c.set_crowded_at(90)
    assert r.accepted is True
    assert r.status is OccupancyStatus.EMPTY
    assert r.status_changed is True
    assert c.current == 80


def test_set_crowded_at_no_status_change_when_full():
    # current=100（FULL）。crowded_at をどう動かしても FULL のまま
    c = make_counter(total=100, crowded=80, full=100, initial=100)
    r = c.set_crowded_at(50)
    assert r.accepted is True
    assert r.status is OccupancyStatus.FULL
    assert r.status_changed is False
