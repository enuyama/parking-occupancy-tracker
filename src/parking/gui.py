from __future__ import annotations

import logging
import threading
import tkinter as tk
from typing import Callable, Protocol

logger = logging.getLogger(__name__)


# ----- 調整用定数 ------------------------------------------------------------
# 色（黒背景パネル前提）
COLOR_BG = "#000000"  # 画面全体の背景（黒）
COLOR_PANEL_BG = "#000000"  # 枠内の背景（黒）
COLOR_BORDER = "#FFFFFF"  # 枠線（白）
COLOR_LABEL_TEXT = "#FFFFFF"  # 見出しラベルの文字色（白）
COLOR_NUMBER_TEXT = "#FFFFFF"  # 数値の文字色（白）
COLOR_SUB_TEXT = "#AAAAAA"  # 補足数字（current/full）の文字色（淡色）
# ＋/－ ボタンは tk.Label で実装する（macOS Aqua の tk.Button は bg を無視して
# 灰色になるため。Label なら背景色が確実に効き、水色で表示できる）。
COLOR_BUTTON_BG = "#4FC3F7"  # ＋/－ ボタン背景（水色）
COLOR_BUTTON_ACTIVE_BG = "#0288D1"  # 押下中の背景（濃い水色）
COLOR_BUTTON_TEXT = "#FFFFFF"  # ボタン記号の色（白）
COLOR_LIMIT = "#FF3B30"  # 上限/下限に達したときの点滅色（赤）

# occupancy(status) -> (表示語, 文字色) のマッピング
STATUS_DISPLAY: dict[str, tuple[str, str]] = {
    "FULL": ("満車", "#FF3B30"),  # 赤
    "CROWDED": ("混雑", "#FF9500"),  # 橙
    "EMPTY": ("空車", "#34C759"),  # 緑
}
# 未知ステータス時のフォールバック
STATUS_FALLBACK: tuple[str, str] = ("―", COLOR_LABEL_TEXT)

# フォントサイズ／太さ（family は環境依存のため明示しない）
FONT_STATUS_WORD = 40  # ステータス語（満車/混雑/空車）
FONT_STATUS_NUMS = 22  # ステータス補足（現在/満車 の数字）
FONT_CURRENT_TITLE = 20  # 「現在の台数」見出し
FONT_CURRENT_NUMBER = 50  # 現在台数の数値（主役・最大）
FONT_STEP_BUTTON = 40  # 現在台数の ＋/－ ボタン（大）
FONT_THRESH_LABEL = 15  # 「満車台数」「混雑台数」見出し（小）
FONT_THRESH_NUMBER = 22  # 閾値の数値（小）
FONT_THRESH_BUTTON = 24  # 閾値の ＋/－ ボタン（小）

# サイズ・余白
NUMBER_BORDER_WIDTH = 2  # 数値枠の枠線幅
CURRENT_BOX_WIDTH = 4  # 現在台数ボックスの文字幅
THRESH_BOX_WIDTH = 4  # 閾値ボックスの文字幅
STEP_BUTTON_PADX = 34  # 現在台数 ＋/－ ボタンの内余白（横・大きめ）
STEP_BUTTON_PADY = 22  # 現在台数 ＋/－ ボタンの内余白（縦・大きめ）
THRESH_BUTTON_PADX = 14  # 閾値 ＋/－ ボタンの内余白（横）
THRESH_BUTTON_PADY = 8  # 閾値 ＋/－ ボタンの内余白（縦）
BUTTON_BORDER_WIDTH = 3  # ボタンの立体枠
OUTER_PAD = 8  # ブロック間の余白
GAP = 8  # 行内の要素間ギャップ

FLASH_MS = 250  # 限界点滅の表示時間


class _AppLike(Protocol):
    """ParkingGui が依存する Application のインターフェース（型ヒント用）。"""

    def state_snapshot(self) -> dict: ...

    def manual_entry(self) -> None: ...

    def manual_exit(self) -> None: ...

    def adjust_full_at(self, delta: int) -> object: ...

    def adjust_crowded_at(self, delta: int) -> object: ...


class ParkingGui:
    """駐車場満空混システムのタッチ/クリック操作パネル（Tkinter）。

    MQTT 等による自動カウントと同時稼働する操作 GUI。受信層は別スレッド、
    本 GUI はメインスレッドで動く。Tkinter ウィジェットはメインスレッド
    からしか触れないため、表示更新は ``root.after`` による定期ポーリングで
    ``app.state_snapshot()`` を読んで反映する（受信スレッドから直接ウィジェ
    ットを触らない）。台数等の状態は保持せず、state_snapshot を唯一の真実
    とする。

    レイアウト（1画面・現在台数を主役に）:
        1) ステータス（満車/混雑/空車 ＋ 現在/満車 の数字）
        2) 現在の台数（主役・大）  [－]  数値  [＋]
        3) 満車台数 / 混雑台数（小さめ）  各 [－] 数値 [＋]
    ＋/－ は水色の ``tk.Label`` ボタンで、押した瞬間に1台/1段だけ増減する
    （``after`` タイマー・長押し連打は使わない）。上限/下限に達したときは数値を
    赤く点滅させる。
    """

    def __init__(self, app: _AppLike, poll_interval_ms: int, fullscreen: bool) -> None:
        """Tk ルートウィンドウとウィジェットを構築する（mainloop は呼ばない）。

        Args:
            app: state_snapshot / manual_* / adjust_* / reset_current を備えた
                Application オブジェクト。
            poll_interval_ms: 表示更新（ポーリング）間隔（ミリ秒）。
            fullscreen: True で全画面表示にする（Esc キーで解除可能）。
        """
        self._app = app
        self._poll_interval_ms = max(1, int(poll_interval_ms))
        self._fullscreen = bool(fullscreen)
        self._stop_event: threading.Event | None = None

        self._root = tk.Tk()
        self._root.title("駐車場 満空混")
        self._root.configure(bg=COLOR_BG)

        if self._fullscreen:
            try:
                self._root.attributes("-fullscreen", True)
            except tk.TclError:
                logger.warning("全画面表示に失敗しました。ウィンドウ表示で継続します。")
            self._root.bind("<Escape>", self._on_escape)

        self._status_word = tk.StringVar(value=STATUS_FALLBACK[0])
        self._status_nums = tk.StringVar(value="- / -")
        self._current_value = tk.StringVar(value="-")
        self._full_at_value = tk.StringVar(value="-")
        self._crowded_at_value = tk.StringVar(value="-")

        self._build_widgets()

    # ----- ウィジェット構築 -------------------------------------------------
    def _build_widgets(self) -> None:
        root = self._root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=2)  # ステータス
        root.rowconfigure(1, weight=3)  # 現在台数（主役・最大）
        root.rowconfigure(2, weight=2)  # 閾値（小）

        self._build_status(row=0)
        self._build_current(row=1)
        self._build_thresholds(row=2)

    def _build_status(self, row: int) -> None:
        """ステータス語＋（現在/満車）の数字を表示する最上段。"""
        block = tk.Frame(self._root, bg=COLOR_BG)
        block.grid(row=row, column=0, padx=OUTER_PAD, pady=OUTER_PAD, sticky="nsew")
        block.columnconfigure(0, weight=1)
        self._status_label = tk.Label(
            block,
            textvariable=self._status_word,
            fg=STATUS_FALLBACK[1],
            bg=COLOR_PANEL_BG,
            font=("", FONT_STATUS_WORD, "bold"),
            relief="solid",
            bd=NUMBER_BORDER_WIDTH,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_BORDER,
            highlightthickness=NUMBER_BORDER_WIDTH,
            padx=24,
            pady=4,
        )
        self._status_label.grid(row=0, column=0)
        nums = tk.Label(
            block,
            textvariable=self._status_nums,
            fg=COLOR_SUB_TEXT,
            bg=COLOR_BG,
            font=("", FONT_STATUS_NUMS, "bold"),
        )
        nums.grid(row=1, column=0, pady=(GAP, 0))

    def _build_current(self, row: int) -> None:
        """現在の台数（主役）。[－] 数値 [＋]。"""
        block = tk.Frame(self._root, bg=COLOR_BG)
        block.grid(row=row, column=0, padx=OUTER_PAD, pady=OUTER_PAD, sticky="nsew")
        block.columnconfigure(0, weight=1)

        title = tk.Label(
            block, text="現在の台数", fg=COLOR_LABEL_TEXT, bg=COLOR_BG,
            font=("", FONT_CURRENT_TITLE, "bold"),
        )
        title.grid(row=0, column=0, pady=(0, GAP))

        controls = tk.Frame(block, bg=COLOR_BG)
        controls.grid(row=1, column=0)

        box = tk.Label(
            controls,
            textvariable=self._current_value,
            fg=COLOR_NUMBER_TEXT,
            bg=COLOR_PANEL_BG,
            font=("", FONT_CURRENT_NUMBER, "bold"),
            relief="solid",
            bd=NUMBER_BORDER_WIDTH,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_BORDER,
            highlightthickness=NUMBER_BORDER_WIDTH,
            width=CURRENT_BOX_WIDTH,
            padx=8,
            pady=4,
        )

        # 1クリック=1台（押した瞬間に発火。長押し連打・タイマーは無し）。
        minus = self._make_button(
            controls, "－", self._make_press(self._app.manual_exit, "current", box),
            font_size=FONT_STEP_BUTTON, padx=STEP_BUTTON_PADX, pady=STEP_BUTTON_PADY, name="現在−",
        )
        plus = self._make_button(
            controls, "＋", self._make_press(self._app.manual_entry, "current", box),
            font_size=FONT_STEP_BUTTON, padx=STEP_BUTTON_PADX, pady=STEP_BUTTON_PADY, name="現在＋",
        )
        minus.grid(row=0, column=0, padx=GAP)
        box.grid(row=0, column=1, padx=GAP)
        plus.grid(row=0, column=2, padx=GAP)

    def _build_thresholds(self, row: int) -> None:
        """満車台数 / 混雑台数（小さめ）を横並びに配置する。"""
        block = tk.Frame(self._root, bg=COLOR_BG)
        block.grid(row=row, column=0, padx=OUTER_PAD, pady=OUTER_PAD, sticky="nsew")
        block.columnconfigure(0, weight=1)
        block.columnconfigure(1, weight=1)
        self._build_threshold_cell(
            block, col=0, title="満車台数", value_var=self._full_at_value,
            value_key="full_at",
            on_minus=lambda: self._app.adjust_full_at(-1),
            on_plus=lambda: self._app.adjust_full_at(1),
        )
        self._build_threshold_cell(
            block, col=1, title="混雑台数", value_var=self._crowded_at_value,
            value_key="crowded_at",
            on_minus=lambda: self._app.adjust_crowded_at(-1),
            on_plus=lambda: self._app.adjust_crowded_at(1),
        )

    def _build_threshold_cell(
        self, parent: tk.Frame, col: int, title: str, value_var: tk.StringVar,
        value_key: str, on_minus: Callable[[], object], on_plus: Callable[[], object],
    ) -> None:
        cell = tk.Frame(parent, bg=COLOR_BG)
        cell.grid(row=0, column=col, padx=OUTER_PAD)
        tk.Label(
            cell, text=title, fg=COLOR_LABEL_TEXT, bg=COLOR_BG,
            font=("", FONT_THRESH_LABEL, "bold"),
        ).grid(row=0, column=0, columnspan=3, pady=(0, GAP // 2))

        box = tk.Label(
            cell, textvariable=value_var, fg=COLOR_NUMBER_TEXT, bg=COLOR_PANEL_BG,
            font=("", FONT_THRESH_NUMBER, "bold"), relief="solid", bd=NUMBER_BORDER_WIDTH,
            highlightbackground=COLOR_BORDER, highlightcolor=COLOR_BORDER,
            highlightthickness=NUMBER_BORDER_WIDTH, width=THRESH_BOX_WIDTH, padx=6, pady=2,
        )
        minus = self._make_button(
            cell, "－", self._make_press(on_minus, value_key, box),
            font_size=FONT_THRESH_BUTTON, padx=THRESH_BUTTON_PADX, pady=THRESH_BUTTON_PADY, name=f"{title}−",
        )
        plus = self._make_button(
            cell, "＋", self._make_press(on_plus, value_key, box),
            font_size=FONT_THRESH_BUTTON, padx=THRESH_BUTTON_PADX, pady=THRESH_BUTTON_PADY, name=f"{title}＋",
        )
        minus.grid(row=1, column=0, padx=GAP // 2)
        box.grid(row=1, column=1, padx=GAP // 2)
        plus.grid(row=1, column=2, padx=GAP // 2)

    def _make_button(
        self, parent: tk.Frame, text: str, command: Callable[[], object], *,
        font_size: int, padx: int, pady: int, name: str = "",
    ) -> tk.Label:
        """水色のボタンを ``tk.Label`` で生成する（macOS でも背景色が効く）。

        押した瞬間(``<ButtonPress-1>``)に command を1回だけ呼ぶ最小実装。
        ``after`` タイマー・長押し連打・ダブル/トリプルの追加バインドは**一切
        使わない**（これらが過去の不具合＝固まる/取りこぼしの原因だった）。
        押下中だけ背景色を濃くし、離した/外れたら戻す（見た目のみ）。
        """

        lbl = tk.Label(
            parent, text=text, fg=COLOR_BUTTON_TEXT, bg=COLOR_BUTTON_BG,
            font=("", font_size, "bold"),
            padx=padx, pady=pady, relief="raised", bd=BUTTON_BORDER_WIDTH, cursor="hand2",
        )

        def on_press(_event: object) -> None:
            logger.debug("ボタン押下: %s", name or text)
            try:
                lbl.configure(bg=COLOR_BUTTON_ACTIVE_BG)
            except tk.TclError:
                pass
            command()  # 押した瞬間に1回だけ

        def restore(_event: object) -> None:
            try:
                lbl.configure(bg=COLOR_BUTTON_BG)
            except tk.TclError:
                pass

        lbl.bind("<ButtonPress-1>", on_press)
        lbl.bind("<ButtonRelease-1>", restore)
        lbl.bind("<Leave>", restore)
        return lbl

    # ----- 操作ハンドラ -----------------------------------------------------
    def _make_press(
        self, action: Callable[[], object], value_key: str, value_box: tk.Label
    ) -> Callable[[], None]:
        """押下を、限界フィードバック＋即時反映つきでラップする。

        押下前後で value_key の値が変わらなければ（上限/下限で拒否）数値を
        赤く点滅。変われば即時反映してポーリング待ちの遅延をなくす。
        """

        def handler() -> None:
            before = self._app.state_snapshot().get(value_key)
            try:
                action()
            except Exception:
                logger.exception("操作の実行に失敗しました（key=%s）。", value_key)
                return
            snapshot = self._app.state_snapshot()
            after = snapshot.get(value_key)
            if before == after:
                self._flash_limit(value_box)
            else:
                self._restore_box_fg(value_box)
                self._update_display(snapshot)

        return handler

    def _flash_limit(self, value_box: tk.Label) -> None:
        """限界到達時に数値ボックスを一瞬赤くして「これ以上動かせない」を伝える。

        連打で限界を叩いても点滅タイマーが溜まらないよう、ボックスごとに直前の
        タイマーを必ずキャンセルしてから1つだけ仕掛ける（保留 after は最大3個
        ＝ボックス数まで）。
        """
        try:
            prev = getattr(value_box, "_flash_id", None)
            if prev is not None:
                try:
                    self._root.after_cancel(prev)
                except Exception:
                    pass
            value_box.configure(fg=COLOR_LIMIT)
            value_box._flash_id = self._root.after(  # type: ignore[attr-defined]
                FLASH_MS, lambda: self._restore_box_fg(value_box)
            )
        except tk.TclError:
            pass

    def _restore_box_fg(self, value_box: tk.Label) -> None:
        try:
            value_box._flash_id = None  # type: ignore[attr-defined]
            value_box.configure(fg=COLOR_NUMBER_TEXT)
        except tk.TclError:
            pass

    def _on_escape(self, _event: object = None) -> None:
        """Esc キーで全画面を解除する（操作・デバッグ用）。"""
        try:
            self._root.attributes("-fullscreen", False)
        except tk.TclError:
            pass

    # ----- ライフサイクル ---------------------------------------------------
    def run(self, stop_event: threading.Event) -> None:
        """mainloop を回し、stop_event 連携で停止する。"""
        self._stop_event = stop_event
        self._root.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._root.after(self._poll_interval_ms, self._poll)
        try:
            self._root.mainloop()
        except tk.TclError:
            logger.debug("mainloop 終了時に TclError を無視しました。", exc_info=True)

    def _on_window_close(self) -> None:
        """ウィンドウ×押下時: stop_event をセットしてから破棄する。"""
        if self._stop_event is not None:
            self._stop_event.set()
        self._destroy_safely()

    def _poll(self) -> None:
        """定期ポーリング: 停止チェック + state_snapshot による表示更新。"""
        if self._stop_event is not None and self._stop_event.is_set():
            self._destroy_safely()
            return
        try:
            snapshot = self._app.state_snapshot()
            self._update_display(snapshot)
        except Exception:
            logger.exception("状態表示の更新に失敗しました。次回ポーリングで再試行します。")
        try:
            self._root.after(self._poll_interval_ms, self._poll)
        except tk.TclError:
            pass

    def _update_display(self, snapshot: dict) -> None:
        """スナップショットの内容をラベルへ反映する。"""
        occupancy = str(snapshot.get("occupancy", ""))
        word, color = STATUS_DISPLAY.get(occupancy, STATUS_FALLBACK)
        self._status_word.set(word)
        self._status_label.configure(fg=color)

        current = snapshot.get("current")
        full_at = snapshot.get("full_at")
        crowded_at = snapshot.get("crowded_at")
        cur_s = str(current) if current is not None else "-"
        full_s = str(full_at) if full_at is not None else "-"
        crowded_s = str(crowded_at) if crowded_at is not None else "-"
        # ステータス補足: 「現在 / 満車」の台数を併記して妥当性を確認しやすく。
        self._status_nums.set(f"{cur_s} / {full_s}")
        self._current_value.set(cur_s)
        self._full_at_value.set(full_s)
        self._crowded_at_value.set(crowded_s)

    def _destroy_safely(self) -> None:
        """ルートウィンドウを安全に破棄する（多重 destroy を無視）。"""
        try:
            self._root.destroy()
        except tk.TclError:
            pass
