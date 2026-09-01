"""探针、request_id、500 脱敏与 /metrics。"""

from __future__ import annotations

from fastapi.testclient import TestClient

import work_agent.api.app as app_mod
from work_agent.api.app import app

client = TestClient(app)
AUTH = {"X-User-Id": "z00888363"}


def test_healthz_does_not_need_auth():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.headers.get("x-request-id")


def test_readyz_unavailable_without_postgres(monkeypatch):
    monkeypatch.setattr("work_agent.api.app.postgres_ready", lambda: (False, "no_dsn"))
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["status"] == "unavailable"


def test_readyz_ok(monkeypatch):
    monkeypatch.setattr("work_agent.api.app.postgres_ready", lambda: (True, "ok"))
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_metrics_exposes_work_agent_series():
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "work_agent_turns_total" in body
    assert "work_agent_turn_duration_seconds" in body


def test_request_id_echoes_incoming_header(monkeypatch):
    monkeypatch.setattr(
        app_mod.runtime,
        "run_turn_step",
        lambda text, *, thread_id=None, user_id="": {
            "_thread_id": thread_id,
            "reply": "ok",
            "summary": {},
        },
    )
    r = client.post(
        "/turns",
        json={"message": "hi"},
        headers={**AUTH, "X-Request-Id": "req-from-gateway"},
    )
    assert r.status_code == 200
    assert r.headers.get("x-request-id") == "req-from-gateway"


def test_unhandled_error_is_500_without_exception_text(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("user secret should not leak")

    monkeypatch.setattr(app_mod._turn_service, "turn", boom)
    r = client.post("/turns", json={"message": "hi"}, headers=AUTH)
    assert r.status_code == 500
    assert r.json() == {"detail": "internal error"}
    assert "secret" not in r.text
    assert r.headers.get("x-request-id")
