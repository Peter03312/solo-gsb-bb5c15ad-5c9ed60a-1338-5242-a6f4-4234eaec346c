"""服务端配置：全部通过环境变量注入，均有默认值。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _get(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    # SQLite 数据库文件路径；:/memory: 之外的路径会在启动时自动建表
    db_path: str = _get("SOP_DB_PATH", "/data/sop.db")
    # HTTP 监听地址 / 端口
    host: str = _get("SOP_HOST", "0.0.0.0")
    port: int = int(_get("SOP_PORT", "8080"))
    # SQLite 写锁等待（毫秒）
    busy_timeout_ms: int = int(_get("SOP_BUSY_TIMEOUT_MS", "5000"))


settings = Settings()
