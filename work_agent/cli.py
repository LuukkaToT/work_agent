"""
PowerShell 可用的命令行入口。

交互：
    python -m work_agent.cli
    python -m work_agent.cli chat -t my-thread

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
from work_agent.runtime import run_turn

app = typer.Typer(add_completion=False, help="测试专属 Agent CLI")
console = Console()


def _runs_table(rows: list[dict], *, title: str | None = None) -> Table:
    """台账表格。runs 命令和 pick_run 的候选列表共用同一种呈现。"""
    table = Table(title=title, show_header=True, header_style="bold")
    for col in ("run_id", "task_id", "用例", "版本", "组网", "状态", "时间"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            str(r.get("run_id", "")),
            str(r.get("task_id", "")),
            ",".join(r.get("cases") or []),
            str(r.get("version", "")),
            str(r.get("topology", "")),
            str(r.get("status", "")),
            str(r.get("created_at", "")),
        )
    return table


def _print_result(result: dict, *, verbose: bool = False) -> None:
    """主体是 reply；路径类信息作为附属行；调试信息只在 -v 下出现。"""
    summary = result.get("summary") or {}
    reply = (result.get("reply") or "").strip()
    if not reply:
        # respond 之前的旧数据或异常兜底
        reply = str(summary.get("message") or summary.get("answer") or "(无回复)")

    branch = summary.get("branch") or result.get("intent") or "?"
    console.print(Panel(reply, title=f"agent · {branch}", border_style="cyan"))

    refs: list[str] = []
    if result.get("run_id"):
        refs.append(f"run_id   : {result['run_id']}")
    if result.get("analysis_path"):
        refs.append(f"analysis : {result['analysis_path']}")
    if result.get("report_path"):
        refs.append(f"report   : {result['report_path']}")
    if refs:
        console.print("[dim]" + "\n".join(refs) + "[/dim]")

    if verbose:
        steps = [a.get("step") for a in (result.get("audit") or []) if a.get("step")]
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

        if kind == "ask_topology":
            frequent = payload.get("frequent") or []
            if frequent:
                lines.append("常用组网：" + " / ".join(str(x) for x in frequent))
        elif kind == "confirm_exec":
            params = payload.get("params") or {}
            lines.append("")
            lines.append("用例：" + ", ".join(params.get("case_names") or ["(未指定)"]))
            lines.append("版本：" + (params.get("version") or "(用默认)"))
            lines.append("组网：" + (params.get("topology") or "(未指定)"))
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


def _repl(thread_id: str | None, *, verbose: bool = False) -> None:
    console.print(
        Panel(
            "测试 Agent REPL\n"
            "示例：分析一下 256T 下行 / 执行用例 case_downlink_001 组网 topo_a\n"
            "      前面那次执行怎么样了？ / quit",
            title="work_agent",
            border_style="green",
        )
    )
    tid = thread_id
    while True:
        try:
            text = console.input("[bold green]you>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nbye")
            break
        if not text:
            continue
        if text.lower() in {"quit", "exit", "q"}:
            console.print("bye")
            break

        try:
            result = run_turn(text, thread_id=tid, ask=_ask, with_checkpoint=True)
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
    """交互式 REPL。输入 quit / exit 退出。"""
    _repl(thread_id, verbose=verbose)


@app.command()
def ask(
    text: str = typer.Argument(..., help="一句话任务"),
    thread_id: Optional[str] = typer.Option(None, "--thread", "-t"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """跑单次任务（遇 HITL 会在终端询问）。"""
    result = run_turn(text, thread_id=thread_id, ask=_ask, with_checkpoint=True)
    _print_result(result, verbose=verbose)


@app.command("runs")
def list_runs(
    limit: int = typer.Option(10, "--limit", "-n", help="最近 N 条"),
) -> None:
    """查看运行台账。"""
    rows = [
        {
            "run_id": r.run_id,
            "task_id": r.task_id,
            "cases": r.case_names,
            "version": r.version,
            "topology": r.topology,
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
    """不带子命令时直接进 chat。"""
    if ctx.invoked_subcommand is None:
        # 不能直接调 chat()：那样 typer 的 OptionInfo 默认值对象会被当成实参传进去，
        # thread_id 就成了 <OptionInfo ...>，等于每轮都新建 thread
        _repl(None)


if __name__ == "__main__":
    app()
