"""真实系统实现预留。接项目时在此实现 CaseProvider / PipelineTool。"""

from work_agent.tools.real.cases import RealCaseProvider
from work_agent.tools.real.pipeline import RealPipelineTool

__all__ = ["RealCaseProvider", "RealPipelineTool"]
