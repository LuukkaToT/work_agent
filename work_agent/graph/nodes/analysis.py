"""
测试分析 Role：

同一个大模型 + 加载 test_analysis skill（markdown）= 测试分析「子 agent」。
不做独立子图；节点内完成：读 skill → 调 LLM → 落盘 test_analysis.md。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from work_agent.core.config import get_settings
from work_agent.core.llm import get_chat_model
from work_agent.core.skills import SkillLoader
from work_agent.graph.state import TestFlowState


def test_analysis(state: TestFlowState) -> dict:
    """analysis 意图的真正实现，替换原来的 do_analysis 桩。"""
    user_input = state.get("user_input") or state.get("requirement") or ""
    task_id = state.get("task_id") or "unknown"

    loader = SkillLoader()
    pack = loader.load("test_analysis")
    # 钩子：现在全量资料；将来可按 user_input 做检索
    refs = loader.select_references(pack, user_input)
    system_prompt = pack.as_system_prompt(refs)

    llm = get_chat_model(temperature=0.2)
    resp = llm.invoke(
        [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=(
                    "请根据以下用户需求生成测试分析文档。\n\n"
                    f"{user_input}"
                )
            ),
        ]
    )
    # 有的模型 content 可能是 list（多段），这里统一成 str
    content = resp.content
    if isinstance(content, list):
        content = "".join(
            block.get("text", str(block)) if isinstance(block, dict) else str(block)
            for block in content
        )
    text = str(content).strip()

    run_dir: Path = get_settings().workspace_dir / "runs" / task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    analysis_path = run_dir / "test_analysis.md"
    analysis_path.write_text(text + "\n", encoding="utf-8")

    summary = {
        "status": "ok",
        "branch": "analysis",
        "task_id": task_id,
        "analysis_path": str(analysis_path),
        "skill": "test_analysis",
        "refs_used": list(refs.keys()),
        "chars": len(text),
    }

    return {
        "requirement": user_input,
        "analysis_path": str(analysis_path),
        "summary": summary,
        "audit": [
            {
                "step": "test_analysis",
                "skill": "test_analysis",
                "analysis_path": str(analysis_path),
                "refs": list(refs.keys()),
            }
        ],
    }
