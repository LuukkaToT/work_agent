"""Mock 日志 tool：按 scenario 生成可判别的长日志。"""

from __future__ import annotations

import random
import re
from typing import Literal

MockScenario = Literal["all_pass", "version_fail", "case_error", "env_error"]

# 默认拉到数千行，模拟真实流水线日志。INFO 噪声占绝大多数；失败场景在
# 特征证据 *之前* 插入足够多的 ERROR 噪声，把单次观察撑到
# react_observation_max_chars（4000）附近，好让 8 步 ReAct 顶满
# react_history_max_chars（20K），多份去重后的大块再顶满抽取预算（12K）触发 Compressor。
# 观察截断改为留尾，根因行在文件末尾，不会被噪声从头部挤掉。
_DEFAULT_TOTAL_LINES = 8000
# 约 80 * 80 字 ≈ 6.4K keyed，压缩到 4000 时留尾部（含特征证据）。
_ERROR_NOISE_LINES = 80


class MockLogTool:
    """实现 LogTool：按 scenario 生成可 grep 的假日志。"""

    def __init__(
        self,
        scenario: MockScenario = "case_error",
        *,
        total_lines: int = _DEFAULT_TOTAL_LINES,
        error_noise_lines: int = _ERROR_NOISE_LINES,
    ) -> None:
        """
        参数:
            scenario: 决定尾部错误特征。
            total_lines: 生成日志总行数；INFO 噪声填满额度，特征行占尾部。
            error_noise_lines: 失败场景在特征证据前插入的 ERROR 噪声行数；
                ``all_pass`` 忽略此项，避免尾部出现 ERROR。
        """
        self.scenario = scenario
        self.total_lines = max(50, total_lines)
        self.error_noise_lines = max(0, error_noise_lines)

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

        signature = _signature_lines(self.scenario)
        error_noise = (
            []
            if self.scenario == "all_pass"
            else _error_noise_lines(rng, self.error_noise_lines)
        )
        header = [
            f"[mock-log] pipeline_id={pid} scenario={self.scenario}",
            "2026-08-09 10:00:00 INFO runner boot ok",
            "2026-08-09 10:00:01 INFO start case CaseA_235T_nmimo",
        ]
        reserved = len(header) + len(error_noise) + len(signature)
        info_n = max(0, self.total_lines - reserved)

        lines: list[str] = list(header)
        for i in range(info_n):
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

        # 特征证据必须在文件最末：tail_lines=80 的单测和默认 tail=200 都要能看到。
        lines.extend(error_noise)
        lines.extend(signature)
        return lines


def _signature_lines(scenario: MockScenario) -> list[str]:
    """场景可判别的尾部特征。eval 的 expected_evidence_keys 绑在这些行上。"""
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
    """
    失败场景的 ERROR 噪声。

    故意不用场景特征词（KeyError / mismatch / refused / antenna_map 等），
    避免污染 evidence_recall；但仍带 ERROR，好让 compress_observation 当成
    证据行留下来，把单次观察撑到数千字符。
    """
    out: list[str] = []
    msgs = (
        "queue backpressure",
        "slot grant delayed",
        "kpi sample dropped",
        "sync slice lag",
        "buffer occupancy high",
    )
    # 不得出现根因特征词，避免和 evidence_recall 抢窗口；ERROR 本身要保留。
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
    for i in range(n):
        msg = rng.choice(msgs)
        line = f"2026-08-09 10:04:{i % 60:02d} ERROR {msg} noise_seq={i}"
        assert not any(k in line.lower() for k in forbidden), line
        out.append(line)
    return out
