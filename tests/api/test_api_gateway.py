"""
FastAPI 网关：鉴权 / 会话归属 / 并发锁 / 响应形状。

全部 mock ``work_agent.runtime`` 的 step 函数，不调 LLM、不起真图——图本身的正确性
由 tests/runtime/test_runtime_step.py 和各节点自己的单测保管，这里只测网关这一层自己的逻辑。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

import work_agent.api.app as app_mod
from work_agent.api.app import app
from work_agent.api.locks import try_acquire_thread_lock

client = TestClient(app)
AUTH = {"X-User-Id": "z00888363"}


def test_missing_auth_header_returns_401():
    r = client.post("/turns", json={"message": "hi"})
    assert r.status_code == 401


def test_invalid_auth_header_format_returns_401():
    r = client.post("/turns", json={"message": "hi"}, headers={"X-User-Id": "!!!"})
    assert r.status_code == 401


def test_create_turn_done_status(monkeypatch):
    monkeypatch.setattr(
        app_mod.runtime,
        "run_turn_step",
        lambda text, *, thread_id=None, user_id="": {
            "_thread_id": thread_id,
            "reply": "好的",
            "summary": {"status": "ok"},
        },
    )

    r = client.post("/turns", json={"message": "hi"}, headers=AUTH)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["reply"] == "好的"
    assert body["thread_id"].startswith("z00888363-")


def test_create_turn_passes_user_id_into_runtime(monkeypatch):
    seen = {}

    def fake_run_turn_step(text, *, thread_id=None, user_id=""):
        seen["user_id"] = user_id
        return {"_thread_id": thread_id, "reply": "好的", "summary": {"status": "ok"}}

    monkeypatch.setattr(app_mod.runtime, "run_turn_step", fake_run_turn_step)

    r = client.post("/turns", json={"message": "hi"}, headers=AUTH)

    assert r.status_code == 200
    assert seen["user_id"] == "z00888363"


def test_create_turn_waiting_input_status(monkeypatch):
    monkeypatch.setattr(
        app_mod.runtime,
        "run_turn_step",
        lambda text, *, thread_id=None, user_id="": {
            "_thread_id": thread_id,
            "__interrupt__": [{"type": "ask_env", "message": "填环境"}],
        },
    )

    r = client.post("/turns", json={"message": "跑一下"}, headers=AUTH)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "waiting_input"
    assert body["interrupt"] == [{"type": "ask_env", "message": "填环境"}]
    assert body["reply"] is None


def test_create_turn_rejects_thread_id_of_another_user(monkeypatch):
    monkeypatch.setattr(app_mod.runtime, "run_turn_step", lambda *a, **k: {})

    r = client.post(
        "/turns",
        json={"message": "hi", "thread_id": "z00099999-abc123"},
        headers=AUTH,
    )

    assert r.status_code == 403


def test_get_turn_status_done(monkeypatch):
    monkeypatch.setattr(
        app_mod.runtime,
        "get_turn_status",
        lambda thread_id: {"_thread_id": thread_id, "reply": "done", "summary": {}},
    )

    r = client.get("/turns/z00888363-abc123", headers=AUTH)

    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_get_turn_status_waiting_input(monkeypatch):
    monkeypatch.setattr(
        app_mod.runtime,
        "get_turn_status",
        lambda thread_id: {
            "_thread_id": thread_id,
            "__interrupt__": [{"type": "ask_env", "message": "填环境"}],
        },
    )

    r = client.get("/turns/z00888363-abc123", headers=AUTH)

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "waiting_input"
    assert body["interrupt"] == [{"type": "ask_env", "message": "填环境"}]


def test_get_turn_status_unknown_thread_returns_404(monkeypatch):
    monkeypatch.setattr(app_mod.runtime, "get_turn_status", lambda thread_id: None)

    r = client.get("/turns/z00888363-abc123", headers=AUTH)

    assert r.status_code == 404


def test_get_turn_status_rejects_other_users_thread(monkeypatch):
    monkeypatch.setattr(app_mod.runtime, "get_turn_status", lambda thread_id: {})

    r = client.get("/turns/z00099999-abc123", headers=AUTH)

    assert r.status_code == 403


def test_resume_turn_success(monkeypatch):
    monkeypatch.setattr(
        app_mod.runtime,
        "resume_step",
        lambda thread_id, answer: {"_thread_id": thread_id, "reply": "done", "summary": {}},
    )

    r = client.post(
        "/turns/z00888363-abc123/resume", json={"answer": "7.223.50.60"}, headers=AUTH
    )

    assert r.status_code == 200
    assert r.json()["status"] == "done"


def test_resume_turn_accepts_structured_multi_select(monkeypatch):
    seen = {}

    def fake_resume(thread_id, answer):
        seen["answer"] = answer
        return {"_thread_id": thread_id, "reply": "done", "summary": {}}

    monkeypatch.setattr(app_mod.runtime, "resume_step", fake_resume)
    r = client.post(
        "/turns/z00888363-abc123/resume",
        json={"answer": {"indices": [1, 3]}},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert seen["answer"] == {"indices": [1, 3]}


def test_resume_turn_no_pending_returns_404(monkeypatch):
    monkeypatch.setattr(app_mod.runtime, "resume_step", lambda thread_id, answer: None)

    r = client.post(
        "/turns/z00888363-abc123/resume", json={"answer": "7.223.50.60"}, headers=AUTH
    )

    assert r.status_code == 404


def test_resume_turn_rejects_other_users_thread(monkeypatch):
    monkeypatch.setattr(app_mod.runtime, "resume_step", lambda *a, **k: {})

    r = client.post(
        "/turns/z00099999-abc123/resume", json={"answer": "hi"}, headers=AUTH
    )

    assert r.status_code == 403


def test_concurrent_request_on_same_thread_returns_409(monkeypatch):
    monkeypatch.setattr(
        app_mod.runtime,
        "resume_step",
        lambda thread_id, answer: {"_thread_id": thread_id, "reply": "done", "summary": {}},
    )

    thread_id = "z00888363-locktest"
    lock = try_acquire_thread_lock(thread_id)
    assert lock is not None
    try:
        r = client.post(
            f"/turns/{thread_id}/resume", json={"answer": "hi"}, headers=AUTH
        )
        assert r.status_code == 409
    finally:
        lock.release()


def test_get_sessions_filters_by_user_prefix(monkeypatch):
    fake_sessions = [
        type(
            "S", (), {"thread_id": "z00888363-aaa", "updated_at": "1", "preview": "p1", "pending": False}
        )(),
        type(
            "S", (), {"thread_id": "z00099999-bbb", "updated_at": "2", "preview": "p2", "pending": False}
        )(),
    ]
    monkeypatch.setattr(app_mod, "list_sessions", lambda limit=20: fake_sessions)

    r = client.get("/sessions", headers=AUTH)

    assert r.status_code == 200
    body = r.json()
    assert [s["thread_id"] for s in body] == ["z00888363-aaa"]


def test_get_config_requires_auth():
    r = client.get("/users/me/config")
    assert r.status_code == 401


def test_get_config_unset_returns_null_debug_mode(pg_env, pg_pool):
    uid = f"z{uuid.uuid4().hex[:8]}"
    try:
        r = client.get("/users/me/config", headers={"X-User-Id": uid})
        assert r.status_code == 200
        assert r.json() == {"debug_mode": None, "version_space": None}
    finally:
        with pg_pool.connection() as conn:
            conn.execute("DELETE FROM user_config WHERE user_id=%s", (uid,))


def test_patch_config_roundtrip(pg_env, pg_pool):
    uid = f"z{uuid.uuid4().hex[:8]}"
    headers = {"X-User-Id": uid}
    try:
        r = client.patch(
            "/users/me/config", json={"debug_mode": True}, headers=headers
        )
        assert r.status_code == 200
        assert r.json() == {"debug_mode": True, "version_space": None}

        r = client.get("/users/me/config", headers=headers)
        assert r.json() == {"debug_mode": True, "version_space": None}

        r = client.patch(
            "/users/me/config", json={"debug_mode": False}, headers=headers
        )
        assert r.json() == {"debug_mode": False, "version_space": None}
    finally:
        with pg_pool.connection() as conn:
            conn.execute("DELETE FROM user_config WHERE user_id=%s", (uid,))


def test_patch_config_version_space_roundtrip_and_merge(pg_env, pg_pool):
    uid = f"z{uuid.uuid4().hex[:8]}"
    headers = {"X-User-Id": uid}
    try:
        r = client.patch(
            "/users/me/config", json={"version_space": "27b"}, headers=headers
        )
        assert r.status_code == 200
        assert r.json() == {"debug_mode": None, "version_space": "27B"}

        r = client.patch(
            "/users/me/config", json={"debug_mode": True}, headers=headers
        )
        assert r.json() == {"debug_mode": True, "version_space": "27B"}

        r = client.patch(
            "/users/me/config", json={"version_space": None}, headers=headers
        )
        assert r.json() == {"debug_mode": True, "version_space": None}
    finally:
        with pg_pool.connection() as conn:
            conn.execute("DELETE FROM user_config WHERE user_id=%s", (uid,))


def test_patch_config_rejects_unknown_version_space(pg_env, pg_pool):
    uid = f"z{uuid.uuid4().hex[:8]}"
    try:
        r = client.patch(
            "/users/me/config",
            json={"version_space": "28C"},
            headers={"X-User-Id": uid},
        )
        assert r.status_code == 422
    finally:
        with pg_pool.connection() as conn:
            conn.execute("DELETE FROM user_config WHERE user_id=%s", (uid,))
