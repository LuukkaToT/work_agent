from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from work_agent.graph.nodes.error_analysis import extract_tool_trace


def test_extract_tool_trace_calls_and_results():
    messages = [
        HumanMessage(content="diagnose"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "1",
                    "name": "fetch_logs",
                    "args": {"pipeline_id": "abc", "tail_lines": 50},
                }
            ],
        ),
        ToolMessage(content="log body " * 10, tool_call_id="1", name="fetch_logs"),
    ]
    trace = extract_tool_trace(messages)
    assert trace[0]["type"] == "call"
    assert trace[0]["name"] == "fetch_logs"
    assert "abc" in trace[0]["args_preview"]
    assert trace[1]["type"] == "result"
    assert trace[1]["content_chars"] > 0