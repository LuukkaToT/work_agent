"""
离线 A/B 评测：同一 golden set 上跑 legacy 与 managed 两种上下文策略。

为什么落 jsonl 而不是 Postgres：这是离线开发产物，不是运行时多租户状态。
没有并发写、没有租户隔离需求、要的是「随手 diff 两次跑分」，为它建表 +
repository 属于过度设计。真需要看趋势时 jsonl 直接读进 pandas 就行。

长表设计（一行 = 一个 ``case × strategy``）而不是宽表
（``baseline_result`` / ``new_result`` 两列）：加第三种策略时长表不用改 schema。

指标里 accuracy 是 20 条 synthetic 样本上的开发期趋势，不是生产统计结论；
``context_chars`` 与 ``token_total`` 才是这次改造真正想压的硬指标。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from work_agent.core.config import get_settings
from work_agent.graph.nodes.error_analysis import DiagnosisResult, run_diagnosis
from work_agent.tools.registry import get_pipeline_tool

DEFAULT_STRATEGIES: tuple[str, ...] = ("legacy", "managed")
ALLOWED_STRATEGIES: tuple[str, ...] = ("legacy", "managed", "component_parallel")
_RESULT_FILENAME = "eval_results.jsonl"


def _context_strategy(strategy: str) -> str:
    """component_parallel 是诊断引擎，不是上下文策略；评测时按 managed 投影。"""
    if strategy == "component_parallel":
        return "managed"
    return strategy


def _diagnosis_engine(strategy: str) -> str | None:
    """只有显式选择新引擎时才覆盖 profile；默认策略保持 legacy 引擎。"""
    if strategy == "component_parallel":
        return "component_parallel"
    return None


@dataclass(frozen=True)
class EvalCase:
    """golden set 里的一条用例。"""

    case_id: str
    scenario: str
    user_input: str
    pipeline: dict[str, Any]
    expected_fail_kind: str
    expected_root_component: str = ""
    expected_evidence_keys: list[str] = field(default_factory=list)


def cases_path() -> Path:
    """golden set 默认位置：仓库根 ``config/eval_cases.json``。"""
    return get_settings().profile_path.parent / "eval_cases.json"


def results_path() -> Path:
    """结果 jsonl 默认位置：``workspace/eval_results.jsonl``。"""
    return get_settings().workspace_dir / _RESULT_FILENAME


def load_cases(path: Path | str | None = None) -> tuple[str, list[EvalCase]]:
    """
    读 golden set。

    参数:
        path: json 文件路径；None 用 ``config/eval_cases.json``。

    返回:
        ``(suite 名, EvalCase 列表)``。

    异常:
        FileNotFoundError: 文件不存在。
        ValueError: cases 为空或缺必填字段。
    """
    p = Path(path) if path is not None else cases_path()
    if not p.exists():
        raise FileNotFoundError(f"golden set 不存在: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    suite = str(data.get("suite") or p.stem)
    raw_cases = data.get("cases") or []
    if not raw_cases:
        raise ValueError(f"golden set {p} 里没有 cases")

    cases: list[EvalCase] = []
    for raw in raw_cases:
        missing = [k for k in ("case_id", "scenario", "expected_fail_kind") if not raw.get(k)]
        if missing:
            raise ValueError(f"case {raw.get('case_id')!r} 缺字段: {missing}")
        cases.append(
            EvalCase(
                case_id=str(raw["case_id"]),
                scenario=str(raw["scenario"]),
                user_input=str(raw.get("user_input") or ""),
                pipeline=dict(raw.get("pipeline") or {}),
                expected_fail_kind=str(raw["expected_fail_kind"]),
                expected_root_component=str(raw.get("expected_root_component") or ""),
                expected_evidence_keys=[str(k) for k in raw.get("expected_evidence_keys") or []],
            )
        )
    return suite, cases


def evidence_recall(context_text: str, keys: Sequence[str]) -> tuple[float, list[str]]:
    """
    关键证据留存率：expected key 有多少活到了最终 working context。

    这是上下文管理最该被盯住的指标 —— 省字符很容易，把根因证据一起省掉
    就是净损失。大小写不敏感，避免为「KeyError vs keyerror」这种噪声扣分。

    参数:
        context_text: 最终喂给抽取器的上下文原文。
        keys: golden set 里声明的必留关键词。

    返回:
        ``(recall, 缺失的 key 列表)``；keys 为空时 recall 记 1.0。
    """
    if not keys:
        return 1.0, []
    low = (context_text or "").lower()
    missing = [k for k in keys if k.lower() not in low]
    return (len(keys) - len(missing)) / len(keys), missing


def prepare_pipeline(case: EvalCase) -> dict[str, Any]:
    """
    在 mock 后端里真建一条流水线并启动，让 ``get_pipeline_status`` 有东西可查。

    直接编造 pipeline_id 的话状态查询会全线报错，诊断质量就不是在测上下文
    策略，而是在测「工具报错时模型怎么瞎猜」。

    参数:
        case: golden case。

    返回:
        run_diagnosis 需要的 pipeline brief。

    异常:
        RuntimeError: 当前 TOOL_BACKEND 不是 mock。
    """
    backend = get_settings().tool_backend
    if backend != "mock":
        raise RuntimeError(
            f"离线 eval 只支持 TOOL_BACKEND=mock（当前 {backend!r}）："
            "golden set 的期望值绑定在 mock 场景的日志特征上"
        )
    tool = get_pipeline_tool(scenario=case.scenario)  # type: ignore[arg-type]
    handle = tool.create(
        case_names=list(case.pipeline.get("case_names") or []),
        version=str(case.pipeline.get("version") or ""),
        physical_env=case.pipeline.get("physical_env"),
        logic_env=case.pipeline.get("logic_env"),
        logic_constraint=case.pipeline.get("logic_constraint"),
    )
    tool.start(handle.pipeline_id)
    result = tool.query(handle.pipeline_id)
    return {
        "pipeline_id": handle.pipeline_id,
        "case_names": handle.case_names,
        "version": handle.version,
        "env": handle.env,
        "status": result.phase,
    }


def run_case(
    case: EvalCase,
    *,
    strategy: str,
    suite: str,
    run_id: str,
    diagnose: Callable[..., DiagnosisResult] = run_diagnosis,
) -> dict[str, Any]:
    """
    跑单个 ``case × strategy``，产出一行长表记录。

    期望值不写进记录：它们只存在 golden 文件里，避免同一份期望在两处漂移。

    参数:
        case: golden case。
        strategy: legacy 或 managed。
        suite: 套件名。
        run_id: 本次批跑标识，同一批的所有行共享。
        diagnose: 诊断内核；单测注入 fake，避免真调 LLM。

    返回:
        一行可直接 json.dumps 的记录。
    """
    started = time.perf_counter()
    row: dict[str, Any] = {
        "suite": suite,
        "run_id": run_id,
        "case_id": case.case_id,
        "strategy": strategy,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        brief = prepare_pipeline(case)
        result = diagnose(
            pipelines=[brief],
            user_input=case.user_input,
            context_strategy=_context_strategy(strategy),
            diagnosis_engine=_diagnosis_engine(strategy),
            scenario=case.scenario,
            run_id=f"{run_id}-{case.case_id}-{strategy}",
        )
    except Exception as exc:  # noqa: BLE001 - 单 case 失败不该让整批中断
        row.update(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "fail_kind": "",
                "root_component": "",
                "correct": False,
                "root_component_correct": False if case.expected_root_component else None,
                "diagnosis_correct": False,
                "evidence_recall": 0.0,
                "missing_evidence": list(case.expected_evidence_keys),
                "context_chars": 0,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "archived_n": 0,
                "readback_calls": 0,
                "react_token_input": 0,
                "react_llm_calls": 0,
                "compress_llm_calls": 0,
                "extract_llm_calls": 0,
                "react_prompt_chars_sum": 0,
            }
        )
        return row

    recall, missing = evidence_recall(result.context_text, case.expected_evidence_keys)
    usage = result.token_usage or {}
    root_component_correct = (
        result.root_component == case.expected_root_component
        if case.expected_root_component
        else None
    )
    fail_kind_correct = result.fail_kind == case.expected_fail_kind
    row.update(
        {
            "error": "",
            "pipeline_id": brief.get("pipeline_id", ""),
            "fail_kind": result.fail_kind,
            "root_component": result.root_component,
            # correct 保留原有 fail_kind 准确率语义，避免历史结果不可比。
            "correct": fail_kind_correct,
            "root_component_correct": root_component_correct,
            "diagnosis_correct": fail_kind_correct
            and (root_component_correct is not False),
            "evidence_recall": round(recall, 4),
            "missing_evidence": missing,
            "context_chars": result.context_chars,
            "token_input": int(usage.get("input", 0)),
            "token_output": int(usage.get("output", 0)),
            "token_total": int(usage.get("total", 0)),
            "llm_calls": int(usage.get("calls", 0)),
            "latency_ms": result.latency_ms,
            "tool_calls": result.tool_calls,
            "selected_context_ids": result.selected_context_ids,
            "compressed_ids": result.compressed_ids,
            "trimmed_steps": result.trimmed_steps,
            "react_context_chars": result.react_context_chars,
            "obs_compressed_n": result.obs_compressed_n,
            "ruled_out_n": result.ruled_out_n,
            "archived_n": getattr(result, "archived_n", 0),
            "readback_calls": sum(
                1
                for t in result.tool_trace
                if t.get("type") == "call" and t.get("name") == "fetch_archived_block"
            ),
            "react_token_input": int(getattr(result, "react_token_input", 0) or 0),
            "react_token_output": int(getattr(result, "react_token_output", 0) or 0),
            "react_llm_calls": int(getattr(result, "react_llm_calls", 0) or 0),
            "compress_token_total": int(getattr(result, "compress_token_total", 0) or 0),
            "compress_llm_calls": int(getattr(result, "compress_llm_calls", 0) or 0),
            "extract_token_input": int(getattr(result, "extract_token_input", 0) or 0),
            "extract_llm_calls": int(getattr(result, "extract_llm_calls", 0) or 0),
            "react_prompt_chars_sum": int(getattr(result, "react_prompt_chars_sum", 0) or 0),
        }
    )
    return row


def run_suite(
    *,
    cases: Iterable[EvalCase] | None = None,
    suite: str = "",
    strategies: Sequence[str] = DEFAULT_STRATEGIES,
    store: bool = True,
    store_path: Path | str | None = None,
    diagnose: Callable[..., DiagnosisResult] = run_diagnosis,
    on_event: Callable[[str], None] | None = None,
    pause_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    """
    跑整套 golden set，每个 ``case × strategy`` 落一行。

    同一 case 的两种策略连着跑：mock 流水线工具是按 scenario 缓存的单实例，
    中间切换 scenario 会清空它的内存，先前建的 pipeline_id 就查不到了。

    参数:
        cases: 用例列表；None 时读默认 golden set。
        suite: 套件名；cases 为 None 时由 golden 文件提供。
        strategies: 要对比的策略，默认 legacy + managed。
        store: 是否追加写 jsonl。
        store_path: jsonl 路径；None 用 ``workspace/eval_results.jsonl``。
        diagnose: 诊断内核，单测注入 fake。
        on_event: 进度回调，收到形如 ``case_id/strategy`` 的字符串。
        pause_seconds: 相邻两次真 LLM 诊断之间的停顿秒数，缓解限流；
            0 表示不停顿（离线假模型跑批用默认即可）。

    返回:
        全部记录行。
    """
    if cases is None:
        suite, case_list = load_cases()
    else:
        case_list = list(cases)
        suite = suite or "adhoc"

    run_id = uuid.uuid4().hex[:12]
    rows: list[dict[str, Any]] = []
    store_target = (
        Path(store_path) if store_path is not None else results_path()
    ) if store else None
    for i, case in enumerate(case_list):
        for j, strategy in enumerate(strategies):
            if on_event is not None:
                on_event(f"{case.case_id}/{strategy}")
            if i + j > 0 and pause_seconds > 0:
                time.sleep(pause_seconds)
            row = run_case(
                case,
                strategy=strategy,
                suite=suite,
                run_id=run_id,
                diagnose=diagnose,
            )
            rows.append(row)
            if store_target is not None:
                # 真 LLM 跑批一整轮要几十分钟：逐行落盘，中断不丢已完成的行
                append_records([row], path=store_target)

    return rows


def append_records(
    rows: Sequence[Mapping[str, Any]], *, path: Path | str | None = None
) -> Path:
    """
    追加写 jsonl。

    参数:
        rows: 记录行。
        path: 目标文件；None 用默认位置。

    返回:
        实际写入的路径。
    """
    target = Path(path) if path is not None else results_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return target


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    按 strategy 汇总指标。

    参数:
        rows: run_suite 的记录行。

    返回:
        ``{strategy: {n, accuracy, root_component_accuracy, evidence_recall, context_chars, token_total,
        latency_ms, tool_calls, errors}}``；数值为均值（errors 为计数）。
    """
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("strategy") or "?"), []).append(row)

    out: dict[str, dict[str, Any]] = {}
    for strategy, items in buckets.items():
        n = len(items)
        root_items = [i for i in items if i.get("root_component_correct") is not None]
        out[strategy] = {
            "n": n,
            "accuracy": _mean(1.0 if i.get("correct") else 0.0 for i in items),
            "root_component_accuracy": _mean(
                1.0 if i.get("root_component_correct") else 0.0 for i in root_items
            ),
            "diagnosis_accuracy": _mean(
                1.0 if i.get("diagnosis_correct") else 0.0 for i in items
            ),
            "evidence_recall": _mean(float(i.get("evidence_recall") or 0.0) for i in items),
            "context_chars": _mean(float(i.get("context_chars") or 0) for i in items),
            "react_context_chars": _mean(float(i.get("react_context_chars") or 0) for i in items),
            "token_total": _mean(float(i.get("token_total") or 0) for i in items),
            "token_median": _median(float(i.get("token_total") or 0) for i in items),
            "latency_ms": _mean(float(i.get("latency_ms") or 0) for i in items),
            "tool_calls": _mean(float(i.get("tool_calls") or 0) for i in items),
            "archived_n": _mean(float(i.get("archived_n") or 0) for i in items),
            "readback_calls": _mean(float(i.get("readback_calls") or 0) for i in items),
            "errors": sum(1 for i in items if i.get("error")),
        }
    return out


def format_report(summary: Mapping[str, Mapping[str, Any]]) -> str:
    """
    把汇总渲染成对比文本。

    参数:
        summary: summarize 的输出。

    返回:
        多行文本；含 managed 相对 legacy 的 context_chars 变化。
    """
    if not summary:
        return "(没有可汇总的记录)"

    header = (
        f"{'strategy':<10}{'n':>4}{'kind_acc':>10}{'root_acc':>10}{'evid_recall':>13}"
        f"{'ctx_chars':>11}{'react_ctx':>11}{'tokens':>9}{'ms':>8}{'tools':>7}{'err':>5}"
    )
    lines = [header, "-" * len(header)]
    for strategy in sorted(summary):
        s = summary[strategy]
        lines.append(
            f"{strategy:<10}{s['n']:>4}{s['accuracy']:>10.2f}"
            f"{s.get('root_component_accuracy', 0):>10.2f}{s['evidence_recall']:>13.2f}"
            f"{s['context_chars']:>11.0f}{s.get('react_context_chars', 0):>11.0f}"
            f"{s['token_total']:>9.0f}"
            f"{s['latency_ms']:>8.0f}{s['tool_calls']:>7.1f}{s['errors']:>5}"
        )

    base = summary.get("legacy")
    new = summary.get("managed")
    if base and new and base["context_chars"]:
        delta = (new["context_chars"] - base["context_chars"]) / base["context_chars"]
        lines.append("")
        lines.append(f"managed 相对 legacy 的抽取 context_chars 变化：{delta:+.1%}")
        if base.get("react_context_chars"):
            rdelta = (
                new.get("react_context_chars", 0) - base["react_context_chars"]
            ) / base["react_context_chars"]
            lines.append(f"managed 相对 legacy 的 ReAct 历史 context_chars 变化：{rdelta:+.1%}")
        lines.append(
            f"证据留存 {base['evidence_recall']:.2f} → {new['evidence_recall']:.2f}，"
            f"token 均值 {base['token_total']:.0f} → {new['token_total']:.0f}，"
            f"中位数 {base.get('token_median', 0):.0f} → {new.get('token_median', 0):.0f}"
        )
    lines.append("")
    lines.append(
        f"注：kind/root accuracy 基于 {sum(s['n'] for s in summary.values()) // max(1, len(summary))} "
        "条样本，只作趋势参考。短任务 token 看中位数；均值会被长尾拉高。"
        "context_chars 是抽取窗口，react_ctx 是最后一轮 ReAct 快照。"
    )
    return "\n".join(lines)


def _mean(values: Iterable[float]) -> float:
    """空序列返回 0.0 的均值。"""
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _median(values: Iterable[float]) -> float:
    """空序列返回 0.0 的中位数。"""
    items = sorted(values)
    if not items:
        return 0.0
    mid = len(items) // 2
    if len(items) % 2:
        return float(items[mid])
    return (items[mid - 1] + items[mid]) / 2.0
