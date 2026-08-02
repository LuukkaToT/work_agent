"""执行子图路由：confirm 读 exec_decision；poll 上限读 profile。"""

from work_agent.core.config import Profile, Settings, get_settings
from work_agent.graph.nodes import exec_flow as exec_flow_mod
from work_agent.graph.nodes.exec_flow import route_after_poll
from work_agent.graph.nodes.hitl import route_after_confirm


def _settings_with_poll_max(max_attempts: int) -> Settings:
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
            poll_max_attempts=max_attempts,
        ),
        workspace_dir=base.workspace_dir,
        profile_path=base.profile_path,
        checkpoint_path=base.checkpoint_path,
    )


def test_route_after_confirm_reads_exec_decision():
    assert route_after_confirm({"exec_decision": "cancel"}) == "cancel"
    assert route_after_confirm({"exec_decision": "proceed"}) == "proceed"
    # 未设置时默认继续（confirm 节点正常会写 proceed）
    assert route_after_confirm({}) == "proceed"


def test_route_after_confirm_ignores_summary_cancelled():
    """取消决定不再借道 summary.status。"""
    assert (
        route_after_confirm(
            {
                "exec_decision": "proceed",
                "summary": {"status": "cancelled"},
            }
        )
        == "proceed"
    )


def test_route_after_poll_done_when_no_run_id():
    assert route_after_poll({}) == "done"
    assert route_after_poll({"run_id": ""}) == "done"


def test_route_after_poll_done_on_terminal_status():
    for phase in ("finished", "failed", "timeout"):
        assert (
            route_after_poll({"run_id": "r1", "run_status": phase, "poll_count": 1})
            == "done"
        )


def test_route_after_poll_continue_while_running(monkeypatch):
    monkeypatch.setattr(
        exec_flow_mod, "get_settings", lambda: _settings_with_poll_max(5)
    )
    assert (
        route_after_poll(
            {"run_id": "r1", "run_status": "running", "poll_count": 2}
        )
        == "continue"
    )


def test_route_after_poll_respects_profile_max(monkeypatch):
    monkeypatch.setattr(
        exec_flow_mod, "get_settings", lambda: _settings_with_poll_max(3)
    )
    assert (
        route_after_poll(
            {"run_id": "r1", "run_status": "running", "poll_count": 3}
        )
        == "done"
    )
    assert (
        route_after_poll(
            {"run_id": "r1", "run_status": "running", "poll_count": 2}
        )
        == "continue"
    )


def test_exec_flow_schemas_hide_private_fields():
    from work_agent.graph.subgraphs.exec_flow import (
        ExecFlowInput,
        ExecFlowOutput,
        ExecFlowState,
    )

    private = (
        set(ExecFlowState.__annotations__)
        - set(ExecFlowInput.__annotations__)
        - set(ExecFlowOutput.__annotations__)
    )
    assert private == {"cases", "exec_decision", "poll_count"}
    assert "audit" not in ExecFlowInput.__annotations__
    assert "audit" in ExecFlowOutput.__annotations__
