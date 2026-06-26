from __future__ import annotations

import json

from parking.models import OccupancyStatus
from parking.store import Store


def test_restore_none_when_file_absent(tmp_path):
    s = Store(tmp_path / "state.json")
    assert s.restore() is None


def test_save_then_restore_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    s = Store(path)
    s.save_state(42, OccupancyStatus.CROWDED, 100, 80)

    restored = Store(path).restore()
    assert restored is not None
    assert restored.current_count == 42
    assert restored.status is OccupancyStatus.CROWDED
    assert restored.updated_at is not None


def test_save_then_restore_full_at_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    Store(path).save_state(50, OccupancyStatus.CROWDED, 88, 70)

    restored = Store(path).restore()
    assert restored is not None
    assert restored.full_at == 88


def test_save_then_restore_crowded_at_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    Store(path).save_state(50, OccupancyStatus.CROWDED, 88, 70)

    restored = Store(path).restore()
    assert restored is not None
    assert restored.crowded_at == 70


def test_restore_full_at_none_when_key_absent(tmp_path):
    # full_at キーが無い旧フォーマット JSON は後方互換で復元でき、
    # full_at は None になる。
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "current_count": 12,
                "status": "CROWDED",
                "updated_at": "2026-05-18T07:34:21.123456+00:00",
            }
        ),
        encoding="utf-8",
    )
    restored = Store(path).restore()
    assert restored is not None
    assert restored.current_count == 12
    assert restored.status is OccupancyStatus.CROWDED
    assert restored.full_at is None


def test_restore_crowded_at_none_when_key_absent(tmp_path):
    # crowded_at キーが無い旧フォーマット JSON は後方互換で復元でき、
    # crowded_at は None になる。
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "current_count": 12,
                "status": "CROWDED",
                "updated_at": "2026-05-18T07:34:21.123456+00:00",
            }
        ),
        encoding="utf-8",
    )
    restored = Store(path).restore()
    assert restored is not None
    assert restored.current_count == 12
    assert restored.status is OccupancyStatus.CROWDED
    assert restored.crowded_at is None


def test_restore_returns_none_when_crowded_at_invalid_type(tmp_path):
    # crowded_at が int 化できない不正な型なら、既存の不正データ扱い（None 返し）。
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "current_count": 12,
                "status": "CROWDED",
                "updated_at": "2026-05-18T07:34:21.123456+00:00",
                "crowded_at": "abc",
            }
        ),
        encoding="utf-8",
    )
    assert Store(path).restore() is None


def test_restore_returns_none_when_full_at_invalid_type(tmp_path):
    # full_at が int 化できない不正な型なら、既存の不正データ扱い（None 返し）。
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "current_count": 12,
                "status": "CROWDED",
                "updated_at": "2026-05-18T07:34:21.123456+00:00",
                "full_at": "abc",
            }
        ),
        encoding="utf-8",
    )
    assert Store(path).restore() is None


def test_save_overwrites_previous(tmp_path):
    path = tmp_path / "state.json"
    s = Store(path)
    s.save_state(10, OccupancyStatus.EMPTY, 100, 80)
    s.save_state(90, OccupancyStatus.CROWDED, 100, 80)
    restored = s.restore()
    assert restored is not None
    assert restored.current_count == 90
    assert restored.status is OccupancyStatus.CROWDED


def test_corrupted_json_returns_none(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not valid json ", encoding="utf-8")
    assert Store(path).restore() is None


def test_missing_key_returns_none(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"current_count": 5}), encoding="utf-8")
    assert Store(path).restore() is None


def test_written_file_is_valid_json_with_expected_keys(tmp_path):
    path = tmp_path / "state.json"
    Store(path).save_state(7, OccupancyStatus.FULL, 100, 80)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["current_count"] == 7
    assert data["status"] == "FULL"
    assert "updated_at" in data
    assert data["full_at"] == 100
    assert data["crowded_at"] == 80


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    Store(path).save_state(3, OccupancyStatus.EMPTY, 100, 80)
    assert path.exists()


def test_no_temp_files_left_behind(tmp_path):
    path = tmp_path / "state.json"
    s = Store(path)
    s.save_state(1, OccupancyStatus.EMPTY, 100, 80)
    s.save_state(2, OccupancyStatus.EMPTY, 100, 80)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".state-")]
    assert leftovers == []
