"""
M12/M13：SkillLoader + 测试分析 Role。

    python -m work_agent.lessons.11_analysis
"""

from __future__ import annotations

from pathlib import Path

from work_agent.core.skills import SkillLoader
from work_agent.graph.main_graph import build_graph


def empty_state(text: str) -> dict:
    return {
        "task_id": "",
        "user_input": text,
        "intent": "",
        "requirement": "",
        "analysis_path": "",
        "cases": [],
        "exec_params": {},
        "run_id": "",
        "run_status": "",
        "poll_count": 0,
        "results": [],
        "logs": "",
        "report_path": "",
        "summary": {},
        "audit": [],
    }


def demo_skill_loader() -> None:
    print("=== SkillLoader ===")
    pack = SkillLoader().load("test_analysis")
    refs = SkillLoader().select_references(pack, "256T 下行")
    print("skill     :", pack.name)
    print("skill chars:", len(pack.skill_md))
    print("template  :", len(pack.template_md))
    print("refs      :", list(refs.keys()))
    prompt = pack.as_system_prompt(refs)
    print("system prompt preview:\n", prompt[:400], "...\n")


def main() -> None:
    demo_skill_loader()

    app = build_graph()
    text = "分析一下 256T 规格下行测试，给出冒烟用例建议"
    print("=== Graph test_analysis ===")
    print("user:", text)
    result = app.invoke(empty_state(text))

    path = result.get("analysis_path") or ""
    print("intent :", result.get("intent"))
    print("path   :", path)
    print("summary:", result.get("summary"))
    print("audit  :", [a.get("step") for a in result.get("audit") or []])
    if path and Path(path).exists():
        body = Path(path).read_text(encoding="utf-8")
        print("--- test_analysis.md (head) ---")
        print(body[:800])
        if len(body) > 800:
            print("...")


if __name__ == "__main__":
    main()
