from work_agent.tools.mock.knowledge import MockKnowledgeSearchTool


def test_search_hits_aw_band():
    tool = MockKnowledgeSearchTool()
    out = tool.search("CELL_BAND unresolved placeholder", top_k=2)
    assert "no hits" not in out
    assert "### " in out


def test_search_empty_query():
    assert "empty query" in MockKnowledgeSearchTool().search("")


def test_search_no_hits():
    out = MockKnowledgeSearchTool().search("zzzznotexist999")
    assert "no hits" in out