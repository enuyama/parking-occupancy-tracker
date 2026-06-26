"""Xvfb 上で GUI を起動し、各 ＋/－ ボタンの画面絶対座標を /tmp/coords.txt に書き出す。

その後 mainloop に入る（コンテナ側スクリプトが xdotool でその座標をクリックして検証する）。
コンテナ内専用のヘルパ。tools/verify_gui_linux.sh から呼ばれる。
"""
from __future__ import annotations

import sys
import threading
import tkinter as tk

sys.path.insert(0, "/work/src")
from parking.app import Application  # noqa: E402
from parking.gui import ParkingGui  # noqa: E402

app = Application("/tmp/demo.toml")
gui = ParkingGui(app, 200, False)


def dump() -> None:
    """＋/－ ボタン（tk.Label）の画面絶対座標とサイズを書き出す。"""
    try:
        gui._root.update_idletasks()
        gui._root.update()
    except Exception:
        pass

    lines: list[str] = []

    def walk(widget: tk.Misc) -> None:
        for child in widget.winfo_children():
            if isinstance(child, tk.Label) and child.cget("text") in ("－", "＋"):
                lines.append(
                    "%s %d %d %d %d"
                    % (
                        child.cget("text"),
                        child.winfo_rootx(),
                        child.winfo_rooty(),
                        child.winfo_width(),
                        child.winfo_height(),
                    )
                )
            walk(child)

    walk(gui._root)
    with open("/tmp/coords.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


gui._root.after(1500, dump)
gui.run(threading.Event())
