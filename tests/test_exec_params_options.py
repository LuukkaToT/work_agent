"""exec_params()：options.debug_mode 结构化抽取 → plans[i]["options"] 透传。"""

from __future__ import annotations

from work_agent.graph.nodes import exec_flow as exec_flow_mod
from work_agent.graph.nodes.exec_flow import (
    ColumnMappingOut,
    ExecParamsOut,
    ExecPlanOut,
    exec_params,
)


class _FakeStructuredLLM:
    def __init__(self, parsed: ExecParamsOut):
        self._parsed = parsed

    def invoke(self, messages):
        return self._parsed


class _FakeFastModel:
    def __init__(self, parsed: ExecParamsOut):
        self._parsed = parsed

    def with_structured_output(self, schema):
        return _FakeStructuredLLM(self._parsed)


def _patch_llm(monkeypatch, parsed: ExecParamsOut) -> None:
    monkeypatch.setattr(
        exec_flow_mod, "get_fast_model", lambda temperature=0: _FakeFastModel(parsed)
    )


def test_exec_params_extracts_debug_mode_true(monkeypatch):
    parsed = ExecParamsOut(
        plans=[
            ExecPlanOut(
                case_names=["HF_20B_PUSCH_001"],
                version="27B",
                env="7.223.50.60",
                options={"debug_mode": True},
            )
        ],
        exec_mode="create_and_start",
    )
    _patch_llm(monkeypatch, parsed)

    out = exec_params({"user_input": "用调测模式跑一下 HF_20B_PUSCH_001"})

    assert out["exec_params"]["plans"][0]["options"] == {"debug_mode": True}


def test_exec_params_defaults_options_to_null_when_not_mentioned(monkeypatch):
    parsed = ExecParamsOut(
        plans=[
            ExecPlanOut(
                case_names=["HF_20B_PUSCH_001"],
                version="27B",
                env="7.223.50.60",
            )
        ],
        exec_mode="create_and_start",
    )
    _patch_llm(monkeypatch, parsed)

    out = exec_params({"user_input": "跑一下 HF_20B_PUSCH_001"})

    assert out["exec_params"]["plans"][0]["options"] == {"debug_mode": None}


def test_exec_params_sheet_path_options_all_none(monkeypatch, tmp_path):
    """表格路径不解析口头 options，应落全 None 默认值。"""
    sheet_path = tmp_path / "cases.csv"
    sheet_path.write_text("case_name,version,env\nHF_20B_PUSCH_001,27B,7.223.50.60\n")

    parsed = ExecParamsOut(
        plans=[],
        exec_mode="create_and_start",
        sheet_path=str(sheet_path),
    )
    mapping = ColumnMappingOut(
        case_name_col=0,
        version_col=1,
        env_col=2,
        confidence="high",
        reason="表头明确",
    )

    class _FakeAnyStructuredLLM:
        def __init__(self, schema):
            self._schema = schema

        def invoke(self, messages):
            if self._schema is ExecParamsOut:
                return parsed
            return mapping

    class _FakeAnyModel:
        def with_structured_output(self, schema):
            return _FakeAnyStructuredLLM(schema)

    monkeypatch.setattr(
        exec_flow_mod, "get_fast_model", lambda temperature=0: _FakeAnyModel()
    )

    out = exec_params({"user_input": f"按表格 {sheet_path} 执行"})

    assert out["exec_params"]["plans"][0]["options"] == {"debug_mode": None}
