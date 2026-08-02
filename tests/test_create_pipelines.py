"""create_pipelines：批量创建、单条失败不阻断。"""

from work_agent.graph.nodes import exec_flow as exec_flow_mod
from work_agent.graph.nodes.exec_flow import create_pipelines
from work_agent.tools.mock.executor import MockPipelineTool


class _FakeLedger:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def upsert(self, **kwargs) -> None:
        self.rows.append(kwargs)


def _patch_tool(monkeypatch, tool):
    def getter(scenario="all_pass"):
        return tool

    getter.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(exec_flow_mod, "get_pipeline_tool", getter)
    monkeypatch.setattr(exec_flow_mod, "get_ledger", lambda: _FakeLedger())


def test_create_pipelines_two_envs(monkeypatch):
    tool = MockPipelineTool(scenario="all_pass", ticks_to_finish=2)
    ledger = _FakeLedger()

    def getter(scenario="all_pass"):
        return tool

    getter.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(exec_flow_mod, "get_pipeline_tool", getter)
    monkeypatch.setattr(exec_flow_mod, "get_ledger", lambda: ledger)

    out = create_pipelines(
        {
            "task_id": "t1",
            "exec_params": {
                "plans": [
                    {
                        "case_names": ["HF_20B_PUSCH_001"],
                        "version": "27B",
                        "env": "7.223.50.60",
                    },
                    {
                        "case_names": ["TDD_26a_85_5002_KPI"],
                        "version": "26A",
                        "env": "7.223.60.11",
                    },
                ]
            },
        }
    )

    assert out["summary"]["status"] == "submitted"
    assert out["summary"]["created"] == 2
    assert len(out["pipelines"]) == 2
    assert {p["env"] for p in out["pipelines"]} == {"7.223.50.60", "7.223.60.11"}
    assert all(p["status"] == "running" for p in out["pipelines"])
    assert len(ledger.rows) == 2


def test_create_pipelines_partial_failure(monkeypatch):
    tool = MockPipelineTool()
    ledger = _FakeLedger()

    def getter(scenario="all_pass"):
        return tool

    getter.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(exec_flow_mod, "get_pipeline_tool", getter)
    monkeypatch.setattr(exec_flow_mod, "get_ledger", lambda: ledger)

    out = create_pipelines(
        {
            "task_id": "t1",
            "exec_params": {
                "plans": [
                    {
                        "case_names": ["bad"],  # 太短，流水线拒绝
                        "version": "27B",
                        "env": "7.223.50.60",
                    },
                    {
                        "case_names": ["HF_20B_PUSCH_001"],
                        "version": "27B",
                        "env": "7.223.60.11",
                    },
                ]
            },
        }
    )

    assert out["summary"]["status"] == "partial"
    assert out["summary"]["created"] == 1
    assert out["summary"]["failed_pipelines"] == 1
    statuses = {p["env"]: p["status"] for p in out["pipelines"]}
    assert statuses["7.223.50.60"] == "failed"
    assert statuses["7.223.60.11"] == "running"
