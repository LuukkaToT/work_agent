from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

PATH = Path("workspace/eval_results.jsonl")
RUNS = ["8241cf1bad71", "b72925255e52", "a4145c290be4"]


def main() -> None:
    by = defaultdict(list)
    with PATH.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("run_id") in RUNS:
                by[row["run_id"]].append(row)
    for rid in RUNS:
        rows = by[rid]
        print("=" * 60, rid)
        for strat in ("legacy", "managed"):
            items = [r for r in rows if r["strategy"] == strat]
            toks = sorted(int(r.get("token_total") or 0) for r in items)
            print(strat, "tokens", toks)
            if toks:
                mid = len(toks) // 2
                if len(toks) % 2:
                    med = toks[mid]
                else:
                    med = (toks[mid - 1] + toks[mid]) / 2
                print("  n", len(toks), "mean", sum(toks) / len(toks), "median", med)
        print("kind/both:")
        for r in sorted(rows, key=lambda x: (x["case_id"], x["strategy"])):
            print(
                f"  {r['case_id']}/{r['strategy'][0]} kind={int(bool(r.get('correct')))} "
                f"root={r.get('root_component_correct')} both={int(bool(r.get('diagnosis_correct')))} "
                f"fk={r.get('fail_kind')} rc={r.get('root_component')} "
                f"extract={r.get('context_chars')} tok={r.get('token_total')} "
                f"tools={r.get('tool_calls')} trim={r.get('trimmed_steps')} "
                f"rb={r.get('readback_calls')}"
            )


if __name__ == "__main__":
    main()
