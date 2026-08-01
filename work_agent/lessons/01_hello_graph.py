"""
在仓库根目录 D:\\Project\\work_agent 执行：

    python -m work_agent.lessons.01_hello_graph
"""

from work_agent.graph.main_graph import build_graph


def main() -> None:
    app = build_graph()
    result = app.invoke(
        {
            "task_id": "",
            "requirement": "分析一下 256T 规格下行测试",
            "summary": {},
            "audit": [],
        }
    )

    print("=== 最终 state ===")
    print(result)
    print()
    print("task_id :", result["task_id"])
    print("summary :", result["summary"])
    print("audit   :", result["audit"])
    print("audit 条数应为 2:", len(result["audit"]))


if __name__ == "__main__":
    main()