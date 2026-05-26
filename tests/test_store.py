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
    s.save_state(42, OccupancyStatus.CROWDED)

    restored = Store(path).restore()
    assert restored is not None
    assert restored.current_count == 42
    assert restored.status is OccupancyStatus.CROWDED
    assert restored.updated_at is not None


def test_save_overwrites_previous(tmp_path):
    path = tmp_path / "state.json"
    s = Store(path)
    s.save_state(10, OccupancyStatus.EMPTY)
    s.save_state(90, OccupancyStatus.CROWDED)
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
    Store(path).save_state(7, OccupancyStatus.FULL)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["current_count"] == 7
    assert data["status"] == "FULL"
    assert "updated_at" in data


def test_save_creates_parent_dirs(tmp_path):
    path = tmp_path / "nested" / "dir" / "state.json"
    Store(path).save_state(3, OccupancyStatus.EMPTY)
    assert path.exists()


def test_no_temp_files_left_behind(tmp_path):
    path = tmp_path / "state.json"
    s = Store(path)
    s.save_state(1, OccupancyStatus.EMPTY)
    s.save_state(2, OccupancyStatus.EMPTY)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".state-")]
    assert leftovers == []
