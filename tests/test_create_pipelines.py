"""create_pipelines / start_pipelines：批量 create、write-ahead、防双建。"""

from __future__ import annotations

from work_agent.core.config import Profile, Settings, get_settings
from work_agent.graph.nodes import exec_flow as exec_flow_mod
from work_agent.graph.nodes.exec_flow import create_pipelines, start_pipelines
from work_agent.tools.mock.executor import MockPipelineTool
from work_agent.tools.models import PipelineHandle


class _FakeLedger:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.updates: list[dict] = []
        self.replacements: list[dict] = []

    def upsert(self, **kwargs) -> None:
        self.rows.append(kwargs)

    def update_status(self, pipeline_id: str, *, status=None, report_path=None) -> None:
        self.updates.append(
            {
                "pipeline_id": pipeline_id,
                "status": status,
                "report_path": report_path,
            }
        )

    def replace_id(self, old_id: str, new_id: str, *, status: str) -> None:
        self.replacements.append(
            {"old_id": old_id, "new_id": new_id, "status": status}
        )


def _settings_with_retry(attempts: int) -> Settings:
    base = get_settings()
    return Settings(
        llm_base_url=base.llm_base_url,
        llm_api_key=base.llm_api_key or "test-key",
        llm_model=base.llm_model,
        llm_fast_model=base.llm_fast_model,
        llm_reasoning_model=base.llm_reasoning_model,
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
        postgres_dsn=base.postgres_dsn,
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
    return {
        "task_id": "t1",
        "exec_params": {"plans": [plan], "exec_mode": "create_and_start"},
    }


def test_create_pipelines_two_envs(monkeypatch):
    tool = MockPipelineTool(scenario="all_pass", ticks_to_finish=2)
    ledger = _FakeLedger()
    _patch(monkeypatch, tool, ledger)

    out = create_pipelines(
        {
            "task_id": "t1",
            "exec_params": {
                "exec_mode": "create_and_start",
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
                ],
            },
        }
    )

    assert out["summary"]["status"] == "submitted"
    assert out["summary"]["created"] == 2
    assert len(out["pipelines"]) == 2
    assert {p["env"] for p in out["pipelines"]} == {"7.223.50.60", "7.223.60.11"}
    assert all(p["status"] == "running" for p in out["pipelines"])
    pids = [p["pipeline_id"] for p in out["pipelines"]]
    assert len(set(pids)) == 2
    assert all(not pid.startswith("local-") for pid in pids)
    # write-ahead：两条 local creating
    assert len(ledger.rows) == 2
    assert all(r["status"] == "creating" for r in ledger.rows)
    assert all(str(r["pipeline_id"]).startswith("local-") for r in ledger.rows)
    assert len(ledger.replacements) == 2
    assert [u["status"] for u in ledger.updates] == ["running", "running"]


def test_create_pipelines_writes_user_id_into_ledger(monkeypatch):
    """state["user_id"] 应原样落进台账 upsert，而不是继续用空串写入。"""
    tool = MockPipelineTool()
    ledger = _FakeLedger()
    _patch(monkeypatch, tool, ledger)

    state = _one_plan()
    state["user_id"] = "z001"
    create_pipelines(state)

    assert ledger.rows[0]["user_id"] == "z001"


class _RecordingTool:
    """记录每次 create 收到的 options，供断言 debug_mode 解析逻辑。"""

    def __init__(self) -> None:
        self.create_options: list[dict] = []

    def create(self, case_names, version, env, options=None):
        self.create_options.append(dict(options or {}))
        return PipelineHandle(
            pipeline_id="33333333-3333-3333-3333-333333333333",
            case_names=case_names,
            version=version,
            env=env,
        )

    def start(self, pipeline_id):
        return True

    def query(self, pipeline_id):
        raise KeyError(pipeline_id)


def test_create_pipelines_falls_back_to_state_debug_mode_when_not_mentioned(
    monkeypatch,
):
    """本轮未提及 debug_mode（options.debug_mode=None）→ 兜底 state["debug_mode"]。"""
    tool = _RecordingTool()
    ledger = _FakeLedger()
    _patch(monkeypatch, tool, ledger)

    state = _one_plan()
    state["debug_mode"] = True
    create_pipelines(state)

    assert tool.create_options[0]["debug_mode"] is True


def test_create_pipelines_prefers_explicit_turn_override_over_state(monkeypatch):
    """本轮显式提到 debug_mode → 用本轮值，覆盖 state 里的持久偏好。"""
    tool = _RecordingTool()
    ledger = _FakeLedger()
    _patch(monkeypatch, tool, ledger)

    plan = {
        "case_names": ["HF_20B_PUSCH_001"],
        "version": "27B",
        "env": "7.223.50.60",
        "options": {"debug_mode": False},
    }
    state = {
        "task_id": "t1",
        "debug_mode": True,
        "exec_params": {"plans": [plan], "exec_mode": "create_and_start"},
    }
    create_pipelines(state)

    assert tool.create_options[0]["debug_mode"] is False


def test_create_pipelines_defaults_debug_mode_false_without_state(monkeypatch):
    """state 里完全没有 debug_mode 字段时兜底 False，不抛错。"""
    tool = _RecordingTool()
    ledger = _FakeLedger()
    _patch(monkeypatch, tool, ledger)

    create_pipelines(_one_plan())  # 不带 debug_mode

    assert tool.create_options[0]["debug_mode"] is False


def test_create_pipelines_defaults_user_id_empty_when_missing(monkeypatch):
    """没有身份信息时兜底空串，而不是抛错——语义是"过滤不到"而非"看到别人的"。"""
    tool = MockPipelineTool()
    ledger = _FakeLedger()
    _patch(monkeypatch, tool, ledger)

    create_pipelines(_one_plan())  # 不带 user_id

    assert ledger.rows[0]["user_id"] == ""


def test_create_only_skips_start(monkeypatch):
    tool = MockPipelineTool()
    ledger = _FakeLedger()
    _patch(monkeypatch, tool, ledger)

    state = _one_plan()
    state["exec_params"]["exec_mode"] = "create_only"
    out = create_pipelines(state)

    assert out["summary"]["status"] == "created"
    assert out["pipelines"][0]["status"] == "created"
    assert ledger.updates == []  # 未 start，无 running
    assert len(ledger.replacements) == 1


def test_create_pipelines_partial_failure(monkeypatch):
    tool = MockPipelineTool()
    ledger = _FakeLedger()
    _patch(monkeypatch, tool, ledger, retry_attempts=0)

    out = create_pipelines(
        {
            "task_id": "t1",
            "exec_params": {
                "exec_mode": "create_and_start",
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
                ],
            },
        }
    )

    assert out["summary"]["status"] == "partial"
    assert out["summary"]["created"] == 1
    assert out["summary"]["failed_pipelines"] == 1
    statuses = {p["env"]: p["status"] for p in out["pipelines"]}
    assert statuses["7.223.50.60"] == "failed"
    assert statuses["7.223.60.11"] == "running"


def test_write_ahead_before_create(monkeypatch):
    """台账在 create 之前就写入 local creating。"""
    ledger = _FakeLedger()
    seen_at_create: list[str] = []

    class Tool:
        def create(self, case_names, version, env, options=None):
            seen_at_create.extend(r["status"] for r in ledger.rows)
            return PipelineHandle(
                pipeline_id="11111111-1111-1111-1111-111111111111",
                case_names=case_names,
                version=version,
                env=env,
            )

        def start(self, pipeline_id):
            return True

        def query(self, pipeline_id):
            raise KeyError(pipeline_id)

    _patch(monkeypatch, Tool(), ledger)
    out = create_pipelines(_one_plan())
    assert out["pipelines"][0]["status"] == "running"
    assert out["pipelines"][0]["pipeline_id"] == (
        "11111111-1111-1111-1111-111111111111"
    )
    assert seen_at_create == ["creating"]
    assert ledger.rows[0]["pipeline_id"].startswith("local-")
    assert ledger.replacements[0]["new_id"] == (
        "11111111-1111-1111-1111-111111111111"
    )
    assert ledger.updates[-1]["status"] == "running"


def test_create_fail_no_retry(monkeypatch):
    """create 失败不盲目重试，防双建。"""
    ledger = _FakeLedger()

    class Tool:
        def __init__(self) -> None:
            self.create_calls = 0

        def create(self, case_names, version, env, options=None):
            self.create_calls += 1
            raise TimeoutError("http timeout")

        def start(self, pipeline_id):
            raise AssertionError("不应 start")

        def query(self, pipeline_id):
            raise AssertionError("不应 query")

    tool = Tool()
    _patch(monkeypatch, tool, ledger, retry_attempts=3)
    out = create_pipelines(_one_plan())

    assert out["summary"]["status"] == "failed"
    assert out["pipelines"][0]["status"] == "failed"
    assert tool.create_calls == 1
    assert any("不重试" in n for n in out["pipelines"][0]["notes"])
    assert ledger.rows[0]["status"] == "creating"
    assert ledger.updates[-1]["status"] == "failed"
    assert ledger.replacements == []


def test_start_pipelines_retries(monkeypatch):
    """start 可同 pipeline_id 重试。"""
    ledger = _FakeLedger()

    class Tool:
        def __init__(self) -> None:
            self.start_calls = 0

        def create(self, case_names, version, env, options=None):
            raise AssertionError("不应 create")

        def start(self, pipeline_id):
            self.start_calls += 1
            if self.start_calls == 1:
                raise TimeoutError("start timeout")
            return True

        def query(self, pipeline_id):
            raise KeyError(pipeline_id)

    tool = Tool()
    _patch(monkeypatch, tool, ledger, retry_attempts=1)
    out = start_pipelines(
        {
            "pipelines": [
                {
                    "pipeline_id": "22222222-2222-2222-2222-222222222222",
                    "case_names": ["HF_20B_PUSCH_001"],
                    "version": "27B",
                    "env": "7.223.50.60",
                    "status": "created",
                    "error": "",
                }
            ]
        }
    )

    assert out["summary"]["status"] == "submitted"
    assert out["pipelines"][0]["status"] == "running"
    assert tool.start_calls == 2
    assert ledger.updates[-1]["status"] == "running"
