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


def test_compress_keyed_overflow_keeps_tail_not_head():
    """超限时留尾部：根因在后，从头部切会把证据丢掉。"""
    noise = "\n".join(f"ERROR queue backpressure noise_seq={i}" for i in range(80))
    text = noise + "\nERROR Traceback\nKeyError: 'antenna_map'\n"
    out = compress_observation(text, max_chars=400)
    assert "KeyError" in out
    assert "antenna_map" in out
    assert len(out) <= 400
    assert "truncated" in out
    assert "noise_seq=0" not in out
