"""从结构化结果确定性渲染测试分析 Markdown。"""

from __future__ import annotations

from work_agent.analysis.schemas import (
    CoverageGap,
    DomainAnalysisResult,
    RequirementFact,
)


def _cell(value: object) -> str:
    text = str(value or "").replace("\n", "<br>")
    return text.replace("|", "\\|")


def _list(values: list[str]) -> str:
    return "<br>".join(_cell(value) for value in values) if values else "待确认"


def render_markdown(
    requirement: RequirementFact,
    results: list[DomainAnalysisResult],
    gaps: list[CoverageGap],
) -> str:
    lines = [
        f"# 测试分析：{requirement.title}",
        "",
        "> 本报告由结构化测试分析流程生成。资料不足或无法确认的内容均标记为待确认。",
        "",
        "## 1. 需求事实与范围",
        "",
        f"- 原始需求：{requirement.raw_requirement or requirement.summary}",
        f"- 需求摘要：{requirement.summary}",
        f"- 产品版本：{requirement.product_version or '待确认'}",
        f"- 3GPP Release：{requirement.release or '待确认'}",
        f"- 方向：{', '.join(requirement.directions) or '待确认'}",
        f"- 涉及信道：{', '.join(requirement.channels) or '待确认'}",
        f"- 涉及特性：{', '.join(requirement.features) or '待确认'}",
        "",
        "### 假设与约束",
        "",
    ]
    lines.extend(f"- {item}" for item in requirement.constraints + requirement.assumptions)
    if not requirement.constraints and not requirement.assumptions:
        lines.append("- 暂无；产品版本、组网和参数范围仍需结合实际需求确认。")

    lines.extend(["", "## 2. 分域分析摘要", ""])
    lines.append("| 领域 | 信道 | 状态 | 场景数 | 摘要 |")
    lines.append("|---|---|---|---:|---|")
    for result in results:
        lines.append(
            "| "
            + " | ".join(
                [
                    _cell(result.domain),
                    _cell(", ".join(result.channels) or "通用"),
                    _cell(result.status),
                    str(len(result.scenarios)),
                    _cell(result.summary),
                ]
            )
            + " |"
        )

    lines.extend(["", "## 3. 测试场景", ""])
    for result in results:
        lines.extend(
            [
                f"### {result.domain}",
                "",
                "| ID | 类型 | 优先级 | 场景 | 前置条件 | 操作步骤 | 预期结果 | 观察点 | 依据 |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
        )
        if not result.scenarios:
            lines.append("| - | - | - | 未生成场景 | - | - | - | - | - |")
        for scenario in result.scenarios:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(scenario.scenario_id),
                        scenario.scenario_type.value,
                        scenario.priority,
                        _cell(scenario.title),
                        _list(scenario.preconditions),
                        _list(scenario.steps),
                        _list(scenario.expected_results),
                        _list(scenario.observation_points),
                        _list(scenario.evidence_refs),
                    ]
                )
                + " |"
            )
        if result.missing_topics:
            lines.extend(
                [
                    "",
                    "**资料缺口：** "
                    + "；".join(_cell(item) for item in result.missing_topics),
                ]
            )
        if result.warnings:
            lines.extend(
                ["", "**告警：** " + "；".join(_cell(item) for item in result.warnings)]
            )
        lines.append("")

    lines.extend(["## 4. 覆盖检查", ""])
    if not gaps:
        lines.append("- 已通过当前代码规则定义的最低覆盖检查。")
    else:
        lines.extend(
            [
                "| 领域 | 维度 | 缺失项 | 原因 |",
                "|---|---|---|---|",
            ]
        )
        for gap in gaps:
            lines.append(
                f"| {_cell(gap.domain)} | {_cell(gap.dimension)} | "
                f"{_cell(gap.missing_item)} | {_cell(gap.reason)} |"
            )

    lines.extend(["", "## 5. 资料依据", ""])
    evidence_by_id = {}
    for result in results:
        for hit in result.evidence:
            evidence_by_id[hit.chunk_id] = hit
    if not evidence_by_id:
        lines.append("- 未检索到可引用资料，本报告只能作为待补充的初步分析。")
    else:
        for hit in evidence_by_id.values():
            channel = f" / {hit.channel}" if hit.channel else ""
            lines.append(
                f"- `{hit.chunk_id}`：{hit.title} / {hit.section}"
                f"（{hit.source_kind}{channel}，`{hit.relative_path}`）"
            )

    lines.extend(["", "## 6. 待确认问题", ""])
    pending = list(requirement.missing_information)
    for result in results:
        pending.extend(result.missing_topics)
    pending = list(dict.fromkeys(item for item in pending if item))
    if pending:
        lines.extend(f"{index}. {item}" for index, item in enumerate(pending, 1))
    else:
        lines.append("1. 无额外待确认项。")
    return "\n".join(lines)
