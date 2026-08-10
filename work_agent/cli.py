"""
PowerShell 可用的命令行入口。

交互：
    python -m work_agent.cli
    python -m work_agent.cli chat -t my-thread

REPL 斜杠命令：
    /session          列出并切换会话
    /session <id>     直接切到指定 thread
    /new              开新会话
    quit / exit / q   退出

单次：
    python -m work_agent.cli ask "分析一下 256T 下行"
    python -m work_agent.cli runs
"""

from __future__ import annotations

import json
from typing import Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from work_agent.core.ledger import get_ledger
from work_agent.core.sessions import (
    SessionInfo,
    list_sessions,
    resolve_session_pick,
    session_exists,
)
from work_agent.runtime import (
    get_pending_interrupts,
    new_thread_id,
    resume_pending,
    run_turn,
)

app = typer.Typer(add_completion=False, help="测试专属 Agent CLI")
console = Console()


def _runs_table(rows: list[dict], *, title: str | None = None) -> Table:
    """台账表格。runs 命令和 pick_run 的候选列表共用同一种呈现。"""
    table = Table(title=title, show_header=True, header_style="bold")
    for col in ("pipeline_id", "task_id", "用例", "版本", "环境", "状态", "时间"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r.get("pipeline_id", "")),
            str(r.get("task_id", "")),
            ",".join(r.get("cases") or []),
            str(r.get("version", "")),
            str(r.get("env", "") or r.get("topology", "")),
            str(r.get("status", "")),
            str(r.get("created_at", "")),
        )
    return table


def _sessions_table(sessions: list[SessionInfo], *, current: str | None) -> Table:
    """把会话列表渲染成 Rich 表格，当前会话标 ←。"""
    table = Table(title="历史会话", show_header=True, header_style="bold")
    for col in ("#", "thread_id", "预览", "当前"):
        table.add_column(col)
    for i, s in enumerate(sessions, 1):
        mark = "←" if current and s.thread_id == current else ""
        table.add_row(str(i), s.thread_id, s.preview, mark)
    return table


def _print_tool_trace(audit: list[dict]) -> None:
    """-v 时打印 tool_trace（工具名 + 结果字符数）。"""
    rows: list[tuple[str, str, str]] = []
    for entry in audit or []:
        for item in entry.get("tool_trace") or []:
            kind = str(item.get("type") or "")
            name = str(item.get("name") or "")
            if kind == "call":
                detail = str(item.get("args_preview") or "")
            elif kind == "result":
                detail = f"{item.get('content_chars', 0)} chars"
            else:
                detail = json.dumps(item, ensure_ascii=False)
            rows.append((kind, name, detail))
    if not rows:
        return
    table = Table(title="tool_trace", show_header=True, header_style="bold")
    table.add_column("type")
    table.add_column("name")
    table.add_column("detail")
    for kind, name, detail in rows:
        table.add_row(kind, name, detail)
    console.print(table)


def _print_result(result: dict, *, verbose: bool = False) -> None:
    """主体是 reply；路径类信息作为附属行；调试信息只在 -v 下出现。"""
    summary = result.get("summary") or {}
    reply = (result.get("reply") or "").strip()
    if not reply:
        reply = str(summary.get("message") or summary.get("answer") or "(无回复)")

    intent = result.get("intent") or "?"
    console.print(Panel(reply, title=f"agent · {intent}", border_style="cyan"))

    refs: list[str] = []
    pipelines = result.get("pipelines") or []
    if pipelines:
        for p in pipelines:
            refs.append(
                f"pipeline : {p.get('pipeline_id')}  env={p.get('env')}  "
                f"status={p.get('status')}"
            )
    if result.get("analysis_path"):
        refs.append(f"analysis : {result['analysis_path']}")
    if refs:
        console.print("[dim]" + "\n".join(refs) + "[/dim]")

    if verbose:
        audit = result.get("audit") or []
        steps = [a.get("step") for a in audit if a.get("step")]
        console.print(
            Panel(
                "\n".join(
                    [
                        f"thread : {result.get('_thread_id', '')}",
                        f"intent : {result.get('intent', '')}",
                        f"audit  : {steps}",
                        "summary:",
                        json.dumps(summary, ensure_ascii=False, indent=2),
                    ]
                ),
                title="debug",
                border_style="grey50",
            )
        )
        _print_tool_trace(audit)


class _RunStatus:
    """Rich status 桥：节点/工具进度；HITL 前停 spinner。"""

    def __init__(self) -> None:
        self._status = None

    def start(self, text: str = "执行中 · …") -> None:
        if self._status is not None:
            self._status.update(text)
            return
        self._status = console.status(text, spinner="dots")
        self._status.start()

    def stop(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def on_event(self, message: str) -> None:
        if message.startswith("node:"):
            self.start(f"执行中 · {message[5:]}")
        elif message.startswith("tool:"):
            name = message[5:]
            console.print(f"[dim]tool · {name}[/dim]")
            self.start(f"执行中 · tool/{name}")
        elif message == "status:waiting_input":
            self.stop()
        elif message.startswith("status:"):
            self.start(f"执行中 · {message[7:]}")


def _ask_with_status(bridge: _RunStatus, payloads: list[Any]) -> str:
    """HITL：先停状态条，再提问。"""
    bridge.stop()
    return _ask(payloads)


def _run_turn_with_status(
    text: str,
    *,
    thread_id: str | None,
) -> dict:
    """包一层 status spinner，再调用 run_turn。"""
    bridge = _RunStatus()
    bridge.start("执行中 · …")
    try:
        return run_turn(
            text,
            thread_id=thread_id,
            ask=lambda payloads: _ask_with_status(bridge, payloads),
            with_checkpoint=True,
            on_event=bridge.on_event,
        )
    finally:
        bridge.stop()


def _render_interrupt(payloads: list[Any]) -> str:
    """把 interrupt 的原始载荷渲染成人能读的提示，而不是直出 dict。"""
    lines: list[str] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            lines.append(str(payload))
            continue

        kind = payload.get("type") or ""
        if payload.get("message"):
            lines.append(str(payload["message"]))

        if kind == "ask_version":
            allowed = payload.get("allowed") or []
            if allowed:
                lines.append("可选版本：" + " / ".join(str(x) for x in allowed))
        elif kind == "ask_env":
            lines.append("示例：7.223.50.60")
        elif kind == "pick_run":
            lines.append("可输入：序号（1 / 第一条）、pipeline_id 前缀、环境 IP，或「全部」")
        elif kind == "pick_sheet_column":
            headers = payload.get("headers") or []
            if headers:
                lines.append(
                    "表头：" + ", ".join(f"[{i}]{h}" for i, h in enumerate(headers))
                )
            lines.append("请输入用例名列的下标数字（从 0 开始）")
        elif kind == "confirm_exec":
            plans = payload.get("plans") or []
            if not plans:
                params = payload.get("params") or {}
                plans = params.get("plans") or []
            lines.append("")
            for i, plan in enumerate(plans, 1):
                cases = plan.get("case_names") or []
                lines.append(
                    f"[{i}] env={plan.get('env') or '(未指定)'}  "
                    f"version={plan.get('version') or '(未指定)'}  "
                    f"cases={len(cases)}"
                )
                for name in cases[:3]:
                    lines.append(f"    - {name}")
                if len(cases) > 3:
                    lines.append(f"    ... 共 {len(cases)} 条")
        elif kind != "pick_run":
            current = payload.get("current") or {}
            if current:
                known = ", ".join(f"{k}={v}" for k, v in current.items() if v)
                if known:
                    lines.append(f"已知参数：{known}")

    return "\n".join(lines)


def _ask(payloads: list[Any]) -> str:
    """HITL 提问：候选列表用表格，其余用面板，然后等用户输入。"""
    for payload in payloads:
        if isinstance(payload, dict) and payload.get("type") == "pick_run":
            console.print(_runs_table(payload.get("options") or []))

    console.print(
        Panel(_render_interrupt(payloads), title="需要你确认", border_style="yellow")
    )
    return console.input("[bold yellow]>[/] ").strip()


def _resume_with_status(tid: str) -> dict | None:
    """续跑 pending HITL，带 status spinner。"""
    bridge = _RunStatus()
    bridge.start("执行中 · …")
    try:
        return resume_pending(
            tid,
            ask=lambda payloads: _ask_with_status(bridge, payloads),
            on_event=bridge.on_event,
        )
    finally:
        bridge.stop()


def _switch_session(tid: str, *, verbose: bool = False) -> str:
    """切换 thread；若有未完成 HITL，先续跑。"""
    console.print(f"[dim]已切换到会话 {tid}[/dim]")
    if get_pending_interrupts(tid):
        console.print("[yellow]该会话有未完成的确认，请先答完。[/yellow]")
        try:
            result = _resume_with_status(tid)
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]已取消续跑，仍停在该会话[/dim]")
            return tid
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]续跑失败:[/] {exc}")
            return tid
        if result is not None:
            _print_result(result, verbose=verbose)
    return tid


def _handle_slash(
    text: str, tid: str, *, verbose: bool = False
) -> tuple[str, bool]:
    """
    处理斜杠命令。
    返回 (thread_id, handled)；handled=True 表示本行已消费，不再 run_turn。
    """
    raw = text.strip()
    lower = raw.lower()

    if lower in {"/new", "/new "}:
        tid = new_thread_id()
        console.print(f"[dim]新会话 {tid}[/dim]")
        return tid, True

    if lower == "/session" or lower.startswith("/session "):
        arg = raw[8:].strip()  # 去掉 "/session"
        sessions = list_sessions(limit=20)
        if arg:
            pick = resolve_session_pick(arg, sessions)
            if pick is None:
                console.print("[red]无效选择[/red]")
                return tid, True
            if not session_exists(pick):
                # 允许切到尚未落库的 id（即将开聊）
                console.print(
                    f"[dim]会话 {pick} 尚无历史，将作为新 thread 使用[/dim]"
                )
                return pick, True
            return _switch_session(pick, verbose=verbose), True

        if not sessions:
            console.print("[dim]还没有历史会话。可继续聊天，或 /new 显式开新会话。[/dim]")
            return tid, True

        console.print(_sessions_table(sessions, current=tid))
        console.print("[dim]输入序号或 thread_id 切换；直接回车取消[/dim]")
        try:
            choice = console.input("[bold cyan]session>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("")
            return tid, True
        if not choice:
            return tid, True
        pick = resolve_session_pick(choice, sessions)
        if pick is None:
            console.print("[red]无效选择[/red]")
            return tid, True
        return _switch_session(pick, verbose=verbose), True

    if lower.startswith("/"):
        console.print("[dim]未知命令。可用：/session  /new  quit[/dim]")
        return tid, True

    return tid, False


def _repl(thread_id: str | None, *, verbose: bool = False) -> None:
    """交互式多轮 REPL：斜杠命令 + run_turn，直到 quit。"""
    tid = thread_id or new_thread_id()
    console.print(
        Panel(
            "测试 Agent REPL\n"
            f"当前会话：{tid}\n"
            "示例：分析一下 256T 下行\n"
            "      在 7.223.50.60 上跑 HF_20B_PUSCH_1Cell_200M_hf_001 版本 27B\n"
            "斜杠：/session 切换会话  /new 新会话  quit 退出",
            title="work_agent",
            border_style="green",
        )
    )
    # -t 指定已有会话且停在 HITL 时，先进续跑
    if thread_id and get_pending_interrupts(tid):
        tid = _switch_session(tid, verbose=verbose)

    while True:
        try:
            text = console.input("[bold green]you>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print(
                f"\nbye\n[dim]下次续聊：python -m work_agent.cli chat -t {tid}[/dim]"
            )
            break
        if not text:
            continue
        if text.lower() in {"quit", "exit", "q"}:
            console.print(
                f"bye\n[dim]下次续聊：python -m work_agent.cli chat -t {tid}[/dim]"
            )
            break

        tid, handled = _handle_slash(text, tid, verbose=verbose)
        if handled:
            continue

        try:
            result = _run_turn_with_status(text, thread_id=tid)
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]已中断本轮[/dim]")
            continue
        except Exception as exc:  # noqa: BLE001 - REPL 不该因单轮失败而退出
            console.print(f"[red]错误:[/] {exc}")
            continue

        tid = result.get("_thread_id") or tid
        _print_result(result, verbose=verbose)


@app.command()
def chat(
    thread_id: Optional[str] = typer.Option(
        None, "--thread", "-t", help="固定 thread_id，便于同一会话续聊/续跑"
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="额外打印 audit 与原始 summary"
    ),
) -> None:
    """
    进入交互式 REPL；输入 quit / exit 退出。

    参数:
        thread_id: 固定会话 id，便于续聊/续跑；默认新建。
        verbose: True 时额外打印 audit 与原始 summary。
    """
    _repl(thread_id, verbose=verbose)


@app.command()
def ask(
    text: str = typer.Argument(..., help="一句话任务"),
    thread_id: Optional[str] = typer.Option(None, "--thread", "-t"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """
    跑单次任务（遇 HITL 会在终端询问）。

    参数:
        text: 一句话用户任务。
        thread_id: 可选固定会话 id。
        verbose: True 时额外打印 debug 面板。
    """
    result = _run_turn_with_status(text, thread_id=thread_id)
    _print_result(result, verbose=verbose)


@app.command("runs")
def list_runs(
    limit: int = typer.Option(10, "--limit", "-n", help="最近 N 条"),
) -> None:
    """
    查看运行台账（最近 N 条流水线记录）。

    参数:
        limit: 最多展示条数。
    """
    rows = [
        {
            "pipeline_id": r.pipeline_id,
            "task_id": r.task_id,
            "cases": r.case_names,
            "version": r.version,
            "env": r.env,
            "status": r.status,
            "created_at": r.created_at,
        }
        for r in get_ledger().list_recent(limit=limit)
    ]
    if not rows:
        console.print("[dim]台账里还没有执行记录[/dim]")
        return
    console.print(_runs_table(rows, title=f"最近 {len(rows)} 条执行"))


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """
    CLI 入口回调：不带子命令时直接进入 chat REPL。

    参数:
        ctx: Typer 上下文（用于判断是否已进入子命令）。
    """
    if ctx.invoked_subcommand is None:
        _repl(None)


if __name__ == "__main__":
    app()
