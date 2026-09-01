"""数据库迁移、最终结构校验、目录种子与 checkpoint 初始化。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from work_agent.core.config import get_settings, project_root

_SCHEMA_PATH = project_root() / "sql" / "schema.sql"
_MIGRATIONS_DIR = project_root() / "sql" / "migrations"


def _migration_files() -> list[Path]:
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def latest_schema_version() -> str:
    files = _migration_files()
    return files[-1].stem if files else ""


def _statements(sql_text: str) -> list[str]:
    out: list[str] = []
    for raw in sql_text.split(";"):
        lines = [
            line
            for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        statement = "\n".join(lines).strip()
        if statement:
            out.append(statement)
    return out


def _execute_sql_file(conn: Any, path: Path) -> None:
    for statement in _statements(path.read_text(encoding="utf-8")):
        conn.execute(statement)


def _bootstrap_migration_table(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )


def _apply_migrations(conn: Any) -> None:
    _bootstrap_migration_table(conn)
    applied = {
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for path in _migration_files():
        if path.stem in applied:
            continue
        with conn.transaction():
            _execute_sql_file(conn, path)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,)
            )


def seed_catalogs(conn: Any) -> tuple[int, int]:
    from work_agent.core.capacity_mappings import seed_capacity_mappings
    from work_agent.core.logic_topologies import seed_logic_topologies

    topology_count = seed_logic_topologies(conn)
    mapping_count = seed_capacity_mappings(conn)
    return topology_count, mapping_count


def _pool(dsn: str) -> ConnectionPool:
    pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=4,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    )
    pool.open(wait=True, timeout=10)
    return pool


def migrate_database(dsn: str, *, setup_checkpointer: bool = True) -> None:
    if not (dsn or "").strip():
        raise RuntimeError("未提供 Postgres DSN")
    pool = _pool(dsn)
    try:
        with pool.connection() as conn:
            _apply_migrations(conn)
            _execute_sql_file(conn, _SCHEMA_PATH)
            seed_catalogs(conn)
        if setup_checkpointer:
            PostgresSaver(pool).setup()
    finally:
        pool.close()


def init_database(dsn: str) -> None:
    """保留原入口；内部统一走版本化迁移。"""
    migrate_database(dsn, setup_checkpointer=True)


def verify_schema_and_seed(dsn: str | None = None) -> None:
    """API 启动时只校验迁移版本并幂等同步 JSON 种子，不执行迁移。"""
    database_dsn = (dsn or get_settings().postgres_dsn or "").strip()
    if not database_dsn:
        raise RuntimeError("API 启动缺少 POSTGRES_DSN")
    pool = _pool(database_dsn)
    try:
        with pool.connection() as conn:
            _bootstrap_migration_table(conn)
            version = latest_schema_version()
            row = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=%s", (version,)
            ).fetchone()
            if version and not row:
                raise RuntimeError(
                    f"数据库未升级到 {version}，请先运行部署迁移脚本"
                )
            seed_catalogs(conn)
    finally:
        pool.close()
