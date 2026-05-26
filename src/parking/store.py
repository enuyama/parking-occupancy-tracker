from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import OccupancyStatus, State

logger = logging.getLogger(__name__)


class Store:
    """現在台数・ステータスを JSON ファイル 1 枚に保存するシンプルなストア。

    フォーマット:
        {
            "current_count": 12,
            "status": "CROWDED",
            "updated_at": "2026-05-18T07:34:21.123456+00:00"
        }

    履歴は logging モジュール経由で parking.log に残るのでここでは持たない。
    書き込みは tempfile + os.replace で原子的に行う（電源断時の半端書き込み防止）。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise OSError(
                f"状態ファイルの保存先ディレクトリ {self._path.parent} を作成できません: {e}"
            ) from e
        logger.debug("Store 初期化: state_file=%s", self._path)

    def restore(self) -> State | None:
        if not self._path.exists():
            logger.info("状態ファイル %s が無いため、新規（0台）で開始します。", self._path)
            return None
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(
                "状態ファイル %s が JSON として壊れています: %s。0台で初期化します。"
                "（必要なら手で削除/修正してください）",
                self._path,
                e,
            )
            return None
        except OSError as e:
            logger.error("状態ファイル %s を読めません: %s。0台で初期化します。", self._path, e)
            return None

        try:
            state = State(
                current_count=int(data["current_count"]),
                status=OccupancyStatus(data["status"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.error(
                "状態ファイル %s の内容が不正です: %s（中身: %r）。0台で初期化します。",
                self._path,
                e,
                data,
            )
            return None
        logger.debug("状態ファイル復元: %r", state)
        return state

    def save_state(self, current_count: int, status: OccupancyStatus) -> None:
        payload = {
            "current_count": current_count,
            "status": status.value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # 原子的書き込み: 同一ディレクトリに tempfile を作って os.replace
        dirpath = self._path.parent
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=".state-", suffix=".tmp", dir=str(dirpath)
            )
        except OSError as e:
            logger.error("状態ファイルの一時ファイルを作成できません（dir=%s）: %s", dirpath, e)
            raise
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_path, self._path)
        except OSError as e:
            logger.error("状態ファイル %s への保存に失敗: %s", self._path, e)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        logger.debug("状態保存: current=%d status=%s -> %s", current_count, status.value, self._path)

    def close(self) -> None:
        # ファイルベースなので明示的な close は不要。互換のためメソッドだけ残す。
        return None
