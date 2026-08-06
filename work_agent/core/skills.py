"""
Skill = markdown 角色包。

目录约定（相对仓库根）：
  skills/<name>/
    SKILL.md          # 角色定义 + 方法论
    template.md       # 输出结构模板
    references/*.md   # 小型角色可选参考资料

该 Loader 保留给简单角色使用。5G 测试分析已经升级为独立子图，通过
work_agent.analysis.corpus 和受限检索 Tool 读取分目录资料库，不再调用
select_references 全量注入。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from work_agent.core.config import project_root


@dataclass
class SkillPack:
    name: str
    skill_md: str
    template_md: str
    references: dict[str, str] = field(default_factory=dict)  # 文件名 → 正文

    def as_system_prompt(self, references: dict[str, str] | None = None) -> str:
        """把角色说明 + 模板 + 资料拼成一条 system prompt。"""
        refs = references if references is not None else self.references
        parts = [
            f"# Skill: {self.name}",
            "",
            "## 角色与方法（SKILL.md）",
            self.skill_md.strip(),
            "",
            "## 输出模板（template.md）",
            "请严格按以下结构输出 Markdown：",
            self.template_md.strip(),
        ]
        if refs:
            parts.append("")
            parts.append("## 参考资料（references）")
            for fname, body in sorted(refs.items()):
                parts.append(f"\n### {fname}\n{body.strip()}")
        return "\n".join(parts)


class SkillLoader:
    def __init__(self, root: Path | None = None) -> None:
        # 默认：仓库根下的 skills/
        self.root = root or (project_root() / "skills")

    def load(self, name: str) -> SkillPack:
        skill_dir = self.root / name
        if not skill_dir.is_dir():
            raise FileNotFoundError(f"Skill 不存在: {skill_dir}")

        skill_path = skill_dir / "SKILL.md"
        template_path = skill_dir / "template.md"
        if not skill_path.exists():
            raise FileNotFoundError(f"缺少 {skill_path}")
        if not template_path.exists():
            raise FileNotFoundError(f"缺少 {template_path}")

        refs: dict[str, str] = {}
        ref_dir = skill_dir / "references"
        if ref_dir.is_dir():
            for path in sorted(ref_dir.glob("*.md")):
                refs[path.name] = path.read_text(encoding="utf-8")

        return SkillPack(
            name=name,
            skill_md=skill_path.read_text(encoding="utf-8"),
            template_md=template_path.read_text(encoding="utf-8"),
            references=refs,
        )

    def select_references(self, pack: SkillPack, query: str) -> dict[str, str]:
        """
        简单角色的资料筛选钩子（测试分析子图不使用）。

        现阶段：全量返回（资料少，全量注入更稳）。
        以后资料变多：在这里改成检索 / RAG，调用方不用改。
        """
        _ = query  # 预留：将来按 query 过滤
        return dict(pack.references)


def load_skill(name: str) -> SkillPack:
    """便捷入口。"""
    return SkillLoader().load(name)
