"""Token / tool breakdown for the two eval runs."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

PATH = Path("workspace/eval_results.jsonl")
RUNS = ("8241cf1bad71", "b72925255e52")


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def load(run_id: str) -> dict[str, list]:
    by: dict[str, list] = defaultdict(list)
    with PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("run_id") == run_id:
                by[row["strategy"]].append(row)
    return by


def main() -> None:
    for run_id in RUNS:
        by = load(run_id)
        print("====", run_id)
        for strat in ("legacy", "managed"):
            items = by[strat]
            print(
                strat,
                "n",
                len(items),
                "tok_mean",
                round(mean([i.get("token_total") or 0 for i in items])),
                "calls_mean",
                round(mean([i.get("llm_calls") or 0 for i in items]), 2),
                "tools_mean",
                round(mean([i.get("tool_calls") or 0 for i in items]), 2),
                "extract",
                round(mean([i.get("context_chars") or 0 for i in items])),
                "react",
                round(mean([i.get("react_context_chars") or 0 for i in items])),
                "archived",
                round(mean([i.get("archived_n") or 0 for i in items]), 2),
            )


if __name__ == "__main__":
    main()
