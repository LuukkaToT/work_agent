"""core/identity.py：CLI 侧身份来源。"""

from __future__ import annotations

import pytest

from work_agent.core.identity import EnvIdentityProvider, StaticIdentityProvider


def test_env_identity_reads_work_agent_user_id(monkeypatch):
    monkeypatch.setenv("WORK_AGENT_USER_ID", "z00888363")
    assert EnvIdentityProvider().get_user_id() == "z00888363"


def test_env_identity_falls_back_to_local_dev_when_unset(monkeypatch):
    monkeypatch.delenv("WORK_AGENT_USER_ID", raising=False)
    assert EnvIdentityProvider().get_user_id() == "local-dev"


def test_env_identity_falls_back_when_blank(monkeypatch):
    monkeypatch.setenv("WORK_AGENT_USER_ID", "   ")
    assert EnvIdentityProvider().get_user_id() == "local-dev"


def test_env_identity_strips_whitespace(monkeypatch):
    monkeypatch.setenv("WORK_AGENT_USER_ID", "  z00888363  ")
    assert EnvIdentityProvider().get_user_id() == "z00888363"


def test_static_identity_returns_fixed_user_id():
    assert StaticIdentityProvider("z00000001").get_user_id() == "z00000001"


def test_static_identity_rejects_empty_user_id():
    with pytest.raises(ValueError, match="不能为空"):
        StaticIdentityProvider("")
    with pytest.raises(ValueError, match="不能为空"):
        StaticIdentityProvider("   ")
