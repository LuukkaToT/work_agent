"""ContextCompressor / ContextArchive / ContextManager：压缩后置、归档引用化、编排。"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from work_agent.graph.helpers.context_archive import (
    ContextArchive,
    reference_note,
)
from work_agent.graph.helpers.context_budget import (
    PRIORITY_CONCLUSION,
    PRIORITY_RAW_TOOL,
)
from work_agent.graph.helpers.context_compressor import ContextCompressor
from work_agent.graph.helpers.context_manager import ContextManager
from work_agent.graph.helpers.context_selector import (
    PIN_IMMUTABLE,
    PIN_NORMAL,
    PIN_PROTECTED,
    ContextItem,
)


class _FakeModel:
    """记录调用次数并返回固定摘要；带 usage_metadata 以便验证 token 归集。"""

    def __init__(self, summary: str = "压缩后的摘要", *, tokens: int = 30) -> None:
        self.summary = summary
        self.tokens = tokens
        self.calls = 0

    def invoke(self, messages):  # noqa: ANN001
        self.calls += 1
        return AIMessage(
            content=self.summary,
            usage_metadata={
                "input_tokens": self.tokens,
                "output_tokens": 5,
                "total_tokens": self.tokens + 5,
            },
        )


class _BoomModel:
    """摘要总是失败，用于验证降级路径。"""

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, messages):  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("summarizer down")


def _item(
    item_id: str,
    text: str,
    *,
    priority: int = PRIORITY_RAW_TOOL,
    pin: str = PIN_NORMAL,
    source: str = "fetch_logs",
) -> ContextItem:
    return ContextItem(
        item_id=item_id,
        kind="tool_result",
        source=source,
        text=text,
        priority=priority,
        pin=pin,  # type: ignore[arg-type]
    )


def test_compressor_skips_llm_when_already_short():
    model = _FakeModel()
    comp = ContextCompressor(model_factory=lambda: model)
    outcome = comp.compress([_item("a", "很短")], max_chars_each=500)
    assert model.calls == 0
    assert outcome.llm_calls == 0
    assert outcome.items[0].text == "很短"
    assert outcome.usage.total_tokens == 0


def test_compressor_appends_suffix_without_llm_for_short_text():
    model = _FakeModel()
    comp = ContextCompressor(model_factory=lambda: model)
    outcome = comp.compress([_item("a", "短")], max_chars_each=500, suffix="[ref]")
    assert model.calls == 0
    assert outcome.items[0].text == "短\n[ref]"


def test_compressor_summarizes_long_text_and_counts_tokens():
    model = _FakeModel()
    comp = ContextCompressor(model_factory=lambda: model)
    outcome = comp.compress([_item("a", "x" * 2000)], max_chars_each=100)
    assert model.calls == 1
    assert outcome.llm_calls == 1
    assert outcome.items[0].text == "压缩后的摘要"
    # 摘要自身的开销必须记下来，否则 A/B 里 managed 会显得凭空变便宜
    assert outcome.usage.total_tokens == 35
    assert outcome.degraded_ids == []


def test_compressor_degrades_when_llm_fails():
    model = _BoomModel()
    comp = ContextCompressor(model_factory=lambda: model)
    long_text = "line ERROR boom\n" + "y" * 2000
    outcome = comp.compress([_item("a", long_text)], max_chars_each=120)
    assert outcome.degraded_ids == ["a"]
    assert len(outcome.items[0].text) <= 120
    # 降级仍要保住报错关键词
    assert "ERROR" in outcome.items[0].text


def test_archive_stores_and_reads_back(tmp_path):
    archive = ContextArchive(tmp_path, run_id="task-1")
    ref = archive.store(_item("a", "完整原始日志内容"))
    assert ref.chars == len("完整原始日志内容")
    assert archive.read(ref.artifact_id) == "完整原始日志内容"
    assert "artifact" in reference_note(ref)


def test_archive_is_idempotent_per_item():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        archive = ContextArchive(tmp, run_id="t")
        first = archive.store(_item("a", "x"))
        second = archive.store(_item("a", "x"))
        assert first == second
        assert len(archive.refs) == 1


def test_manager_under_budget_does_no_llm_and_no_io(tmp_path):
    model = _FakeModel()
    archive = ContextArchive(tmp_path, run_id="t")
    manager = ContextManager(
        compressor=ContextCompressor(model_factory=lambda: model), archive=archive
    )

    result = manager.render([_item("a", "短证据")], goal="证据", limit=5000)

    assert model.calls == 0
    assert result.llm_used is False
    assert result.selected_ids == ["a"]
    assert result.context_chars == len(result.text)
    # 装得下就不该产生归档目录
    assert not archive.directory.exists()


def test_manager_compresses_and_archives_when_over_budget(tmp_path):
    model = _FakeModel()
    archive = ContextArchive(tmp_path, run_id="t")
    manager = ContextManager(
        compressor=ContextCompressor(model_factory=lambda: model),
        archive=archive,
        summary_max_chars=100,
    )

    items = [
        _item("goal", "为什么失败", pin=PIN_IMMUTABLE, priority=PRIORITY_CONCLUSION, source="user"),
        _item("draft", "d" * 1500, pin=PIN_PROTECTED, priority=PRIORITY_CONCLUSION, source="react"),
        _item("obs1", "ERROR KeyError " + "a" * 1500),
        _item("obs2", "b" * 1500, source="grep_logs"),
    ]
    result = manager.render(items, goal="为什么失败", limit=800)

    assert result.llm_used is True
    assert result.usage.total_tokens > 0
    # protected 被压缩而不是被删
    assert "draft" in result.selected_ids
    assert "draft" in result.compressed_ids
    # 原文落盘可回溯
    assert result.archived
    assert archive.directory.exists()
    assert result.context_chars <= 800


def test_manager_archives_dropped_items_so_nothing_is_lost(tmp_path):
    model = _FakeModel()
    archive = ContextArchive(tmp_path, run_id="t")
    manager = ContextManager(
        compressor=ContextCompressor(model_factory=lambda: model),
        archive=archive,
        max_compress_items=1,
        summary_max_chars=80,
    )

    items = [_item(f"obs{i}", f"ERROR n{i} " + "z" * 1200, source=f"tool{i}") for i in range(4)]
    result = manager.render(items, goal="ERROR", limit=400)

    archived_chars = {r.chars for r in archive.refs}
    assert archived_chars, "被裁掉的内容必须归档，不能静默丢"
    # 归档条数 + 入选条数应覆盖全部候选
    assert len(archive.refs) + len(result.selected_ids) >= len(items)


def test_manager_without_archive_still_renders(tmp_path):
    model = _FakeModel()
    manager = ContextManager(
        compressor=ContextCompressor(model_factory=lambda: model), archive=None
    )
    result = manager.render(
        [_item("a", "ERROR " + "x" * 2000)], goal="ERROR", limit=300
    )
    assert result.archived == []
    assert result.context_chars <= 300
