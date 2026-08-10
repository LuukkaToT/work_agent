"""优先级上下文组装与 observation 压缩。"""

from work_agent.graph.helpers.context_budget import (
    PRIORITY_CONCLUSION,
    PRIORITY_EVIDENCE,
    PRIORITY_RAW_TOOL,
    ContextBlock,
    assemble_blocks,
    compress_observation,
)


def test_assemble_drops_low_priority_first():
    blocks = [
        ContextBlock("conclusion", "结论很短", PRIORITY_CONCLUSION),
        ContextBlock("evidence", "证据" * 50, PRIORITY_EVIDENCE),
        ContextBlock("raw", "原始" * 200, PRIORITY_RAW_TOOL),
    ]
    out = assemble_blocks(blocks, limit=80)
    assert "结论很短" in out
    assert "【raw】" not in out or "truncated" in out


def test_compress_keeps_error_lines():
    text = "INFO ok\n" * 40 + "ERROR KeyError: band\n" + "INFO done\n" * 40
    out = compress_observation(text, max_chars=120)
    assert "KeyError" in out
    assert len(out) <= 150
