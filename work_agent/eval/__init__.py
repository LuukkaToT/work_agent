"""离线评测：在固定 golden set 上对比不同上下文策略。"""

from work_agent.eval.runner import (
    EvalCase,
    evidence_recall,
    format_report,
    load_cases,
    run_suite,
    summarize,
)

__all__ = [
    "EvalCase",
    "evidence_recall",
    "format_report",
    "load_cases",
    "run_suite",
    "summarize",
]
