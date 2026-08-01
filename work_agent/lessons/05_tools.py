"""
在仓库根目录执行：

    python -m work_agent.lessons.05_tools
"""

from __future__ import annotations

from work_agent.tools.mock import MockScenario
from work_agent.tools.protocols import CaseProvider, Executor
from work_agent.tools.registry import get_case_provider, get_executor


def demo_cases(provider: CaseProvider) -> None:
    print("=== CaseProvider ===")
    print("all :", [c.name for c in provider.list_cases()])
    print("q=downlink:", [c.name for c in provider.list_cases("downlink")])
    fetched = provider.fetch_cases(["case_downlink_001", "case_uplink_001"])
    print("fetch:", [(c.name, c.title) for c in fetched])
    print("isinstance Protocol:", isinstance(provider, CaseProvider))


def demo_executor(scenario: MockScenario) -> None:
    print(f"\n=== Executor scenario={scenario} ===")
    get_executor.cache_clear()
    ex: Executor = get_executor(scenario=scenario)
    print("isinstance Protocol:", isinstance(ex, Executor))

    handle = ex.run(
        case_names=["case_downlink_001", "case_downlink_002"],
        version="27B",
        topology="topo_a",
    )
    print("run_id:", handle.run_id)

    while True:
        st = ex.status(handle.run_id)
        print(f"  status: {st.phase} progress={st.progress:.2f} {st.message}")
        if st.phase in ("finished", "failed", "timeout"):
            break

    if st.phase == "finished":
        for r in ex.results(handle.run_id):
            print(
                f"  result: {r.case_name} {r.verdict} "
                f"kind={r.fail_kind} {r.detail}"
            )
    print("  logs snippet:")
    print("  " + ex.logs(handle.run_id).replace("\n", "\n  "))


def main() -> None:
    demo_cases(get_case_provider())
    for scenario in ("all_pass", "version_fail", "case_error", "env_error"):
        demo_executor(scenario)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()