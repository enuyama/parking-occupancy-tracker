#!/usr/bin/env bash
#
# 本番ラズパイ相当（Linux/X11）で GUI ボタンの「実クリック」動作を検証する。
#
# macOS の Aqua 版 Tkinter は実マウスのクリック処理が特殊で、ローカルの mac では
# ボタンがうまく反応しないことがある（本番ラズパイには無関係）。このスクリプトは
# Docker で Debian（=Raspberry Pi OS 相当）コンテナを立て、Xvfb 仮想ディスプレイ上に
# GUI を表示し、xdotool で本物の OS クリック（単発・ダブルクリック・高速連打）を
# 自動送出して、現在台数が正しく増減するかを検証する。
#
# 使い方:
#   bash tools/verify_gui_linux.sh
#
# 必要なもの: Docker（Docker Desktop 等）。初回はイメージ取得＋依存導入で1〜2分かかる。
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE="debian:bookworm-slim"

if ! command -v docker >/dev/null 2>&1; then
  echo "docker が見つかりません。Docker Desktop 等をインストール・起動してください。" >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "docker デーモンに接続できません。Docker を起動してから再実行してください。" >&2
  exit 2
fi

echo "本番相当 Linux/X11 (${IMAGE}) で GUI ボタンを実クリック検証します..."
exec docker run --rm -v "$PROJECT_ROOT":/work:ro "$IMAGE" bash /work/tools/_container_gui_test.sh
