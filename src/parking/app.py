from __future__ import annotations

import dataclasses
import logging
import logging.handlers
import signal
import sys
import threading
from pathlib import Path

from .config import Config, ConfigError, load_config
from .counter import CountResult, OccupancyCounter
from .receivers.base import EventReceiver
from .store import Store

logger = logging.getLogger(__name__)


def _setup_logging(level: str, file_path: str) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 二重起動（テスト等）でハンドラが重複しないよう、既存を一旦クリア。
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    try:
        fh = logging.handlers.RotatingFileHandler(
            file_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        # ファイルに書けなくても標準出力ログだけで動作は継続する。
        root.warning("ログファイル %s を開けません: %s。標準出力のみに出力します。", file_path, e)

    # uvicorn のログも root 経由で拾えるようにレベルだけ整える。
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _build_receiver(
    cfg: Config,
    on_entry,
    on_exit,
    state_provider,
) -> EventReceiver:
    rtype = cfg.receiver.type
    if rtype == "http":
        if cfg.receiver.http is None:
            raise ValueError("receiver.type=http だが [receiver.http] が無い")
        from .receivers.http import HttpReceiver

        return HttpReceiver(
            config=cfg.receiver.http,
            on_entry=on_entry,
            on_exit=on_exit,
            state_provider=state_provider,
        )
    if rtype == "gpio":
        if cfg.receiver.gpio is None:
            raise ValueError("receiver.type=gpio だが [receiver.gpio] が無い")
        from .receivers.gpio import GpioReceiver

        return GpioReceiver(
            config=cfg.receiver.gpio,
            on_entry=on_entry,
            on_exit=on_exit,
        )
    if rtype == "mqtt":
        if cfg.receiver.mqtt is None:
            raise ValueError("receiver.type=mqtt だが [receiver.mqtt] が無い")
        from .receivers.mqtt import MqttReceiver

        return MqttReceiver(
            config=cfg.receiver.mqtt,
            on_entry=on_entry,
            on_exit=on_exit,
        )
    if rtype == "dummy":
        from .receivers.dummy import DummyReceiver

        return DummyReceiver(on_entry=on_entry, on_exit=on_exit)
    raise ValueError(f"未対応の receiver.type: {rtype}")


class Application:
    def __init__(self, config_path: str | Path) -> None:
        self.cfg = load_config(config_path)
        _setup_logging(self.cfg.logging.level, self.cfg.logging.file)
        logger.info("設定を読み込みました (%s): %s", config_path, self.cfg.summary())

        self.store = Store(self.cfg.storage.state_file)
        restored = self.store.restore()
        initial_count = restored.current_count if restored is not None else 0
        if restored is not None:
            logger.info(
                "状態を復元: current=%d status=%s updated_at=%s",
                restored.current_count,
                restored.status.value,
                restored.updated_at.isoformat(),
            )

        # GUI で調整した閾値(full_at / crowded_at)を永続化から復元する。
        # 旧フォーマット（キー無し=None）は config 値で補完し、組として
        # 1 <= crowded_at <= full_at <= total を満たす場合のみ採用する。
        # 不整合・範囲外なら config 既定の閾値にフォールバック（組単位）。
        thresholds = self.cfg.thresholds
        if restored is not None and (restored.full_at is not None or restored.crowded_at is not None):
            total = self.cfg.parking.total_spaces
            cand_full = restored.full_at if restored.full_at is not None else thresholds.full_at
            cand_crowded = restored.crowded_at if restored.crowded_at is not None else thresholds.crowded_at
            if 1 <= cand_crowded <= cand_full <= total:
                thresholds = dataclasses.replace(thresholds, full_at=cand_full, crowded_at=cand_crowded)
                if (cand_full, cand_crowded) != (self.cfg.thresholds.full_at, self.cfg.thresholds.crowded_at):
                    logger.info(
                        "閾値を復元: crowded_at=%d full_at=%d（config 既定 crowded=%d full=%d を上書き）",
                        cand_crowded,
                        cand_full,
                        self.cfg.thresholds.crowded_at,
                        self.cfg.thresholds.full_at,
                    )
            else:
                logger.warning(
                    "復元した閾値が不整合（crowded_at=%d, full_at=%d, total=%d）。"
                    "1 <= crowded_at <= full_at <= total を満たさないため、"
                    "config 既定（crowded=%d full=%d）を使用します。",
                    cand_crowded,
                    cand_full,
                    total,
                    thresholds.crowded_at,
                    thresholds.full_at,
                )

        self.counter = OccupancyCounter(
            total_spaces=self.cfg.parking.total_spaces,
            thresholds=thresholds,
            initial_count=initial_count,
        )
        if restored is not None and restored.current_count != self.counter.current:
            logger.warning(
                "復元した台数 %d が総台数 %d の範囲外だったため %d に補正しました。",
                restored.current_count,
                self.counter.total_spaces,
                self.counter.current,
            )

        # 復元直後の状態を1度保存（初回起動時に state ファイルを確実に作る）
        try:
            self.store.save_state(
                self.counter.current, self.counter.status, self.counter.full_at, self.counter.crowded_at
            )
        except OSError:
            logger.warning("初期状態の保存に失敗しました（メモリ上では継続します）。")

        self._counter_lock = threading.Lock()
        self.receiver = _build_receiver(
            self.cfg,
            on_entry=self._handle_entry,
            on_exit=self._handle_exit,
            state_provider=self._state_snapshot,
        )
        self._stop_event = threading.Event()

    # ----- callbacks ----------------------------------------------------
    def _handle_entry(self) -> None:
        self._apply_event("入庫", self.counter.record_entry)

    def _handle_exit(self) -> None:
        self._apply_event("出庫", self.counter.record_exit)

    def _apply_event(self, label: str, record) -> None:
        with self._counter_lock:
            prev_status = self.counter.status
            result = record()
            if not result.accepted:
                # 範囲外（counter 側で WARNING 済み）。台数は変えない。
                logger.debug("%s イベントは範囲外のため却下されました (current=%d)", label, self.counter.current)
                return
            logger.info(
                "%s検出: current=%d/%d status=%s",
                label,
                result.current,
                self.counter.total_spaces,
                result.status.value,
            )
            if result.status_changed:
                logger.info(
                    "ステータス変化: %s -> %s（現在 %d台）",
                    prev_status.value,
                    result.status.value,
                    result.current,
                )
            try:
                self.store.save_state(
                    result.current, result.status, self.counter.full_at, self.counter.crowded_at
                )
            except OSError:
                # 保存失敗してもメモリ上のカウントは維持して動作継続。
                logger.warning(
                    "状態の永続化に失敗しました（current=%d）。メモリ上では継続します。",
                    result.current,
                )

    def _state_snapshot(self) -> dict:
        with self._counter_lock:
            return {
                "current": self.counter.current,
                "total": self.counter.total_spaces,
                "occupancy": self.counter.status.value,
                "full_at": self.counter.full_at,
                "crowded_at": self.counter.crowded_at,
            }

    # ----- GUI 操作 API -------------------------------------------------
    # GUI（メインスレッド）と受信層（別スレッド）の双方から呼ばれるため、
    # いずれも _counter_lock で直列化し、結果を永続化する。
    def state_snapshot(self) -> dict:
        """GUI 表示用の現在状態スナップショット（スレッド安全）。"""
        return self._state_snapshot()

    def manual_entry(self) -> None:
        """GUI の「現在台数 ＋」ボタン用。手動で1台入庫扱いにする。"""
        self._apply_event("手動入庫", self.counter.record_entry)

    def manual_exit(self) -> None:
        """GUI の「現在台数 －」ボタン用。手動で1台出庫扱いにする。"""
        self._apply_event("手動出庫", self.counter.record_exit)

    def adjust_full_at(self, delta: int) -> CountResult:
        """GUI の「満車台数 ＋/－」ボタン用。full_at を delta だけ増減する。

        範囲外（counter 側で WARNING 済み）の場合は現状維持。受理時は
        ステータス再計算結果を永続化する。
        """
        with self._counter_lock:
            prev_status = self.counter.status
            result = self.counter.set_full_at(self.counter.full_at + delta)
            if not result.accepted:
                return result
            logger.info(
                "満車台数を変更: full_at=%d（current=%d/%d status=%s）",
                self.counter.full_at,
                result.current,
                self.counter.total_spaces,
                result.status.value,
            )
            if result.status_changed:
                logger.info(
                    "ステータス変化: %s -> %s（現在 %d台 / 満車 %d台）",
                    prev_status.value,
                    result.status.value,
                    result.current,
                    self.counter.full_at,
                )
            self._save_after_threshold_change("満車台数", result)
            return result

    def adjust_crowded_at(self, delta: int) -> CountResult:
        """GUI の「混雑台数 ＋/－」ボタン用。crowded_at を delta だけ増減する。

        範囲外（counter 側で WARNING 済み）の場合は現状維持。受理時は
        ステータス再計算結果を永続化する。
        """
        with self._counter_lock:
            prev_status = self.counter.status
            result = self.counter.set_crowded_at(self.counter.crowded_at + delta)
            if not result.accepted:
                return result
            logger.info(
                "混雑台数を変更: crowded_at=%d（current=%d/%d status=%s）",
                self.counter.crowded_at,
                result.current,
                self.counter.total_spaces,
                result.status.value,
            )
            if result.status_changed:
                logger.info(
                    "ステータス変化: %s -> %s（現在 %d台 / 混雑 %d台）",
                    prev_status.value,
                    result.status.value,
                    result.current,
                    self.counter.crowded_at,
                )
            self._save_after_threshold_change("混雑台数", result)
            return result

    def _save_after_threshold_change(self, label: str, result: CountResult) -> None:
        """閾値変更後の状態を永続化する（_counter_lock 保持中に呼ぶこと）。"""
        try:
            self.store.save_state(
                result.current, result.status, self.counter.full_at, self.counter.crowded_at
            )
        except OSError:
            logger.warning(
                "%s変更の永続化に失敗しました（full_at=%d crowded_at=%d）。メモリ上では継続します。",
                label,
                self.counter.full_at,
                self.counter.crowded_at,
            )

    def adjust_current(self, delta: int) -> CountResult:
        """GUI の「現在台数 ＋N/－N」用。現在台数を delta だけまとめて増減する。

        delta の符号方向に record_entry/record_exit を繰り返し適用し、途中で
        範囲外（0未満/total超過）になった時点で打ち切る。1件でも適用できたら
        最後に1回だけ永続化する。1件も適用できなければ accepted=False を返す
        （GUI 側はこれを見て「限界」点滅を出す）。
        """
        with self._counter_lock:
            if delta == 0:
                return CountResult(True, self.counter.current, self.counter.status, False)
            prev_status = self.counter.status
            record = self.counter.record_entry if delta > 0 else self.counter.record_exit
            applied = 0
            for _ in range(abs(delta)):
                if not record().accepted:
                    break
                applied += 1
            if applied == 0:
                # 限界（0未満/total超過）で1件も動かせなかった。
                return CountResult(False, self.counter.current, self.counter.status, False)
            changed = self.counter.status != prev_status
            logger.info(
                "現在台数を補正: 要求 %+d / 適用 %d件 -> current=%d/%d status=%s",
                delta,
                applied,
                self.counter.current,
                self.counter.total_spaces,
                self.counter.status.value,
            )
            self._save_current_state()
            return CountResult(True, self.counter.current, self.counter.status, changed)

    def reset_current(self) -> CountResult:
        """GUI の「0にリセット」用。現在台数を 0 に戻す。"""
        with self._counter_lock:
            if self.counter.current == 0:
                return CountResult(False, 0, self.counter.status, False)
            prev_status = self.counter.status
            while self.counter.current > 0:
                if not self.counter.record_exit().accepted:
                    break
            changed = self.counter.status != prev_status
            logger.info(
                "現在台数を 0 にリセットしました（status=%s）。",
                self.counter.status.value,
            )
            self._save_current_state()
            return CountResult(True, self.counter.current, self.counter.status, changed)

    def _save_current_state(self) -> None:
        """現在状態を永続化する（_counter_lock 保持中に呼ぶこと）。"""
        try:
            self.store.save_state(
                self.counter.current, self.counter.status, self.counter.full_at, self.counter.crowded_at
            )
        except OSError:
            logger.warning(
                "状態の永続化に失敗しました（current=%d）。メモリ上では継続します。",
                self.counter.current,
            )

    # ----- lifecycle ----------------------------------------------------
    def run(self) -> None:
        try:
            self.receiver.start()
        except Exception:
            logger.exception("受信層の起動に失敗しました。終了します。")
            self.store.close()
            raise

        logger.info(
            "起動完了: total=%d current=%d status=%s。イベント待機中。",
            self.counter.total_spaces,
            self.counter.current,
            self.counter.status.value,
        )

        def _sigterm(signum, _frame):
            logger.info("シグナル %s を受信。停止します。", signal.Signals(signum).name)
            self._stop_event.set()

        signal.signal(signal.SIGTERM, _sigterm)
        signal.signal(signal.SIGINT, _sigterm)

        try:
            if self.cfg.gui.enabled:
                self._run_gui()
            else:
                self._stop_event.wait()
        finally:
            logger.info("停止処理を開始します。")
            try:
                self.receiver.stop()
            except Exception:
                logger.exception("受信層の停止中に例外。")
            self.store.close()
            logger.info("終了完了")

    def _run_gui(self) -> None:
        """Tkinter GUI をメインスレッドで起動する。

        ウィンドウを閉じるか、SIGTERM/SIGINT で _stop_event がセットされると
        mainloop を抜ける。Tkinter のインポート/起動失敗時はヘッドレスに
        フォールバックして動作を継続する（受信層は生きている）。
        """
        try:
            from .gui import ParkingGui
        except Exception:
            logger.exception(
                "GUI モジュールの読み込みに失敗しました（python3-tk 未導入や DISPLAY 未設定の可能性）。"
                "ヘッドレスで継続します。"
            )
            self._stop_event.wait()
            return
        try:
            gui = ParkingGui(self, poll_interval_ms=self.cfg.gui.poll_interval_ms, fullscreen=self.cfg.gui.fullscreen)
        except Exception:
            logger.exception("GUI の初期化に失敗しました。ヘッドレスで継続します。")
            self._stop_event.wait()
            return
        # SIGTERM 等で停止要求が来たら GUI 側からも閉じられるよう、stop_event を渡す。
        gui.run(self._stop_event)


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    config_path = argv[0] if argv else "config.toml"

    # ここはまだ logging 未設定なので stderr に出す。
    if not Path(config_path).exists():
        sys.stderr.write(
            f"[FATAL] 設定ファイル {config_path} が見つかりません。"
            "config.example.toml をコピーして作成してください。\n"
        )
        return 2

    try:
        app = Application(config_path)
    except ConfigError as e:
        sys.stderr.write(f"[FATAL] 設定エラー: {e}\n")
        return 2
    except Exception as e:  # 初期化中の予期しない失敗
        # logging が設定済みなら logger にも残る。確実に stderr にも出す。
        logging.getLogger(__name__).exception("初期化に失敗しました。")
        sys.stderr.write(f"[FATAL] 初期化に失敗: {e}\n")
        return 1

    try:
        app.run()
    except Exception as e:
        logging.getLogger(__name__).exception("実行中に致命的エラー。")
        sys.stderr.write(f"[FATAL] 実行中に致命的エラー: {e}\n")
        return 1
    return 0
