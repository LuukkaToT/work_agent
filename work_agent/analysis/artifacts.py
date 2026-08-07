"""测试分析产物存储。

所有写入都被限制在 workspace/runs/<task_id>/analysis 下，并使用原子替换。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from work_agent.core.config import get_settings


class ArtifactStore:
    """按任务隔离测试分析产物，并阻止相对路径逃逸分析目录。"""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_task(cls, task_id: str) -> "ArtifactStore":
        """根据安全化后的 task_id 创建固定产物目录。"""

        safe_task_id = "".join(
            ch for ch in task_id if ch.isalnum() or ch in {"-", "_"}
        )
        if not safe_task_id:
            raise ValueError("task_id 不能用于创建分析产物目录")
        root = get_settings().workspace_dir / "runs" / safe_task_id / "analysis"
        return cls(root)

    def _target(self, relative_name: str) -> Path:
        """解析产物路径，并验证最终路径仍位于当前任务目录内。"""

        target = (self.root / relative_name).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError(f"非法分析产物路径: {relative_name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        # 先写同目录临时文件再替换，进程中断时不会留下半份 JSON/Markdown。
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)

    def write_json(self, relative_name: str, data: Any) -> str:
        """原子写入便于审计的 UTF-8 JSON，并返回绝对路径引用。"""

        path = self._target(relative_name)
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        self._atomic_write(path, text)
        return str(path)

    def write_markdown(self, relative_name: str, content: str) -> str:
        """原子写入 Markdown，并统一保证文件末尾只有一个换行。"""

        path = self._target(relative_name)
        self._atomic_write(path, content.rstrip() + "\n")
        return str(path)

    @staticmethod
    def read_json(ref: str) -> Any:
        """从 State 保存的产物引用恢复结构化数据。"""

        return json.loads(Path(ref).read_text(encoding="utf-8"))
