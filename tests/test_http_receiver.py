from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from parking.config import HttpReceiverConfig
from parking.receivers.http import HttpReceiver


class Recorder:
    def __init__(self) -> None:
        self.entries = 0
        self.exits = 0

    def on_entry(self) -> None:
        self.entries += 1

    def on_exit(self) -> None:
        self.exits += 1


def make_client(
    *,
    entry_switch: int = 1,
    exit_switch: int = 2,
    active_value: str = "0",
    min_event_interval: float = 0.0,
    state_provider=None,
):
    rec = Recorder()
    cfg = HttpReceiverConfig(
        host="127.0.0.1",
        port=8080,
        entry_switch=entry_switch,
        exit_switch=exit_switch,
        active_value=active_value,
        min_event_interval=min_event_interval,
    )
    receiver = HttpReceiver(
        config=cfg,
        on_entry=rec.on_entry,
        on_exit=rec.on_exit,
        state_provider=state_provider,
    )
    client = TestClient(receiver.app)
    return client, rec


def control(client, alert: str):
    return client.get("/api/control", params={"alert": alert, "id": "test"})


# --- 正常系: エッジ検出 -------------------------------------------------

def test_rising_edge_triggers_entry():
    client, rec = make_client(active_value="0")
    # 初期 last は非ACTIVE("1")。SW1="0"(ACTIVE) で立ち上がり -> 入庫
    r = control(client, "01999999")
    assert r.status_code == 200
    assert r.json()["entries"] == 1
    assert rec.entries == 1
    assert rec.exits == 0


def test_falling_edge_ignored():
    client, rec = make_client(active_value="0")
    control(client, "01999999")  # 立ち上がり -> +1
    r = control(client, "11999999")  # SW1="1" 非ACTIVE = 立ち下がり
    assert r.json()["entries"] == 0
    assert rec.entries == 1  # 増えない


def test_sustained_active_not_recounted():
    client, rec = make_client(active_value="0")
    control(client, "01999999")  # 立ち上がり -> +1
    control(client, "01999999")  # ACTIVE 維持
    control(client, "01999999")  # ACTIVE 維持
    assert rec.entries == 1


def test_two_separate_pulses_count_twice():
    client, rec = make_client(active_value="0")
    control(client, "01999999")  # 立ち上がり -> +1
    control(client, "11999999")  # 戻る
    control(client, "01999999")  # 再度立ち上がり -> +1
    assert rec.entries == 2


def test_exit_via_second_switch():
    client, rec = make_client(active_value="0")
    r = control(client, "10999999")  # SW2="0"(ACTIVE) -> 出庫
    assert r.json()["exits"] == 1
    assert rec.exits == 1
    assert rec.entries == 0


def test_entry_and_exit_same_request():
    client, rec = make_client(active_value="0")
    r = control(client, "00999999")  # SW1=SW2="0" 両方立ち上がり
    body = r.json()
    assert body["entries"] == 1
    assert body["exits"] == 1
    assert rec.entries == 1
    assert rec.exits == 1


# --- "9"(状態非表示)の扱い ---------------------------------------------

def test_nine_keeps_previous_state():
    client, rec = make_client(active_value="0")
    control(client, "01999999")  # SW1 ACTIVE -> +1, last_entry="0"
    # SW1="9": 前回状態を維持（上書きしない）
    control(client, "91999999")
    # その後 SW1="0" が来ても「維持された ACTIVE」なので立ち上がりにならない
    r = control(client, "01999999")
    assert r.json()["entries"] == 0
    assert rec.entries == 1


def test_all_nines_is_noop():
    client, rec = make_client()
    r = control(client, "99999999")
    assert r.status_code == 200
    assert r.json().get("message") == "all_nines"
    assert rec.entries == 0
    assert rec.exits == 0


# --- min_event_interval ガード -----------------------------------------

def test_min_event_interval_suppresses_rapid_re_edge():
    # 大きな間隔を設定すると、立ち下がり→再立ち上がりが間隔内なら無視される
    client, rec = make_client(active_value="0", min_event_interval=100.0)
    control(client, "01999999")  # +1 (最初のエッジは通る)
    control(client, "11999999")  # 戻る
    control(client, "01999999")  # 100秒以内の再エッジ -> 抑制
    assert rec.entries == 1


def test_zero_interval_allows_back_to_back():
    client, rec = make_client(active_value="0", min_event_interval=0.0)
    control(client, "01999999")
    control(client, "11999999")
    control(client, "01999999")
    assert rec.entries == 2


# --- active_value 極性 --------------------------------------------------

def test_active_value_one_polarity():
    # 反転無し構成: "1" が ACTIVE
    client, rec = make_client(active_value="1")
    r = control(client, "10999999")  # SW1="1"(ACTIVE) -> 入庫
    assert r.json()["entries"] == 1
    assert rec.entries == 1


# --- カスタム switch 割り当て -------------------------------------------

def test_custom_switch_assignment():
    # SW3=入庫, SW4=出庫
    client, rec = make_client(entry_switch=3, exit_switch=4, active_value="0")
    control(client, "99019999")  # SW3="0" -> 入庫
    assert rec.entries == 1
    assert rec.exits == 0
    control(client, "99109999")  # SW4="0" -> 出庫
    assert rec.exits == 1


# --- バリデーション -----------------------------------------------------

def test_missing_alert_returns_400():
    client, _ = make_client()
    r = client.get("/api/control")
    assert r.status_code == 400
    assert r.json()["detail"] == "Parameter_not_found"


def test_wrong_length_returns_400():
    client, _ = make_client()
    r = control(client, "0199")
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid_parameter_length"


def test_invalid_chars_return_400():
    client, _ = make_client()
    r = control(client, "0X999999")
    assert r.status_code == 400
    assert r.json()["detail"] == "Parameter_contains_invalid_value"


# --- 補助エンドポイント -------------------------------------------------

def test_health_endpoint():
    client, _ = make_client()
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


def test_state_provider_merged_into_responses():
    snapshot = {"current": 7, "total": 100, "occupancy": "EMPTY"}
    client, _ = make_client(state_provider=lambda: dict(snapshot))
    r = control(client, "01999999")
    body = r.json()
    assert body["current"] == 7
    assert body["occupancy"] == "EMPTY"

    h = client.get("/health").json()
    assert h["current"] == 7

    s = client.get("/state").json()
    assert s == snapshot
