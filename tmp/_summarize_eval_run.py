"""Summarize one eval-diagnose run_id from workspace/eval_results.jsonl."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

RUN_ID = sys.argv[1] if len(sys.argv) > 1 else "8241cf1bad71"
PATH = Path("workspace/eval_results.jsonl")


def main() -> None:
    rows = []
    with PATH.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("run_id") == RUN_ID:
                rows.append(row)
    print("run_id", RUN_ID, "n", len(rows))
    by: dict[str, list] = defaultdict(list)
    for row in rows:
        by[row["strategy"]].append(row)
    for strat in ("legacy", "managed"):
        items = by.get(strat, [])
        n = len(items)
        if not n:
            print(strat, "missing")
            continue
        kind = sum(1 for i in items if i.get("correct"))
        root = sum(1 for i in items if i.get("root_component_correct") is True)
        both = sum(1 for i in items if i.get("diagnosis_correct"))
        rec = sum(i.get("evidence_recall") or 0 for i in items) / n
        ctx = sum(i.get("context_chars") or 0 for i in items) / n
        rctx = sum(i.get("react_context_chars") or 0 for i in items) / n
        tok = sum(i.get("token_total") or 0 for i in items)
        lat = sum(i.get("latency_ms") or 0 for i in items) / n
        print(
            f"{strat}: fail_kind {kind}/{n} root {root}/{n} both {both}/{n} "
            f"recall {rec:.3f} extract {ctx:.0f} react {rctx:.0f} "
            f"tokens {tok} lat {lat/1000:.1f}s"
        )
    if "legacy" not in by or "managed" not in by:
        return
    leg = {r["case_id"]: r for r in by["legacy"]}
    man = {r["case_id"]: r for r in by["managed"]}
    if rows:
        last = rows[-1]
        print("last", last.get("case_id"), last.get("strategy"), last.get("recorded_at"))
    print("--- diagnosis_correct legacy yes managed no ---")
    for cid in sorted(set(leg) & set(man)):
        left, right = leg[cid], man[cid]
        if left.get("diagnosis_correct") and not right.get("diagnosis_correct"):
            print(
                f"{cid}: L={left.get('fail_kind')}/{left.get('root_component')} "
                f"M={right.get('fail_kind')}/{right.get('root_component')} "
                f"kind {left.get('correct')}/{right.get('correct')} "
                f"root {left.get('root_component_correct')}/{right.get('root_component_correct')}"
            )
    print("--- fail_kind legacy yes managed no ---")
    for cid in sorted(set(leg) & set(man)):
        left, right = leg[cid], man[cid]
        if left.get("correct") and not right.get("correct"):
            print(cid, "L", left.get("fail_kind"), "M", right.get("fail_kind"))
    print("--- both diagnosis_correct false ---")
    for cid in sorted(set(leg) & set(man)):
        left, right = leg[cid], man[cid]
        if not left.get("diagnosis_correct") and not right.get("diagnosis_correct"):
            print(
                cid,
                "L",
                left.get("fail_kind"),
                left.get("root_component"),
                "M",
                right.get("fail_kind"),
                right.get("root_component"),
            )


if __name__ == "__main__":
    main()
