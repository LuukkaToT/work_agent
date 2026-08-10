"""runtime.on_event：用 mock stream 收集节点事件，不调 LLM。"""

from __future__ import annotations

from types import SimpleNamespace

from work_agent.runtime import resume_pending, run_turn


class _FakeApp:
    """模拟 LangGraph app.stream / get_state。"""

    def __init__(self, *, updates: list[dict] | None = None, values: dict | None = None):
        self.updates = list(updates or [{"intake": {"x": 1}}, {"router": {"intent": "query"}}])
        self.values = dict(values or {"reply": "ok", "intent": "query", "summary": {}})
        self.stream_calls: list[tuple] = []

    def stream(self, payload, config=None, stream_mode="updates"):
        self.stream_calls.append((payload, config, stream_mode))
        if stream_mode == ["updates", "values"] or (
            isinstance(stream_mode, list) and set(stream_mode) == {"updates", "values"}
        ):
            for u in self.updates:
                yield ("updates", u)
            yield ("values", self.values)
            return
        for u in self.updates:
            yield u

    def get_state(self, config):
        return SimpleNamespace(
            values=self.values,
            tasks=(),
            next=(),
            interrupts=(),
        )


def test_run_turn_on_event_collects_nodes(monkeypatch):
    app = _FakeApp()
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )

    events: list[str] = []
    result = run_turn(
        "hello",
        thread_id="cli-test01",
        with_checkpoint=True,
        on_event=events.append,
    )

    assert events == ["node:intake", "node:router"]
    assert result["reply"] == "ok"
    assert result["_thread_id"] == "cli-test01"


def test_run_turn_on_event_without_checkpoint(monkeypatch):
    app = _FakeApp()
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)

    events: list[str] = []
    result = run_turn(
        "hello",
        thread_id="cli-test02",
        with_checkpoint=False,
        on_event=events.append,
    )

    assert events == ["node:intake", "node:router"]
    assert result["reply"] == "ok"


def test_run_turn_emits_waiting_input_on_interrupt(monkeypatch):
    interrupt = SimpleNamespace(value={"type": "ask_env", "message": "填环境"})
    call_n = {"n": 0}

    class InterruptApp:
        def __init__(self):
            self.values = {"reply": "done", "intent": "execute"}

        def stream(self, payload, config=None, stream_mode="updates"):
            call_n["n"] += 1
            if call_n["n"] == 1:
                yield {"intake": {}}
                yield {"__interrupt__": (interrupt,)}
                return
            yield {"exec_params": {"env": "1.2.3.4"}}
            yield {"reply_node": {"reply": "done"}}

        def get_state(self, config):
            if call_n["n"] <= 1:
                return SimpleNamespace(
                    values={"intent": "execute"},
                    tasks=(SimpleNamespace(interrupts=(interrupt,)),),
                    next=("exec_params",),
                    interrupts=(),
                )
            return SimpleNamespace(
                values=self.values,
                tasks=(),
                next=(),
                interrupts=(),
            )

    app = InterruptApp()
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )

    events: list[str] = []
    result = run_turn(
        "跑一下",
        thread_id="cli-hitl",
        ask=lambda _payloads: "7.223.50.60",
        with_checkpoint=True,
        on_event=events.append,
    )

    assert "status:waiting_input" in events
    assert "node:intake" in events
    assert "node:exec_params" in events
    assert result["reply"] == "done"


def test_resume_pending_forwards_on_event(monkeypatch):
    app = _FakeApp(
        updates=[{"router": {"intent": "query"}}],
        values={"reply": "resumed"},
    )
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )
    monkeypatch.setattr(
        "work_agent.runtime.get_pending_interrupts",
        lambda tid: [{"type": "ask_env"}],
    )

    events: list[str] = []
    result = resume_pending(
        "cli-resume",
        ask=lambda _p: "ok",
        on_event=events.append,
    )
    assert result is not None
    assert events == ["node:router"]
    assert result["reply"] == "resumed"
