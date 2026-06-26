#!/usr/bin/env bash
#
# コンテナ内専用。tools/verify_gui_linux.sh から docker 経由で実行される。
# Xvfb 上に GUI を表示し、xdotool で実 OS クリックを送って現在台数の増減を検証する。
# プロジェクトは /work に read-only マウントされている前提（書き込みは /tmp のみ）。
#
set -e
export DEBIAN_FRONTEND=noninteractive

echo "[1/4] 依存インストール (python3-tk, xvfb, xdotool, 日本語フォント)..."
apt-get update -qq >/tmp/apt.log 2>&1
apt-get install -y -qq python3 python3-tk xvfb xdotool x11-utils procps fonts-noto-cjk >/tmp/apt2.log 2>&1

echo "[2/4] Xvfb（仮想ディスプレイ）起動..."
export DISPLAY=:99
Xvfb :99 -screen 0 1024x768x24 >/tmp/xvfb.log 2>&1 &
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
level = "DEBUG"
file = "/tmp/demo.log"
[gui]
enabled = true
fullscreen = false
poll_interval_ms = 200
EOF
echo '{"current_count": 50, "status": "FULL", "updated_at": "2026-01-01T00:00:00+00:00", "full_at": 19, "crowded_at": 15}' > /tmp/demo_state.json

echo "[3/4] GUI 起動..."
python3 /work/tools/gui_probe.py >/tmp/app.out 2>&1 &
APPID=$!
sleep 4
if [ ! -s /tmp/coords.txt ]; then
  echo "GUI 起動失敗。アプリ出力:"; cat /tmp/app.out
  kill "$APPID" 2>/dev/null || true
  exit 1
fi

# 現在台数の －ボタン（最初の '－' 行）の中心座標を求める
line=$(grep '^－' /tmp/coords.txt | head -1)
x=$(echo "$line" | awk '{print $2}'); y=$(echo "$line" | awk '{print $3}')
w=$(echo "$line" | awk '{print $4}'); h=$(echo "$line" | awk '{print $5}')
cx=$((x + w / 2)); cy=$((y + h / 2))

cur() { python3 -c "import json;print(json.load(open('/tmp/demo_state.json'))['current_count'])"; }

echo "[4/4] 実 OS クリック検証 (xdotool)  現在台数 －ボタン中心=($cx,$cy)"
fail=0
check() {  # $1=ラベル $2=実測 $3=期待
  if [ "$2" = "$3" ]; then
    echo "  PASS  $1 -> current=$2"
  else
    echo "  FAIL  $1 -> current=$2 (期待 $3)"
    fail=1
  fi
}

start=$(cur)
[ "$start" = "50" ] || { echo "  初期状態が想定外: current=$start"; fail=1; }

# A) ゆっくり単発クリック ×5  -> 50-5=45
for _ in 1 2 3 4 5; do xdotool mousemove $cx $cy click 1; sleep 0.3; done; sleep 0.4
check "単発クリック x5" "$(cur)" "45"

# B) ダブルクリック ×3（各2クリック=OS のダブルクリック判定が走る）-> 45-6=39
for _ in 1 2 3; do xdotool mousemove $cx $cy click --repeat 2 --delay 30 1; sleep 0.5; done; sleep 0.4
check "ダブルクリック x3 (各2カウント)" "$(cur)" "39"

# C) 高速連打 ×10  -> 39-10=29
for _ in $(seq 1 10); do xdotool mousemove $cx $cy click 1; sleep 0.08; done; sleep 0.5
check "高速連打 x10" "$(cur)" "29"

# D) 取りこぼし0（総クリック 5+6+10=21 がすべて押下ログに残る）
presses=$(grep -c "ボタン押下" /tmp/demo.log || true)
check "押下ログ件数(取りこぼし0)" "$presses" "21"

kill "$APPID" 2>/dev/null || true
echo "----------------------------------------"
if [ "$fail" = "0" ]; then
  echo "✅ 本番相当(Linux/X11)で GUI ボタンは全て正しく動作しました。"
else
  echo "❌ 失敗があります。上記 FAIL を確認してください。"
fi
exit "$fail"
