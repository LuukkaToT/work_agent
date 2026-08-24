"""
公司流水线请求体 / 响应的映射。

字段名和层级是**模拟 schema**，明天到公司后优先改本文件带
``COMPANY_REPLACE`` 标记的函数；``PipelineTool`` Protocol 与图节点不用动。
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from work_agent.tools.create_mode import EnvKind
from work_agent.tools.models import CaseResult, CaseVerdict, FailKind, PipelineResult, RunPhase

# 明天替换真实 schema 时，从这些函数改起。
COMPANY_REPLACE = "COMPANY_REPLACE"

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")

_PHASE_MAP: dict[str, RunPhase] = {
    "pending": "pending",
    "created": "created",
    "queued": "created",
    "accepted": "created",
    "running": "running",
    "executing": "running",
    "finished": "finished",
    "success": "finished",
    "succeeded": "finished",
    "completed": "finished",
    "failed": "failed",
    "error": "failed",
    "timeout": "timeout",
    "timed_out": "timeout",
}

_VERDICT_MAP: dict[str, CaseVerdict] = {
    "pass": "pass",
    "passed": "pass",
    "success": "pass",
    "ok": "pass",
    "fail": "fail",
    "failed": "fail",
    "failure": "fail",
    "error": "error",
    "errored": "error",
    "skipped": "skipped",
    "skip": "skipped",
}

_FAIL_KIND_MAP: dict[str, FailKind] = {
    "none": "none",
    "": "none",
    "version": "version",
    "case": "case",
    "env": "env",
    "environment": "env",
}


def load_pipeline_defaults(extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """
    加载拼进请求体的平台侧默认参数（项目、租户、资源池、镜像等）。

    COMPANY_REPLACE: 换成从公司配置中心 / 本地 yaml 拉真实默认值。
    当前只合并调用方传入的 ``extra``（一般来自环境变量）。
    """
    defaults: dict[str, Any] = {
        "project_id": "",
        "tenant": "",
        "operator": "",
        "resource_pool": "default",
        "runtime_image": "",
        "callback_url": "",
        "site": "",
        "rack": "",
    }
    if extra:
        for key, value in extra.items():
            if key in defaults and value is not None:
                defaults[key] = value
    return defaults


def load_case_runtime_params(case_names: list[str]) -> list[dict[str, Any]]:
    """
    按用例名加载平台侧额外参数（超时、标签、附属配置等）。

    COMPANY_REPLACE: 换成带着 token 调公司用例库，补全每条用例的运行参数。
    当前只把用例路径摊成显式列表，不访问网络。
    """
    items: list[dict[str, Any]] = []
    for name in case_names:
        path = str(name).strip()
        if not path:
            continue
        items.append(
            {
                "path": path,
                "enabled": True,
                "timeoutSeconds": 0,
                "tags": [],
                "extraParams": {},
            }
        )
    return items


def build_create_payload(
    *,
    case_names: list[str],
    version: str,
    env_kind: EnvKind,
    display_env: str,
    logic_constraint: str,
    options: Mapping[str, Any] | None = None,
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """
    把 Protocol 参数摊成公司 create 请求体（模拟的大 JSON）。

    COMPANY_REPLACE: 按真实 API 文档改字段名与层级；保持入参不变。

    物理模式只填 ``spec.environment.physical``；逻辑模式只填
    ``spec.environment.logical``；另一侧为 null。
    """
    opts = dict(options or {})
    runtime = load_pipeline_defaults(defaults)
    debug_mode = bool(opts.get("debug_mode", False))
    case_items = load_case_runtime_params(case_names)
    env_block = _environment_block(
        env_kind=env_kind,
        display_env=display_env,
        logic_constraint=logic_constraint,
        site=str(runtime.get("site") or ""),
        rack=str(runtime.get("rack") or ""),
    )
    return {
        "apiVersion": "ci.pipeline.company/v1",
        "kind": "TestJob",
        "metadata": {
            "name": _job_name(case_names, version),
            "labels": {
                "source": "work_agent",
                "version": version,
                "envKind": env_kind,
            },
            "annotations": {
                "work_agent.debug_mode": "true" if debug_mode else "false",
            },
        },
        "spec": {
            "project": {
                "id": str(runtime.get("project_id") or ""),
                "tenant": str(runtime.get("tenant") or ""),
                "operator": str(runtime.get("operator") or ""),
            },
            "version": version,
            "debugMode": debug_mode,
            "cases": {
                "selectMode": "explicit",
                "items": case_items,
                "execution": {
                    "strategy": "serial",
                    "parallelism": 1,
                    "failFast": False,
                    "retryOnError": 0,
                    "timeoutSeconds": 7200,
                    "collectCoverage": False,
                },
            },
            "environment": env_block,
            "resources": {
                "pool": str(runtime.get("resource_pool") or "default"),
                "priority": "normal",
                "exclusive": True,
                "quota": {"cpu": "4", "memoryGi": 8, "diskGi": 40},
            },
            "runtime": {
                "workdir": "/data/pipeline",
                "image": str(runtime.get("runtime_image") or ""),
                "envVars": [],
                "preHooks": [],
                "postHooks": [],
                "artifacts": {
                    "enabled": True,
                    "paths": ["logs/", "reports/"],
                    "ttlDays": 14,
                },
            },
            "reporting": {
                "uploadLogs": True,
                "keepJunit": True,
                "callbackUrl": str(runtime.get("callback_url") or ""),
            },
            "notifications": {
                "onSuccess": [],
                "onFailure": [],
                "channels": [],
            },
            "security": {
                "needPrivilege": False,
                "allowRoot": False,
            },
            "schedule": {"type": "immediate", "cron": ""},
        },
    }


def build_start_payload(pipeline_id: str) -> dict[str, Any]:
    """
    启动请求体。

    COMPANY_REPLACE: 若真实 start 是无 body 的 POST，把本函数改成返回 ``{}``。
    """
    return {"pipelineId": pipeline_id, "action": "start"}


def parse_create_response(raw: Mapping[str, Any]) -> str:
    """
    从公司 create 响应里取出 pipeline_id。

    COMPANY_REPLACE: 按真实响应字段改查找顺序。
    """
    pid = _first_id(raw)
    if pid:
        return pid
    data = raw.get("data")
    if isinstance(data, Mapping):
        pid = _first_id(data)
        if pid:
            return pid
    raise ValueError(f"公司 create 响应缺少 pipeline_id: {raw!r}")


def parse_start_response(raw: Mapping[str, Any] | None) -> bool:
    """
    把公司 start 响应收成 bool。

    COMPANY_REPLACE: 按真实成功字段改。HTTP 已成功且未显式拒绝时默认 True。
    """
    if not raw:
        return True
    if isinstance(raw.get("ok"), bool):
        return bool(raw["ok"])
    if isinstance(raw.get("success"), bool):
        return bool(raw["success"])
    data = raw.get("data")
    if isinstance(data, Mapping):
        if isinstance(data.get("accepted"), bool):
            return bool(data["accepted"])
        if isinstance(data.get("ok"), bool):
            return bool(data["ok"])
    return True


def parse_query_response(raw: Mapping[str, Any], pipeline_id: str) -> PipelineResult:
    """
    把公司 query 响应映射成 ``PipelineResult``。

    COMPANY_REPLACE: 按真实状态枚举 / 用例结果字段改。
    """
    body = raw.get("data") if isinstance(raw.get("data"), Mapping) else raw
    if not isinstance(body, Mapping):
        body = raw
    status_raw = (
        body.get("status")
        or body.get("phase")
        or body.get("state")
        or ""
    )
    message = str(body.get("message") or body.get("msg") or "")
    cases_raw = body.get("cases") or body.get("results") or body.get("caseResults") or []
    results: list[CaseResult] = []
    if isinstance(cases_raw, list):
        for item in cases_raw:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or item.get("caseName") or item.get("path") or "")
            if not name:
                continue
            results.append(
                CaseResult(
                    case_name=name,
                    verdict=_map_verdict(item.get("verdict") or item.get("result") or item.get("status")),
                    fail_kind=_map_fail_kind(item.get("failKind") or item.get("fail_kind") or "none"),
                    detail=str(item.get("detail") or item.get("message") or ""),
                )
            )
    return PipelineResult(
        pipeline_id=str(body.get("pipelineId") or body.get("pipeline_id") or pipeline_id),
        phase=_map_phase(status_raw),
        results=results,
        message=message,
    )


def _environment_block(
    *,
    env_kind: EnvKind,
    display_env: str,
    logic_constraint: str,
    site: str,
    rack: str,
) -> dict[str, Any]:
    """物理 / 逻辑二选一的 environment 块；另一侧保持 null。"""
    if env_kind == "physical":
        return {
            "mode": "physical",
            "physical": {
                "neIp": display_env,
                "sshPort": 22,
                "protocol": "ip",
                "site": site,
                "rack": rack,
            },
            "logical": None,
        }
    return {
        "mode": "logical",
        "physical": None,
        "logical": {
            "topology": display_env,
            "constraint": logic_constraint,
            "allocator": "platform",
            "waitTimeoutSeconds": 1800,
        },
    }


def _job_name(case_names: list[str], version: str) -> str:
    """生成请求体 metadata.name；不含空格等特殊字符。"""
    head = (case_names[0] if case_names else "job")[:40]
    safe = _SAFE_NAME_RE.sub("-", head).strip("-") or "job"
    ver = _SAFE_NAME_RE.sub("-", version).strip("-") or "ver"
    return f"wa-{safe}-{ver}"


def _first_id(data: Mapping[str, Any]) -> str:
    """从一层 dict 里按常见键取出非空 id。"""
    for key in ("pipelineId", "pipeline_id", "jobId", "job_id", "id"):
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _map_phase(raw: Any) -> RunPhase:
    """COMPANY_REPLACE: 公司状态字 → Agent ``RunPhase``。"""
    key = str(raw or "").strip().lower()
    return _PHASE_MAP.get(key, "running" if key else "created")


def _map_verdict(raw: Any) -> CaseVerdict:
    """COMPANY_REPLACE: 公司用例结论 → ``CaseVerdict``。"""
    key = str(raw or "").strip().lower()
    return _VERDICT_MAP.get(key, "error")


def _map_fail_kind(raw: Any) -> FailKind:
    """COMPANY_REPLACE: 公司失败分类 → ``FailKind``。"""
    key = str(raw or "").strip().lower()
    return _FAIL_KIND_MAP.get(key, "none")
