"""工具结果截断与归因轮次字符预算。"""

from __future__ import annotations

from dataclasses import dataclass


def clip_text(
    text: str,
    *,
    max_chars: int,
    head_chars: int | None = None,
    tail_chars: int | None = None,
) -> str:
    """
    超限：head + 省略标记 + tail；未超限原样返回。
    省略标记本身占预算，先扣掉再切 head/tail。
    """
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text

    line_count = text.count("\n") + (1 if text else 0)
    # 占位：先用估算，算出 head/tail 后再用真实 omitted 重写一次也可；
    # 这里用两遍：先按预估 marker 长度切，再拼真实 marker。
    head_n = head_chars if head_chars is not None else max_chars // 2
    tail_n = tail_chars if tail_chars is not None else max_chars - head_n

    # 预留 marker 空间（保守 80 字符），避免拼完又超 max_chars
    reserve = 80
    budget = max(0, max_chars - reserve)
    if head_chars is None and tail_chars is None:
        head_n = budget // 2
        tail_n = budget - head_n
    else:
        # 调用方指定了 head/tail：按比例缩进 reserve
        total_ht = max(1, head_n + tail_n)
        scale = budget / total_ht
        head_n = int(head_n * scale)
        tail_n = max(0, budget - head_n)

    head = text[:head_n]
    tail = text[-tail_n:] if tail_n else ""
    omitted = len(text) - len(head) - len(tail)
    if omitted < 0:
        # 极端短 max_chars：退化为硬截断
        return text[:max_chars]

    marker = (
        f"\n...[truncated chars={omitted} total_chars={len(text)} "
        f"lines={line_count}]...\n"
    )
    out = head + marker + tail
    if len(out) > max_chars:
        # 仍超：优先保 marker + tail，再砍 head
        keep_tail = marker + tail
        room = max_chars - len(keep_tail)
        if room <= 0:
            return out[:max_chars]
        out = text[:room] + keep_tail
    return out


@dataclass
class CharBudget:
    """同一轮 ReAct 内累计工具返回字符。"""

    limit: int
    used: int = 0

    def take(self, text: str, *, max_chars: int) -> str:
        """先 clip 到 max_chars，再计入预算；超预算返回短提示。"""
        if self.used >= self.limit:
            return (
                f"[budget exceeded] used={self.used} limit={self.limit}；"
                "请改用 grep_logs 缩小范围"
            )
        clipped = clip_text(text, max_chars=max_chars)
        remaining = self.limit - self.used
        if len(clipped) > remaining:
            # 还能塞一点：再 clip 一次到 remaining；若 remaining 太小直接提示
            if remaining < 64:
                return (
                    f"[budget exceeded] used={self.used} limit={self.limit}；"
                    "请改用 grep_logs 缩小范围"
                )
            clipped = clip_text(clipped, max_chars=remaining)
        self.used += len(clipped)
        return clipped