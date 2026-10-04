#!/bin/sh
set -e

# 启动即幂等建表（init_db），随后拉起 uvicorn；单条 docker compose up 完成全部步骤
exec uvicorn app.main:app \
    --host "${SOP_HOST:-0.0.0.0}" \
    --port "${SOP_PORT:-8080}"
