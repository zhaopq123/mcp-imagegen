#!/bin/bash
set -a
. /etc/mcp-imagegen/env
set +a
export UPSTREAM_BASE_URL=http://127.0.0.1:8001
export PORT=8002
export MCPACCESS_TOKEN=stresstoken
export UPSTREAM_TOKEN=mock
export IMAGE_DIR=/tmp/stress-images
export VIDEO_DIR=/tmp/stress-videos
export UPSTREAM_RPS=50
export UPSTREAM_BURST=32
export IMAGE_MAX_CONCURRENCY=1
export IMAGE_BATCH_CONCURRENCY=1
export VIDEO_MAX_CONCURRENCY=1
mkdir -p /tmp/stress-images /tmp/stress-videos
cd /opt/mcp-imagegen
exec ./venv/bin/python server.py
