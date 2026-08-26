"""core/policy.py：POLICIES 表 + assert_read_only_whitelist 自检。"""

from __future__ import annotations

import pytest

from work_agent.core.policy import POLICIES, assert_read_only_whitelist

_DIAGNOSE_TOOL_NAMES = [
    "get_pipeline_status",
    "fetch_logs",
    "grep_logs",
    "find_case_history",
    "get_case_spec",
    "search_knowledge",
]


def test_assert_read_only_whitelist_passes_for_diagnose_tools():
    assert_read_only_whitelist(_DIAGNOSE_TOOL_NAMES)  # 不抛异常


def test_assert_read_only_whitelist_rejects_write_action():
    with pytest.raises(ValueError, match="非只读"):
        assert_read_only_whitelist(["create_pipeline"])


def test_assert_read_only_whitelist_rejects_unregistered_name():
    with pytest.raises(ValueError, match="未在 POLICIES 注册"):
        assert_read_only_whitelist(["delete_everything"])


def test_create_and_start_pipeline_require_confirmation():
    assert POLICIES["create_pipeline"].requires_confirmation is True
    assert POLICIES["start_pipeline"].requires_confirmation is True
    assert POLICIES["create_pipeline"].read_only is False
    assert POLICIES["start_pipeline"].read_only is False


def test_query_pipeline_is_read_only():
    assert POLICIES["query_pipeline"].read_only is True
    assert POLICIES["query_pipeline"].requires_confirmation is False


def test_fetch_archived_block_is_registered_and_read_only():
    policy = POLICIES.get("fetch_archived_block")
    assert policy is not None, "回读工具必须在 POLICIES 注册，否则构造期自检会拦"
    assert policy.read_only is True
    assert policy.requires_confirmation is False
    # 回读工具也要能通过只读白名单自检
    assert_read_only_whitelist(["fetch_archived_block"])
