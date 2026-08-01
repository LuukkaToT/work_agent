"""
在仓库根目录执行：

    python -m work_agent.lessons.06_exec
"""

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
        "analysis_path": "",
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
        print("-" * 60)
        print("input :", text)
        print("intent:", result["intent"])
        print("params:", result["exec_params"])
        print("run_id:", result["run_id"])
        print("summary:", result["summary"])
        print("audit :", [a["step"] for a in result["audit"]])


if __name__ == "__main__":
    main()