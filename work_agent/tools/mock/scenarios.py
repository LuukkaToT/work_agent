"""共享的 mock benchmark 场景目录。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TypeAlias

MockScenario: TypeAlias = str

LOG_COMPONENTS: tuple[str, ...] = (
    "comm",
    "rat",
    "bbh",
    "bbl",
    "marp",
    "compare",
)


@dataclass(frozen=True)
class BenchmarkScenario:
    """一组分层日志及其离线期望。"""

    scenario: str
    case_id: str
    title: str
    fail_kind: str
    root_component: str
    root_cause: str
    expected_evidence_keys: tuple[str, ...]
    user_input: str
    pipeline: dict[str, object]
    events: dict[str, tuple[str, ...]]

    @property
    def is_environment_failure(self) -> bool:
        return self.fail_kind == "env"


def kb_root() -> Path:
    return Path(__file__).resolve().parent / "mock_5g_fault_kb"


def benchmark_logs_root() -> Path:
    return kb_root() / "logs" / "benchmark"


def benchmark_cases_path() -> Path:
    return kb_root() / "dataset" / "benchmark_cases.json"


def error_codes_path() -> Path:
    return kb_root() / "dataset" / "error_codes.json"


@lru_cache(maxsize=1)
def load_benchmark_scenarios() -> tuple[BenchmarkScenario, ...]:
    """读取受版本控制的 20 组 synthetic benchmark 定义。"""
    raw = json.loads(benchmark_cases_path().read_text(encoding="utf-8"))
    out: list[BenchmarkScenario] = []
    for item in raw.get("cases") or []:
        events = {
            str(component): tuple(str(line) for line in lines)
            for component, lines in dict(item.get("events") or {}).items()
        }
        unknown = set(events) - set(LOG_COMPONENTS)
        if unknown:
            raise ValueError(
                f"benchmark scenario {item.get('scenario')!r} 包含未知日志组件: {sorted(unknown)}"
            )
        out.append(
            BenchmarkScenario(
                scenario=str(item["scenario"]),
                case_id=str(item["case_id"]),
                title=str(item["title"]),
                fail_kind=str(item["fail_kind"]),
                root_component=str(item["root_component"]),
                root_cause=str(item["root_cause"]),
                expected_evidence_keys=tuple(
                    str(key) for key in item.get("expected_evidence_keys") or []
                ),
                user_input=str(item.get("user_input") or ""),
                pipeline=dict(item.get("pipeline") or {}),
                events=events,
            )
        )
    if len(out) != 20:
        raise ValueError(f"benchmark 场景应为 20 组，实际 {len(out)}")
    names = [item.scenario for item in out]
    if len(names) != len(set(names)):
        raise ValueError("benchmark scenario 名称重复")
    return tuple(out)


@lru_cache(maxsize=32)
def get_benchmark_scenario(name: str) -> BenchmarkScenario | None:
    """按 scenario 名返回定义；四个旧场景不在此目录时返回 None。"""
    return next((item for item in load_benchmark_scenarios() if item.scenario == name), None)


def scenario_fail_kind(name: str) -> str:
    """统一解析旧场景与 20 组 benchmark 的归因类别。"""
    legacy = {
        "all_pass": "none",
        "version_fail": "version",
        "case_error": "case",
        "env_error": "env",
    }
    if name in legacy:
        return legacy[name]
    item = get_benchmark_scenario(name)
    if item is None:
        raise ValueError(f"未知 mock scenario: {name!r}")
    return item.fail_kind


def clear_scenario_caches() -> None:
    """单测/重生成数据后清理目录缓存。"""
    load_benchmark_scenarios.cache_clear()
    get_benchmark_scenario.cache_clear()
