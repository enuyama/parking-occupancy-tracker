from __future__ import annotations

import logging
import logging.handlers
import signal
import sys
import threading
from pathlib import Path

from .config import Config, ConfigError, load_config
from .counter import OccupancyCounter
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

        self.counter = OccupancyCounter(
            total_spaces=self.cfg.parking.total_spaces,
            thresholds=self.cfg.thresholds,
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
            self.store.save_state(self.counter.current, self.counter.status)
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
                self.store.save_state(result.current, result.status)
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
            }

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
            self._stop_event.wait()
        finally:
            logger.info("停止処理を開始します。")
            try:
                self.receiver.stop()
            except Exception:
                logger.exception("受信層の停止中に例外。")
            self.store.close()
            logger.info("終了完了")


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
