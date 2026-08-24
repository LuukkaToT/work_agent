"""
公司流水线 HTTP/SDK 客户端。

鉴权、发请求、路径都集中在这里。明天接入真实 API 时优先改带
``COMPANY_REPLACE`` 的函数；``RealPipelineTool`` 只负责编排。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol


HttpRequestFn = Callable[..., dict[str, Any]]
TokenFn = Callable[["PipelineApiConfig"], str]


@dataclass(frozen=True)
class PipelineApiConfig:
    """从环境变量加载的公司流水线连接配置。"""

    base_url: str = ""
    token: str = ""
    username: str = ""
    password: str = ""
    timeout_seconds: float = 60.0
    project_id: str = ""
    tenant: str = ""
    operator: str = ""
    resource_pool: str = "default"
    runtime_image: str = ""
    callback_url: str = ""
    site: str = ""
    rack: str = ""
    # COMPANY_REPLACE: 真实路径（可用环境变量覆盖，不必改代码）
    login_path: str = "/auth/token"
    create_path: str = "/pipelines"
    start_path: str = "/pipelines/{pipeline_id}/start"
    query_path: str = "/pipelines/{pipeline_id}"

    def is_configured(self) -> bool:
        """是否已配到足以发 HTTP（至少要有 base_url）。"""
        return bool(self.base_url.strip())

    def runtime_defaults(self) -> dict[str, str]:
        """喂给 ``load_pipeline_defaults`` 的平台参数。"""
        return {
            "project_id": self.project_id,
            "tenant": self.tenant,
            "operator": self.operator,
            "resource_pool": self.resource_pool,
            "runtime_image": self.runtime_image,
            "callback_url": self.callback_url,
            "site": self.site,
            "rack": self.rack,
        }


def load_pipeline_api_config_from_env() -> PipelineApiConfig:
    """
    从环境变量构造配置（调用方应已 ``get_settings()`` 触发 load_dotenv）。
    """
    return PipelineApiConfig(
        base_url=os.getenv("PIPELINE_API_BASE_URL", "").strip(),
        token=os.getenv("PIPELINE_API_TOKEN", "").strip(),
        username=os.getenv("PIPELINE_API_USERNAME", "").strip(),
        password=os.getenv("PIPELINE_API_PASSWORD", "").strip(),
        timeout_seconds=float(os.getenv("PIPELINE_API_TIMEOUT", "60")),
        project_id=os.getenv("PIPELINE_PROJECT_ID", "").strip(),
        tenant=os.getenv("PIPELINE_TENANT", "").strip(),
        operator=os.getenv("PIPELINE_API_OPERATOR", "").strip(),
        resource_pool=os.getenv("PIPELINE_RESOURCE_POOL", "default").strip() or "default",
        runtime_image=os.getenv("PIPELINE_RUNTIME_IMAGE", "").strip(),
        callback_url=os.getenv("PIPELINE_CALLBACK_URL", "").strip(),
        site=os.getenv("PIPELINE_SITE", "").strip(),
        rack=os.getenv("PIPELINE_RACK", "").strip(),
        login_path=os.getenv("PIPELINE_API_LOGIN_PATH", "/auth/token").strip() or "/auth/token",
        create_path=os.getenv("PIPELINE_API_CREATE_PATH", "/pipelines").strip() or "/pipelines",
        start_path=os.getenv(
            "PIPELINE_API_START_PATH", "/pipelines/{pipeline_id}/start"
        ).strip()
        or "/pipelines/{pipeline_id}/start",
        query_path=os.getenv(
            "PIPELINE_API_QUERY_PATH", "/pipelines/{pipeline_id}"
        ).strip()
        or "/pipelines/{pipeline_id}",
    )


def fetch_access_token(config: PipelineApiConfig) -> str:
    """
    拿到调用公司 API 用的 access token。

    COMPANY_REPLACE: 换成公司 IAM / 统一认证。当前顺序：
      1. ``PIPELINE_API_TOKEN`` 静态 token
      2. 用户名+密码走 ``login_with_password``
      3. 都没有则报错
    """
    static = (config.token or "").strip()
    if static:
        return static
    if (config.username or "").strip() and (config.password or "").strip():
        return login_with_password(config)
    raise RuntimeError(
        "未配置流水线鉴权：请设 PIPELINE_API_TOKEN，或同时设 "
        "PIPELINE_API_USERNAME + PIPELINE_API_PASSWORD"
    )


def login_with_password(config: PipelineApiConfig) -> str:
    """
    用户名密码换 token。

    COMPANY_REPLACE: 改登录路径、请求体字段、响应里 token 的键名。
    """
    if not config.is_configured():
        raise RuntimeError("用户名密码登录需要 PIPELINE_API_BASE_URL")
    raw = company_http_request(
        method="POST",
        url=_join_url(config.base_url, config.login_path),
        token="",
        json_body={
            "username": config.username,
            "password": config.password,
        },
        timeout=config.timeout_seconds,
    )
    token = _extract_token(raw)
    if not token:
        raise RuntimeError(f"登录响应缺少 token: {raw!r}")
    return token


def company_http_request(
    *,
    method: str,
    url: str,
    token: str,
    json_body: Mapping[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    """
    发 JSON HTTP 请求并解析响应。

    COMPANY_REPLACE: 换成公司 SDK / 内网网关客户端。当前用标准库 urllib，
    便于本地先把调用链跑通；有 SDK 后只改本函数即可。
    """
    data = None
    headers = {"Accept": "application/json"}
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=data, headers=headers, method=method.upper()
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw_bytes = resp.read() or b"{}"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"公司流水线 API {method.upper()} {url} 失败: HTTP {exc.code}: {body}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"公司流水线 API {method.upper()} {url} 无法连接: {exc.reason}"
        ) from exc
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"公司流水线 API {method.upper()} {url} 返回非 JSON: {text[:300]}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(
            f"公司流水线 API {method.upper()} {url} 响应不是对象: {parsed!r}"
        )
    return parsed


class CompanyPipelineClient(Protocol):
    """可注入的公司流水线客户端（单测用 fake）。"""

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        """提交 create 请求体，返回原始 JSON。"""
        ...

    def start_job(self, pipeline_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """按 id 启动，返回原始 JSON。"""
        ...

    def query_job(self, pipeline_id: str) -> dict[str, Any]:
        """按 id 查询，返回原始 JSON。"""
        ...


class HttpCompanyPipelineClient:
    """
    默认客户端：先鉴权拿 token，再按配置路径发 HTTP。

    单测可注入 ``request_fn`` / ``token_fn``，不打真实网络。
    """

    def __init__(
        self,
        config: PipelineApiConfig,
        *,
        request_fn: HttpRequestFn | None = None,
        token_fn: TokenFn | None = None,
    ) -> None:
        self._config = config
        self._request_fn = request_fn or company_http_request
        self._token_fn = token_fn or fetch_access_token
        self._token: str | None = None

    def ensure_token(self) -> str:
        """缓存 token；进程内复用（registry 已保证客户端单例）。"""
        if not self._token:
            self._token = self._token_fn(self._config)
        return self._token

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST create_path。"""
        return self._call("POST", self._config.create_path, json_body=payload)

    def start_job(self, pipeline_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST start_path。"""
        path = self._config.start_path.format(pipeline_id=pipeline_id)
        return self._call("POST", path, json_body=payload)

    def query_job(self, pipeline_id: str) -> dict[str, Any]:
        """GET query_path。"""
        path = self._config.query_path.format(pipeline_id=pipeline_id)
        return self._call("GET", path, json_body=None)

    def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if not self._config.is_configured():
            raise RuntimeError(
                "未配置 PIPELINE_API_BASE_URL：TOOL_BACKEND=real 时需要公司流水线地址"
            )
        token = self.ensure_token()
        return self._request_fn(
            method=method,
            url=_join_url(self._config.base_url, path),
            token=token,
            json_body=json_body,
            timeout=self._config.timeout_seconds,
        )


def _join_url(base: str, path: str) -> str:
    """拼接 base 与 path，避免重复斜杠。"""
    return base.rstrip("/") + "/" + path.lstrip("/")


def _extract_token(raw: Mapping[str, Any]) -> str:
    """COMPANY_REPLACE: 登录响应里 token 的键名。"""
    for key in ("token", "accessToken", "access_token", "id_token"):
        value = raw.get(key)
        if value:
            return str(value).strip()
    data = raw.get("data")
    if isinstance(data, Mapping):
        for key in ("token", "accessToken", "access_token"):
            value = data.get(key)
            if value:
                return str(value).strip()
    return ""
