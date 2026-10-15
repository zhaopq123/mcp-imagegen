#!/bin/bash
# MCP 图片生成 Server · 启动脚本（proot 容器内运行）
# 由 Termux 层 tmux 守护调用；环境变量从 /etc/mcp-imagegen/env 注入
set -eu

ENVFILE=/etc/mcp-imagegen/env
TARGET=/opt/mcp-imagegen

if [ -r "$ENVFILE" ]; then
  set -a
  . "$ENVFILE"
  set +a
else
  echo "[mcp] 无法读取 $ENVFILE（权限不足？）" >&2
  exit 2
fi

cd "$TARGET"
echo "[mcp] 启动 $(date '+%F %T')  port=$PORT  image_dir=$IMAGE_DIR"
exec ./venv/bin/python server.py
