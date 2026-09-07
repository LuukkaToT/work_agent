"""
上下文归档：Working Context 与 External Context 分离。

不删日志，而是把原文移到外部存储，working context 里只留一句摘要 + artifact
引用（形如「发现 3 条 RRC timeout，详见 artifact fetch_logs_002」）。
这样信息没丢，只是不再常驻上下文，事后仍可追溯。

本模块是唯一会写盘的一段（Selector 纯函数、Compressor 只调 LLM）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from work_agent.core.config import get_settings
from work_agent.graph.helpers.context_selector import ContextItem

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ArchiveRef:
    """一条归档记录的引用。"""

    artifact_id: str
    path: str
    chars: int


class Archive(Protocol):
    """Storage contract shared by offline files and durable online archives."""

    @property
    def refs(self) -> list[ArchiveRef]: ...

    def store(self, item: ContextItem) -> ArchiveRef: ...

    def store_all(self, items: list[ContextItem]) -> list[ArchiveRef]: ...

    def read(self, artifact_id: str) -> str: ...


class ContextArchive:
    """把被裁掉的原文落盘，并给出可回读的 artifact 引用。"""

    def __init__(self, root: Path | str | None = None, *, run_id: str = "") -> None:
        """
        参数:
            root: 归档根目录；None 用 ``Settings.workspace_dir/diagnose_archive``。
            run_id: 本次诊断标识，用于分目录；空串归到 ``adhoc``。
        """
        base = Path(root) if root is not None else get_settings().workspace_dir / "diagnose_archive"
        self._dir = Path(base) / (_safe(run_id) or "adhoc")
        self._refs: dict[str, ArchiveRef] = {}

    @property
    def directory(self) -> Path:
        """本次归档所在目录（首次 store 时才真正创建）。"""
        return self._dir

    @property
    def refs(self) -> list[ArchiveRef]:
        """已归档的全部引用。"""
        return list(self._refs.values())

    def store(self, item: ContextItem) -> ArchiveRef:
        """
        把 item 原文写盘。

        同一 ``item_id`` 重复归档只写一次，返回既有引用。

        参数:
            item: 待归档的上下文块。

        返回:
            ArchiveRef（含 artifact_id 与落盘路径）。
        """
        existing = self._refs.get(item.item_id)
        if existing is not None:
            return existing

        artifact_id = self._artifact_id(item)
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{artifact_id}.txt"
        path.write_text(item.text, encoding="utf-8")
        ref = ArchiveRef(artifact_id=artifact_id, path=str(path), chars=len(item.text))
        self._refs[item.item_id] = ref
        return ref

    def store_all(self, items: list[ContextItem]) -> list[ArchiveRef]:
        """批量归档，顺序与入参一致。"""
        return [self.store(i) for i in items]

    def read(self, artifact_id: str) -> str:
        """
        按 artifact_id 回读原文。

        参数:
            artifact_id: ``store`` 返回的 id。

        返回:
            原文文本。

        异常:
            FileNotFoundError: 该 artifact 不存在。
        """
        path = self._dir / f"{_safe(artifact_id)}.txt"
        if not path.exists():
            raise FileNotFoundError(f"未找到 artifact {artifact_id!r}: {path}")
        return path.read_text(encoding="utf-8")

    def _artifact_id(self, item: ContextItem) -> str:
        """用 source + 序号生成可读 id，冲突时递增。"""
        stem = _safe(item.source or item.kind) or "item"
        candidate = f"{stem}_{len(self._refs) + 1:03d}"
        taken = {r.artifact_id for r in self._refs.values()}
        n = len(self._refs) + 1
        while candidate in taken:
            n += 1
            candidate = f"{stem}_{n:03d}"
        return candidate


def reference_note(ref: ArchiveRef) -> str:
    """working context 里留的一行引用说明。"""
    return f"[原文 {ref.chars} 字符已归档，详见 artifact {ref.artifact_id}]"


def _safe(name: str) -> str:
    """去掉文件名里的不安全字符。"""
    return _UNSAFE_RE.sub("_", (name or "").strip()).strip("_")
