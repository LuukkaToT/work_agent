"""
PowerShell 可用的命令行入口。

交互：
    python -m work_agent.cli

单次：
    python -m work_agent.cli ask "分析一下 256T 下行"
    python -m work_agent.cli runs
"""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from work_agent.core.ledger import get_ledger
from work_agent.runtime import interrupt_payloads, run_turn

app = typer.Typer(add_completion=False, help="测试专属 Agent CLI")
console = Console()


def _print_result(result: dict) -> None:
    summary = result.get("summary") or {}
    branch = summary.get("branch") or result.get("intent") or "?"

    lines = [
        f"thread   : {result.get('_thread_id', '')}",
        f"intent   : {result.get('intent', '')}",
        f"branch   : {branch}",
        f"status   : {summary.get('status', '')}",
    ]
    if result.get("run_id"):
        lines.append(f"run_id   : {result.get('run_id')}")
    if result.get("analysis_path"):
        lines.append(f"analysis : {result.get('analysis_path')}")
    if result.get("report_path"):
        lines.append(f"report   : {result.get('report_path')}")
    if summary.get("answer"):
        lines.append("")
        lines.append(str(summary.get("answer")))
    if summary.get("message"):
        lines.append(f"message  : {summary.get('message')}")
    if summary.get("failed"):
        lines.append(f"failed   : {summary.get('failed')}")

    audit = [a.get("step") for a in (result.get("audit") or [])]
    lines.append(f"audit    : {audit}")

    console.print(Panel("\n".join(lines), title="Result", border_style="cyan"))


def _ask(prompt: str) -> str:
    console.print(Panel(prompt, title="HITL", border_style="yellow"))
    return console.input("[bold yellow]>[/] ").strip()


@app.command()
def chat(
    thread_id: Optional[str] = typer.Option(
        None, "--thread", "-t", help="固定 thread_id，便于同一会话续聊/续跑"
    ),
) -> None:
    """交互式 REPL。输入 quit / exit 退出。"""
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
            tid = result.get("_thread_id") or tid
            _print_result(result)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]错误:[/] {exc}")


@app.command()
def ask(
    text: str = typer.Argument(..., help="一句话任务"),
    thread_id: Optional[str] = typer.Option(None, "--thread", "-t"),
) -> None:
    """跑单次任务（遇 HITL 会在终端询问）。"""
    result = run_turn(text, thread_id=thread_id, ask=_ask, with_checkpoint=True)
    _print_result(result)


@app.command("runs")
def list_runs(
    limit: int = typer.Option(10, "--limit", "-n", help="最近 N 条"),
) -> None:
    """查看运行台账。"""
    rows = get_ledger().list_recent(limit=limit)
    table = Table(title=f"最近 {len(rows)} 条执行")
    table.add_column("run_id")
    table.add_column("task_id")
    table.add_column("cases")
    table.add_column("version")
    table.add_column("topology")
    table.add_column("status")
    table.add_column("created_at")
    for r in rows:
        table.add_row(
            r.run_id,
            r.task_id,
            ",".join(r.case_names),
            r.version,
            r.topology,
            r.status,
            r.created_at,
        )
    console.print(table)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """默认进入 chat。"""
    if ctx.invoked_subcommand is None:
        chat()


if __name__ == "__main__":
    app()
