from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import os

import yaml
from dotenv import load_dotenv


def project_root() -> Path:
    """仓库根目录：.../work_agent（含 requirements.txt 的那一层）"""
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Profile:
    default_version: str = "27B"
    frequent_topologies: list[str] = field(default_factory=lambda: ["topo_a", "topo_b"])
    poll_interval_seconds: int = 30
    poll_max_attempts: int = 40


@dataclass(frozen=True)
class Settings:
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_temperature: float
    llm_timeout: int
    llm_max_retries: int
    tool_backend: str
    profile: Profile
    workspace_dir: Path
    profile_path: Path


def _load_profile(path: Path) -> Profile:
    if not path.exists():
        return Profile()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Profile(
        default_version=str(data.get("default_version", "27B")),
        frequent_topologies=list(data.get("frequent_topologies", ["topo_a", "topo_b"])),
        poll_interval_seconds=int(data.get("poll_interval_seconds", 30)),
        poll_max_attempts=int(data.get("poll_max_attempts", 40)),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = project_root()
    load_dotenv(root / ".env", override=False)

    api_key = os.getenv("LLM_API_KEY") or os.getenv("GEMINI_API_KEY") or ""
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
    )