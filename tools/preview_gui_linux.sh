#!/usr/bin/env bash
#
# 本番ラズパイ相当（Linux/X11）の GUI を、Xvfb + noVNC で「ブラウザに出して実際に操作」する。
#
# Docker コンテナ内で Xvfb 仮想ディスプレイにアプリを表示し、noVNC(ブラウザVNC) で配信する。
# 起動後、ホストのブラウザで http://localhost:6080/vnc.html を開くと、その GUI を
# 自分のマウスでクリックして動作確認できる（Linux/X11 上の本物の操作になる）。
#
# 使い方:
#   bash tools/preview_gui_linux.sh
#   起動したら ブラウザで http://localhost:6080/vnc.html を開き、[Connect] を押す。
#   （パスワード不要。macOS の「画面共有」アプリは不要）
#   終了は このターミナルで Ctrl-C。
#
# 注意: ローカル開発用。localhost:6080 にパスワード無しで出る（VNC 自体は内部のみ）。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE="debian:bookworm-slim"

if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "docker が見つからない/起動していません。Docker を起動してください。" >&2
  exit 2
fi

echo "起動準備中... 起動後、ブラウザで http://localhost:6080/vnc.html を開いて [Connect]。"
exec docker run --rm -p 6080:6080 -v "$PROJECT_ROOT":/work:ro "$IMAGE" bash /work/tools/_container_preview.sh
