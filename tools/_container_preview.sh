#!/usr/bin/env bash
#
# コンテナ内専用。tools/preview_gui_linux.sh から docker 経由で実行される。
# Xvfb にアプリ GUI を表示し、noVNC(ブラウザVNC) で配信する。
# ホストのブラウザで http://localhost:6080/vnc.html を開いて操作する。
# プロジェクトは /work に read-only マウントされている前提（書き込みは /tmp のみ）。
#
set -e
export DEBIAN_FRONTEND=noninteractive

echo "[1/3] 依存インストール (python3-tk, xvfb, x11vnc, novnc, 日本語フォント)..."
apt-get update -qq >/tmp/apt.log 2>&1
apt-get install -y -qq python3 python3-tk xvfb x11vnc novnc websockify fonts-noto-cjk >/tmp/apt2.log 2>&1

echo "[2/3] Xvfb + アプリ起動..."
export DISPLAY=:99
Xvfb :99 -screen 0 900x640x24 >/tmp/xvfb.log 2>&1 &
sleep 2

cat > /tmp/demo.toml <<'EOF'
[parking]
total_spaces = 100
[thresholds]
crowded_at = 15
full_at = 19
[receiver]
type = "dummy"
[storage]
state_file = "/tmp/demo_state.json"
[logging]
level = "INFO"
file = "/tmp/demo.log"
[gui]
enabled = true
fullscreen = false
poll_interval_ms = 200
EOF
echo '{"current_count": 20, "status": "FULL", "updated_at": "2026-01-01T00:00:00+00:00", "full_at": 19, "crowded_at": 15}' > /tmp/demo_state.json

PYTHONPATH=/work/src python3 -m parking /tmp/demo.toml >/tmp/app.out 2>&1 &
sleep 3

# x11vnc は内部のみ(localhost:5900)で配信。ブラウザへは noVNC/websockify が中継する。
x11vnc -display :99 -forever -shared -nopw -localhost -rfbport 5900 >/tmp/vnc.log 2>&1 &
sleep 1

# noVNC の web 資産パス（ディストリ差を吸収）
NOVNC_WEB=/usr/share/novnc
[ -d "$NOVNC_WEB" ] || NOVNC_WEB=/usr/share/webapps/novnc

echo "==================================================================="
echo " 準備完了。ホストのブラウザで次の URL を開いてください:"
echo "     http://localhost:6080/vnc.html"
echo " 画面の [Connect] を押すと GUI が表示されます（パスワード不要）。"
echo " 接続後、＋/－ を実際にクリックして動作を確認できます。"
echo " 終了: このターミナルで Ctrl-C"
echo "==================================================================="

echo "[3/3] noVNC(ブラウザVNC) 配信開始..."
exec websockify --web="$NOVNC_WEB" 6080 localhost:5900
