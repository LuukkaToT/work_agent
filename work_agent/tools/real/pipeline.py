"""真实流水线适配层：Protocol create/start/query → 公司 API。"""

from __future__ import annotations

from typing import Any

from work_agent.core.config import get_settings
from work_agent.tools.create_mode import resolve_create_env
from work_agent.tools.models import PipelineHandle, PipelineResult
from work_agent.tools.real.pipeline_client import (
    CompanyPipelineClient,
    HttpCompanyPipelineClient,
    PipelineApiConfig,
    load_pipeline_api_config_from_env,
)
from work_agent.tools.real.pipeline_payload import (
    build_create_payload,
    build_start_payload,
    load_pipeline_defaults,
    parse_create_response,
    parse_query_response,
    parse_start_response,
)


class RealPipelineTool:
    """
    公司流水线适配器。

    执行流（create）：
      1. 校验环境二选一（物理 IP / 逻辑组网+约束）
      2. 加载平台默认参数（项目、租户、资源池等）
      3. 拼 create 请求体
      4. 客户端鉴权拿 token 后提交
      5. 从响应取出服务端 pipeline_id

    明天到公司后优先改 ``pipeline_payload.py`` / ``pipeline_client.py``
    里带 COMPANY_REPLACE 的函数；本类编排顺序不用动。
    """

    def __init__(
        self,
        *,
        client: CompanyPipelineClient | None = None,
        config: PipelineApiConfig | None = None,
    ) -> None:
        """
        参数:
            client: 可注入的公司客户端（单测 fake）；None 用 HTTP 客户端。
            config: 连接配置；None 从环境变量加载。
        """
        get_settings()
        self._config = config if config is not None else load_pipeline_api_config_from_env()
        self._client = client if client is not None else HttpCompanyPipelineClient(self._config)

    def create(
        self,
        case_names: list[str],
        version: str,
        *,
        physical_env: str | None = None,
        logic_env: str | None = None,
        logic_constraint: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> PipelineHandle:
        """
        创建流水线，不启动。

        参数:
            case_names: 用例名列表。
            version: 版本。
            physical_env: 物理组网 IP；与逻辑模式互斥。
            logic_env: 规范逻辑组网名；须与 ``logic_constraint`` 成对。
            logic_constraint: 逻辑约束。
            options: 可选开关（目前 ``debug_mode``）。

        返回:
            含服务端 ``pipeline_id`` 的句柄。
        """
        names = [str(n).strip() for n in (case_names or []) if str(n).strip()]
        ver = (version or "").strip()
        if not names:
            raise ValueError("case_names 不能为空")
        if not ver:
            raise ValueError("version 不能为空")
        env_kind, display_env, constraint = resolve_create_env(
            physical_env=physical_env,
            logic_env=logic_env,
            logic_constraint=logic_constraint,
        )
        payload = build_create_payload(
            case_names=names,
            version=ver,
            env_kind=env_kind,
            display_env=display_env,
            logic_constraint=constraint,
            options=options,
            defaults=load_pipeline_defaults(self._config.runtime_defaults()),
        )
        raw = self._client.create_job(payload)
        pipeline_id = parse_create_response(raw)
        return PipelineHandle(
            pipeline_id=pipeline_id,
            case_names=list(names),
            version=ver,
            env=display_env,
            env_kind=env_kind,
            logic_constraint=constraint,
        )

    def start(self, pipeline_id: str) -> bool:
        """
        按 id 启动已创建的流水线。

        参数:
            pipeline_id: create 返回的 id。

        返回:
            是否成功受理启动。
        """
        pid = (pipeline_id or "").strip()
        if not pid:
            raise ValueError("pipeline_id 不能为空")
        payload = build_start_payload(pid)
        raw = self._client.start_job(pid, payload)
        return parse_start_response(raw)

    def query(self, pipeline_id: str) -> PipelineResult:
        """
        查询流水线状态与各用例结果。

        参数:
            pipeline_id: 流水线 id。

        返回:
            含 phase / results / message 的查询结果。
        """
        pid = (pipeline_id or "").strip()
        if not pid:
            raise ValueError("pipeline_id 不能为空")
        raw = self._client.query_job(pid)
        return parse_query_response(raw, pid)
