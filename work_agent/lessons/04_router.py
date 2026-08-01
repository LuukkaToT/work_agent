"""
在仓库根目录执行：

    python -m work_agent.lessons.04_router
"""

from work_agent.graph.main_graph import build_graph


CASES = [
    "分析一下 256T 规格下行测试",
    "执行用例 case_downlink_001，版本 27B",
    "前面那次用例执行怎么样了？",
    "今天天气怎么样？",
]


def run_one(app, text: str) -> None:
    result = app.invoke(
        {
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
    )
    print("-" * 60)
    print("input :", text)
    print("intent:", result["intent"])
    print("branch:", result["summary"].get("branch"))
    print("audit :", [a["step"] for a in result["audit"]])


def main() -> None:
    app = build_graph()
    for text in CASES:
        run_one(app, text)


if __name__ == "__main__":
    main()