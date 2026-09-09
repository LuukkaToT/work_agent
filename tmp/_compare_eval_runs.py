"""Compare eval-diagnose run_ids from workspace/eval_results.jsonl."""

from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

PATH = Path("workspace/eval_results.jsonl")
RUNS = sys.argv[1:] or ["b72925255e52", "a4145c290be4"]


def load(run_id: str) -> list[dict]:
    rows = []
    with PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("run_id") == run_id:
                rows.append(row)
    return rows


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def median(xs: list[float]) -> float:
    return float(statistics.median(xs)) if xs else 0.0


def summarize(rows: list[dict]) -> None:
    by: dict[str, list] = defaultdict(list)
    for row in rows:
        by[row["strategy"]].append(row)
    print("n", len(rows), "start", rows[0]["recorded_at"], "end", rows[-1]["recorded_at"])
    for strat in ("legacy", "managed"):
        items = by[strat]
        n = len(items)
        tok = [float(i.get("token_total") or 0) for i in items]
        print(
            f"{strat}: fail_kind {sum(1 for i in items if i.get('correct'))}/{n} "
            f"root {sum(1 for i in items if i.get('root_component_correct') is True)}/{n} "
            f"both {sum(1 for i in items if i.get('diagnosis_correct'))}/{n} "
            f"recall {mean([float(i.get('evidence_recall') or 0) for i in items]):.3f} "
            f"extract {mean([float(i.get('context_chars') or 0) for i in items]):.0f} "
            f"react {mean([float(i.get('react_context_chars') or 0) for i in items]):.0f} "
            f"prompt_sum {mean([float(i.get('react_prompt_chars_sum') or 0) for i in items]):.0f} "
            f"token_mean {mean(tok):.0f} token_med {median(tok):.0f} "
            f"react_in {mean([float(i.get('react_token_input') or 0) for i in items]):.0f} "
            f"extract_in {mean([float(i.get('extract_token_input') or 0) for i in items]):.0f} "
            f"compress {mean([float(i.get('compress_token_total') or 0) for i in items]):.0f} "
            f"tools {mean([float(i.get('tool_calls') or 0) for i in items]):.1f} "
            f"trim {mean([float(i.get('trimmed_steps') or 0) for i in items]):.2f} "
            f"readback {sum(int(i.get('readback_calls') or 0) for i in items)} "
            f"zero_tool {sum(1 for i in items if int(i.get('tool_calls') or 0) == 0)} "
            f"err {sum(1 for i in items if i.get('error'))}"
        )
    return by


def diffs(label: str, a: dict[str, dict], b: dict[str, dict], pred) -> None:
    print(f"--- {label} ---")
    for cid in sorted(set(a) & set(b)):
        if pred(a[cid], b[cid]):
            left, right = a[cid], b[cid]
            print(
                f"{cid}: L={left.get('fail_kind')}/{left.get('root_component')} "
                f"correct={left.get('correct')}/{left.get('root_component_correct')}/{left.get('diagnosis_correct')} "
                f"M={right.get('fail_kind')}/{right.get('root_component')} "
                f"correct={right.get('correct')}/{right.get('root_component_correct')}/{right.get('diagnosis_correct')} "
                f"tok {left.get('token_total')}->{right.get('token_total')} "
                f"tools {left.get('tool_calls')}->{right.get('tool_calls')}"
            )


def token_heads(rows: list[dict], n: int = 5) -> None:
    by: dict[str, dict] = defaultdict(dict)
    for row in rows:
        by[row["case_id"]][row["strategy"]] = row
    deltas = []
    for cid, pair in by.items():
        if "legacy" not in pair or "managed" not in pair:
            continue
        d = int(pair["managed"].get("token_total") or 0) - int(pair["legacy"].get("token_total") or 0)
        deltas.append((d, cid, pair["legacy"], pair["managed"]))
    deltas.sort(reverse=True)
    print("--- managed-legacy token heads ---")
    for d, cid, left, right in deltas[:n]:
        print(
            f"{cid}: {d:+d} L={left.get('token_total')} M={right.get('token_total')} "
            f"tools {left.get('tool_calls')}->{right.get('tool_calls')} "
            f"prompt_sum {left.get('react_prompt_chars_sum')}->{right.get('react_prompt_chars_sum')}"
        )
    print("--- managed-legacy token tails ---")
    for d, cid, left, right in deltas[-n:]:
        print(
            f"{cid}: {d:+d} L={left.get('token_total')} M={right.get('token_total')} "
            f"tools {left.get('tool_calls')}->{right.get('tool_calls')}"
        )


def main() -> None:
    loaded = {rid: load(rid) for rid in RUNS}
    for rid, rows in loaded.items():
        print("=" * 72)
        print("run_id", rid)
        if len(rows) != 40:
            print("INCOMPLETE", len(rows))
        summarize(rows)
        if rows:
            token_heads(rows)
            by = defaultdict(list)
            for row in rows:
                by[row["strategy"]].append(row)
            leg = {r["case_id"]: r for r in by["legacy"]}
            man = {r["case_id"]: r for r in by["managed"]}
            diffs(
                "legacy yes managed no (diagnosis_correct)",
                leg,
                man,
                lambda l, r: l.get("diagnosis_correct") and not r.get("diagnosis_correct"),
            )
            diffs(
                "legacy no managed yes (diagnosis_correct)",
                leg,
                man,
                lambda l, r: (not l.get("diagnosis_correct")) and r.get("diagnosis_correct"),
            )
            diffs(
                "both diagnosis_correct false",
                leg,
                man,
                lambda l, r: (not l.get("diagnosis_correct")) and (not r.get("diagnosis_correct")),
            )
            diffs(
                "fail_kind legacy yes managed no",
                leg,
                man,
                lambda l, r: l.get("correct") and not r.get("correct"),
            )

    if len(RUNS) >= 2:
        old, new = loaded[RUNS[0]], loaded[RUNS[1]]
        old_by = {(r["case_id"], r["strategy"]): r for r in old}
        new_by = {(r["case_id"], r["strategy"]): r for r in new}
        print("=" * 72)
        print(f"quality flips {RUNS[0]} -> {RUNS[1]}")
        for key in sorted(set(old_by) & set(new_by)):
            a, b = old_by[key], new_by[key]
            if a.get("diagnosis_correct") != b.get("diagnosis_correct") or a.get("correct") != b.get("correct"):
                cid, strat = key
                print(
                    f"{cid}/{strat}: kind {a.get('correct')}->{b.get('correct')} "
                    f"both {a.get('diagnosis_correct')}->{b.get('diagnosis_correct')} "
                    f"{a.get('fail_kind')}/{a.get('root_component')} -> "
                    f"{b.get('fail_kind')}/{b.get('root_component')}"
                )


if __name__ == "__main__":
    main()
