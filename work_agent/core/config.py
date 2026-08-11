"""
配置加载：

1. .env          → LLM、TOOL_BACKEND 等（密钥不入库）
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
    llm_temperature: float
    llm_timeout: int
    llm_max_retries: int
    tool_backend: str  # mock | real
    profile: Profile
    workspace_dir: Path  # 报告落盘根目录
    profile_path: Path
    checkpoint_path: Path  # LangGraph 状态库（SQLite）


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

    return Settings(
        llm_base_url=os.getenv(
            "LLM_BASE_URL",
            "https://generativelanguage.googleapis.com/v1beta/openai/",
        ),
        llm_api_key=api_key,
        llm_model=os.getenv("LLM_MODEL", "gemini-2.5-flash"),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        llm_timeout=int(os.getenv("LLM_TIMEOUT", "60")),
        llm_max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
        tool_backend=os.getenv("TOOL_BACKEND", "mock"),
        profile=_load_profile(profile_path),
        workspace_dir=root / "workspace",
        profile_path=profile_path,
        checkpoint_path=root / "workspace" / "checkpoints.sqlite",
    )
