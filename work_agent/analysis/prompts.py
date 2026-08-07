"""测试分析 Prompt 文件加载。"""

from __future__ import annotations

from functools import lru_cache

from work_agent.core.config import project_root


@lru_cache(maxsize=16)
def load_prompt(name: str) -> str:
    """按名称加载阶段 Prompt；缓存避免同一进程反复读取静态文件。"""

    path = project_root() / "skills" / "test_analysis" / "prompts" / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"缺少测试分析 Prompt: {path}")
    return path.read_text(encoding="utf-8").strip()
