"""
配置加载：

1. .env          → LLM、TOOL_BACKEND、Postgres DSN 等（密钥不入库）
2. profile.yaml  → 个人默认版本、常用组网、轮询参数

get_settings() 带缓存，进程内只读一次。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import os

import yaml
from dotenv import load_dotenv


def project_root() -> Path:
    """
    仓库根目录（含 requirements.txt / .env 的那一层）。
    本文件在 work_agent/core/config.py → parents[2] 就是仓库根。
    """
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Profile:
    """个人偏好；组网即便有默认值，执行前也应确认（架构约定）。"""

    default_version: str = "27B"
    frequent_topologies: list[str] = field(default_factory=lambda: ["topo_a", "topo_b"])
    poll_interval_seconds: int = 30
    poll_max_attempts: int = 40
    # create 失败不盲目重试（防双建）；start 可同 pipeline_id 重试
    create_retry_attempts: int = 1
    # 诊断 ReAct
    react_max_steps: int = 8
    tool_result_max_chars: int = 8000
    react_total_chars_budget: int = 40000
    # 单次工具结果写进 ToolMessage 前的压缩上限（比 tool_result_max_chars 更紧：
    # 前者是工具出口的截断，这里是进 ReAct 上下文时的再压缩）
    react_observation_max_chars: int = 4000
    # ReAct 历史 step 的字符预算；0 表示不裁剪历史
    react_history_max_chars: int = 20000
    # 对话记忆
    memory_summary_threshold: int = 12  # 超过此消息数才触发摘要
    memory_keep_recent: int = 8  # 摘要后保留的最近消息数
    # 检索（本地混合 RAG）
    rag_top_k: int = 3  # 最终返回条数
    rag_pool_n: int = 20  # 每路召回池大小
    rag_min_rrf_score: float = 0.01  # RRF 分低于此丢弃
    rag_use_embeddings: bool = False  # 是否启用 embedding 路（失败则降级 BM25）


@dataclass(frozen=True)
class Settings:
    """进程级配置：LLM、TOOL_BACKEND、路径与 profile。"""

    llm_base_url: str
    llm_api_key: str
    llm_model: str
    # 按 Flow/Role 分工的快/慢模型；未配对应环境变量时退回 llm_model（现有单模型
    # 部署零改动）。同一个 OpenAI 兼容网关，只是 model 参数不同。
    llm_fast_model: str
    llm_reasoning_model: str
    llm_temperature: float
    llm_timeout: int
    llm_max_retries: int
    tool_backend: str  # mock | real
    profile: Profile
    workspace_dir: Path  # 报告落盘根目录
    profile_path: Path
    postgres_dsn: str  # 生产库；空串表示未配置
    postgres_test_dsn: str  # 测试库；必须与生产 DSN 指向不同 database
    # MCP 对外入口；默认关闭，部署环境显式开启并提供服务令牌与 Host 白名单。
    mcp_enabled: bool = False
    mcp_service_token: str = ""
    mcp_allowed_hosts: tuple[str, ...] = ()
    mcp_allowed_origins: tuple[str, ...] = ()
    log_level: str = "INFO"
    log_format: str = "json"  # json | text；API 默认 json，CLI 可不调用 configure_logging


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().casefold()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_csv(name: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            part.strip()
            for part in (os.getenv(name) or "").split(",")
            if part.strip()
        )
    )


def _load_profile(path: Path) -> Profile:
    """从 profile.yaml 加载个人偏好；文件不存在则用默认 Profile。"""
    if not path.exists():
        return Profile()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Profile(
        default_version=str(data.get("default_version", "27B")),
        frequent_topologies=list(data.get("frequent_topologies", ["topo_a", "topo_b"])),
        poll_interval_seconds=int(data.get("poll_interval_seconds", 30)),
        poll_max_attempts=int(data.get("poll_max_attempts", 40)),
        create_retry_attempts=int(data.get("create_retry_attempts", 1)),
        react_max_steps=int(data.get("react_max_steps", 8)),
        tool_result_max_chars=int(data.get("tool_result_max_chars", 8000)),
        react_total_chars_budget=int(data.get("react_total_chars_budget", 40000)),
        react_observation_max_chars=int(data.get("react_observation_max_chars", 4000)),
        react_history_max_chars=int(data.get("react_history_max_chars", 20000)),
        memory_summary_threshold=int(data.get("memory_summary_threshold", 12)),
        memory_keep_recent=int(data.get("memory_keep_recent", 8)),
        rag_top_k=int(data.get("rag_top_k", 3)),
        rag_pool_n=int(data.get("rag_pool_n", 20)),
        rag_min_rrf_score=float(data.get("rag_min_rrf_score", 0.01)),
        rag_use_embeddings=bool(data.get("rag_use_embeddings", False)),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    加载并缓存全局 Settings（读 .env + config/profile.yaml）。

    返回:
        不可变 Settings；进程内只构建一次。
    """
    root = project_root()
    # override=False：已在环境里的变量优先，不被 .env 覆盖
    load_dotenv(root / ".env", override=False)

    # 兼容：可以直接写 LLM_API_KEY，也可以继续用 GEMINI_API_KEY
    api_key = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
    # 注意：读的是仓库根下 config/profile.yaml，不是 work_agent/config/
    profile_path = root / "config" / "profile.yaml"
    llm_model = os.getenv("LLM_MODEL", "gemini-2.5-flash")

    return Settings(
        llm_base_url=os.getenv(
            "LLM_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
        ),
        llm_api_key=api_key,
        llm_model=llm_model,
        llm_fast_model=os.getenv("LLM_FAST_MODEL") or llm_model,
        llm_reasoning_model=os.getenv("LLM_REASONING_MODEL") or llm_model,
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        llm_timeout=int(os.getenv("LLM_TIMEOUT", "60")),
        llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
        tool_backend=os.getenv("TOOL_BACKEND", "mock"),
        profile=_load_profile(profile_path),
        workspace_dir=root / "workspace",
        profile_path=profile_path,
        postgres_dsn=os.getenv("POSTGRES_DSN", ""),
        postgres_test_dsn=os.getenv("POSTGRES_TEST_DSN", ""),
        mcp_enabled=_env_bool("MCP_ENABLED"),
        mcp_service_token=(os.getenv("MCP_SERVICE_TOKEN") or "").strip(),
        mcp_allowed_hosts=_env_csv("MCP_ALLOWED_HOSTS"),
        mcp_allowed_origins=_env_csv("MCP_ALLOWED_ORIGINS"),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper() or "INFO",
        log_format=(os.getenv("LOG_FORMAT") or "json").strip().lower() or "json",
    )
