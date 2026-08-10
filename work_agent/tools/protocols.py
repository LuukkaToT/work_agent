"""
公司系统的「插座」定义。

图和节点只依赖这里的 Protocol，不依赖 mock 或 real 的类名。
接公司 tool 时：在 tools/real/ 写适配器（映射 external SDK）→ registry 切换 → 图不用改。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from work_agent.tools.models import CaseInfo, PipelineHandle, PipelineResult, SheetTable


@runtime_checkable
class CaseProvider(Protocol):
    """用例库：列目录 / 按名拉取。执行链路暂不用；分析场景以后再用。"""

    def list_cases(self, query: str | None = None) -> list[CaseInfo]: ...

    def fetch_cases(self, names: list[str]) -> list[CaseInfo]: ...


@runtime_checkable
class PipelineTool(Protocol):
    """
    流水线三件套（Agent 侧纯净命名）：

      create  创建，返回服务端 pipeline_id
      start   按 pipeline_id 启动
      query   按 pipeline_id 查询

    多环境 = 多次 create，多个 pipeline_id；start/query 逐个调用。
    """

    def create(
        self,
        case_names: list[str],
        version: str,
        env: str,
    ) -> PipelineHandle: ...

    def start(self, pipeline_id: str) -> bool: ...

    def query(self, pipeline_id: str) -> PipelineResult: ...

@runtime_checkable
class LogTool(Protocol):
    """只读拉日志 / 检索日志；供 error_analysis ReAct 使用。"""

    def fetch_logs(
        self,
        pipeline_id: str,
        *,
        tail_lines: int | None = 200,
    ) -> str:
        """
        拉取日志。
        tail_lines=None 返回全文；否则只返回尾部 N 行。
        返回文本第一行建议带 [log meta] ...
        """
        ...

    def grep_logs(
        self,
        pipeline_id: str,
        pattern: str,
        *,
        context_lines: int = 3,
        max_matches: int = 20,
    ) -> str:
        """在全文上按正则/关键词检索，带上下文行。"""
        ...


@runtime_checkable
class CaseSheetTool(Protocol):
    """读本地用例表（xlsx/csv）为表头+行。"""

    def read(self, path: str) -> SheetTable: ...

@runtime_checkable
class KnowledgeSearchTool(Protocol):
    """
    只读知识检索；诊断时作旁证，不替代日志证据。
    real 侧对接内网 w3_search_tool MCP；mock 读本地故障 kb。
    """

    def search(self, query: str, *, top_k: int = 3) -> str:
        """返回可读检索摘要（供 LLM 用）。"""
        ...