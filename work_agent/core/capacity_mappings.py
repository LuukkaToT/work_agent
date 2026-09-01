"""容量 ID 组到逻辑组网的映射、精确查询和相似组推荐。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from work_agent.core.config import project_root
from work_agent.core.db import get_pool
from work_agent.core.logic_topologies import LogicTopologyRecord, record_from_row

_JSON_PATH = project_root() / "config" / "capacity_topology_mappings.json"
_CAPACITY_ID_RE = re.compile(r"^0x[0-9a-f]+$", re.IGNORECASE)
_CAPACITY_ID_IN_TEXT_RE = re.compile(r"0x[0-9a-f]+", re.IGNORECASE)


def normalize_capacity_id(value: object) -> str:
    text = str(value or "").strip().lower()
    if not _CAPACITY_ID_RE.fullmatch(text):
        raise ValueError(f"非法容量 ID: {value!r}")
    return text


def normalize_capacity_ids(values: Iterable[object]) -> tuple[str, ...]:
    normalized = sorted({normalize_capacity_id(value) for value in values})
    if not normalized:
        raise ValueError("容量 ID 组不能为空")
    return tuple(normalized)


def extract_capacity_ids(text: str) -> tuple[str, ...]:
    found = _CAPACITY_ID_IN_TEXT_RE.findall(text or "")
    return normalize_capacity_ids(found) if found else ()


@dataclass(frozen=True)
class CapacityGroupCandidate:
    capacity_ids: tuple[str, ...]
    score: float

    def as_choice(self) -> dict[str, Any]:
        return {"capacity_ids": list(self.capacity_ids), "score": round(self.score, 4)}


def lookup_capacity_topologies(capacity_ids: Iterable[object]) -> list[LogicTopologyRecord]:
    group = normalize_capacity_ids(capacity_ids)
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT lt.id, lt.name, lt.constraint_value AS topology_constraint,
                   lt.config, lt.aliases
            FROM capacity_topology_mappings AS m
            JOIN logic_topologies AS lt ON lt.id=m.logic_topology_id
            WHERE m.capacity_ids=%s
            ORDER BY lt.name, lt.constraint_value
            """,
            (list(group),),
        ).fetchall()
    return [record_from_row(row) for row in rows]


def _group_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    def directional(source: tuple[str, ...], target: tuple[str, ...]) -> float:
        return sum(
            max(SequenceMatcher(None, item, candidate).ratio() for candidate in target)
            for item in source
        ) / len(source)

    return (directional(left, right) + directional(right, left)) / 2


def find_similar_capacity_groups(
    capacity_ids: Iterable[object], *, limit: int = 5, min_score: float = 0.6
) -> list[CapacityGroupCandidate]:
    wanted = normalize_capacity_ids(capacity_ids)
    with get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT capacity_ids FROM capacity_topology_mappings"
        ).fetchall()
    candidates: list[CapacityGroupCandidate] = []
    for row in rows:
        group = normalize_capacity_ids(row["capacity_ids"] or ())
        if group == wanted:
            continue
        score = _group_similarity(wanted, group)
        if score >= min_score:
            candidates.append(CapacityGroupCandidate(group, score))
    candidates.sort(key=lambda item: (-item.score, item.capacity_ids))
    return candidates[: max(1, int(limit))]


def seed_capacity_mappings(conn: Any, json_path: str | Path | None = None) -> int:
    path = Path(json_path) if json_path else _JSON_PATH
    if not path.exists():
        return 0
    raw_items = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_items, list):
        raise ValueError("capacity_topology_mappings.json 顶层必须是数组")
    count = 0
    for raw in raw_items:
        group = normalize_capacity_ids(raw.get("capacity_ids") or ())
        topologies = raw.get("topologies") or []
        if not topologies:
            raise ValueError(f"容量组 {group!r} 没有映射逻辑组网")
        for topology in topologies:
            name = str(topology.get("name") or "").strip()
            constraint = str(topology.get("constraint") or "").strip()
            row = conn.execute(
                "SELECT id FROM logic_topologies WHERE name=%s AND constraint_value=%s",
                (name, constraint),
            ).fetchone()
            if not row:
                raise ValueError(f"容量映射引用了不存在的逻辑组网: {name},{constraint}")
            conn.execute(
                """
                INSERT INTO capacity_topology_mappings (capacity_ids, logic_topology_id)
                VALUES (%s, %s)
                ON CONFLICT (capacity_ids, logic_topology_id) DO NOTHING
                """,
                (list(group), int(row["id"])),
            )
            count += 1
    return count
