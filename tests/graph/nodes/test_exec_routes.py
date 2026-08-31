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


def test_plan_dict_logical_env_without_constraint_is_missing():
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="3BBL_86_1BBL86",
    )
    assert plan["env_kind"] == "logical"
    assert "env" in plan["missing"]
    assert plan["logic_constraint"] == ""


def test_plan_dict_logical_env_with_constraint_is_complete():
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="3BBL_86_1BBL86",
        logic_constraint="85+86",
    )
    assert plan["env_kind"] == "logical"
    assert plan["missing"] == []
    assert plan["logic_constraint"] == "85+86"


def test_plan_dict_physical_clears_constraint():
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="27B",
        env="7.223.50.60",
        logic_constraint="85+86",
    )
    assert plan["env_kind"] == "physical"
    assert plan["logic_constraint"] == ""
    assert plan["missing"] == []


def test_plan_dict_invalid_version():
    plan = _plan_dict(
        case_names=["HF_20B_PUSCH_001"],
        version="99Z",
        env="7.223.50.60",
    )
    assert "version" in plan["missing"]


def test_allowed_versions():
    assert ALLOWED_VERSIONS == frozenset({"27B", "27A", "26B", "26A"})


def test_pipeline_ops_schemas_hide_private_fields():
    from work_agent.graph.subgraphs.pipeline_ops import (
        PipelineOpsInput,
        PipelineOpsOutput,
        PipelineOpsState,
    )

    private = (
        set(PipelineOpsState.__annotations__)
        - set(PipelineOpsInput.__annotations__)
        - set(PipelineOpsOutput.__annotations__)
    )
    assert private == {"ops_kind"}
    assert "audit" not in PipelineOpsInput.__annotations__
    assert "audit" in PipelineOpsOutput.__annotations__
    assert "pipelines" in PipelineOpsOutput.__annotations__


def test_route_after_resolve():
    from work_agent.graph.nodes.pipeline_ops import route_after_resolve

    assert route_after_resolve({"pipelines": [], "ops_kind": "query"}) == "skip"
    assert (
        route_after_resolve(
            {"pipelines": [{"pipeline_id": "x"}], "ops_kind": "start"}
        )
        == "start"
    )
    assert (
        route_after_resolve(
            {"pipelines": [{"pipeline_id": "x"}], "ops_kind": "diagnose"}
        )
        == "diagnose"
    )
