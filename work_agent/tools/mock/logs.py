"""Mock 日志工具：兼容旧长日志，并支持六组件分层 benchmark。"""

from __future__ import annotations

import json
import random
import re
from functools import lru_cache

from work_agent.tools.mock.scenarios import (
    LOG_COMPONENTS,
    MockScenario,
    benchmark_logs_root,
    error_codes_path,
    get_benchmark_scenario,
    scenario_fail_kind,
)

# 旧四场景继续生成 8000 行，避免破坏上下文压缩回归测试。20 组 benchmark 则从
# 仓库内的 120 个 .log 文件读取，每组约 6600 行。
_DEFAULT_TOTAL_LINES = 8000
_ERROR_NOISE_LINES = 80
_PIPELINE_PLACEHOLDER = "{{PIPELINE_ID}}"


class MockLogTool:
    """按 scenario 提供可列举、分组件拉取、跨组件 grep 的只读日志。"""

    def __init__(
        self,
        scenario: MockScenario = "case_error",
        *,
        total_lines: int = _DEFAULT_TOTAL_LINES,
        error_noise_lines: int = _ERROR_NOISE_LINES,
    ) -> None:
        scenario_fail_kind(scenario)  # 构造期校验名称。
        self.scenario = scenario
        self.total_lines = max(50, total_lines)
        self.error_noise_lines = max(0, error_noise_lines)

    @property
    def is_layered(self) -> bool:
        """当前是否为六组件 benchmark 场景。"""
        return get_benchmark_scenario(self.scenario) is not None

    def list_logs(self, pipeline_id: str) -> str:
        """列出可用日志文件、组件、行数与 UTF-8 字节数。"""
        pid = (pipeline_id or "").strip() or "(empty)"
        if not self.is_layered:
            lines = self._legacy_lines(pid)
            payload = {
                "pipeline_id": pid,
                "scenario": self.scenario,
                "layered": False,
                "files": [
                    {
                        "file": "pipeline.log",
                        "component": "all",
                        "lines": len(lines),
                        "bytes": len("\n".join(lines).encode("utf-8")),
                    }
                ],
            }
            return json.dumps(payload, ensure_ascii=False, indent=2)

        files = []
        for component in LOG_COMPONENTS:
            template = _read_component_template(self.scenario, component)
            files.append(
                {
                    "file": f"{component}.log",
                    "component": component,
                    "lines": len(template),
                    "bytes": len(("\n".join(template) + "\n").encode("utf-8")),
                }
            )
        payload = {
            "pipeline_id": pid,
            "scenario": self.scenario,
            "layered": True,
            "components": list(LOG_COMPONENTS),
            "files": files,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def fetch_logs(
        self,
        pipeline_id: str,
        *,
        tail_lines: int | None = 200,
        component: str | None = None,
    ) -> str:
        """拉取单组件日志或六组件按时间排序后的合并时间线。"""
        pid = (pipeline_id or "").strip() or "(empty)"
        selected = self._normalize_component(component)
        lines = self._full_lines(pid, selected)
        total = len(lines)
        if tail_lines is None or tail_lines >= total:
            body = lines
            truncated = False
        else:
            count = max(0, tail_lines)
            body = lines[-count:] if count else ()
            truncated = True
        label = selected or ("all" if self.is_layered else "pipeline")
        meta = (
            f"[log meta] pipeline_id={pid} scenario={self.scenario} "
            f"component={label} total_lines={total} returned_lines={len(body)} "
            f"truncated={str(truncated).lower()}"
        )
        return meta + "\n" + "\n".join(body)

    def grep_logs(
        self,
        pipeline_id: str,
        pattern: str,
        *,
        context_lines: int = 3,
        max_matches: int = 20,
        component: str | None = None,
    ) -> str:
        """在单组件或合并时间线上按正则检索，返回命中及上下文。"""
        pid = (pipeline_id or "").strip() or "(empty)"
        selected = self._normalize_component(component)
        lines = self._full_lines(pid, selected)
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return f"[grep error] 非法正则: {exc}"

        limit = max(1, max_matches)
        hits: list[int] = []
        for index, line in enumerate(lines):
            if rx.search(line):
                hits.append(index)
            if len(hits) >= limit:
                break

        label = selected or ("all" if self.is_layered else "pipeline")
        if not hits:
            return f"[grep] component={label} pattern={pattern!r} matches=0"

        blocks = [
            f"[grep] component={label} pattern={pattern!r} matches={len(hits)}"
            + (" (truncated)" if len(hits) >= limit else "")
        ]
        ctx = max(0, context_lines)
        for index in hits:
            start = max(0, index - ctx)
            end = min(len(lines), index + ctx + 1)
            blocks.append(f"--- match at line {index + 1} ---")
            for line_index in range(start, end):
                mark = ">" if line_index == index else " "
                blocks.append(f"{mark} {line_index + 1}: {lines[line_index]}")
        return "\n".join(blocks)

    def lookup_error_code(self, code: str) -> str:
        """按完整错误码或摘要关键词查询 synthetic 错误码目录。"""
        query = (code or "").strip().lower()
        if not query:
            return "[error-code] empty query"
        matches = []
        for item in _read_error_codes():
            haystack = " ".join(
                [
                    str(item.get("code") or ""),
                    str(item.get("summary") or ""),
                    " ".join(str(value) for value in item.get("probable_components") or []),
                ]
            ).lower()
            if query in haystack:
                matches.append(item)
        if not matches:
            return f"[error-code] query={code!r} matches=0"
        return json.dumps(
            {"query": code, "matches": len(matches), "codes": matches[:10]},
            ensure_ascii=False,
            indent=2,
        )

    def _normalize_component(self, component: str | None) -> str | None:
        value = (component or "").strip().lower()
        if not value or value in {"all", "*", "merged"}:
            return None
        if not self.is_layered:
            raise ValueError("旧 mock 场景只有 pipeline.log，不支持 component 参数")
        if value not in LOG_COMPONENTS:
            raise ValueError(
                f"未知日志组件 {component!r}；可选: {', '.join(LOG_COMPONENTS)}"
            )
        return value

    @lru_cache(maxsize=64)
    def _full_lines(self, pipeline_id: str, component: str | None) -> tuple[str, ...]:
        """返回替换过 pipeline_id 的确定性日志；合并模式按时间戳排序。"""
        if not self.is_layered:
            return tuple(self._legacy_lines(pipeline_id))

        components = (component,) if component else LOG_COMPONENTS
        lines: list[str] = []
        for name in components:
            template = _read_component_template(self.scenario, name)
            lines.extend(line.replace(_PIPELINE_PLACEHOLDER, pipeline_id) for line in template)
        if component is None:
            # 所有行都以 ISO 时间戳开头；整行排序也会自然用组件字段打破同毫秒并列。
            lines.sort()
        return tuple(lines)

    def _legacy_lines(self, pipeline_id: str) -> list[str]:
        """保留旧四场景的 8000 行生成逻辑。"""
        seed = _stable_seed(f"{pipeline_id}:{self.scenario}")
        rng = random.Random(seed)
        signature = _legacy_signature_lines(self.scenario)
        error_noise = (
            []
            if self.scenario == "all_pass"
            else _error_noise_lines(rng, self.error_noise_lines)
        )
        header = [
            f"[mock-log] pipeline_id={pipeline_id} scenario={self.scenario}",
            "2026-08-09 10:00:00 INFO runner boot ok",
            "2026-08-09 10:00:01 INFO start case CaseA_235T_nmimo",
        ]
        reserved = len(header) + len(error_noise) + len(signature)
        info_n = max(0, self.total_lines - reserved)
        lines = list(header)
        for index in range(info_n):
            sec = 2 + (index % 50)
            kind = rng.choice(["INFO", "INFO", "INFO", "DEBUG", "WARN"])
            msg = rng.choice(
                [
                    "heartbeat ok",
                    "poll kpi sample",
                    "sync config slice",
                    "buffer flush",
                    "wait slot grant",
                ]
            )
            lines.append(f"2026-08-09 10:00:{sec:02d} {kind} {msg} seq={index}")
        lines.extend(error_noise)
        lines.extend(signature)
        return lines


@lru_cache(maxsize=128)
def _read_component_template(scenario: str, component: str) -> tuple[str, ...]:
    path = benchmark_logs_root() / scenario / f"{component}.log"
    if not path.is_file():
        raise FileNotFoundError(f"benchmark 分层日志不存在: {path}")
    return tuple(path.read_text(encoding="utf-8").splitlines())


@lru_cache(maxsize=1)
def _read_error_codes() -> tuple[dict[str, object], ...]:
    raw = json.loads(error_codes_path().read_text(encoding="utf-8"))
    return tuple(dict(item) for item in raw.get("codes") or [])


def _stable_seed(value: str) -> int:
    """不用进程随机化的 hash()，保证跨进程生成完全一致。"""
    out = 2166136261
    for char in value:
        out ^= ord(char)
        out = (out * 16777619) & 0xFFFFFFFF
    return out


def _legacy_signature_lines(scenario: MockScenario) -> list[str]:
    if scenario == "all_pass":
        return [
            "2026-08-09 10:05:01 INFO assert kpi=0.995 threshold=0.99",
            "2026-08-09 10:05:02 INFO case finished verdict=pass",
        ]
    if scenario == "version_fail":
        return [
            "2026-08-09 10:05:01 ERROR protocol mismatch with peer",
            "2026-08-09 10:05:01 ERROR version 27B incompatible with env firmware",
            "2026-08-09 10:05:02 INFO case finished verdict=fail",
        ]
    if scenario == "case_error":
        return [
            "2026-08-09 10:05:01 ERROR AssertionError: KPI below threshold",
            "2026-08-09 10:05:01 ERROR Traceback (most recent call last):",
            '  File "case_runner.py", line 42, in run',
            "    cfg = params['antenna_map']",
            "KeyError: 'antenna_map'",
            "2026-08-09 10:05:02 INFO case finished verdict=fail",
        ]
    return [
        "2026-08-09 10:05:01 ERROR Connection refused to 7.223.50.60:22",
        "2026-08-09 10:05:01 ERROR node unreachable after 3 retries",
        "2026-08-09 10:05:02 INFO case finished verdict=error",
    ]


def _error_noise_lines(rng: random.Random, n: int) -> list[str]:
    """旧场景的非根因 ERROR 噪声；避免出现任何 golden 关键词。"""
    out = []
    messages = (
        "queue backpressure",
        "slot grant delayed",
        "kpi sample dropped",
        "sync slice lag",
        "buffer occupancy high",
    )
    forbidden = (
        "fail",
        "exception",
        "traceback",
        "rejected",
        "timeout",
        "refused",
        "keyerror",
        "mismatch",
    )
    for index in range(n):
        message = rng.choice(messages)
        line = f"2026-08-09 10:04:{index % 60:02d} ERROR {message} noise_seq={index}"
        assert not any(key in line.lower() for key in forbidden), line
        out.append(line)
    return out
