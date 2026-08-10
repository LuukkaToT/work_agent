"""按优先级组装上下文块：超预算从低优先级裁掉。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextBlock:
    """一块可裁剪上下文。priority 越小越重要（越后裁）。"""

    name: str
    text: str
    priority: int  # 0 = highest


# 约定优先级（诊断）
PRIORITY_CONCLUSION = 0
PRIORITY_EVIDENCE = 1
PRIORITY_RULED_OUT = 2
PRIORITY_RAG = 3
PRIORITY_RAW_TOOL = 4


def assemble_blocks(
    blocks: list[ContextBlock],
    *,
    limit: int,
) -> str:
    """
    按 priority 升序尽量装入；超限时从 priority 大的块开始丢弃整块，
    若仍超则对最低优先级块做尾部截断。
    """
    if limit <= 0:
        return ""

    usable = [b for b in blocks if (b.text or "").strip()]
    usable.sort(key=lambda b: (b.priority, b.name))

    selected = list(usable)
    while selected:
        parts: list[str] = []
        for b in selected:
            parts.append(f"【{b.name}】\n{b.text.strip()}")
        joined = "\n\n".join(parts)
        if len(joined) <= limit:
            return joined
        # 丢掉当前优先级最低的一块
        worst = max(selected, key=lambda b: (b.priority, len(b.text)))
        if len(selected) == 1:
            # 最后一块：硬截断
            header = f"【{worst.name}】\n"
            room = max(0, limit - len(header) - 20)
            body = worst.text.strip()[:room] + "\n...(truncated by priority budget)"
            return header + body
        selected.remove(worst)
    return ""


def compress_observation(text: str, *, max_chars: int = 400) -> str:
    """
    把长 tool observation 压成短 evidence 摘录：
    优先保留含 ERROR/FAIL/Exception/Traceback 的行，否则 head+tail。
    """
    raw = (text or "").strip()
    if len(raw) <= max_chars:
        return raw

    lines = raw.splitlines()
    key_re_parts = (
        "ERROR",
        "FAIL",
        "Exception",
        "Traceback",
        "rejected",
        "timeout",
        "refused",
        "KeyError",
        "mismatch",
    )
    keyed = [
        ln
        for ln in lines
        if any(k.lower() in ln.lower() for k in key_re_parts)
    ]
    if keyed:
        out = "\n".join(keyed)
        if len(out) > max_chars:
            out = out[: max_chars - 20] + "\n...(truncated)"
        return out

    head = max_chars // 2
    tail = max_chars - head - 20
    return raw[:head] + "\n...(truncated)\n" + raw[-tail:]
