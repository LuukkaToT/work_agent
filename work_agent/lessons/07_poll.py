"""
在仓库根目录执行：

    python -m work_agent.lessons.07_poll
"""

from work_agent.graph.main_graph import build_graph


CASES = [
    "执行用例 case_downlink_001，版本 27B，组网 topo_a",
    "执行用例 case_downlink_001，版本 27B",  # 缺组网，应跳过 poll
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
        poll_steps = [a for a in result["audit"] if a.get("step") == "exec_poll"]
        print("-" * 60)
        print("input :", text)
        print("run_id:", result["run_id"])
        print("run_status:", result["run_status"])
        print("poll_count:", result["poll_count"])
        print("summary:", result["summary"])
        print("audit steps:", [a["step"] for a in result["audit"]])
        print("poll details:", poll_steps)


if __name__ == "__main__":
    main()