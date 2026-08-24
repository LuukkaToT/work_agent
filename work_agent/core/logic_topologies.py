"""
逻辑组网目录：校验白名单，并按特征筛 HITL 候选。

只走 Postgres。表由 ``python -m work_agent init-db`` 创建，运行时不建表。
``config/logic_topologies.csv`` 是导入种子，运行时权威数据在库里。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import Any

from work_agent.core.config import project_root
from work_agent.core.db import get_pool

_CSV_PATH = project_root() / "config" / "logic_topologies.csv"
_CANDIDATE_LIMIT = 8


@dataclass(frozen=True)
class LogicTopologyRecord:
    """目录里的一条逻辑组网（环境 + 约束）。"""

    logic_env: str
    logic_constraint: str
    bbh_count: int
    bbl_count: int
    bbh_board: str
    bbl_board: str
    aliases: str

    def as_choice(self) -> dict[str, Any]:
        """HITL 选项用的精简 dict。"""
        return {
            "logic_env": self.logic_env,
            "logic_constraint": self.logic_constraint,
            "bbh_count": self.bbh_count,
            "bbl_count": self.bbl_count,
            "bbh_board": self.bbh_board,
            "bbl_board": self.bbl_board,
        }


def _row_to_record(row: Any) -> LogicTopologyRecord:
    return LogicTopologyRecord(
        logic_env=row["logic_env"] or "",
        logic_constraint=row["logic_constraint"] or "",
        bbh_count=int(row["bbh_count"] or 0),
        bbl_count=int(row["bbl_count"] or 0),
        bbh_board=row["bbh_board"] or "",
        bbl_board=row["bbl_board"] or "",
        aliases=row["aliases"] or "",
    )


def lookup_logic_topology(
    logic_env: str, logic_constraint: str
) -> LogicTopologyRecord | None:
    """
    按 (logic_env, logic_constraint) 精确查一条。

    参数:
        logic_env: 规范逻辑环境名。
        logic_constraint: 规范约束编码。

    返回:
        命中返回记录；任一侧为空或未命中返回 ``None``。
    """
    env = (logic_env or "").strip()
    constraint = (logic_constraint or "").strip()
    if not env or not constraint:
        return None
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT logic_env, logic_constraint, bbh_count, bbl_count,
                   bbh_board, bbl_board, aliases
            FROM logic_topologies
            WHERE logic_env=%s AND logic_constraint=%s
            """,
            (env, constraint),
        ).fetchone()
    if not row:
        return None
    return _row_to_record(row)


def is_valid_logic_topology(logic_env: str, logic_constraint: str) -> bool:
    """规范 (env, constraint) 是否在目录里。"""
    return lookup_logic_topology(logic_env, logic_constraint) is not None


def find_logic_topology_candidates(
    *,
    bbh_count: int | None = None,
    bbl_count: int | None = None,
    bbh_board: str = "",
    bbl_board: str = "",
    text: str = "",
    limit: int = _CANDIDATE_LIMIT,
) -> list[LogicTopologyRecord]:
    """
    按结构 / 板型 / 别名筛候选，供校验失败时 HITL 点选。

    没有有效过滤条件时返回空列表，避免整表倾倒。
    """
    board_h = (bbh_board or "").strip().upper()
    board_l = (bbl_board or "").strip().upper()
    needle = (text or "").strip()
    has_filter = any(
        [
            bbh_count is not None,
            bbl_count is not None,
            board_h,
            board_l,
            needle,
        ]
    )
    if not has_filter:
        return []

    clauses = ["TRUE"]
    params: list[Any] = []
    if bbh_count is not None:
        clauses.append("bbh_count=%s")
        params.append(int(bbh_count))
    if bbl_count is not None:
        clauses.append("bbl_count=%s")
        params.append(int(bbl_count))
    if board_h:
        clauses.append("upper(bbh_board)=%s")
        params.append(board_h)
    if board_l:
        clauses.append("upper(bbl_board)=%s")
        params.append(board_l)
    if needle:
        like = f"%{needle}%"
        clauses.append(
            "(aliases ILIKE %s OR logic_env ILIKE %s OR logic_constraint ILIKE %s)"
        )
        params.extend([like, like, like])
    params.append(max(1, int(limit)))

    sql = f"""
        SELECT logic_env, logic_constraint, bbh_count, bbl_count,
               bbh_board, bbl_board, aliases
        FROM logic_topologies
        WHERE {" AND ".join(clauses)}
        ORDER BY logic_env, logic_constraint
        LIMIT %s
    """
    with get_pool().connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_record(r) for r in rows]


def seed_logic_topologies(conn: Any, csv_path: Any | None = None) -> int:
    """
    从 CSV upsert 种子行。已有同行则更新特征列，不删库里多出来的真实数据。

    参数:
        conn: 已打开的 psycopg 连接（autocommit 即可）。
        csv_path: 覆盖默认 ``config/logic_topologies.csv``。

    返回:
        upsert 的行数。
    """
    path = csv_path or _CSV_PATH
    path = path if hasattr(path, "open") else project_root() / str(path)
    if not path.exists():
        return 0
    count = 0
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            env = (raw.get("logic_env") or "").strip()
            constraint = (raw.get("logic_constraint") or "").strip()
            if not env or not constraint:
                continue
            conn.execute(
                """
                INSERT INTO logic_topologies (
                    logic_env, logic_constraint, bbh_count, bbl_count,
                    bbh_board, bbl_board, aliases
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (logic_env, logic_constraint) DO UPDATE SET
                    bbh_count = EXCLUDED.bbh_count,
                    bbl_count = EXCLUDED.bbl_count,
                    bbh_board = EXCLUDED.bbh_board,
                    bbl_board = EXCLUDED.bbl_board,
                    aliases = EXCLUDED.aliases
                """,
                (
                    env,
                    constraint,
                    int(raw.get("bbh_count") or 0),
                    int(raw.get("bbl_count") or 0),
                    (raw.get("bbh_board") or "").strip(),
                    (raw.get("bbl_board") or "").strip(),
                    (raw.get("aliases") or "").strip(),
                ),
            )
            count += 1
    return count
