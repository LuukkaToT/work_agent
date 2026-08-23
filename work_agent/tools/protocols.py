"""
公司系统的「插座」定义。

图和节点只依赖这里的 Protocol，不依赖 mock 或 real 的类名。
接公司 tool 时：在 tools/real/ 写适配器（映射 external SDK）→ registry 切换 → 图不用改。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from work_agent.tools.models import CaseInfo, PipelineHandle, PipelineResult, SheetTable


@runtime_checkable
class CaseProvider(Protocol):
    """用例库：列目录 / 按名拉取。执行链路暂不用；分析场景以后再用。"""

    def list_cases(self, query: str | None = None) -> list[CaseInfo]:
        """
        列出用例目录。

        参数:
            query: 可选过滤关键词；None 表示全部。

        返回:
            CaseInfo 列表。
        """
        ...

    def fetch_cases(self, names: list[str]) -> list[CaseInfo]:
        """
        按用例名批量拉取详情。

        参数:
            names: 用例名列表。

        返回:
            对应的 CaseInfo 列表（缺名行为由实现决定）。
        """
        ...


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
        options: dict[str, Any] | None = None,
    ) -> PipelineHandle:
        """
        创建流水线，不启动。

        参数:
            case_names: 要跑的用例名列表。
            version: 软件版本（如 27B）。
            env: 物理组网 IP。
            options: 可选开关收纳参数（目前只有 ``debug_mode``）；新增开关都进
                这个 dict 内部字段，本 Protocol 签名不再变。

        返回:
            含服务端 ``pipeline_id`` 的句柄。
        """
        ...

    def start(self, pipeline_id: str) -> bool:
        """
        按 id 启动已创建的流水线。

        参数:
            pipeline_id: create 返回的 id。

        返回:
            是否成功受理启动。
        """
        ...

    def query(self, pipeline_id: str) -> PipelineResult:
        """
        查询流水线状态与各用例结果。

        参数:
            pipeline_id: 流水线 id。

        返回:
            含 phase / results / message 的查询结果。
        """
        ...

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
        拉取流水线日志文本。

        参数:
            pipeline_id: 流水线 id。
            tail_lines: None 返回全文；否则只返回尾部 N 行。

        返回:
            日志文本；建议首行带 ``[log meta]``。
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
        """
        在全文上按正则/关键词检索，带上下文行。

        参数:
            pipeline_id: 流水线 id。
            pattern: 检索模式（实现可按正则或子串）。
            context_lines: 匹配行前后保留的上下文行数。
            max_matches: 最多返回多少处匹配。

        返回:
            可读检索结果文本。
        """
        ...


@runtime_checkable
class CaseSheetTool(Protocol):
    """读本地用例表（xlsx/csv）为表头+行。"""

    def read(self, path: str) -> SheetTable:
        """
        读取本地用例表。

        参数:
            path: xlsx/csv 文件路径。

        返回:
            表头 + 行（全部为字符串）。
        """
        ...

@runtime_checkable
class KnowledgeSearchTool(Protocol):
    """
    只读知识检索；诊断时作旁证，不替代日志证据。
    real 侧对接内网 w3_search_tool MCP；mock 读本地故障 kb。
    """

    def search(self, query: str, *, top_k: int = 3) -> str:
        """
        按查询检索知识库。

        参数:
            query: 检索语句。
            top_k: 返回条数上限。

        返回:
            可读检索摘要（供 LLM 用）。
        """
        ...
