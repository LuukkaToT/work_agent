"""
组件 Registry：配置驱动，Skill Router 不调模型。

按组件标识加载 ``skills/components/<id>/SKILL.md`` 与通用 SOP。
工具白名单为空时沿用 ``config/components.json`` 的 ``tools_default``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from work_agent.core.config import project_root
from work_agent.tools.mock.scenarios import LOG_COMPONENTS

_REGISTRY_PATH = project_root() / "config" / "components.json"
_SKILLS_ROOT = project_root() / "skills"


@dataclass(frozen=True)
class ComponentSpec:
    """一条组件登记项。"""

    id: str
    aliases: tuple[str, ...]
    log_component: str
    related: tuple[str, ...]
    skill_dir: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class ComponentRegistry:
    """六组件登记表；查找走规范化标识或别名。"""

    components: tuple[ComponentSpec, ...]
    tools_default: tuple[str, ...]

    @property
    def by_id(self) -> dict[str, ComponentSpec]:
        """稳定 id → 规格。"""
        return {item.id: item for item in self.components}

    def get(self, name: str) -> ComponentSpec:
        """
        按 id 或别名解析组件。

        参数:
            name: 组件名，大小写不敏感；别名见登记表。

        返回:
            对应 ComponentSpec。

        异常:
            KeyError: 未登记。
        """
        key = (name or "").strip()
        if not key:
            raise KeyError("组件名不能为空")
        lower = key.casefold()
        for spec in self.components:
            if spec.id.casefold() == lower:
                return spec
            if any(alias.casefold() == lower for alias in spec.aliases):
                return spec
        raise KeyError(f"未知组件: {name!r}")

    def require(self, name: str) -> ComponentSpec:
        """``get`` 的别名，强调调用方必须拿到已登记组件。"""
        return self.get(name)


def _validate_spec(raw: dict, *, tools_default: tuple[str, ...]) -> ComponentSpec:
    """
    把登记 JSON 的一条收成 ``ComponentSpec``。

    参数:
        raw: ``components.json`` 里单条组件对象。
        tools_default: 文件级默认工具白名单；条目未写 ``tools`` 时沿用。

    返回:
        校验后的规格。

    异常:
        ValueError: id 不在六类日志组件、log_component 与 id 不一致，或 related 含未知标识。
    """
    cid = str(raw.get("id") or "").strip()
    if cid not in LOG_COMPONENTS:
        raise ValueError(f"组件 id 必须是六类日志组件之一，收到 {cid!r}")
    log_name = str(raw.get("log_component") or cid).strip()
    if log_name != cid:
        raise ValueError(f"组件 {cid} 的 log_component 必须与 id 相同，收到 {log_name!r}")
    tools = tuple(str(t).strip() for t in (raw.get("tools") or []) if str(t).strip())
    if not tools:
        tools = tools_default
    related = tuple(str(t).strip() for t in (raw.get("related") or []) if str(t).strip())
    unknown_related = [item for item in related if item not in LOG_COMPONENTS]
    if unknown_related:
        raise ValueError(f"组件 {cid} 的 related 含未知标识: {unknown_related}")
    skill_dir = str(raw.get("skill_dir") or f"components/{cid}").strip().replace("\\", "/")
    aliases = tuple(str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip())
    return ComponentSpec(
        id=cid,
        aliases=aliases,
        log_component=log_name,
        related=related,
        skill_dir=skill_dir,
        tools=tools,
    )


@lru_cache(maxsize=1)
def load_component_registry(path: Path | None = None) -> ComponentRegistry:
    """
    读取 ``config/components.json``。进程内缓存；单测可 ``cache_clear``。

    参数:
        path: 覆盖登记文件；默认仓库 ``config/components.json``。
    """
    target = path or _REGISTRY_PATH
    data = json.loads(target.read_text(encoding="utf-8"))
    tools_default = tuple(
        str(t).strip() for t in (data.get("tools_default") or []) if str(t).strip()
    )
    if not tools_default:
        raise ValueError("components.json 缺少 tools_default")
    specs = [_validate_spec(item, tools_default=tools_default) for item in data.get("components") or []]
    ids = [spec.id for spec in specs]
    if tuple(ids) != LOG_COMPONENTS:
        raise ValueError(
            f"组件登记必须恰好覆盖 {LOG_COMPONENTS} 且保持该顺序，收到 {tuple(ids)}"
        )
    return ComponentRegistry(components=tuple(specs), tools_default=tools_default)


def get_component(name: str) -> ComponentSpec:
    """按名称解析已登记组件。"""
    return load_component_registry().get(name)


def load_component_skill(name: str, *, skills_root: Path | None = None) -> str:
    """
    Skill Router：按组件名拼 system prompt，不调模型。

    顺序：通用 SOP → 组件 SKILL.md → 输出模板。缺文件直接报错，避免静默用错角色。
    """
    spec = get_component(name)
    root = skills_root or _SKILLS_ROOT
    common_path = root / "components" / "_common.md"
    skill_path = root / spec.skill_dir / "SKILL.md"
    template_path = root / spec.skill_dir / "template.md"
    missing = [str(p) for p in (common_path, skill_path, template_path) if not p.is_file()]
    if missing:
        raise FileNotFoundError("组件 skill 文件缺失: " + ", ".join(missing))
    return "\n\n".join(
        [
            "# 通用取证 SOP",
            common_path.read_text(encoding="utf-8").strip(),
            f"# Skill: {spec.id}",
            skill_path.read_text(encoding="utf-8").strip(),
            "## 输出结构",
            template_path.read_text(encoding="utf-8").strip(),
            "不得创建其他 Agent，不得修改全局假设。跨组件事项只作为建议，由主诊断下轮派发。",
        ]
    )
