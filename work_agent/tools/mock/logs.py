"""Mock 日志 tool：按 scenario 生成可判别的长日志。"""

from __future__ import annotations

import random
import re
from typing import Literal

MockScenario = Literal["all_pass", "version_fail", "case_error", "env_error"]


class MockLogTool:
    """实现 LogTool：按 scenario 生成可 grep 的假日志。"""

    def __init__(
        self,
        scenario: MockScenario = "case_error",
        *,
        total_lines: int = 480,
    ) -> None:
        """
        参数:
            scenario: 决定尾部错误特征。
            total_lines: 生成日志总行数下限约 50。
        """
        self.scenario = scenario
        self.total_lines = max(50, total_lines)

    def fetch_logs(
        self,
        pipeline_id: str,
        *,
        tail_lines: int | None = 200,
    ) -> str:
        """
        拉取日志（可截尾）。

        参数:
            pipeline_id: 流水线 id（参与确定性随机种子）。
            tail_lines: None 返回全文；否则尾部 N 行。

        返回:
            带 ``[log meta]`` 首行的日志文本。
        """
        lines = self._full_lines(pipeline_id)
        total = len(lines)
        if tail_lines is None or tail_lines >= total:
            body = lines
            truncated = False
        else:
            body = lines[-tail_lines:]
            truncated = True
        meta = (
            f"[log meta] pipeline_id={pipeline_id or '(empty)'} "
            f"total_lines={total} returned_lines={len(body)} "
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
    ) -> str:
        """
        在全文上按正则检索，带上下文。

        参数:
            pipeline_id: 流水线 id。
            pattern: 正则；非法时返回错误说明。
            context_lines: 命中行前后保留行数。
            max_matches: 最多命中数。

        返回:
            可读 grep 结果文本。
        """
        lines = self._full_lines(pipeline_id)
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return f"[grep error] 非法正则: {exc}"

        hits: list[int] = []
        for i, line in enumerate(lines):
            if rx.search(line):
                hits.append(i)
            if len(hits) >= max_matches:
                break

        if not hits:
            return f"[grep] pattern={pattern!r} matches=0"

        blocks: list[str] = [
            f"[grep] pattern={pattern!r} matches={len(hits)}"
            + (" (truncated)" if len(hits) >= max_matches else "")
        ]
        ctx = max(0, context_lines)
        for idx in hits:
            start = max(0, idx - ctx)
            end = min(len(lines), idx + ctx + 1)
            blocks.append(f"--- match at line {idx + 1} ---")
            for j in range(start, end):
                mark = ">" if j == idx else " "
                blocks.append(f"{mark} {j + 1}: {lines[j]}")
        return "\n".join(blocks)

    def _full_lines(self, pipeline_id: str) -> list[str]:
        """按 pipeline_id+scenario 生成确定性假日志全文。"""
        pid = (pipeline_id or "").strip() or "(empty)"
        seed = hash(f"{pid}:{self.scenario}") % (2**32)
        rng = random.Random(seed)

        lines: list[str] = [
            f"[mock-log] pipeline_id={pid} scenario={self.scenario}",
            "2026-08-09 10:00:00 INFO runner boot ok",
            "2026-08-09 10:00:01 INFO start case CaseA_235T_nmimo",
        ]

        noise_n = self.total_lines - 30
        for i in range(max(0, noise_n)):
            sec = 2 + (i % 50)
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
            lines.append(f"2026-08-09 10:00:{sec:02d} {kind} {msg} seq={i}")

        # 错误特征放在尾部，保证 tail_lines=200 也能看到
        if self.scenario == "all_pass":
            lines.extend(
                [
                    "2026-08-09 10:05:01 INFO assert kpi=0.995 threshold=0.99",
                    "2026-08-09 10:05:02 INFO case finished verdict=pass",
                ]
            )
        elif self.scenario == "version_fail":
            lines.extend(
                [
                    "2026-08-09 10:05:01 ERROR protocol mismatch with peer",
                    "2026-08-09 10:05:01 ERROR version 27B incompatible with env firmware",
                    "2026-08-09 10:05:02 INFO case finished verdict=fail",
                ]
            )
        elif self.scenario == "case_error":
            lines.extend(
                [
                    "2026-08-09 10:05:01 ERROR AssertionError: KPI below threshold",
                    "2026-08-09 10:05:01 ERROR Traceback (most recent call last):",
                    '  File "case_runner.py", line 42, in run',
                    "    cfg = params['antenna_map']",
                    "KeyError: 'antenna_map'",
                    "2026-08-09 10:05:02 INFO case finished verdict=fail",
                ]
            )
        else:  # env_error
            lines.extend(
                [
                    "2026-08-09 10:05:01 ERROR Connection refused to 7.223.50.60:22",
                    "2026-08-09 10:05:01 ERROR node unreachable after 3 retries",
                    "2026-08-09 10:05:02 INFO case finished verdict=error",
                ]
            )

        # 补齐到接近 total_lines（若噪声不够）
        while len(lines) < self.total_lines:
            lines.append(
                f"2026-08-09 10:06:00 INFO pad line {len(lines)}"
            )
        return lines[: self.total_lines] if len(lines) > self.total_lines else lines
