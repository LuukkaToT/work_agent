"""
验证 collect_results + write_report。

    python -m work_agent.lessons.08_report
"""

from pathlib import Path

from work_agent.graph.main_graph import build_graph


CASES = [
    "执行用例 case_downlink_001，版本 27B，组网 topo_a",
    "执行用例 case_downlink_001，版本 27B",
]


def empty_state(text: str) -> dict:
    return {
        "task_id": "",
        "user_input": text,
        "intent": "",
        "requirement": "",
        "cases": [],
        "exec_params": {},
        "run_id": "",
        "run_status": "",
        "poll_count": 0,
        "results": [],
        "logs": "",
        "report_path": "",
        "summary": {},
        "audit": [],
    }


def main() -> None:
    app = build_graph()
    for text in CASES:
        result = app.invoke(empty_state(text))
        report_path = result.get("report_path") or ""
        print("-" * 60)
        print("input :", text)
        print("run_id:", result.get("run_id"))
        print("status:", result.get("run_status"))
        print("results:", result.get("results"))
        print("summary:", result.get("summary"))
        print("report :", report_path)
        print("audit  :", [a["step"] for a in result["audit"]])
        if report_path and Path(report_path).exists():
            print("--- report.md ---")
            print(Path(report_path).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
