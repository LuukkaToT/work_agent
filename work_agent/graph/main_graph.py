"""
主图组装：只负责「有哪些节点、边怎么连」，业务逻辑在 nodes/ 里。

当前执行分支完整路径：
  START → intake → router
                 ├─ analysis → END          （桩）
                 ├─ execute → exec_params → exec_run → exec_poll ⟲
                 │                              └→ collect → report → END
                 ├─ query → END             （桩）
                 └─ chat → END              （桩）
"""

import uuid

from langgraph.graph import END, START, StateGraph

from work_agent.graph.nodes.branches import do_analysis, do_chat, do_query
from work_agent.graph.nodes.exec_flow import (
    exec_params,
    exec_poll,
    exec_run,
    route_after_poll,
)
from work_agent.graph.nodes.report import collect_results, write_report
from work_agent.graph.nodes.router import route_by_intent, router
from work_agent.graph.state import TestFlowState


def intake(state: TestFlowState) -> dict:
    """入口节点：生成 task_id，写下第一条 audit。"""
    task_id = state.get("task_id") or str(uuid.uuid4())[:8]
    return {
        "task_id": task_id,
        "audit": [
            {
                "step": "intake",
                "task_id": task_id,
                "user_input": state["user_input"],
            }
        ],
    }


def build_graph():
    # StateGraph(状态类型)：声明这张图上流动的数据结构
    graph = StateGraph(TestFlowState)

    # ---------- 注册节点：名字 → 函数 ----------
    graph.add_node("intake", intake)
    graph.add_node("router", router)
    graph.add_node("analysis", do_analysis)
    graph.add_node("exec_params", exec_params)
    graph.add_node("exec_run", exec_run)
    graph.add_node("exec_poll", exec_poll)
    graph.add_node("collect_results", collect_results)
    graph.add_node("write_report", write_report)
    graph.add_node("query", do_query)
    graph.add_node("chat", do_chat)

    # ---------- 固定边：A 做完一定去 B ----------
    graph.add_edge(START, "intake")
    graph.add_edge("intake", "router")

    # ---------- 条件边：根据路由函数返回值选下一条路 ----------
    # route_by_intent 返回 "execute" 时，走到 exec_params（不是旧的桩节点）
    graph.add_conditional_edges(
        "router",
        route_by_intent,
        {
            "analysis": "analysis",
            "execute": "exec_params",
            "query": "query",
            "chat": "chat",
        },
    )

    graph.add_edge("analysis", END)
    graph.add_edge("exec_params", "exec_run")
    graph.add_edge("exec_run", "exec_poll")

    # 自循环：route_after_poll 返回 "continue" 就再进 exec_poll
    # 返回 "done" 则去收结果（缺参跳过执行时也会走这里，collect 内部会 skip）
    graph.add_conditional_edges(
        "exec_poll",
        route_after_poll,
        {
            "continue": "exec_poll",
            "done": "collect_results",
        },
    )
    graph.add_edge("collect_results", "write_report")
    graph.add_edge("write_report", END)
    graph.add_edge("query", END)
    graph.add_edge("chat", END)

    # compile：把定义编译成可 invoke / stream 的可运行对象
    return graph.compile()
