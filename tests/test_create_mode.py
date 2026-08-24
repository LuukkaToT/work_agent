"""create 环境模式：物理 IP 与逻辑组网+约束互斥。"""

import pytest

from work_agent.tools.create_mode import create_kwargs_from_plan, resolve_create_env


def test_resolve_physical():
    kind, env, constraint = resolve_create_env(physical_env="7.223.50.60")
    assert (kind, env, constraint) == ("physical", "7.223.50.60", "")


def test_resolve_logical():
    kind, env, constraint = resolve_create_env(
        logic_env="BESA_SDV_2BBH_1BBL", logic_constraint="1G_2A"
    )
    assert (kind, env, constraint) == ("logical", "BESA_SDV_2BBH_1BBL", "1G_2A")


def test_resolve_rejects_both():
    with pytest.raises(ValueError, match="二选一"):
        resolve_create_env(
            physical_env="7.223.50.60",
            logic_env="BESA_SDV_2BBH_1BBL",
            logic_constraint="1G_2A",
        )


def test_resolve_rejects_neither():
    with pytest.raises(ValueError, match="必须指定"):
        resolve_create_env()


def test_kwargs_from_physical_plan():
    env, kind, constraint, kwargs = create_kwargs_from_plan(
        {"env": "7.223.50.60", "env_kind": "physical"}
    )
    assert kind == "physical"
    assert env == "7.223.50.60"
    assert constraint == ""
    assert kwargs == {"physical_env": "7.223.50.60"}


def test_kwargs_from_logical_plan():
    env, kind, constraint, kwargs = create_kwargs_from_plan(
        {
            "env": "BESA_SDV_2BBH_1BBL",
            "env_kind": "logical",
            "logic_constraint": "1G_2A",
        }
    )
    assert kwargs == {
        "logic_env": "BESA_SDV_2BBH_1BBL",
        "logic_constraint": "1G_2A",
    }
    assert (kind, env, constraint) == ("logical", "BESA_SDV_2BBH_1BBL", "1G_2A")
