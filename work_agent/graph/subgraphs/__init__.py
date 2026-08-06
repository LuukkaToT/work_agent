"""子图组装包。"""

from work_agent.graph.subgraphs.exec_flow import build_exec_flow
from work_agent.graph.subgraphs.analysis_flow import build_test_analysis_graph

__all__ = ["build_exec_flow", "build_test_analysis_graph"]
