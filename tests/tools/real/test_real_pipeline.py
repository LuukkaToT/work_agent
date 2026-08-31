"""RealPipelineTool：请求体、鉴权、create/start/query 编排。"""

from __future__ import annotations

from typing import Any

import pytest

from work_agent.tools.real.pipeline import RealPipelineTool
from work_agent.tools.real.pipeline_client import (
    HttpCompanyPipelineClient,
    PipelineApiConfig,
    fetch_access_token,
)
from work_agent.tools.real.pipeline_payload import (
    build_create_payload,
    parse_create_response,
    parse_query_response,
    parse_start_response,
)


class FakeCompanyClient:
    """记录调用、返回罐头 JSON；不打网络。"""

    def __init__(self) -> None:
        self.create_payloads: list[dict[str, Any]] = []
        self.start_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_ids: list[str] = []

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.create_payloads.append(payload)
        return {"data": {"pipelineId": "p-real-1"}}

    def start_job(self, pipeline_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.start_calls.append((pipeline_id, payload))
        return {"ok": True}

    def query_job(self, pipeline_id: str) -> dict[str, Any]:
        self.query_ids.append(pipeline_id)
        return {
            "data": {
                "pipelineId": pipeline_id,
                "status": "FINISHED",
                "message": "执行完成",
                "cases": [
                    {
                        "name": "case_downlink_001",
                        "verdict": "PASS",
                        "failKind": "NONE",
                        "detail": "ok",
                    }
                ],
            }
        }


def test_payload_physical_nulls_logical():
    payload = build_create_payload(
        case_names=["case_downlink_001"],
        version="27B",
        env_kind="physical",
        display_env="7.223.50.60",
        logic_constraint="",
        options={"debug_mode": True},
        defaults={"project_id": "P-001", "resource_pool": "lab-a"},
    )
    env = payload["spec"]["environment"]
    assert env["mode"] == "physical"
    assert env["physical"]["neIp"] == "7.223.50.60"
    assert env["logical"] is None
    assert payload["spec"]["debugMode"] is True
    assert payload["spec"]["project"]["id"] == "P-001"
    assert payload["spec"]["resources"]["pool"] == "lab-a"
    assert payload["spec"]["cases"]["items"][0]["path"] == "case_downlink_001"


def test_payload_logical_nulls_physical():
    payload = build_create_payload(
        case_names=["case_downlink_001"],
        version="27B",
        env_kind="logical",
        display_env="BESA_SDV_2BBH_1BBL",
        logic_constraint="1G_2A",
    )
    env = payload["spec"]["environment"]
    assert env["mode"] == "logical"
    assert env["physical"] is None
    assert env["logical"]["topology"] == "BESA_SDV_2BBH_1BBL"
    assert env["logical"]["constraint"] == "1G_2A"


def test_parse_create_and_query_nested_data():
    assert parse_create_response({"data": {"pipelineId": "abc"}}) == "abc"
    result = parse_query_response(
        {
            "data": {
                "status": "RUNNING",
                "message": "执行中",
                "cases": [{"name": "c1", "verdict": "FAIL", "failKind": "version"}],
            }
        },
        "p-1",
    )
    assert result.pipeline_id == "p-1"
    assert result.phase == "running"
    assert result.results[0].verdict == "fail"
    assert result.results[0].fail_kind == "version"
    assert parse_start_response({"data": {"accepted": False}}) is False


def test_fetch_access_token_static():
    cfg = PipelineApiConfig(token="static-token")
    assert fetch_access_token(cfg) == "static-token"


def test_fetch_access_token_missing():
    with pytest.raises(RuntimeError, match="PIPELINE_API_TOKEN"):
        fetch_access_token(PipelineApiConfig())


def test_http_client_create_uses_token_and_path():
    captured: dict[str, Any] = {}

    def fake_request(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"data": {"pipelineId": "x"}}

    cfg = PipelineApiConfig(base_url="http://pipeline.internal/api", token="t-1")
    client = HttpCompanyPipelineClient(
        cfg, request_fn=fake_request, token_fn=lambda c: c.token
    )
    raw = client.create_job({"k": 1})
    assert raw["data"]["pipelineId"] == "x"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://pipeline.internal/api/pipelines"
    assert captured["token"] == "t-1"
    assert captured["json_body"] == {"k": 1}


def test_http_client_requires_base_url():
    client = HttpCompanyPipelineClient(
        PipelineApiConfig(token="t"),
        token_fn=lambda c: c.token,
        request_fn=lambda **_: {},
    )
    with pytest.raises(RuntimeError, match="PIPELINE_API_BASE_URL"):
        client.create_job({})


def test_real_tool_create_physical_records_payload():
    fake = FakeCompanyClient()
    tool = RealPipelineTool(client=fake, config=PipelineApiConfig(project_id="P-9"))
    handle = tool.create(
        ["case_downlink_001"],
        "27B",
        physical_env="7.223.50.60",
        options={"debug_mode": False},
    )
    assert handle.pipeline_id == "p-real-1"
    assert handle.env_kind == "physical"
    assert handle.env == "7.223.50.60"
    env = fake.create_payloads[0]["spec"]["environment"]
    assert env["mode"] == "physical"
    assert env["logical"] is None
    assert fake.create_payloads[0]["spec"]["project"]["id"] == "P-9"


def test_real_tool_create_logical():
    fake = FakeCompanyClient()
    tool = RealPipelineTool(client=fake)
    handle = tool.create(
        ["case_downlink_001"],
        "27B",
        logic_env="BESA_SDV_2BBH_1BBL",
        logic_constraint="1G_2A",
    )
    assert handle.env_kind == "logical"
    assert handle.logic_constraint == "1G_2A"
    env = fake.create_payloads[0]["spec"]["environment"]
    assert env["logical"]["constraint"] == "1G_2A"
    assert env["physical"] is None


def test_real_tool_rejects_env_before_client():
    fake = FakeCompanyClient()
    tool = RealPipelineTool(client=fake)
    with pytest.raises(ValueError, match="必须指定"):
        tool.create(["case_downlink_001"], "27B")
    assert fake.create_payloads == []


def test_real_tool_start_and_query():
    fake = FakeCompanyClient()
    tool = RealPipelineTool(client=fake)
    assert tool.start("p-real-1") is True
    assert fake.start_calls[0][0] == "p-real-1"
    result = tool.query("p-real-1")
    assert result.phase == "finished"
    assert result.results[0].case_name == "case_downlink_001"
    assert result.results[0].verdict == "pass"
