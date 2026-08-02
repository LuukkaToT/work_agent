"""执行参数校验与子图 schema；confirm 路由。"""

from work_agent.graph.nodes.exec_flow import (
    ALLOWED_VERSIONS,
    _classify_env,
    _plan_dict,
)
from work_agent.graph.nodes.hitl import route_after_confirm


def test_route_after_confirm_reads_exec_decision():
    assert route_after_confirm({"exec_decision": "cancel"}) == "cancel"
    assert route_after_confirm({"exec_decision": "proceed"}) == "proceed"
    assert route_after_confirm({}) == "proceed"


def test_route_after_confirm_ignores_summary_cancelled():
    assert (
        route_after_confirm(
            {
                "exec_decision": "proceed",
                "summary": {"status": "cancelled"},
            }
        )
        == "proceed"
    )


def test_classify_env_physical_ip():
    assert _classify_env("7.223.50.60") == "physical"
    assert _classify_env("192.168.1.1") == "physical"


def test_classify_env_logical():
    assert _classify_env("3BBL_86_1BBL86") == "logical"
    assert _classify_env("1BBL_85_1BBL_8021") == "logical"


def test_classify_env_empty():
    assert _classify_env("") == ""
    assert _classify_env("  ") == ""


def test_plan_dict_marks_missing_env():
    plan = _plan_dict(case_names=["HF_20B_PUSCH_001"], version="27B", env="")
    assert plan["missing"] == ["env"]
    assert plan["env_kind"] == ""


def test_plan_dict_logical_env_is_missing():
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="3BBL_86_1BBL86",
    )
    assert plan["env_kind"] == "logical"
    assert "env" in plan["missing"]


def test_plan_dict_invalid_version():
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="99Z",
        env="7.223.50.60",
    )
    assert "version" in plan["missing"]


def test_allowed_versions():
    assert ALLOWED_VERSIONS == frozenset({"27B", "27A", "26B", "26A"})


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
    assert private == {"exec_decision"}
    assert "audit" not in ExecFlowInput.__annotations__
    assert "audit" in ExecFlowOutput.__annotations__
    assert "pipelines" in ExecFlowOutput.__annotations__
    assert "dialogue_summary" in ExecFlowInput.__annotations__
    assert "poll_count" not in ExecFlowState.__annotations__
