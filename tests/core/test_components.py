"""组件 Registry 与 Skill Router：按文件加载，不调模型。"""

from __future__ import annotations

import pytest

from work_agent.core.components import (
    get_component,
    load_component_registry,
    load_component_skill,
)
from work_agent.core.config import get_settings
from work_agent.tools.mock.scenarios import LOG_COMPONENTS


def test_registry_covers_six_components_in_stable_order():
    registry = load_component_registry()
    assert tuple(spec.id for spec in registry.components) == LOG_COMPONENTS
    bbh = get_component("BBH")
    assert bbh.id == "bbh"
    assert bbh.log_component == "bbh"
    assert "bbl" in bbh.related
    assert "fetch_logs" in bbh.tools


def test_unknown_component_is_rejected():
    with pytest.raises(KeyError, match="未知组件"):
        get_component("phy")


def test_skill_router_loads_markdown_without_model():
    prompt = load_component_skill("comm")
    assert "通用取证 SOP" in prompt
    assert "COMM 检查点" in prompt
    assert "不得创建其他 Agent" in prompt


def test_each_component_skill_has_specialized_checkpoints():
    markers = {
        "comm": "Connection refused",
        "rat": "cascade=true",
        "bbh": "ptp_state",
        "bbl": "CELL_ACTIVATION_TIMEOUT",
        "marp": "ANTENNA_MAP_MISSING",
        "compare": "KPI_OUT_OF_TOLERANCE",
    }
    for cid, marker in markers.items():
        text = load_component_skill(cid)
        assert marker in text, cid


def test_profile_diagnosis_engine_defaults_to_legacy():
    assert get_settings().profile.diagnosis_engine == "legacy"
    assert get_settings().profile.diagnosis_max_workers == 3
