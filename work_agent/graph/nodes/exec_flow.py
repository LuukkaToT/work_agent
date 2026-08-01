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
    """从用户话里抽出参数；版本可吃个人配置默认值，组网不静默填充。"""
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
    """调用 Executor.run；缺参数则 need_input，不真跑。"""
    params = state.get("exec_params") or {}
    case_names = params.get("case_names") or []
    version = params.get("version") or ""
    topology = params.get("topology") or ""

    if not case_names or not topology:
        summary = {
            "status": "need_input",
            "branch": "execute",
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
    """查一次 status；没 run_id（例如缺组网）则跳过。"""
    run_id = state.get("run_id") or ""
    if not run_id:
        return {
            "audit": [{"step": "exec_poll", "skipped": True}],
        }

    # 关键：不要 cache_clear！必须和 exec_run 用同一个 MockExecutor 实例
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
    """返回 continue 继续轮询，done 结束。"""
    if not (state.get("run_id") or ""):
        return "done"

    if state.get("run_status") in ("finished", "failed", "timeout"):
        return "done"

    # 学习阶段先用小上限，避免死循环；以后可改读 profile.poll_max_attempts
    max_attempts = 5
    if int(state.get("poll_count") or 0) >= max_attempts:
        return "done"

    return "continue"