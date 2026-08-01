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