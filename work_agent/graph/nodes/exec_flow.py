"""
执行流三个节点 + 一个路由函数：

  exec_params  从自然语言抽出 用例名/版本/组网
  exec_run     调 Executor.run 提交任务（异步语义：只拿 run_id）
  exec_poll    调 Executor.status 查一次进度
  route_after_poll  决定是再 poll，还是去做 collect

还没有 interrupt：缺组网时只写 need_input 并跳过真正执行。
"""

from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from work_agent.core.config import get_settings
from work_agent.core.llm import get_chat_model
from work_agent.graph.state import TestFlowState
from work_agent.tools.registry import get_case_provider, get_executor


class ExecParamsOut(BaseModel):
    case_names: list[str] = Field(description="要执行的用例名列表")
    version: Optional[str] = Field(default=None, description="版本，如 27B")
    topology: Optional[str] = Field(default=None, description="逻辑组网，如 topo_a")


def exec_params(state: TestFlowState) -> dict:
    """
    参数优先级（与架构约定一致）：
    - version：用户没说 → 用 profile.default_version
    - topology：用户没说 → 留空，后面不允许静默填（跑错组网代价高）
    """
    llm = get_chat_model(temperature=0).with_structured_output(ExecParamsOut)
    parsed: ExecParamsOut = llm.invoke(
        [
            SystemMessage(
                content=(
                    "从用户输入提取执行参数。"
                    "用例名通常类似 case_xxx。"
                    "没提到的字段返回 null，不要编造组网。"
                )
            ),
            HumanMessage(content=state["user_input"]),
        ]
    )

    profile = get_settings().profile
    version = parsed.version or profile.default_version
    topology = parsed.topology or ""

    params = {
        "case_names": parsed.case_names,
        "version": version,
        "topology": topology,
    }

    # 顺便用 CaseProvider 校验/补全用例元信息（标题、标签）
    cases: list[dict] = []
    if parsed.case_names:
        provider = get_case_provider()
        infos = provider.fetch_cases(parsed.case_names)
        cases = [{"name": c.name, "title": c.title, "tags": c.tags} for c in infos]

    return {
        "exec_params": params,
        "cases": cases,
        "audit": [{"step": "exec_params", "params": params}],
    }


def exec_run(state: TestFlowState) -> dict:
    """
    提交执行。注意：这里不是「跑完」，只是拿到 run_id。
    缺 case_names 或 topology → need_input，不调用 tool。
    """
    params = state.get("exec_params") or {}
    case_names = params.get("case_names") or []
    version = params.get("version") or ""
    topology = params.get("topology") or ""

    if not case_names or not topology:
        summary = {
            "status": "need_input",
            "branch": "execute",
            # missing：告诉上游/用户缺了啥（以后 interrupt 会用到）
            "missing": [
                k
                for k, ok in [
                    ("case_names", bool(case_names)),
                    ("topology", bool(topology)),
                ]
                if not ok
            ],
            "exec_params": params,
        }
        return {
            "summary": summary,
            "audit": [{"step": "exec_run", "status": "need_input"}],
        }

    # 新任务清一下缓存，拿到干净的 MockExecutor
    get_executor.cache_clear()
    ex = get_executor(scenario="all_pass")
    handle = ex.run(
        case_names=case_names,
        version=version,
        topology=topology,
    )

    return {
        "run_id": handle.run_id,
        "run_status": "pending",
        "summary": {
            "status": "submitted",
            "branch": "execute",
            "run_id": handle.run_id,
            "exec_params": params,
            "cases": state.get("cases") or [],
        },
        "audit": [
            {
                "step": "exec_run",
                "run_id": handle.run_id,
                "version": version,
                "topology": topology,
            }
        ],
    }


def exec_poll(state: TestFlowState) -> dict:
    """
    查一次 status。

    为什么要循环：真实执行是分钟级，run 只提交；
    mock 用 ticks_to_finish=2 模拟「第 1 次还在跑，第 2 次才完成」。
    """
    run_id = state.get("run_id") or ""
    if not run_id:
        # 前面 need_input 没提交：跳过，让路由走 done → collect
        return {
            "audit": [{"step": "exec_poll", "skipped": True}],
        }

    # 关键：这里不能 cache_clear！
    # Mock 把 run 存在进程内的 Executor 实例上；清缓存会换新实例，旧 run_id 就丢了。
    ex = get_executor(scenario="all_pass")
    st = ex.status(run_id)
    poll_count = int(state.get("poll_count") or 0) + 1

    summary = dict(state.get("summary") or {})
    summary.update(
        {
            "status": st.phase,
            "branch": "execute",
            "run_id": run_id,
            "progress": st.progress,
            "message": st.message,
            "poll_count": poll_count,
        }
    )

    return {
        "poll_count": poll_count,
        "run_status": st.phase,
        "summary": summary,
        "audit": [
            {
                "step": "exec_poll",
                "phase": st.phase,
                "poll_count": poll_count,
                "progress": st.progress,
            }
        ],
    }


def route_after_poll(state: TestFlowState) -> str:
    """
    条件边返回值：
    - continue → 再进 exec_poll（自循环）
    - done     → 去 collect_results
    """
    if not (state.get("run_id") or ""):
        return "done"

    if state.get("run_status") in ("finished", "failed", "timeout"):
        return "done"

    # 刹车：防止 status 一直 running 时死循环（商用会读 profile.poll_max_attempts）
    max_attempts = 5
    if int(state.get("poll_count") or 0) >= max_attempts:
        return "done"

    return "continue"
