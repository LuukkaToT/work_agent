"""
Skill = markdown 角色包（不是 tool，也不是子图）。

目录约定（相对仓库根）：
  skills/<name>/
    SKILL.md          # 角色定义 + 方法论
    template.md       # 输出结构模板
    references/*.md   # 业务/规格资料（现阶段全量注入，不做 RAG）

load_skill("test_analysis") → 拼成 system prompt，交给同一个大模型，
相当于「加载了测试分析角色」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from work_agent.core.config import project_root


@dataclass
class SkillPack:
    """已加载的 skill 包内容。"""

    name: str
    skill_md: str
    template_md: str
    references: dict[str, str] = field(default_factory=dict)  # 文件名 → 正文

    def as_system_prompt(self, references: dict[str, str] | None = None) -> str:
        """
        把角色说明 + 模板 + 资料拼成一条 system prompt。

        参数:
            references: 覆盖注入的资料；None 用 pack 自带 references。

        返回:
            完整 system prompt 文本。
        """
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
    """从 skills/<name>/ 目录加载 SkillPack。"""

    def __init__(self, root: Path | None = None) -> None:
        """
        参数:
            root: skills 根目录；默认仓库根下的 skills/。
        """
        # 默认：仓库根下的 skills/
        self.root = root or (project_root() / "skills")

    def load(self, name: str) -> SkillPack:
        """
        加载指定 skill。

        参数:
            name: skill 目录名（如 test_analysis）。

        返回:
            SkillPack；目录或必备文件缺失时抛 FileNotFoundError。
        """
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
        资料筛选钩子（现阶段全量返回）。

        参数:
            pack: 已加载的 skill。
            query: 用户问题（预留检索用）。

        返回:
            文件名 → 正文 的资料 dict。
        """
        _ = query  # 预留：将来按 query 过滤
        return dict(pack.references)


def load_skill(name: str) -> SkillPack:
    """
    便捷入口：用默认 SkillLoader 加载 skill。

    参数:
        name: skill 目录名。

    返回:
        SkillPack。
    """
    return SkillLoader().load(name)
