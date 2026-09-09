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
    """
    把单个容量 ID 收成小写 ``0x`` 十六进制。

    参数:
        value: 原始字符串或可转 str 的值。

    返回:
        规范化后的 ID。

    异常:
        ValueError: 不是 ``0x`` + 十六进制。
    """
    text = str(value or "").strip().lower()
    if not _CAPACITY_ID_RE.fullmatch(text):
        raise ValueError(f"非法容量 ID: {value!r}")
    return text


def normalize_capacity_ids(values: Iterable[object]) -> tuple[str, ...]:
    """
    规范化一组容量 ID：去重、排序，得到稳定主键。

    参数:
        values: 任意可迭代的容量 ID。

    返回:
        排序后的元组。

    异常:
        ValueError: 组为空，或其中任一项非法。
    """
    normalized = sorted({normalize_capacity_id(value) for value in values})
    if not normalized:
        raise ValueError("容量 ID 组不能为空")
    return tuple(normalized)


def extract_capacity_ids(text: str) -> tuple[str, ...]:
    """
    从自由文本里抽出容量 ID 并规范化。

    参数:
        text: 用户话术或日志片段。

    返回:
        抽出的 ID 组；没有命中则空元组。
    """
    found = _CAPACITY_ID_IN_TEXT_RE.findall(text or "")
    return normalize_capacity_ids(found) if found else ()


@dataclass(frozen=True)
class CapacityGroupCandidate:
    """相似容量组候选：ID 组与双向相似度。"""

    capacity_ids: tuple[str, ...]
    score: float

    def as_choice(self) -> dict[str, Any]:
        """收成前端/工具可选的 JSON：``capacity_ids`` 与四位小数 ``score``。"""
        return {"capacity_ids": list(self.capacity_ids), "score": round(self.score, 4)}


def lookup_capacity_topologies(capacity_ids: Iterable[object]) -> list[LogicTopologyRecord]:
    """
    按精确容量 ID 组查已映射的逻辑组网。

    参数:
        capacity_ids: 容量 ID 组；会先规范化。

    返回:
        命中的逻辑组网记录，按名称与约束排序。
    """
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
    """两组 ID 的双向平均字符串相似度，给近似推荐用。"""

    def directional(source: tuple[str, ...], target: tuple[str, ...]) -> float:
        return sum(
            max(SequenceMatcher(None, item, candidate).ratio() for candidate in target)
            for item in source
        ) / len(source)

    return (directional(left, right) + directional(right, left)) / 2


def find_similar_capacity_groups(
    capacity_ids: Iterable[object], *, limit: int = 5, min_score: float = 0.6
) -> list[CapacityGroupCandidate]:
    """
    在已落库映射里找与给定组相近、但不是同一组的容量 ID 组。

    参数:
        capacity_ids: 查询组。
        limit: 最多返回条数；至少 1。
        min_score: 相似度下限。

    返回:
        按分数降序、ID 组升序的候选列表。
    """
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
    """
    把 ``config/capacity_topology_mappings.json`` 灌进库；已有映射跳过。

    参数:
        conn: 已打开的 Postgres 连接（须能 ``execute``）。
        json_path: 覆盖种子文件；默认仓库配置路径。文件不存在则返回 0。

    返回:
        尝试写入的映射条数（含因冲突未插入的尝试次数）。

    异常:
        ValueError: JSON 不是数组、组没有组网，或引用了不存在的逻辑组网。
    """
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
