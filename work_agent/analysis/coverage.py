"""结构化测试场景的确定性覆盖检查。"""

from __future__ import annotations

from collections import defaultdict

from work_agent.analysis.schemas import (
    CoverageGap,
    DomainAnalysisResult,
    DomainTask,
)


def review_coverage(
    tasks: list[DomainTask],
    results: list[DomainAnalysisResult],
) -> list[CoverageGap]:
    """检查每个任务的场景类型和场景必填字段。

    这里刻意使用代码规则而不是让模型自评“是否完整”，从而给不同模型提供
    一致的最低质量下限。它不承诺发现开放世界中的所有业务遗漏。
    """

    result_by_task = {result.task_id: result for result in results}
    gaps: list[CoverageGap] = []

    for task in tasks:
        result = result_by_task.get(task.task_id)
        if result is None:
            for scenario_type in task.required_scenario_types:
                gaps.append(
                    CoverageGap(
                        task_id=task.task_id,
                        domain=task.domain,
                        dimension="scenario_type",
                        missing_item=scenario_type.value,
                        reason="领域分析没有产出结果",
                    )
                )
            continue

        # required_scenario_types 来自规划任务，允许以后按策略矩阵动态变化。
        actual_types = {scenario.scenario_type for scenario in result.scenarios}
        for scenario_type in task.required_scenario_types:
            if scenario_type not in actual_types:
                gaps.append(
                    CoverageGap(
                        task_id=task.task_id,
                        domain=task.domain,
                        dimension="scenario_type",
                        missing_item=scenario_type.value,
                        reason=f"缺少 {scenario_type.value} 类型场景",
                    )
                )

        for scenario in result.scenarios:
            checks = [
                ("precondition", scenario.preconditions, "缺少前置条件"),
                ("steps", scenario.steps, "缺少操作步骤"),
                ("expected_result", scenario.expected_results, "缺少预期结果"),
                ("observation", scenario.observation_points, "缺少观察点"),
                ("evidence", scenario.evidence_refs, "缺少资料依据"),
            ]
            for dimension, value, reason in checks:
                if not value:
                    gaps.append(
                        CoverageGap(
                            task_id=task.task_id,
                            domain=task.domain,
                            dimension=dimension,  # type: ignore[arg-type]
                            missing_item=scenario.scenario_id,
                            reason=reason,
                        )
                    )
    return gaps


def gaps_by_task(gaps: list[CoverageGap]) -> dict[str, list[CoverageGap]]:
    """按原始任务聚合缺口，供定向 Gap Repair 构造补充任务。"""

    grouped: dict[str, list[CoverageGap]] = defaultdict(list)
    for gap in gaps:
        grouped[gap.task_id].append(gap)
    return dict(grouped)
