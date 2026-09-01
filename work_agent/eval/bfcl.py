"""BFCL 工具调用评测：接到 ``run_agent_loop``，AST / irrelevance 离线比对。

数据来自 Berkeley Function Calling Leaderboard v4（Gorilla 仓库 JSONL），
缓存到 ``workspace/eval_cache/bfcl/``，不进 git。不把分数说成公开榜排名。

评测的是第一轮模型 tool_calls（官方 AST 口径），不是 Testing Agent 主图。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import Field, create_model

from work_agent.core.config import get_settings
from work_agent.core.llm import get_reasoning_model
from work_agent.eval.runner import DEFAULT_STRATEGIES, append_records
from work_agent.graph.helpers.agent_loop import run_agent_loop

BFCL_VERSION = "v4"
# 钉 Gorilla 仓库路径；文件名随 v4 重命名（simple → simple_python）。
BFCL_DATA_BASE = (
    "https://raw.githubusercontent.com/ShishirPatil/gorilla/"
    "master/berkeley-function-call-leaderboard/bfcl_eval/data"
)
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "simple",
    "multiple",
    "parallel",
    "irrelevance",
)
_CATEGORY_FILES = {
    "simple": "BFCL_v4_simple_python.json",
    "multiple": "BFCL_v4_multiple.json",
    "parallel": "BFCL_v4_parallel.json",
    "irrelevance": "BFCL_v4_irrelevance.json",
}
_JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": float,
    "float": float,
    "boolean": bool,
    "array": list,
    "object": dict,
    "any": str,
    "tuple": list,
}
_SYSTEM = (
    "You are a function-calling assistant. Use the provided tools when they "
    "match the user request. If none of the tools are relevant, reply in plain "
    "text and do not call any tool."
)
_RESULT_FILENAME = "eval_react_results.jsonl"


@dataclass(frozen=True)
class BfclCase:
    """一条 BFCL 样本。"""

    case_id: str
    category: str
    user: str
    functions: list[dict[str, Any]]
    possible_answer: Any = None


def cache_dir() -> Path:
    """官方 JSONL 缓存目录。"""
    return get_settings().workspace_dir / "eval_cache" / "bfcl" / BFCL_VERSION


def results_path() -> Path:
    """BFCL 评测结果 jsonl。"""
    return get_settings().workspace_dir / _RESULT_FILENAME


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """按行 JSON 读取。"""
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            rows.append(json.loads(text))
    return rows


def ensure_category_files(
    category: str, *, cache: Path | None = None, opener=urllib.request.urlopen
) -> tuple[Path, Path | None]:
    """
    下载（或命中缓存）某一类的题目与 possible_answer。

    参数:
        category: simple / multiple / parallel / irrelevance。
        cache: 缓存根；None 用 ``cache_dir()``。
        opener: 可注入的 HTTP opener（单测用）。

    返回:
        ``(题目路径, possible_answer 路径或 None)``。
    """
    if category not in _CATEGORY_FILES:
        raise ValueError(f"未知 BFCL 类别 {category!r}，可选 {list(_CATEGORY_FILES)}")
    root = cache if cache is not None else cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    name = _CATEGORY_FILES[category]
    question_path = root / name
    _download_if_missing(f"{BFCL_DATA_BASE}/{name}", question_path, opener=opener)
    answer_path = root / "possible_answer" / name
    if category == "irrelevance":
        return question_path, None
    try:
        _download_if_missing(
            f"{BFCL_DATA_BASE}/possible_answer/{name}",
            answer_path,
            opener=opener,
        )
    except OSError:
        return question_path, None
    return question_path, answer_path


def _download_if_missing(url: str, dest: Path, *, opener) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with opener(url) as resp:
        data = resp.read()
    dest.write_bytes(data)


def load_cases(
    *,
    categories: Sequence[str] = DEFAULT_CATEGORIES,
    limit: int = 0,
    cache: Path | None = None,
    opener=urllib.request.urlopen,
) -> list[BfclCase]:
    """读官方 JSONL，拼成 BfclCase 列表。``limit`` 为每类上限，0 表示不截。"""
    cases: list[BfclCase] = []
    for category in categories:
        q_path, a_path = ensure_category_files(
            category, cache=cache, opener=opener
        )
        questions = load_jsonl(q_path)
        answers = _index_answers(load_jsonl(a_path)) if a_path and a_path.exists() else {}
        picked = questions if limit <= 0 else questions[:limit]
        for raw in picked:
            case_id = str(raw.get("id") or "")
            cases.append(
                BfclCase(
                    case_id=case_id,
                    category=category,
                    user=_question_text(raw.get("question")),
                    functions=list(raw.get("function") or []),
                    possible_answer=answers.get(case_id),
                )
            )
    return cases


def _index_answers(rows: list[dict[str, Any]]) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for row in rows:
        if "id" in row:
            indexed[str(row["id"])] = row.get("ground_truth", row)
            continue
        if len(row) == 1:
            key = next(iter(row))
            indexed[str(key)] = row[key]
    return indexed


def _question_text(question: Any) -> str:
    if isinstance(question, str):
        return question
    if isinstance(question, list):
        parts: list[str] = []
        for turn in question:
            if isinstance(turn, list):
                for msg in turn:
                    parts.append(_message_text(msg))
            else:
                parts.append(_message_text(turn))
        return "\n".join(p for p in parts if p)
    return str(question or "")


def _message_text(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        return str(msg.get("content") or "")
    return str(msg or "")


def tool_from_bfcl(spec: dict[str, Any]) -> BaseTool:
    """把 BFCL function schema 编成只记录调用的 LangChain 工具。"""
    original = str(spec.get("name") or "unnamed")
    name = re.sub(r"[^A-Za-z0-9_-]", "_", original)[:64] or "unnamed"
    params = spec.get("parameters") or {}
    props = params.get("properties") or {}
    required = set(params.get("required") or [])
    fields: dict[str, Any] = {}
    for pname, schema in props.items():
        schema = schema if isinstance(schema, dict) else {}
        py_type = _JSON_TYPES.get(str(schema.get("type") or "string"), str)
        if pname in required:
            fields[pname] = (py_type, Field(...))
        else:
            fields[pname] = (py_type | None, Field(default=None))
    if not fields:
        fields["unused"] = (str | None, Field(default=None))
    args_model = create_model(f"{name}_Args", **fields)

    def _run(**kwargs: Any) -> str:
        payload = {k: v for k, v in kwargs.items() if v is not None and k != "unused"}
        return json.dumps({"ok": True, "name": original, "args": payload}, ensure_ascii=False)

    tool = StructuredTool.from_function(
        func=_run,
        name=name,
        description=str(spec.get("description") or original),
        args_schema=args_model,
    )
    tool.metadata = {"bfcl_name": original}
    return tool


def first_tool_calls(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """取第一轮 AI 的 tool_calls；没有则空列表（irrelevance 期望如此）。"""
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        calls = []
        for tc in msg.tool_calls or []:
            name = str(tc.get("name") or "")
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            calls.append({"name": name, "args": args})
        return calls
    return []


def _std(value: Any) -> Any:
    if isinstance(value, str):
        return re.sub(r"[^a-z0-9]", "", value.strip().lower())
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return [_std(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _std(v) for k, v in value.items()}
    return value


def _value_allowed(actual: Any, allowed: Any) -> bool:
    if not isinstance(allowed, list):
        allowed = [allowed]
    if actual is None:
        return "" in allowed or None in allowed
    std_actual = _std(actual)
    for item in allowed:
        if item == "":
            continue
        if _std(item) == std_actual:
            return True
        try:
            if float(actual) == float(item):
                return True
        except (TypeError, ValueError):
            pass
    return False


def _canon_name(name: str) -> str:
    return name.replace(".", "_")


def _match_one(
    call: dict[str, Any],
    expected: Mapping[str, Any],
    functions: Sequence[Mapping[str, Any]],
) -> bool:
    if len(expected) != 1:
        return False
    exp_name, exp_params = next(iter(expected.items()))
    got_name = _canon_name(str(call.get("name") or ""))
    if got_name != _canon_name(str(exp_name)):
        return False
    args = call.get("args") if isinstance(call.get("args"), dict) else {}
    spec = next(
        (f for f in functions if _canon_name(str(f.get("name") or "")) == got_name),
        None,
    )
    required = set((spec or {}).get("parameters", {}).get("required") or [])
    params = exp_params if isinstance(exp_params, dict) else {}
    for key in required:
        if key not in args:
            return False
    for key, value in args.items():
        if key not in params:
            return False
        if not _value_allowed(value, params[key]):
            return False
    for key, allowed in params.items():
        if key not in args and not (
            isinstance(allowed, list) and "" in allowed
        ):
            return False
    return True


def ast_correct(
    *,
    category: str,
    functions: Sequence[Mapping[str, Any]],
    calls: Sequence[Mapping[str, Any]],
    possible_answer: Any,
) -> bool:
    """官方口径的简化 AST：函数名 + 参数落在 possible_answer 列表里。"""
    call_list = list(calls)
    if category == "irrelevance":
        return not call_list
    answers = _as_answer_list(possible_answer)
    if not answers:
        return False
    if category == "parallel":
        if len(call_list) != len(answers):
            return False
        remaining = list(answers)
        for call in call_list:
            hit = next(
                (i for i, exp in enumerate(remaining) if _match_one(call, exp, functions)),
                None,
            )
            if hit is None:
                return False
            remaining.pop(hit)
        return True
    if len(call_list) != 1:
        return False
    return any(_match_one(call_list[0], exp, functions) for exp in answers)


def _as_answer_list(possible_answer: Any) -> list[dict[str, Any]]:
    if possible_answer is None:
        return []
    if isinstance(possible_answer, list):
        out: list[dict[str, Any]] = []
        for item in possible_answer:
            if isinstance(item, dict):
                out.append(item)
        return out
    if isinstance(possible_answer, dict):
        if any(isinstance(v, dict) for v in possible_answer.values()):
            return [possible_answer]
        return [possible_answer]
    return []


def run_case(
    case: BfclCase,
    *,
    strategy: str,
    suite: str,
    run_id: str,
    model: BaseChatModel | None = None,
) -> dict[str, Any]:
    """跑 ``case × strategy``，产出一行长表记录。"""
    started = time.perf_counter()
    profile = get_settings().profile
    row: dict[str, Any] = {
        "suite": suite,
        "run_id": run_id,
        "case_id": case.case_id,
        "category": case.category,
        "strategy": strategy,
        "bfcl_version": BFCL_VERSION,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    try:
        tools = [tool_from_bfcl(fn) for fn in case.functions]
        name_map = {
            t.name: (t.metadata or {}).get("bfcl_name") or t.name for t in tools
        }
        used_model = model or get_reasoning_model(temperature=0)
        loop = run_agent_loop(
            model=used_model,
            tools=tools,
            system=_SYSTEM,
            user=case.user,
            max_steps=profile.react_max_steps,
            observation_max_chars=profile.react_observation_max_chars,
            history_max_chars=(
                profile.react_history_max_chars if strategy == "managed" else 0
            ),
        )
        calls = first_tool_calls(loop.messages)
        for call in calls:
            original = name_map.get(call["name"])
            if original:
                call["name"] = original
        ok = ast_correct(
            category=case.category,
            functions=case.functions,
            calls=calls,
            possible_answer=case.possible_answer,
        )
        usage = loop.usage.as_dict() if loop.usage else {}
        row.update(
            {
                "error": "",
                "ast_correct": ok,
                "tool_calls": len(calls),
                "predicted": calls,
                "context_chars": loop.context_chars,
                "trimmed_steps": loop.trimmed_steps,
                "token_input": int(usage.get("input", 0)),
                "token_output": int(usage.get("output", 0)),
                "token_total": int(usage.get("total", 0)),
                "llm_calls": int(usage.get("calls", 0)),
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        )
    except Exception as exc:  # noqa: BLE001
        row.update(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "ast_correct": False,
                "tool_calls": 0,
                "predicted": [],
                "context_chars": 0,
                "trimmed_steps": 0,
                "token_total": 0,
                "latency_ms": int((time.perf_counter() - started) * 1000),
            }
        )
    return row


def run_suite(
    *,
    cases: Sequence[BfclCase],
    strategies: Sequence[str] = DEFAULT_STRATEGIES,
    store: bool = True,
    store_path: Path | str | None = None,
    model: BaseChatModel | None = None,
    on_event: Callable[[str], None] | None = None,
    pause_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    """每个 ``case × strategy`` 一行。"""
    run_id = uuid.uuid4().hex[:12]
    rows: list[dict[str, Any]] = []
    store_target = (
        Path(store_path) if store_path is not None else results_path()
    ) if store else None
    for i, case in enumerate(cases):
        for j, strategy in enumerate(strategies):
            if on_event is not None:
                on_event(f"{case.case_id}/{strategy}")
            if i + j > 0 and pause_seconds > 0:
                time.sleep(pause_seconds)
            row = run_case(
                case,
                strategy=strategy,
                suite=f"bfcl-{BFCL_VERSION}",
                run_id=run_id,
                model=model,
            )
            rows.append(row)
            if store_target is not None:
                append_records([row], path=store_target)
    return rows


def summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 strategy 汇总 AST 正确率与成本。"""
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(str(row.get("strategy") or "?"), []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for strategy, items in buckets.items():
        n = len(items)
        out[strategy] = {
            "n": n,
            "ast_accuracy": _mean(1.0 if i.get("ast_correct") else 0.0 for i in items),
            "token_total": _mean(float(i.get("token_total") or 0) for i in items),
            "context_chars": _mean(float(i.get("context_chars") or 0) for i in items),
            "latency_ms": _mean(float(i.get("latency_ms") or 0) for i in items),
            "tool_calls": _mean(float(i.get("tool_calls") or 0) for i in items),
            "errors": sum(1 for i in items if i.get("error")),
        }
        by_cat: dict[str, list[Mapping[str, Any]]] = {}
        for item in items:
            by_cat.setdefault(str(item.get("category") or "?"), []).append(item)
        out[strategy]["by_category"] = {
            cat: _mean(1.0 if i.get("ast_correct") else 0.0 for i in group)
            for cat, group in by_cat.items()
        }
    return out


def format_report(summary: Mapping[str, Mapping[str, Any]]) -> str:
    """渲染 BFCL 汇总。分数只作本仓库 loop 门槛，不是公开榜排名。"""
    if not summary:
        return "(没有可汇总的记录)"
    header = (
        f"{'strategy':<10}{'n':>4}{'ast_acc':>10}"
        f"{'ctx_chars':>11}{'tokens':>9}{'ms':>8}{'tools':>7}{'err':>5}"
    )
    lines = [header, "-" * len(header)]
    for strategy in sorted(summary):
        s = summary[strategy]
        lines.append(
            f"{strategy:<10}{s['n']:>4}{s['ast_accuracy']:>10.2f}"
            f"{s['context_chars']:>11.0f}{s['token_total']:>9.0f}"
            f"{s['latency_ms']:>8.0f}{s['tool_calls']:>7.1f}{s['errors']:>5}"
        )
        cats = s.get("by_category") or {}
        if cats:
            detail = ", ".join(f"{k}={v:.2f}" for k, v in sorted(cats.items()))
            lines.append(f"  by_category: {detail}")
    lines.append("")
    lines.append(
        "注：ast_acc 是本仓库 run_agent_loop 在 BFCL 题目上的门槛，"
        "不是 Berkeley 公开榜排名；也不能解释为诊断准确率。"
    )
    return "\n".join(lines)


def _mean(values: Iterable[float]) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0
