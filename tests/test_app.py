from __future__ import annotations

from pathlib import Path

from parking.app import Application
from parking.models import OccupancyStatus

CONFIG_TEMPLATE = """
[parking]
total_spaces = 100

[thresholds]
crowded_at = 80
full_at    = 100

[receiver]
type = "dummy"

[storage]
state_file = "{state_file}"

[logging]
level = "INFO"
file  = "{log_file}"
"""


def write_app_config(tmp_path: Path) -> Path:
    state_file = tmp_path / "state.json"
    log_file = tmp_path / "parking.log"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        CONFIG_TEMPLATE.format(state_file=state_file, log_file=log_file),
        encoding="utf-8",
    )
    return cfg


def test_entry_exit_wiring_updates_counter_and_store(tmp_path):
    app = Application(write_app_config(tmp_path))
    app._handle_entry()
    app._handle_entry()
    app._handle_exit()
    assert app.counter.current == 1

    # store にも反映されている
    restored = app.store.restore()
    assert restored is not None
    assert restored.current_count == 1


def test_out_of_range_exit_does_not_go_negative(tmp_path):
    app = Application(write_app_config(tmp_path))
    app._handle_exit()  # 0 からの出庫は拒否
    assert app.counter.current == 0
    restored = app.store.restore()
    assert restored is not None
    assert restored.current_count == 0


def test_state_persists_across_restart(tmp_path):
    cfg = write_app_config(tmp_path)
    app1 = Application(cfg)
    for _ in range(5):
        app1._handle_entry()
    assert app1.counter.current == 5
    app1.store.close()

    # 別インスタンス = 再起動相当
    app2 = Application(cfg)
    assert app2.counter.current == 5


def test_status_reflected_in_snapshot(tmp_path):
    app = Application(write_app_config(tmp_path))
    for _ in range(80):
        app._handle_entry()
    snap = app._state_snapshot()
    assert snap["current"] == 80
    assert snap["occupancy"] == OccupancyStatus.CROWDED.value
    assert snap["total"] == 100
