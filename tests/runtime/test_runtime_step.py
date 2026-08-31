"""
run_turn_step / resume_step：非阻塞版 run_turn / resume_pending，专给 HTTP 网关用。

跟 test_runtime_on_event.py 一样用假 app（mock stream/get_state），不调 LLM、不起真图。
关键区别：这两个函数遇到 interrupt 立刻返回，不在内部循环等 ask() 的答案。
"""

from __future__ import annotations

from types import SimpleNamespace

from work_agent.runtime import get_turn_status, resume_step, run_turn_step


class _FakeApp:
    """模拟 LangGraph app.stream / get_state（同 test_runtime_on_event.py）。"""

    def __init__(self, *, updates: list[dict] | None = None, values: dict | None = None):
        self.updates = list(updates or [{"intake": {"x": 1}}, {"router": {"intent": "query"}}])
        self.values = dict(values or {"reply": "ok", "intent": "query", "summary": {}})
        self.stream_payloads: list[object] = []

    def stream(self, payload, config=None, stream_mode="updates"):
        self.stream_payloads.append(payload)
        for u in self.updates:
            yield u

    def get_state(self, config):
        return SimpleNamespace(values=self.values, tasks=(), next=(), interrupts=())


def test_run_turn_step_returns_final_result_without_interrupt(monkeypatch):
    app = _FakeApp()
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )

    result = run_turn_step("hello", thread_id="z00001-abc123")

    assert result["reply"] == "ok"
    assert result["_thread_id"] == "z00001-abc123"
    assert "__interrupt__" not in result


def test_run_turn_step_stops_immediately_at_first_interrupt(monkeypatch):
    interrupt = SimpleNamespace(value={"type": "ask_env", "message": "填环境"})

    class InterruptApp:
        def stream(self, payload, config=None, stream_mode="updates"):
            yield {"intake": {}}
            yield {"__interrupt__": (interrupt,)}

        def get_state(self, config):
            return SimpleNamespace(
                values={"intent": "execute"},
                tasks=(SimpleNamespace(interrupts=(interrupt,)),),
                next=("exec_params",),
                interrupts=(),
            )

    app = InterruptApp()
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )

    result = run_turn_step("跑一下", thread_id="z00001-hitl")

    # 不像 run_turn：这里不会自己去问 ask()，interrupt 原样带回去
    assert result["_thread_id"] == "z00001-hitl"
    interrupts = result.get("__interrupt__")
    assert interrupts and interrupts[0] is interrupt


def test_run_turn_step_generates_thread_id_with_user_id_prefix(monkeypatch):
    app = _FakeApp()
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )

    result = run_turn_step("hello", user_id="z00888363")

    assert result["_thread_id"].startswith("z00888363-")


def test_run_turn_step_defaults_thread_prefix_to_cli_without_user_id(monkeypatch):
    app = _FakeApp()
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )

    result = run_turn_step("hello")

    assert result["_thread_id"].startswith("cli-")


def test_run_turn_step_passes_user_id_into_graph_payload(monkeypatch):
    app = _FakeApp()
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )

    run_turn_step("hello", thread_id="z00001-abc", user_id="z00888363")

    assert app.stream_payloads[0]["user_id"] == "z00888363"


def test_resume_step_returns_none_without_pending_interrupt(monkeypatch):
    monkeypatch.setattr("work_agent.runtime.get_pending_interrupts", lambda tid: [])

    result = resume_step("z00001-none", "some answer")

    assert result is None


def test_resume_step_runs_one_round_and_returns(monkeypatch):
    app = _FakeApp(updates=[{"router": {"intent": "query"}}], values={"reply": "resumed"})
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )
    monkeypatch.setattr(
        "work_agent.runtime.get_pending_interrupts", lambda tid: [{"type": "ask_env"}]
    )

    result = resume_step("z00001-resume", "7.223.50.60")

    assert result is not None
    assert result["reply"] == "resumed"
    assert result["_thread_id"] == "z00001-resume"


def test_get_turn_status_returns_none_for_unknown_thread(monkeypatch):
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.thread_checkpoint_exists", lambda saver, tid: False
    )

    assert get_turn_status("z00001-unknown") is None


def test_get_turn_status_returns_none_for_empty_thread_id():
    assert get_turn_status("") is None


def test_get_turn_status_returns_final_state_without_interrupt(monkeypatch):
    app = _FakeApp(values={"reply": "done", "summary": {"status": "ok"}})
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.thread_checkpoint_exists", lambda saver, tid: True
    )
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )

    result = get_turn_status("z00001-done")

    assert result is not None
    assert result["reply"] == "done"
    assert result["_thread_id"] == "z00001-done"
    assert "__interrupt__" not in result


def test_get_turn_status_returns_pending_interrupt(monkeypatch):
    interrupt = SimpleNamespace(value={"type": "ask_env", "message": "填环境"})

    class PendingApp:
        def get_state(self, config):
            return SimpleNamespace(
                values={"intent": "execute"},
                tasks=(SimpleNamespace(interrupts=(interrupt,)),),
                next=("exec_params",),
                interrupts=(),
            )

    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.thread_checkpoint_exists", lambda saver, tid: True
    )
    monkeypatch.setattr(
        "work_agent.runtime.build_graph", lambda checkpointer=None: PendingApp()
    )
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )

    result = get_turn_status("z00001-pending")

    assert result is not None
    interrupts = result.get("__interrupt__")
    assert interrupts and interrupts[0] is interrupt.value


def test_resume_step_can_return_another_interrupt(monkeypatch):
    next_interrupt = SimpleNamespace(value={"type": "ask_version", "message": "填版本"})

    class OneMoreInterruptApp:
        def stream(self, payload, config=None, stream_mode="updates"):
            yield {"__interrupt__": (next_interrupt,)}

        def get_state(self, config):
            return SimpleNamespace(
                values={"intent": "execute"},
                tasks=(SimpleNamespace(interrupts=(next_interrupt,)),),
                next=("exec_params",),
                interrupts=(),
            )

    app = OneMoreInterruptApp()
    monkeypatch.setattr("work_agent.runtime.build_graph", lambda checkpointer=None: app)
    monkeypatch.setattr("work_agent.runtime.get_checkpointer", lambda: object())
    monkeypatch.setattr(
        "work_agent.runtime.make_thread_config",
        lambda tid: {"configurable": {"thread_id": tid}},
    )
    monkeypatch.setattr(
        "work_agent.runtime.get_pending_interrupts", lambda tid: [{"type": "ask_env"}]
    )

    result = resume_step("z00001-chain", "7.223.50.60")

    assert result is not None
    interrupts = result.get("__interrupt__")
    assert interrupts and interrupts[0] is next_interrupt
