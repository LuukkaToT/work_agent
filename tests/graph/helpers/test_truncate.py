from work_agent.graph.helpers.truncate import CharBudget, clip_text


def test_clip_short_unchanged():
    assert clip_text("hello", max_chars=100) == "hello"


def test_clip_long_has_marker():
    text = "A" * 1000 + "\n" + "B" * 1000
    out = clip_text(text, max_chars=200)
    assert "[truncated" in out
    assert "total_chars=" in out
    assert len(out) <= 200


def test_budget_second_call_exceeds():
    budget = CharBudget(limit=100)
    first = budget.take("x" * 80, max_chars=80)
    assert "budget exceeded" not in first
    second = budget.take("y" * 80, max_chars=80)
    assert "budget exceeded" in second
    assert budget.used <= 100