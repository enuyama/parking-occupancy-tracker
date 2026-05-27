from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from ..config import HttpReceiverConfig
from .base import EventCallback

logger = logging.getLogger(__name__)


StateProvider = Callable[[], dict]


class HttpReceiver:
    """LinkBase (満空灯制御装置) からの HTTP 通知を受信し、
    立ち上がりエッジを検出して on_entry / on_exit を呼ぶ受信層。

    プロトコル: GET /api/control?alert=XXXX9999&id=...
    詳細は docs/DESIGN_HTTP_RECEIVER.md
    """

    def __init__(
        self,
        config: HttpReceiverConfig,
        on_entry: EventCallback,
        on_exit: EventCallback,
        state_provider: StateProvider | None = None,
    ) -> None:
        self._cfg = config
        self._on_entry = on_entry
        self._on_exit = on_exit
        self._state_provider = state_provider

        # alert 文字列は 0-indexed。SW1 が先頭。
        self._entry_idx = config.entry_switch - 1
        self._exit_idx = config.exit_switch - 1
        self._active = config.active_value

        # エッジ検出用の前回状態（"0"/"1"/None=未観測）。
        # 起動直後は「非ACTIVE」とみなすため、active と異なる値で初期化。
        initial = "1" if self._active == "0" else "0"
        self._last_entry: str = initial
        self._last_exit: str = initial

        # 直近の確定エッジ時刻（最小間隔ガード用）
        self._last_entry_edge_at = 0.0
        self._last_exit_edge_at = 0.0

        self._lock = threading.Lock()
        self._server: "uvicorn.Server | None" = None  # type: ignore[name-defined]
        self._thread: threading.Thread | None = None

        self.app = FastAPI(title="parking-occupancy-tracker")
        self._setup_routes()

    # ------------------------------------------------------------------
    # routes
    # ------------------------------------------------------------------
    def _setup_routes(self) -> None:
        @self.app.get("/api/control")
        def control(
            alert: Optional[str] = Query(None),
            id: Optional[str] = Query(None),
        ):
            logger.debug("受信: alert=%r id=%r", alert, id)
            err = self._validate_alert(alert)
            if err is not None:
                logger.warning(
                    "alert バリデーション失敗: %s (alert=%r id=%r)", err, alert, id
                )
                raise HTTPException(status_code=400, detail=err)
            assert alert is not None

            if re.fullmatch(r"9+", alert):
                logger.debug("alert が全桁9のため処理スキップ（状態問い合わせ扱い）")
                return JSONResponse(
                    content={"status": "ok", "message": "all_nines"},
                    status_code=200,
                )

            if alert[4:] != "9999":
                # LinkBase の Mode4 では下位4桁は9999固定の想定。異なる=設定/配線の疑い。
                logger.debug("alert 下位4桁が9999でない: %r（仕様外だが処理は継続）", alert[4:])

            try:
                entries, exits = self._process(alert)
            except Exception:
                logger.exception("alert 処理中に予期しない例外 (alert=%r)", alert)
                raise HTTPException(status_code=500, detail="Internal_error")

            payload: dict = {
                "status": "ok",
                "entries": entries,
                "exits": exits,
            }
            if self._state_provider is not None:
                payload.update(self._state_provider())
            return JSONResponse(content=payload, status_code=200)

        @self.app.get("/health")
        def health():
            payload: dict = {"status": "healthy"}
            if self._state_provider is not None:
                payload.update(self._state_provider())
            return JSONResponse(content=payload, status_code=200)

        @self.app.get("/state")
        def state():
            if self._state_provider is None:
                return JSONResponse(content={"status": "ok"}, status_code=200)
            return JSONResponse(content=self._state_provider(), status_code=200)

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_alert(alert: str | None) -> str | None:
        if not alert:
            return "Parameter_not_found"
        if len(alert) != 8:
            return "Invalid_parameter_length"
        if re.search(r"[^019]", alert):
            return "Parameter_contains_invalid_value"
        return None

    # ------------------------------------------------------------------
    # edge detection
    # ------------------------------------------------------------------
    def _process(self, alert: str) -> tuple[int, int]:
        sw_entry = alert[self._entry_idx]
        sw_exit = alert[self._exit_idx]
        logger.debug(
            "解析: SW%d(入庫)=%s SW%d(出庫)=%s active=%r (last_entry=%s last_exit=%s)",
            self._cfg.entry_switch,
            sw_entry,
            self._cfg.exit_switch,
            sw_exit,
            self._active,
            self._last_entry,
            self._last_exit,
        )

        with self._lock:
            now = time.monotonic()
            entries, self._last_entry, self._last_entry_edge_at = self._detect(
                "入庫", sw_entry, self._last_entry, self._last_entry_edge_at, now
            )
            exits, self._last_exit, self._last_exit_edge_at = self._detect(
                "出庫", sw_exit, self._last_exit, self._last_exit_edge_at, now
            )

            # コールバックはロック内で呼ぶ。counter 側でも更新が逐次化される前提。
            for _ in range(entries):
                try:
                    self._on_entry()
                except Exception:
                    logger.exception("on_entry コールバックで例外（カウントが反映されていない可能性）")
            for _ in range(exits):
                try:
                    self._on_exit()
                except Exception:
                    logger.exception("on_exit コールバックで例外（カウントが反映されていない可能性）")

        return entries, exits

    def _detect(
        self, direction: str, sw_now: str, last: str, last_edge_at: float, now: float
    ) -> tuple[int, str, float]:
        """1方向ぶんの立ち上がりエッジ判定。

        戻り値: (発火数 0/1, 更新後 last, 更新後 last_edge_at)
        判定理由を DEBUG/INFO でログに残し、なぜカウントした/しなかったを追えるようにする。
        """
        if sw_now == "9":
            logger.debug("%s: SW=9（状態非表示）のため前回状態 %s を維持・判定スキップ", direction, last)
            return 0, last, last_edge_at

        is_active = sw_now == self._active
        was_active = last == self._active
        rising = is_active and not was_active

        if not rising:
            logger.debug(
                "%s: エッジなし (now=%s last=%s active=%r) → カウントせず",
                direction,
                sw_now,
                last,
                self._active,
            )
            return 0, sw_now, last_edge_at

        # ここまで来たら立ち上がりエッジ。min_event_interval で抑制するか判定。
        elapsed = now - last_edge_at
        if elapsed < self._cfg.min_event_interval:
            logger.info(
                "%s: 立ち上がりを検出したが min_event_interval(%.3fs)以内(%.3fs)のため無視。"
                "連続通過を取りこぼしている場合は値を下げる/カメラのパルス幅を見直す。",
                direction,
                self._cfg.min_event_interval,
                elapsed,
            )
            return 0, sw_now, last_edge_at

        logger.debug("%s: 立ち上がりエッジ検出 → カウント", direction)
        return 1, sw_now, now

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            self.app,
            host=self._cfg.host,
            port=self._cfg.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._start_error: BaseException | None = None

        def _run() -> None:
            try:
                self._server.run()
            except BaseException as e:  # ポート競合・権限不足などをここで捕捉
                self._start_error = e
                logger.error("HTTPサーバ起動に失敗: %s", e)

        self._thread = threading.Thread(target=_run, name="http-receiver", daemon=True)
        self._thread.start()

        # 起動完了（bind成功）を最大5秒待ち、失敗していれば例外を上げてアプリに伝える。
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self._start_error is not None:
                raise RuntimeError(
                    f"HTTPサーバを {self._cfg.host}:{self._cfg.port} で起動できませんでした: "
                    f"{self._start_error}。ポート使用中(別プロセス/二重起動)や権限を確認してください。"
                ) from self._start_error
            if getattr(self._server, "started", False):
                break
            time.sleep(0.05)
        else:
            raise RuntimeError(
                f"HTTPサーバが {self._cfg.host}:{self._cfg.port} で起動完了しませんでした（タイムアウト）。"
            )

        logger.info(
            "HttpReceiver 起動: http://%s:%d/api/control "
            "(entry=SW%d, exit=SW%d, active=%r, min_interval=%.3fs)",
            self._cfg.host,
            self._cfg.port,
            self._cfg.entry_switch,
            self._cfg.exit_switch,
            self._active,
            self._cfg.min_event_interval,
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                logger.warning("HTTPサーバスレッドが5秒以内に停止しませんでした。")
        logger.info("HttpReceiver 停止")
