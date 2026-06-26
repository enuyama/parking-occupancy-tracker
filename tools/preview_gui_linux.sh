#!/usr/bin/env bash
#
# 本番ラズパイ相当（Linux/X11）の GUI を、Xvfb + VNC で「画面に出して実際に操作」する。
#
# Docker コンテナ内で Xvfb 仮想ディスプレイにアプリを表示し、x11vnc で配信する。
# 起動後、macOS から VNC で接続すると、その GUI を自分のマウスでクリックして
# 動作確認できる（Linux/X11 上の本物の操作になる）。
#
# 使い方:
#   bash tools/preview_gui_linux.sh
#   起動したら macOS の Finder で  Cmd+K → サーバ「vnc://localhost:5900」 で接続。
#   （または「画面共有」アプリでホスト localhost:5900。パスワードなし）
#   終了は このターミナルで Ctrl-C。
#
# 注意: ローカル開発用。VNC はパスワード無しで localhost:5900 に出る。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE="debian:bookworm-slim"

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "docker が見つからない/起動していません。Docker を起動してください。" >&2
  exit 2
fi

echo "起動準備中... 起動後、macOS から vnc://localhost:5900 に接続してください。"
exec docker run --rm -p 5900:5900 -v "$PROJECT_ROOT":/work:ro "$IMAGE" bash /work/tools/_container_preview.sh
