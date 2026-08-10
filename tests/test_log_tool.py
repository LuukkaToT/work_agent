from work_agent.tools.mock.logs import MockLogTool


def test_fetch_logs_contains_pipeline_id():
    text = MockLogTool().fetch_logs("abc-123")
    assert "abc-123" in text
    assert "[log meta]" in text


def test_fetch_logs_deterministic():
    t = MockLogTool(scenario="case_error")
    assert t.fetch_logs("p1", tail_lines=None) == t.fetch_logs("p1", tail_lines=None)


def test_tail_lines_truncates():
    t = MockLogTool(scenario="case_error", total_lines=480)
    full = t.fetch_logs("p1", tail_lines=None)
    tail = t.fetch_logs("p1", tail_lines=50)
    assert "truncated=true" in tail.splitlines()[0]
    assert len(tail.splitlines()) < len(full.splitlines())


def test_scenario_markers():
    assert "KeyError" in MockLogTool("case_error").fetch_logs("p1", tail_lines=80)
    assert "protocol mismatch" in MockLogTool("version_fail").fetch_logs("p1", tail_lines=80)
    assert "unreachable" in MockLogTool("env_error").fetch_logs("p1", tail_lines=80)
    assert "ERROR" not in MockLogTool("all_pass").fetch_logs("p1", tail_lines=80)


def test_grep_logs_hits_keyerror():
    out = MockLogTool("case_error").grep_logs("p1", "KeyError")
    assert "matches=" in out
    assert "KeyError" in out