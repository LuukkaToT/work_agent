import json
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from work_agent.analysis.schemas import EvidenceHit
from work_agent.analysis.tools import RESEARCH_TOOLS
from work_agent.analysis.research import _agent_input_messages


class ToolState(TypedDict):
    allowed_channels: list[str]
    research_messages: Annotated[list[AnyMessage], add_messages]


class FakeRetriever:
    def search_channel(self, *, channel, queries, top_k):
        return [
            EvidenceHit(
                chunk_id="chunk-1",
                doc_id="doc-1",
                source_kind="channel",
                channel=channel,
                title="PUCCH",
                section="重配置",
                relative_path="PUCCH/a.md",
                content="检查资源切换。",
                score=3,
            )
        ]

    def search_basic(self, *, queries, dimensions, top_k):
        return []


def _invoke_tool(tool_call):
    graph = StateGraph(ToolState)
    graph.add_node(
        "tools",
        ToolNode(RESEARCH_TOOLS, messages_key="research_messages"),
    )
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    app = graph.compile()
    return app.invoke(
        {
            "allowed_channels": ["PUCCH"],
            "research_messages": [
                AIMessage(content="", tool_calls=[tool_call])
            ],
        }
    )["research_messages"][-1]


def test_channel_tool_receives_injected_whitelist(monkeypatch):
    monkeypatch.setattr(
        "work_agent.analysis.tools.get_knowledge_retriever",
        lambda: FakeRetriever(),
    )
    message = _invoke_tool(
        {
            "name": "search_channel_knowledge",
            "args": {
                "channel": "PUCCH",
                "query": "资源重配置",
                "top_k": 3,
            },
            "id": "call-1",
            "type": "tool_call",
        }
    )

    payload = json.loads(message.content)
    assert payload["hit_count"] == 1
    assert payload["hits"][0]["chunk_id"] == "chunk-1"


def test_channel_tool_rejects_out_of_scope_channel(monkeypatch):
    monkeypatch.setattr(
        "work_agent.analysis.tools.get_knowledge_retriever",
        lambda: FakeRetriever(),
    )
    message = _invoke_tool(
        {
            "name": "search_channel_knowledge",
            "args": {
                "channel": "PUSCH",
                "query": "越权查询",
                "top_k": 3,
            },
            "id": "call-2",
            "type": "tool_call",
        }
    )

    payload = json.loads(message.content)
    assert payload["error"] == "channel_not_allowed"
    assert payload["hits"] == []


def test_research_model_turn_does_not_replay_old_function_call():
    tool_payload = json.dumps(
        {
            "hits": [
                EvidenceHit(
                    chunk_id="chunk-1",
                    doc_id="doc-1",
                    source_kind="channel",
                    channel="PUCCH",
                    title="PUCCH",
                    section="配置",
                    relative_path="PUCCH/a.md",
                    content="检查资源配置。",
                ).model_dump(mode="json")
            ]
        },
        ensure_ascii=False,
    )
    state = {
        "requirement_fact": {
            "title": "PUCCH",
            "summary": "资源配置",
            "channels": ["PUCCH"],
        },
        "domain_task": {
            "task_id": "t1",
            "name": "PUCCH",
            "domain": "PUCCH",
            "channels": ["PUCCH"],
            "objectives": ["分析资源配置"],
        },
        "allowed_channels": ["PUCCH"],
        "research_messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "search_channel_knowledge",
                        "args": {"channel": "PUCCH", "query": "配置"},
                        "id": "old-call",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content=tool_payload,
                tool_call_id="old-call",
                name="search_channel_knowledge",
            ),
        ],
        "assessment": {},
    }

    messages = _agent_input_messages(state)

    assert all(not isinstance(message, AIMessage) for message in messages)
    assert "chunk-1" in messages[-1].content
