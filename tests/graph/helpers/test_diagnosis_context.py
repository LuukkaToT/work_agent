"""抽取专用上下文：普通块不调 LLM、证据不走摘要、本案故障行不被挤出窗口。"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from work_agent.core.usage import TokenUsage
from work_agent.graph.helpers.context_archive import ContextArchive
from work_agent.graph.helpers.context_budget import PRIORITY_CONCLUSION, PRIORITY_RAG
from work_agent.graph.helpers.context_compressor import CompressionOutcome, ContextCompressor
from work_agent.graph.helpers.context_selector import (
    PIN_IMMUTABLE,
    PIN_NORMAL,
    PIN_PROTECTED,
    ContextItem,
    ImmutableBudgetExceeded,
)
from work_agent.graph.helpers.diagnosis_context import log_excerpt, render_diagnosis_context


class _UnusedModel:
    def invoke(self, messages):  # noqa: ANN001
        raise AssertionError("抽取普通块溢出不得调用摘要模型")


class _RecordingCompressor(ContextCompressor):
    def __init__(self, model_factory=None) -> None:
        super().__init__(model_factory=model_factory or (lambda: _UnusedModel()))
        self.compress_calls = 0
        self.batch_calls = 0
        self.batch_ids: list[list[str]] = []

    def compress(self, items, *, max_chars_each, suffix=""):  # noqa: ANN001
        self.compress_calls += 1
        return super().compress(items, max_chars_each=max_chars_each, suffix=suffix)

    def compress_batch(self, items, *, max_chars_each):  # noqa: ANN001
        self.batch_calls += 1
        self.batch_ids.append([item.item_id for item in items])
        return super().compress_batch(items, max_chars_each=max_chars_each)


class _BatchJsonModel:
    def __init__(self, *, boom: bool = False) -> None:
        self.calls = 0
        self.payload: dict[str, str] | None = None
        self.boom = boom

    def invoke(self, messages):  # noqa: ANN001
        self.calls += 1
        if self.boom:
            raise RuntimeError("summarizer down")
        self.payload = json.loads(messages[-1].content)
        return AIMessage(
            content=json.dumps(
                {key: f"摘要:{key}" for key in self.payload},
                ensure_ascii=False,
            ),
            usage_metadata={
                "input_tokens": 11,
                "output_tokens": 6,
                "total_tokens": 17,
            },
        )


class _PassthroughCompressor(ContextCompressor):
    def __init__(self) -> None:
        super().__init__(model_factory=lambda: _UnusedModel())
        self.batch_calls = 0

    def compress_batch(self, items, *, max_chars_each):  # noqa: ANN001
        self.batch_calls += 1
        return CompressionOutcome(list(items), TokenUsage(calls=1), 1, [])


def _item(
    item_id: str,
    text: str,
    *,
    source: str,
    kind: str = "tool_result",
    priority: int = 4,
    pin: str = PIN_NORMAL,
) -> ContextItem:
    return ContextItem(
        item_id=item_id,
        kind=kind,
        source=source,
        text=text,
        priority=priority,
        pin=pin,  # type: ignore[arg-type]
    )


def _goal(text: str = "小区 load fail") -> ContextItem:
    return _item(
        "goal",
        text,
        source="user_request",
        kind="note",
        priority=PRIORITY_CONCLUSION,
        pin=PIN_IMMUTABLE,
    )


def _mixed_log(*, include_meta: bool = True) -> str:
    noise = "\n".join(f"2026-09-09 09:{i:02d}:00 debug heartbeat seq={i}" for i in range(40))
    meta = "[log meta] pipeline=p1 component=bbh file=ue.log\n" if include_meta else ""
    return (
        f"{meta}"
        "2026-09-09 10:00:00 other_topic subscribe_ack topic=sys\n"
        "2026-09-09 10:00:01 cell_id=A cell_state=ACTIVE\n"
        f"{noise}\n"
        "2026-09-09 10:05:00 cell_id=A attempt=12 RACH start\n"
        "2026-09-09 10:05:01 cell_id=A ERROR RRC timeout\n"
        "2026-09-09 10:05:02 other_topic subscribe_ack topic=sys\n"
        "2026-09-09 10:05:03 cell_id=A cell_state=ACTIVE\n"
    )


def test_normal_overflow_does_not_call_compressor(tmp_path):
    """轴 1：旁证普通块超预算只归档，不走摘要。日志已升 protected，不能再当淘汰样本。"""
    archive = ContextArchive(root=tmp_path, run_id="axis1")
    rec = _RecordingCompressor()
    items = [_goal()] + [
        _item(
            f"kb-{i}",
            "旁证知识段落 " * 80,
            source="search_knowledge",
            kind="rag",
            priority=PRIORITY_RAG,
        )
        for i in range(10)
    ]
    result = render_diagnosis_context(
        items, goal="load fail", limit=900, compressor=rec, archive=archive,
    )
    assert rec.compress_calls == 0
    assert rec.batch_calls == 0
    assert result.usage.calls == 0
    assert result.llm_used is False
    assert result.context_chars <= 900
    assert result.archived
    assert "goal" in result.selected_ids
    assert len(result.selected_ids) < len(items)


def test_protected_draft_compresses_once_with_aligned_json_keys(tmp_path):
    """轴 2：结论草稿超预算恰好一次 compress_batch，JSON 键对齐 item_id。"""
    archive = ContextArchive(root=tmp_path, run_id="axis2-json")
    model = _BatchJsonModel()
    rec = _RecordingCompressor(model_factory=lambda: model)
    items = [
        _goal("诊断小区加载失败"),
        _item(
            "conclusion_draft",
            "结论草稿。" * 400,
            source="react",
            kind="conclusion",
            priority=PRIORITY_CONCLUSION,
            pin=PIN_PROTECTED,
        ),
    ]
    result = render_diagnosis_context(
        items, goal="诊断小区加载失败", limit=800, compressor=rec, archive=archive,
    )
    assert rec.batch_calls == 1
    assert rec.compress_calls == 0
    assert model.calls == 1
    assert model.payload is not None
    assert set(model.payload) == {"conclusion_draft"}
    assert rec.batch_ids == [["conclusion_draft"]]
    assert result.llm_used is True
    assert result.usage.calls == 1
    assert result.context_chars <= 800
    assert "conclusion_draft" in result.selected_ids
    assert "摘要:conclusion_draft" in result.text


def test_protected_draft_degrades_to_plain_excerpt_when_model_fails(tmp_path):
    archive = ContextArchive(root=tmp_path, run_id="axis2-boom")
    model = _BatchJsonModel(boom=True)
    rec = _RecordingCompressor(model_factory=lambda: model)
    draft = "结论草稿需保留 fail_kind=case 与 root=rat。" * 80
    items = [
        _goal("加载失败"),
        _item(
            "conclusion_draft",
            draft,
            source="react",
            kind="conclusion",
            priority=PRIORITY_CONCLUSION,
            pin=PIN_PROTECTED,
        ),
    ]
    result = render_diagnosis_context(
        items, goal="加载失败", limit=900, compressor=rec, archive=archive,
    )
    assert rec.batch_calls == 1
    assert model.calls == 1
    assert result.degraded_ids == ["conclusion_draft"]
    assert result.usage.calls == 1
    assert "fail_kind=case" in result.text or "root=rat" in result.text
    assert result.context_chars <= 900


def test_protected_draft_still_over_raises_without_second_llm():
    rec = _PassthroughCompressor()
    items = [
        _goal("加载失败"),
        _item(
            "conclusion_draft",
            "超长结论草稿。" * 500,
            source="react",
            kind="conclusion",
            priority=PRIORITY_CONCLUSION,
            pin=PIN_PROTECTED,
        ),
    ]
    with pytest.raises(ImmutableBudgetExceeded, match="必保留块摘要仍超抽取预算"):
        render_diagnosis_context(
            items, goal="加载失败", limit=400, compressor=rec, archive=None,
        )
    assert rec.batch_calls == 1


def test_protected_logs_do_not_call_compress_batch(tmp_path):
    """轴 2 收口：日志升 PIN_PROTECTED 后仍不得走 LLM，只做确定性再摘录。"""
    archive = ContextArchive(root=tmp_path, run_id="axis2-logs")
    rec = _RecordingCompressor()
    items = [_goal()] + [
        _item(
            f"log-{i}",
            _mixed_log() + (" debug heartbeat padding\n" * 80),
            source="fetch_logs",
        )
        for i in range(8)
    ]
    result = render_diagnosis_context(
        items, goal="load fail", limit=2000, compressor=rec, archive=archive,
    )
    assert rec.compress_calls == 0
    assert rec.batch_calls == 0
    assert result.llm_used is False
    assert result.usage.calls == 0
    assert result.context_chars <= 2000
    assert "ERROR RRC timeout" in result.text
    assert "attempt=12" in result.text
    assert any(item_id.startswith("log-") for item_id in result.selected_ids)


def test_log_excerpt_keeps_case_fault_and_attempt():
    """轴 3：摘录器不按对象过滤，但本案 ERROR / attempt= 不能被外对象 ACK 挤出。"""
    text = _mixed_log()
    excerpt = log_excerpt(text, limit=500)
    assert "ERROR RRC timeout" in excerpt
    assert "attempt=12" in excerpt
    assert "[log meta]" in excerpt
    assert len(excerpt) <= 500


def test_log_excerpt_keeps_same_object_recovery_and_foreign_ack():
    """b20 形态：故障后同一对象恢复与其他对象 ACK 两边都要能看见。"""
    text = _mixed_log()
    excerpt = log_excerpt(text, limit=800)
    assert "ERROR RRC timeout" in excerpt
    assert "cell_state=ACTIVE" in excerpt
    assert "subscribe_ack" in excerpt


def test_selected_timestamp_lines_are_deduped_but_same_body_different_time_kept(
    tmp_path,
):
    archive = ContextArchive(root=tmp_path, run_id="axis3-dedup")
    rec = _RecordingCompressor()
    line = "2026-09-09 10:05:01 cell_id=A ERROR RRC timeout"
    later = "2026-09-09 10:06:01 cell_id=A ERROR RRC timeout"
    items = [
        _goal(),
        _item("log-a", f"[log meta] file=ue.log\n{line}\n{line}\n", source="fetch_logs"),
        _item("log-b", f"[log meta] file=ue2.log\n{line}\n{later}\n", source="fetch_logs"),
    ]
    result = render_diagnosis_context(
        items, goal="load fail", limit=2000, compressor=rec, archive=archive,
    )
    assert result.text.count(line) == 1
    assert later in result.text
    assert rec.batch_calls == 0


def test_excerpt_archived_block_keeps_fault_and_bounds_size():
    from work_agent.graph.helpers.diagnosis_context import excerpt_archived_block

    text = _mixed_log() + (" debug heartbeat padding\n" * 80)
    out = excerpt_archived_block(text, limit=500)
    assert "ERROR RRC timeout" in out
    assert "attempt=12" in out
    assert len(out) <= 500
    assert excerpt_archived_block("短", limit=500) == "短"
