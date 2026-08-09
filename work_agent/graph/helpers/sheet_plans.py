"""从用例表 + 列映射组装执行计划（确定性切片，不改用例名原文）。"""

from __future__ import annotations

from typing import Any

from work_agent.tools.models import SheetTable

_SHEET_ROW_CAP = 200


def apply_column_mapping(
    table: SheetTable,
    *,
    case_name_col: int,
    version_col: int | None,
    env_col: int | None,
    row_limit: int | None,
    spoken_env: str,
    spoken_version: str,
) -> list[dict[str, Any]]:
    """
    按列下标从表格取原文，组装粗 plans（尚未跑 _plan_dict 校验）。

    优先级：
      env: spoken_env > 表内 env 列
      version: 表内 version 列（按行）> spoken_version
      条数: row_limit 非空行；上限 _SHEET_ROW_CAP
    """
    width = len(table.headers)
    if case_name_col < 0 or case_name_col >= width:
        raise ValueError(f"用例名列下标越界: {case_name_col}")

    limit = _SHEET_ROW_CAP if row_limit is None else max(0, int(row_limit))
    # 按「环境+版本」分组合并 case_names
    groups: dict[tuple[str, str], list[str]] = {}
    taken = 0

    for row in table.rows:
        if taken >= limit:
            break
        if case_name_col >= len(row):
            continue
        name = (row[case_name_col] or "").strip()
        if not name:
            continue

        version = spoken_version
        if version_col is not None and 0 <= version_col < len(row):
            cell = (row[version_col] or "").strip()
            if cell:
                version = cell.upper() if len(cell) <= 8 else cell

        env = spoken_env
        if not env and env_col is not None and 0 <= env_col < len(row):
            env = (row[env_col] or "").strip()

        key = (version, env)
        groups.setdefault(key, []).append(name)
        taken += 1

    plans: list[dict[str, Any]] = []
    for (version, env), names in groups.items():
        plans.append(
            {
                "case_names": names,
                "version": version,
                "env": env,
            }
        )
    return plans


def mapping_prompt_payload(table: SheetTable, sample_rows: int = 8) -> str:
    lines = ["表头（下标从 0 起）:"]
    for i, h in enumerate(table.headers):
        lines.append(f"  [{i}] {h}")
    lines.append("样本行:")
    for r in table.rows[:sample_rows]:
        lines.append("  " + " | ".join(r))
    return "\n".join(lines)
