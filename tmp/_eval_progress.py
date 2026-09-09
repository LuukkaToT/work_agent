"""Print progress for the latest eval-diagnose run_id."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

PATH = Path("workspace/eval_results.jsonl")


def main() -> None:
    rows = []
    with PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    by: dict[str, list] = defaultdict(list)
    for row in rows:
        by[row["run_id"]].append(row)
    rid = max(by, key=lambda k: max(r.get("recorded_at") or "" for r in by[k]))
    rs = sorted(by[rid], key=lambda r: r.get("recorded_at") or "")
    print(f"run_id {rid}  {len(rs)}/40")
    print(f"start {rs[0]['recorded_at']}  last {rs[-1]['recorded_at']}")
    print(f"last {rs[-1]['case_id']}/{rs[-1]['strategy']}")
    print("done:", " ".join(f"{r['case_id']}/{r['strategy'][0]}" for r in rs))
    buckets: dict[str, list] = defaultdict(list)
    for row in rs:
        buckets[row["strategy"]].append(row)
    for strat in ("legacy", "managed"):
        items = buckets.get(strat, [])
        n = len(items)
        if not n:
            print(strat, "n=0")
            continue
        kind = sum(1 for i in items if i.get("correct"))
        both = sum(1 for i in items if i.get("diagnosis_correct"))
        rec = sum(i.get("evidence_recall") or 0 for i in items) / n
        ctx = sum(i.get("context_chars") or 0 for i in items) / n
        tok = [i.get("token_total") or 0 for i in items]
        med = sorted(tok)[n // 2]
        mean = sum(tok) / n
        rb = sum(i.get("readback_calls") or 0 for i in items)
        print(
            f"{strat}: n={n} fail_kind {kind}/{n} both {both}/{n} "
            f"recall {rec:.3f} extract {ctx:.0f} token_mean {mean:.0f} "
            f"token_med {med:.0f} readback {rb}"
        )
    print("errors:", sum(1 for r in rs if r.get("error")))


if __name__ == "__main__":
    main()
