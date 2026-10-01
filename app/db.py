"""数据库连接与初始化。

通过环境变量 DATABASE_URL 配置，默认指向 docker-compose 中的 db 服务。
应用启动时自动重试等待数据库就绪，并幂等执行 schema.sql。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from psycopg2.pool import ThreadedConnectionPool

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://freq:freq@localhost:5432/freqdb")

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

_pool: ThreadedConnectionPool | None = None


def init_pool(retries: int = 30, delay: float = 1.0) -> None:
    """初始化连接池，等待数据库就绪（供容器编排启动顺序使用）。"""
    global _pool
    last_error: Exception | None = None
    for _ in range(retries):
        try:
            pool = ThreadedConnectionPool(1, 10, dsn=DATABASE_URL)
            conn = pool.getconn()
            pool.putconn(conn)
            _pool = pool
            return
        except Exception as exc:  # noqa: BLE001 - 等待期间任何错误都重试
            last_error = exc
            time.sleep(delay)
    raise RuntimeError(f"cannot connect to database: {last_error}")


def init_schema() -> None:
    conn = _pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    finally:
        _pool.putconn(conn)


def get_conn():
    """FastAPI 依赖：借出一个连接，请求结束后回滚未提交事务并归还。"""
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        conn.rollback()
        _pool.putconn(conn)


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
