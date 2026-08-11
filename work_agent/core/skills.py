"""
Skill = markdown 角色包（不是 tool，也不是子图）。

目录约定（相对仓库根）：
  skills/<name>/
    SKILL.md          # 角色定义 + 方法论
    template.md       # 输出结构模板
    references/*.md   # 业务/规格资料（select_references 按 query 检索注入）

load_skill("test_analysis") → 拼成 system prompt，交给同一个大模型，
相当于「加载了测试分析角色」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from work_agent.core.config import project_root
from work_agent.core.retrieval import (
    HybridRetriever,
    chunk_markdown,
    hits_to_reference_dict,
)


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

    def select_references(
        self,
        pack: SkillPack,
        query: str,
        *,
        top_k: int = 3,
        use_embeddings: bool = False,
    ) -> dict[str, str]:
        """
        按 query 混合检索 references（BM25∥Embedding→RRF）。
        无资料或无命中时返回空 dict（不把全量灌进 prompt）。

        参数:
            pack: 已加载的 skill。
            query: 用户问题；空串时保守只塞最短一篇。
            top_k: 检索返回条数。
            use_embeddings: 是否启用向量路。

        返回:
            ``source::title`` → 正文 的资料 dict。
        """
        if not pack.references:
            return {}
        q = (query or "").strip()
        if not q:
            # 无 query 时保守：最多塞一篇最短的，避免全量
            items = sorted(pack.references.items(), key=lambda x: len(x[1]))
            name, body = items[0]
            return {name: body}

        chunks = []
        for name, body in pack.references.items():
            chunks.extend(chunk_markdown(body, source=name))
        if not chunks:
            return {}

        retriever = HybridRetriever(chunks, use_embeddings=use_embeddings)
        hits = retriever.search(q, top_k=top_k, pool_n=20, min_rrf_score=0.01)
        if not hits:
            return {}
        return hits_to_reference_dict(hits)


def load_skill(name: str) -> SkillPack:
    """
    便捷入口：用默认 SkillLoader 加载 skill。

    参数:
        name: skill 目录名。

    返回:
        SkillPack。
    """
    return SkillLoader().load(name)
