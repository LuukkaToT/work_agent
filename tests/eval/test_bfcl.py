"""BFCL 适配：AST 比对与假模型跑 run_agent_loop，不下载全集。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage

from work_agent.eval.bfcl import (
    BfclCase,
    ast_correct,
    first_tool_calls,
    format_report,
    run_case,
    summarize,
    tool_from_bfcl,
)


class _ScriptedModel:
    def __init__(self, responses: list[AIMessage]) -> None:
        self._responses = list(responses)

    def bind_tools(self, tools):  # noqa: ANN001
        return self

    def invoke(self, messages):  # noqa: ANN001
        del messages
        return self._responses.pop(0)


def _fn(name: str, required: list[str] | None = None) -> dict:
    props = {key: {"type": "string"} for key in (required or ["city"])}
    return {
        "name": name,
        "description": name,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": list(required or ["city"]),
        },
    }


def test_ast_simple_and_irrelevance():
    functions = [_fn("get_weather")]
    assert ast_correct(
        category="simple",
        functions=functions,
        calls=[{"name": "get_weather", "args": {"city": "Paris"}}],
        possible_answer=[{"get_weather": {"city": ["Paris", "paris"]}}],
    )
    assert not ast_correct(
        category="simple",
        functions=functions,
        calls=[{"name": "get_weather", "args": {"city": "London"}}],
        possible_answer=[{"get_weather": {"city": ["Paris"]}}],
    )
    assert ast_correct(
        category="irrelevance",
        functions=functions,
        calls=[],
        possible_answer=None,
    )
    assert not ast_correct(
        category="irrelevance",
        functions=functions,
        calls=[{"name": "get_weather", "args": {"city": "Paris"}}],
        possible_answer=None,
    )


def test_ast_parallel_unordered():
    functions = [_fn("get_weather"), _fn("get_time", required=["zone"])]
    calls = [
        {"name": "get_time", "args": {"zone": "UTC"}},
        {"name": "get_weather", "args": {"city": "Paris"}},
    ]
    possible = [
        {"get_weather": {"city": ["Paris"]}},
        {"get_time": {"zone": ["UTC"]}},
    ]
    assert ast_correct(
        category="parallel",
        functions=functions,
        calls=calls,
        possible_answer=possible,
    )


def test_first_tool_calls_reads_first_ai_message():
    messages: list[BaseMessage] = [
        AIMessage(
            content="",
            tool_calls=[
                {"id": "1", "name": "get_weather", "args": {"city": "Paris"}}
            ],
        )
    ]
    assert first_tool_calls(messages) == [
        {"name": "get_weather", "args": {"city": "Paris"}}
    ]


def test_run_case_with_scripted_model_simple_hit():
    case = BfclCase(
        case_id="simple_fixture_0",
        category="simple",
        user="weather in Paris",
        functions=[_fn("get_weather")],
        possible_answer=[{"get_weather": {"city": ["Paris"]}}],
    )
    model = _ScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "get_weather",
                        "args": {"city": "Paris"},
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    row = run_case(
        case, strategy="managed", suite="bfcl-v4", run_id="t", model=model
    )
    assert row["error"] == ""
    assert row["ast_correct"] is True
    assert row["tool_calls"] == 1


def test_run_case_irrelevance_plain_text():
    case = BfclCase(
        case_id="irr_fixture_0",
        category="irrelevance",
        user="how are you",
        functions=[_fn("get_weather")],
        possible_answer=None,
    )
    model = _ScriptedModel([AIMessage(content="I cannot help with that.")])
    row = run_case(
        case, strategy="legacy", suite="bfcl-v4", run_id="t", model=model
    )
    assert row["ast_correct"] is True
    assert row["tool_calls"] == 0


def test_summarize_and_report_do_not_claim_leaderboard():
    rows = [
        {
            "strategy": "managed",
            "ast_correct": True,
            "category": "simple",
            "token_total": 10,
            "context_chars": 100,
            "latency_ms": 5,
            "tool_calls": 1,
            "error": "",
        }
    ]
    text = format_report(summarize(rows))
    assert "ast_acc" in text
    assert "公开榜排名" in text


def test_tool_from_bfcl_stub_returns_json():
    tool = tool_from_bfcl(_fn("get_weather"))
    out = tool.invoke({"city": "Paris"})
    assert "Paris" in out
    assert tool.name == "get_weather"
