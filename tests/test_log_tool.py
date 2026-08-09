"""MockLogTool 基本行为。"""

from work_agent.tools.mock.logs import MockLogTool


def test_fetch_logs_contains_pipeline_id():
    text = MockLogTool().fetch_logs("abc-123")
    assert "abc-123" in text
    assert "ERROR" in text
