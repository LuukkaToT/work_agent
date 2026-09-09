"""打满 ReAct 历史预算的脚本化 A/B：固定 8 步轨迹，不调真 LLM。

短任务 20 条 golden 打不中 20k 封顶。本组用旧四场景的 8000 行日志 + ERROR 噪声，
按同一条 fetch/grep 轨迹走真实 ``run_diagnosis``，token 按发送字符 / 4 估算。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from work_agent.eval.runner import EvalCase
from work_agent.graph.nodes.error_analysis import ErrorAnalysisOut, run_diagnosis
import work_agent.graph.nodes.error_analysis as ea

FEATURE_BY_SCENARIO = {
    "case_error": "KeyError",
    "version_fail": "mismatch",
    "env_error": "Connection refused",
    "all_pass": "verdict=pass",
}

STRESS_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        case_id="stress_case_error",
        scenario="case_error",
        user_input="用例失败，定位 KeyError。",
        pipeline={"case_names": ["CaseA_235T_nmimo"], "version": "27B", "physical_env": "7.223.50.60"},
        expected_fail_kind="case",
        expected_root_component="unknown",
        expected_evidence_keys=["KeyError", "antenna_map"],
    ),
    EvalCase(
        case_id="stress_version_fail",
        scenario="version_fail",
        user_input="协议对不上，是不是版本问题。",
        pipeline={"case_names": ["CaseA_235T_nmimo"], "version": "27B", "physical_env": "7.223.50.60"},
        expected_fail_kind="version",
        expected_root_component="unknown",
        expected_evidence_keys=["mismatch"],
    ),
    EvalCase(
        case_id="stress_env_error",
        scenario="env_error",
        user_input="连不上环境。",
        pipeline={"case_names": ["CaseA_235T_nmimo"], "version": "27B", "physical_env": "7.223.50.60"},
        expected_fail_kind="env",
        expected_root_component="unknown",
        expected_evidence_keys=["Connection refused", "retries"],
    ),
    EvalCase(
        case_id="stress_all_pass",
        scenario="all_pass",
        user_input="这条看起来过了，确认一下。",
        pipeline={"case_names": ["CaseA_235T_nmimo"], "version": "27B", "physical_env": "7.223.50.60"},
        expected_fail_kind="none",
        expected_root_component="none",
        expected_evidence_keys=["verdict=pass"],
    ),
)


def _prompt_chars(messages: list) -> int:
    total = 0
    for message in messages:
        content = getattr(message, "content", "") or ""
        total += len(content) if isinstance(content, str) else len(str(content))
    return total


def _usage(messages: list, *, output_tokens: int = 16) -> dict[str, int]:
    prompt = max(1, _prompt_chars(messages) // 4)
    return {
        "input_tokens": prompt,
        "output_tokens": output_tokens,
        "total_tokens": prompt + output_tokens,
    }


def tool_script(pipeline_id: str, feature: str) -> list[AIMessage]:
    """8 步：重复 fetch + 多路 grep，把 20k 历史打满且抽取侧不会被去重成一块。"""

    def fetch(step: str) -> AIMessage:
        return AIMessage(
            content=f"取日志 {step}",
            tool_calls=[{
                "id": step,
                "name": "fetch_logs",
                "args": {"pipeline_id": pipeline_id, "tail_lines": 400},
            }],
        )

    def grep(step: str, pattern: str) -> AIMessage:
        return AIMessage(
            content=f"检索 {pattern}",
            tool_calls=[{
                "id": step,
                "name": "grep_logs",
                "args": {
                    "pipeline_id": pipeline_id,
                    "pattern": pattern,
                    "max_matches": 40,
                },
            }],
        )

    return [
        fetch("1"),
        fetch("2"),
        grep("3", "ERROR"),
        grep("4", "noise_seq"),
        grep("5", feature),
        fetch("6"),
        grep("7", "ERROR"),
        grep("8", feature),
    ]


class ScriptedReasoning:
    """按固定 8 步吐 tool_calls；用量随当次发送字符估算。"""

    def __init__(self, pipeline_id: str, feature: str) -> None:
        self._steps = tool_script(pipeline_id, feature)
        self.n = 0

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        self.n += 1
        meta = _usage(messages)
        if self.n <= len(self._steps):
            src = self._steps[self.n - 1]
            return AIMessage(content=src.content, tool_calls=src.tool_calls, usage_metadata=meta)
        return AIMessage(content="根据目前日志给出结论", usage_metadata=meta)


class ScriptedFast:
    """抽取返回 golden 类别；压缩器若被调用则保留特征词。accuracy 在本组无信息量。"""

    def __init__(self, *, fail_kind: str, root_component: str, feature: str) -> None:
        self.fail_kind = fail_kind
        self.root_component = root_component
        self.feature = feature

    def with_structured_output(self, schema, include_raw: bool = False, method: str = ""):  # noqa: ANN001
        assert include_raw
        return self

    def invoke(self, messages):  # noqa: ANN001
        human = ""
        if messages:
            human = getattr(messages[-1], "content", "") or ""
        if isinstance(human, str) and human.lstrip().startswith("{"):
            return AIMessage(
                content=f"摘要保留 {self.feature}",
                usage_metadata={"input_tokens": 200, "output_tokens": 70, "total_tokens": 270},
            )
        return {
            "raw": AIMessage(content="", usage_metadata=_usage(messages, output_tokens=40)),
            "parsed": ErrorAnalysisOut(
                fail_kind=self.fail_kind,
                root_component=self.root_component,
                evidence=self.feature,
                conclusion="脚本化抽取",
                suggestion="无需真模型",
            ),
            "parsing_error": None,
        }


def install_scripted_models(monkeypatch, *, pipeline_id: str, case: EvalCase) -> None:
    """把诊断用的推理/快模型换成脚本，供 run_suite(diagnose=...) 注入。"""
    feature = FEATURE_BY_SCENARIO[case.scenario]
    reasoning = ScriptedReasoning(pipeline_id, feature)
    fast = ScriptedFast(
        fail_kind=case.expected_fail_kind,
        root_component=case.expected_root_component or "unknown",
        feature=feature,
    )
    monkeypatch.setattr(ea, "get_reasoning_model", lambda **kw: reasoning)
    monkeypatch.setattr(ea, "get_fast_model", lambda **kw: fast)


def make_scripted_diagnose(monkeypatch, case: EvalCase):
    """每次调用新建脚本模型，避免 legacy/managed 共用计数器。"""

    def diagnose(**kwargs):
        pipelines = kwargs["pipelines"]
        pipeline_id = str(pipelines[0]["pipeline_id"])
        install_scripted_models(monkeypatch, pipeline_id=pipeline_id, case=case)
        return run_diagnosis(
            pipelines=pipelines,
            user_input=str(kwargs.get("user_input") or case.user_input),
            context_strategy=kwargs.get("context_strategy") or "managed",
            scenario=str(kwargs.get("scenario") or case.scenario),
            run_id=str(kwargs.get("run_id") or ""),
        )

    return diagnose
