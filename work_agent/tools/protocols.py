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
        *,
        physical_env: str | None = None,
        logic_env: str | None = None,
        logic_constraint: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> PipelineHandle:
        """
        创建流水线，不启动。

        环境二选一：``physical_env``（物理 IP），或完整逻辑组网。
        为兼容平台接口，完整逻辑组网按名称、约束两个参数传入。

        参数:
            case_names: 要跑的用例名列表。
            version: 软件版本（如 27B）。
            physical_env: 物理组网 IP；与逻辑模式互斥。
            logic_env: 完整逻辑组网的名称部分；须与 ``logic_constraint`` 成对。
            logic_constraint: 完整逻辑组网的约束部分。
            options: 可选开关收纳参数（目前只有 ``debug_mode``）；新增开关都进
                这个 dict 内部字段，本 Protocol 签名不再为开关改形。
                ``debug_mode`` 由 ``create_pipelines`` 提交时点查 ``user_config``。

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
    """只读列举、拉取、检索分组件日志；供 error_analysis ReAct 使用。"""

    def list_logs(self, pipeline_id: str) -> str:
        """列出可读取的日志文件、组件、行数和大小。"""
        ...

    def fetch_logs(
        self,
        pipeline_id: str,
        *,
        tail_lines: int | None = 200,
        component: str | None = None,
    ) -> str:
        """
        拉取流水线日志文本。

        参数:
            pipeline_id: 流水线 id。
            tail_lines: None 返回全文；否则只返回尾部 N 行。
            component: 可选组件名；None 表示合并时间线。

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
        component: str | None = None,
    ) -> str:
        """
        在全文上按正则/关键词检索，带上下文行。

        参数:
            pipeline_id: 流水线 id。
            pattern: 检索模式（实现可按正则或子串）。
            context_lines: 匹配行前后保留的上下文行数。
            max_matches: 最多返回多少处匹配。
            component: 可选组件名；None 表示检索所有组件。

        返回:
            可读检索结果文本。
        """
        ...

    def lookup_error_code(self, code: str) -> str:
        """按错误码或摘要关键词查询可能组件和下一步检查。"""
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
