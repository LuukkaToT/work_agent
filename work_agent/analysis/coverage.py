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
    grouped: dict[str, list[CoverageGap]] = defaultdict(list)
    for gap in gaps:
        grouped[gap.task_id].append(gap)
    return dict(grouped)
