from __future__ import annotations

from pathlib import Path

import pytest

from parking.config import load_config

BASE_CONFIG = """
[parking]
total_spaces = 100

[thresholds]
crowded_at = 80
full_at    = 100

[receiver]
type = "http"

[receiver.http]
host = "127.0.0.1"
port = 8080
entry_switch = 1
exit_switch  = 2
active_value = "0"
min_event_interval = 0.5

[receiver.gpio]
entry_pin    = 17
exit_pin     = 27
pull_up      = true
bounce_time  = 0.05
min_interval = 0.2

[storage]
state_file = "parking_state.json"

[logging]
level = "INFO"
file  = "parking.log"
"""


def write_config(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_valid_config(tmp_path):
    cfg = load_config(write_config(tmp_path, BASE_CONFIG))
    assert cfg.parking.total_spaces == 100
    assert cfg.thresholds.crowded_at == 80
    assert cfg.thresholds.full_at == 100
    assert cfg.receiver.type == "http"
    assert cfg.receiver.http is not None
    assert cfg.receiver.http.entry_switch == 1
    assert cfg.receiver.http.exit_switch == 2
    assert cfg.receiver.http.active_value == "0"
    assert cfg.storage.state_file == "parking_state.json"


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "does_not_exist.toml")


def test_total_spaces_must_be_positive(tmp_path):
    bad = BASE_CONFIG.replace("total_spaces = 100", "total_spaces = 0")
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, bad))


def test_crowded_must_not_exceed_full(tmp_path):
    bad = BASE_CONFIG.replace("crowded_at = 80", "crowded_at = 120")
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, bad))


def test_full_must_not_exceed_total(tmp_path):
    bad = BASE_CONFIG.replace("full_at    = 100", "full_at    = 150")
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, bad))


def test_invalid_receiver_type(tmp_path):
    bad = BASE_CONFIG.replace('type = "http"', 'type = "smoke_signals"')
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, bad))


def test_http_switch_out_of_range(tmp_path):
    bad = BASE_CONFIG.replace("entry_switch = 1", "entry_switch = 5")
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, bad))


def test_http_switches_must_differ(tmp_path):
    bad = BASE_CONFIG.replace("exit_switch  = 2", "exit_switch  = 1")
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, bad))


def test_http_active_value_must_be_0_or_1(tmp_path):
    bad = BASE_CONFIG.replace('active_value = "0"', 'active_value = "9"')
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, bad))


def test_min_event_interval_defaults_when_omitted(tmp_path):
    content = BASE_CONFIG.replace("min_event_interval = 0.5\n", "")
    cfg = load_config(write_config(tmp_path, content))
    assert cfg.receiver.http is not None
    assert cfg.receiver.http.min_event_interval == 0.5
