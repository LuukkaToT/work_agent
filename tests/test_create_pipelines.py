"""create_pipelines：批量创建、write-ahead、超时对账与同 ID 重试。"""

from __future__ import annotations

from work_agent.core.config import Profile, Settings, get_settings
from work_agent.graph.nodes import exec_flow as exec_flow_mod
from work_agent.graph.nodes.exec_flow import create_pipelines
from work_agent.tools.mock.executor import MockPipelineTool
from work_agent.tools.models import PipelineHandle, PipelineResult


class _FakeLedger:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.updates: list[dict] = []

    def upsert(self, **kwargs) -> None:
        self.rows.append(kwargs)

    def update_status(self, run_id: str, *, status=None, report_path=None) -> None:
        self.updates.append(
            {"run_id": run_id, "status": status, "report_path": report_path}
        )


def _settings_with_retry(attempts: int) -> Settings:
    base = get_settings()
    return Settings(
        llm_base_url=base.llm_base_url,
        llm_api_key=base.llm_api_key or "test-key",
        llm_model=base.llm_model,
        llm_temperature=base.llm_temperature,
        llm_timeout=base.llm_timeout,
        llm_max_retries=base.llm_max_retries,
        tool_backend=base.tool_backend,
        profile=Profile(
            default_version=base.profile.default_version,
            frequent_topologies=list(base.profile.frequent_topologies),
            poll_interval_seconds=base.profile.poll_interval_seconds,
            poll_max_attempts=base.profile.poll_max_attempts,
            create_retry_attempts=attempts,
        ),
        workspace_dir=base.workspace_dir,
        profile_path=base.profile_path,
        checkpoint_path=base.checkpoint_path,
    )


def _patch(monkeypatch, tool, ledger, *, retry_attempts: int = 1) -> None:
    def getter(scenario="all_pass"):
        return tool

    getter.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(exec_flow_mod, "get_pipeline_tool", getter)
    monkeypatch.setattr(exec_flow_mod, "get_ledger", lambda: ledger)
    monkeypatch.setattr(
        exec_flow_mod, "get_settings", lambda: _settings_with_retry(retry_attempts)
    )


def _one_plan(**overrides):
    plan = {
        "case_names": ["HF_20B_PUSCH_001"],
        "version": "27B",
        "env": "7.223.50.60",
    }
    plan.update(overrides)
    return {"task_id": "t1", "exec_params": {"plans": [plan]}}


def test_create_pipelines_two_envs(monkeypatch):
    tool = MockPipelineTool(scenario="all_pass", ticks_to_finish=2)
    ledger = _FakeLedger()
    _patch(monkeypatch, tool, ledger)

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
    # write-ahead：两条 creating
    assert len(ledger.rows) == 2
    assert all(r["status"] == "creating" for r in ledger.rows)
    assert [u["status"] for u in ledger.updates] == ["running", "running"]


def test_create_pipelines_partial_failure(monkeypatch):
    tool = MockPipelineTool()
    ledger = _FakeLedger()
    _patch(monkeypatch, tool, ledger, retry_attempts=0)

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


def test_write_ahead_before_init(monkeypatch):
    """台账在 init 之前就写入 creating。"""
    ledger = _FakeLedger()
    seen_at_init: list[str] = []

    class Tool:
        def init_pipline(self, run_id, case_names, version, env):
            seen_at_init.extend(r["status"] for r in ledger.rows)
            return PipelineHandle(run_id, case_names, version, env)

        def check_pipline(self, run_id):
            return True

        def query_result(self, run_id):
            raise KeyError(run_id)

    _patch(monkeypatch, Tool(), ledger)
    out = create_pipelines(_one_plan())
    assert out["pipelines"][0]["status"] == "running"
    assert seen_at_init == ["creating"]
    assert ledger.rows[0]["status"] == "creating"
    assert ledger.updates[-1]["status"] == "running"


def test_timeout_but_already_created(monkeypatch):
    """init 超时但服务端已建：对账成功，不重复 init。"""
    ledger = _FakeLedger()
    created: dict[str, PipelineHandle] = {}

    class Tool:
        def __init__(self) -> None:
            self.init_calls = 0

        def init_pipline(self, run_id, case_names, version, env):
            self.init_calls += 1
            created[run_id] = PipelineHandle(run_id, case_names, version, env)
            raise TimeoutError("http timeout")

        def check_pipline(self, run_id):
            assert run_id in created
            return True

        def query_result(self, run_id):
            if run_id not in created:
                raise KeyError(run_id)
            return PipelineResult(run_id=run_id, phase="pending", message="ok")

    tool = Tool()
    _patch(monkeypatch, tool, ledger, retry_attempts=3)
    out = create_pipelines(_one_plan())

    assert out["pipelines"][0]["status"] == "running"
    assert tool.init_calls == 1  # 对账成功，不再重试 init
    assert any("对账发现已创建" in n for n in out["pipelines"][0]["notes"])
    assert ledger.updates[-1]["status"] == "running"


def test_retry_same_run_id_when_not_created(monkeypatch):
    """init 失败且未创建：同 run_id 重试成功。"""
    ledger = _FakeLedger()
    run_ids_seen: list[str] = []

    class Tool:
        def __init__(self) -> None:
            self.init_calls = 0

        def init_pipline(self, run_id, case_names, version, env):
            self.init_calls += 1
            run_ids_seen.append(run_id)
            if self.init_calls == 1:
                raise TimeoutError("timeout")
            return PipelineHandle(run_id, case_names, version, env)

        def check_pipline(self, run_id):
            return True

        def query_result(self, run_id):
            raise KeyError(run_id)

    tool = Tool()
    _patch(monkeypatch, tool, ledger, retry_attempts=1)
    out = create_pipelines(_one_plan())

    assert out["pipelines"][0]["status"] == "running"
    assert tool.init_calls == 2
    assert len(set(run_ids_seen)) == 1  # 同一 run_id
    assert ledger.updates[-1]["status"] == "running"


def test_retries_exhausted_marks_failed(monkeypatch):
    """重试用尽 → failed，台账经历 creating → failed。"""
    ledger = _FakeLedger()

    class Tool:
        def __init__(self) -> None:
            self.init_calls = 0

        def init_pipline(self, run_id, case_names, version, env):
            self.init_calls += 1
            raise TimeoutError("always timeout")

        def check_pipline(self, run_id):
            raise AssertionError("不应走到 check")

        def query_result(self, run_id):
            raise KeyError(run_id)

    tool = Tool()
    _patch(monkeypatch, tool, ledger, retry_attempts=1)
    out = create_pipelines(_one_plan())

    assert out["summary"]["status"] == "failed"
    assert out["pipelines"][0]["status"] == "failed"
    assert tool.init_calls == 2  # 首次 + 1 次重试
    assert ledger.rows[0]["status"] == "creating"
    assert ledger.updates[-1]["status"] == "failed"
