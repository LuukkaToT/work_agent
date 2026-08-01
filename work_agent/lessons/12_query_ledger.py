"""
M14：台账 + query_run。

    python -m work_agent.lessons.12_query_ledger
"""

from __future__ import annotations

from work_agent.core.ledger import get_ledger
from work_agent.runtime import run_turn


def main() -> None:
    # 1) 先跑一次完整执行（自动回答 HITL）
    answers = iter(["topo_a", "yes"])

    def ask(_prompt: str) -> str:
        return next(answers)

    print("=== 先执行一条用例，写入台账 ===")
    exec_result = run_turn(
        "执行用例 case_downlink_001，版本 27B",
        ask=ask,
        with_checkpoint=True,
    )
    run_id = exec_result.get("run_id")
    print("run_id:", run_id)
    print("ledger recent:", get_ledger().list_recent(3))

    # 2) 新 thread 问「上次怎么样了」——靠台账，不靠旧 thread
    print("\n=== 新会话查询：前面那次执行怎么样了？ ===")
    q = run_turn(
        "前面那次用例执行怎么样了？",
        ask=None,
        with_checkpoint=True,
    )
    print("intent :", q.get("intent"))
    print("summary:", q.get("summary"))
    print("audit  :", [a.get("step") for a in q.get("audit") or []])

    # 3) 按 run_id 精确查
    if run_id:
        print("\n=== 按 run_id 查询 ===")
        q2 = run_turn(f"查一下 {run_id} 的结果", ask=None, with_checkpoint=True)
        print("summary:", q2.get("summary"))


if __name__ == "__main__":
    main()
