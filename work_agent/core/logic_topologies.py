"""逻辑组网目录：一个逻辑组网由名称和约束共同组成。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from work_agent.core.config import project_root
from work_agent.core.db import get_pool

_JSON_PATH = project_root() / "config" / "logic_topologies.json"
_CANDIDATE_LIMIT = 8


def _normalize_boards(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict) or not isinstance(config.get("boards"), list):
        raise ValueError("逻辑组网 config 必须包含 boards 数组")
    boards: list[dict[str, Any]] = []
    for raw in config["boards"]:
        if not isinstance(raw, dict):
            raise ValueError("boards 中的每一项必须是对象")
        board_type = str(raw.get("type") or "").strip().upper()
        model = str(raw.get("model") or "").strip().upper()
        try:
            count = int(raw.get("count"))
        except (TypeError, ValueError) as exc:
            raise ValueError("板型 count 必须是正整数") from exc
        if not board_type or not model or count <= 0:
            raise ValueError("板型 type/model 不能为空，count 必须大于 0")
        boards.append({"type": board_type, "model": model, "count": count})
    return {"boards": boards}


@dataclass(frozen=True)
class LogicTopologyRecord:
    """完整逻辑组网；名称和约束不可拆开单独代表一个组网。"""

    id: int
    name: str
    constraint: str
    config: dict[str, Any]
    aliases: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        return f"{self.name},{self.constraint}"

    # 兼容真实流水线适配器仍使用的公司字段名。
    @property
    def logic_env(self) -> str:
        return self.name

    @property
    def logic_constraint(self) -> str:
        return self.constraint

    def _boards(self, board_type: str) -> list[dict[str, Any]]:
        wanted = board_type.upper()
        return [
            b for b in self.config.get("boards", [])
            if str(b.get("type") or "").upper() == wanted
        ]

    def board_count(self, board_type: str) -> int:
        return sum(int(b.get("count") or 0) for b in self._boards(board_type))

    def board_models(self, board_type: str) -> tuple[str, ...]:
        return tuple(str(b.get("model") or "") for b in self._boards(board_type))

    @property
    def bbh_count(self) -> int:
        return self.board_count("BBH")

    @property
    def bbl_count(self) -> int:
        return self.board_count("BBL")

    @property
    def bbh_board(self) -> str:
        return "+".join(self.board_models("BBH"))

    @property
    def bbl_board(self) -> str:
        return "+".join(self.board_models("BBL"))

    def as_choice(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "constraint": self.constraint,
            "display_name": self.display_name,
            "config": self.config,
            # 兼容旧前端/测试一段时间；业务文案不再把二者称为两个组网字段。
            "logic_env": self.name,
            "logic_constraint": self.constraint,
        }


def record_from_row(row: Any) -> LogicTopologyRecord:
    raw_config = row["config"] or {"boards": []}
    if isinstance(raw_config, str):
        raw_config = json.loads(raw_config)
    return LogicTopologyRecord(
        id=int(row["id"]),
        name=row["name"] or "",
        constraint=row["topology_constraint"] or "",
        config=dict(raw_config),
        aliases=tuple(row["aliases"] or ()),
    )


def lookup_logic_topology(name: str, constraint: str) -> LogicTopologyRecord | None:
    topology_name = (name or "").strip()
    topology_constraint = (constraint or "").strip()
    if not topology_name or not topology_constraint:
        return None
    with get_pool().connection() as conn:
        row = conn.execute(
            """
            SELECT id, name, constraint_value AS topology_constraint, config, aliases
            FROM logic_topologies
            WHERE name=%s AND constraint_value=%s
            """,
            (topology_name, topology_constraint),
        ).fetchone()
    return record_from_row(row) if row else None


def lookup_logic_topology_by_id(topology_id: int) -> LogicTopologyRecord | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT id, name, constraint_value AS topology_constraint, config, aliases FROM logic_topologies WHERE id=%s",
            (int(topology_id),),
        ).fetchone()
    return record_from_row(row) if row else None


def is_valid_logic_topology(name: str, constraint: str) -> bool:
    return lookup_logic_topology(name, constraint) is not None


def find_logic_topology_candidates(
    *,
    bbh_count: int | None = None,
    bbl_count: int | None = None,
    bbh_board: str = "",
    bbl_board: str = "",
    text: str = "",
    limit: int = _CANDIDATE_LIMIT,
) -> list[LogicTopologyRecord]:
    filters_present = any(
        (bbh_count is not None, bbl_count is not None, bbh_board, bbl_board, text)
    )
    if not filters_present:
        return []
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, name, constraint_value AS topology_constraint, config, aliases FROM logic_topologies ORDER BY name, constraint_value"
        ).fetchall()
    needle = (text or "").strip().casefold()
    wanted_bbh = (bbh_board or "").strip().upper()
    wanted_bbl = (bbl_board or "").strip().upper()
    out: list[LogicTopologyRecord] = []
    for row in rows:
        rec = record_from_row(row)
        if bbh_count is not None and rec.bbh_count != int(bbh_count):
            continue
        if bbl_count is not None and rec.bbl_count != int(bbl_count):
            continue
        if wanted_bbh and wanted_bbh not in rec.board_models("BBH"):
            continue
        if wanted_bbl and wanted_bbl not in rec.board_models("BBL"):
            continue
        haystack = " ".join((rec.name, rec.constraint, *rec.aliases)).casefold()
        if needle and needle not in haystack:
            continue
        out.append(rec)
        if len(out) >= max(1, int(limit)):
            break
    return out


def seed_logic_topologies(conn: Any, json_path: str | Path | None = None) -> int:
    path = Path(json_path) if json_path else _JSON_PATH
    if not path.exists():
        return 0
    raw_items = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_items, list):
        raise ValueError("logic_topologies.json 顶层必须是数组")
    count = 0
    for raw in raw_items:
        name = str(raw.get("name") or "").strip()
        constraint = str(raw.get("constraint") or "").strip()
        if not name or not constraint:
            raise ValueError("逻辑组网 name/constraint 不能为空")
        config = _normalize_boards(raw.get("config"))
        aliases = tuple(
            dict.fromkeys(str(x).strip() for x in raw.get("aliases", []) if str(x).strip())
        )
        conn.execute(
            """
            INSERT INTO logic_topologies (name, constraint_value, config, aliases)
            VALUES (%s, %s, %s::jsonb, %s)
            ON CONFLICT (name, constraint_value) DO UPDATE SET
                config=excluded.config,
                aliases=excluded.aliases
            """,
            (name, constraint, json.dumps(config, ensure_ascii=False), list(aliases)),
        )
        count += 1
    return count
