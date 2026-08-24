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


def test_default_log_is_thousands_of_lines():
    t = MockLogTool(scenario="case_error")
    full = t.fetch_logs("p1", tail_lines=None)
    assert "total_lines=8000" in full.splitlines()[0]
    assert full.count("\n") >= 7900


def test_error_noise_does_not_push_evidence_out_of_tail():
    text = MockLogTool("case_error").fetch_logs("p1", tail_lines=80)
    assert "KeyError" in text
    assert "ERROR queue backpressure" in text or "ERROR slot grant delayed" in text or "noise_seq=" in text


def test_error_noise_stays_under_observation_budget():
    from work_agent.graph.helpers.context_budget import compress_observation

    raw = MockLogTool("case_error").fetch_logs("p1", tail_lines=400)
    compressed = compress_observation(raw, max_chars=4000)
    assert "KeyError" in compressed
    assert "antenna_map" in compressed
    assert "Traceback" in compressed
    assert len(compressed) <= 4000
