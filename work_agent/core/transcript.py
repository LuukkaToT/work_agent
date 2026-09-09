"""按用户、任务、代理和执行轮次隔离的轻量事件与原文存储。

事件只接收小型 JSON 字典，大正文请用 ``put_text`` 归档后记录引用。这里不
序列化配置对象或完整模型对象，也不把事件当作工具已经执行的凭据。事件与
正文写入后不可修改，读取返回独立副本，重新绑定相同作用域即可回读。

默认脱敏只覆盖已知敏感键、Bearer 和常见口令表达，不能保证识别所有业务
敏感信息。可通过 ``Transcript(..., redactor=...)`` 增加业务规则。内容摘要
用于校验幂等与内容寻址，不包含原始秘密，但也不能代替访问控制或加密。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import islice
from typing import TYPE_CHECKING, Any, Callable

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from work_agent.core.db import get_pool

if TYPE_CHECKING:
    from work_agent.graph.helpers.context_archive import ArchiveRef
    from work_agent.graph.helpers.context_selector import ContextItem

_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_SEQ = 2**63 - 1
_REDACTED = "[已脱敏]"
_ARTIFACT = re.compile(r"sha256_[0-9a-f]{64}\Z")
_SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "secret", "secrets", "apikey", "token",
    "accesstoken", "refreshtoken", "authtoken", "authorization",
    "proxyauthorization", "cookie", "setcookie", "privatekey", "clientsecret",
    "credential", "credentials", "dsn", "postgresdsn", "postgrestestdsn",
    "connectionstring", "密码", "口令", "密钥",
}
_BEARER = re.compile(r"(?i)\b(Bearer[ \t]+)[a-z0-9._~+/=-]+")
_SECRET_TEXT = re.compile(
    r'''(?ix)(?<![\w])
    (?P<prefix>["']?(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|
        refresh[_-]?token|auth[_-]?token|token|client[_-]?secret|密码|口令|密钥)
        ["']?[ \t]*[:=：][ \t]*)
    (?:"(?P<double>(?:\\.|[^"\\])*)"|'(?P<single>(?:\\.|[^'\\])*)'|
        (?P<plain>[^\s,;，；}\]\["']+))'''
)


class TranscriptError(RuntimeError):
    """参数、脱敏或存储失败，调用方必须显式处理，不能视作写入成功。"""


class TranscriptConflict(TranscriptError):
    """同一个事件标识已绑定不同种类或内容，原记录保持不变。"""


class TranscriptExpired(TranscriptError):
    """作用域已闲置到期，需要由调用方显式选择新的执行标识。"""


def _identifier(value: str, name: str, maximum: int = 256) -> None:
    """
    校验隔离键字段：非空、无首尾空白与控制字符、长度上限。

    参数:
        value: 待校验字符串。
        name: 字段名，写进错误信息。
        maximum: 最大字符数。

    异常:
        TranscriptError: 不合规则。
    """
    if (
        not isinstance(value, str) or not value.strip() or value != value.strip()
        or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise TranscriptError(f"{name} 必须是无首尾空白及控制字符的非空字符串，最多 {maximum} 字符")
    _text(value)


def _text(value: str) -> str:
    """
    校验正文：必须是不含空字符、可 UTF-8 编码的字符串。

    返回:
        原字符串（不裁剪、不改大小写）。
    """
    if not isinstance(value, str) or "\x00" in value:
        raise TranscriptError("正文必须是字符串，且不能包含空字符")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise TranscriptError("正文必须能使用 UTF-8 完整编码") from exc
    return value


def _canonical(value: Any) -> str:
    """稳定 JSON：按键排序、无多余空格、保留中文、禁止 NaN。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _json_copy(value: Any) -> Any:
    """只复制 JSON 原生类型，不调用对象的字符串化或模型导出方法。"""
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) is str:
        return _text(value)
    if type(value) is list:
        return [_json_copy(item) for item in value]
    if type(value) is dict and all(type(key) is str for key in value):
        return {_text(key): _json_copy(item) for key, item in value.items()}
    raise TranscriptError("内容必须只含 JSON 原生类型与字符串键，不能保存配置或完整模型对象")


def _mask(value: str) -> str:
    """替换秘密片段，保留原有回车和换行。"""
    return re.sub(r"[^\r\n]+", lambda _: _REDACTED, value) or _REDACTED


def _redact_text(value: str) -> str:
    """替换 Bearer 与「键: 值」口令片段；其它正文原样保留。"""

    def replace(match: re.Match) -> str:
        for name, quote in (("double", '"'), ("single", "'"), ("plain", "")):
            secret = match.group(name)
            if secret is not None:
                return match.group("prefix") + quote + _mask(secret) + quote
        return match.group(0)

    return _SECRET_TEXT.sub(replace, _BEARER.sub(lambda m: m.group(1) + _REDACTED, value))


def redact(value: Any) -> Any:
    """递归脱敏 JSON，返回新对象，业务正文不改变大小写、不裁剪、不归一化。

    参数:
        value: JSON 字典、数组或原生标量，字符串可包含多行原文。
    返回:
        已知敏感键的值及文本内口令被替换后的独立副本。
    注意:
        键名匹配忽略大小写、下划线和连字符，只改变敏感值。无法识别所有
        业务秘密，调用方应按业务补充规则，不能把此函数当作安全边界。
    """
    def visit(item: Any) -> Any:
        """递归走 dict/list；敏感键整值打码，字符串走 ``_redact_text``。"""
        if isinstance(item, dict):
            return {
                key: (_mask(val) if isinstance(val, str) else _REDACTED)
                if re.sub(r"[\s_-]", "", key).casefold() in _SENSITIVE_KEYS else visit(val)
                for key, val in item.items()
            }
        if isinstance(item, list):
            return [visit(val) for val in item]
        return _redact_text(item) if isinstance(item, str) else item

    try:
        return visit(_json_copy(value))
    except RecursionError as exc:
        raise TranscriptError("JSON 嵌套过深或包含循环引用") from exc


@dataclass(frozen=True)
class TranscriptScope:
    """完整隔离键，各字段为 1～256 字符，不接受首尾空白或控制字符。

    不替调用方裁剪或转换大小写，执行标识由调用方提供。``stream_id`` 对四个
    字段的无歧义 JSON 编码计算 SHA-256，跨进程稳定，不使用运行时哈希。
    """

    user_id: str
    task_id: str
    agent_id: str
    execution_id: str

    def __post_init__(self) -> None:
        """构造后立即校验四个隔离字段。"""
        for name in ("user_id", "task_id", "agent_id", "execution_id"):
            _identifier(getattr(self, name), name)

    @property
    def stream_id(self) -> str:
        """四个字段稳定 JSON 的 SHA-256 十六进制，跨进程可复现。"""
        return hashlib.sha256(_canonical(self._values).encode("utf-8")).hexdigest()

    @property
    def _values(self) -> tuple[str, str, str, str]:
        """按固定顺序取出隔离键，给编码与库行比对用。"""
        return self.user_id, self.task_id, self.agent_id, self.execution_id


@dataclass(frozen=True)
class TranscriptEvent:
    """不可重新赋值的事件快照，嵌套字典可供调用方修改但不影响存储。"""

    event_id: str
    seq: int
    kind: str
    payload: dict


def _scope(scope: TranscriptScope) -> None:
    """确认作用域类型；不是 ``TranscriptScope`` 直接失败。"""
    if not isinstance(scope, TranscriptScope):
        raise TranscriptError("作用域必须是 TranscriptScope")


def _retention(days: float) -> float:
    """
    把闲置天数收成秒。

    参数:
        days: 大于 0 且不超过 36500，允许小数。

    返回:
        对应秒数。
    """
    if type(days) not in (int, float) or not 0 < days <= 36500:
        raise TranscriptError("闲置保留天数必须大于零且不超过 36500，允许小数")
    return float(days) * 86400


def _page(after_seq: int, limit: int) -> None:
    """校验事件分页：``after_seq`` 非负 64 位，``limit`` 为 1～1000。"""
    if type(after_seq) is not int or not 0 <= after_seq <= _MAX_SEQ:
        raise TranscriptError("after_seq 必须是非负的有符号 64 位整数")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise TranscriptError("limit 必须是 1～1000 的整数")


def _payload(value: Any) -> tuple[dict, str]:
    """
    复制并编码事件 payload，同时给出原始 JSON 的 SHA-256。

    返回:
        ``(独立 dict 副本, 十六进制摘要)``。
    """
    if type(value) is not dict:
        raise TranscriptError("事件 payload 必须是 JSON 字典")
    try:
        detached = _json_copy(value)
        encoded = _canonical(detached).encode("utf-8")
    except (RecursionError, ValueError, OverflowError) as exc:
        raise TranscriptError("事件 JSON 无法完整编码，可能嵌套过深或包含循环引用") from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise TranscriptError("事件 payload 最多 65536 字节，大正文请归档后保存引用")
    return detached, hashlib.sha256(encoded).hexdigest()


def _sanitize(value: Any, redactor: Callable | None) -> Any:
    """先默认脱敏，再跑业务 redactor；业务异常阻止写入。"""
    safe = redact(value)
    if redactor is None:
        return safe
    try:
        return redact(redactor(safe))
    except Exception as exc:
        raise TranscriptError("业务脱敏失败，本次内容未写入") from exc


def _ref(scope: TranscriptScope, artifact_id: str, chars: int, backend: str) -> ArchiveRef:
    """构造归档引用 URI；运行时才导入 ArchiveRef，避免与图层循环导入。"""
    # 运行时仅在构造引用时导入，避免图层接线反向导入本模块形成循环。
    from work_agent.graph.helpers.context_archive import ArchiveRef

    return ArchiveRef(artifact_id, f"{backend}://agent_transcript/{scope.stream_id}/{artifact_id}", chars)


class _TranscriptStore:
    """共享校验与脱敏入口，具体后端只接收已复制和脱敏的内容。"""

    def append(self, scope: TranscriptScope, event_id: str, kind: str, payload: dict) -> TranscriptEvent:
        """追加事件，相同标识与内容幂等，种类或原始 JSON 不同则冲突。

        参数:
            scope: 完整隔离键。
            event_id: 调用方提供的稳定标识，最多 256 字符。
            kind: 事件种类，最多 64 字符，不限定工具或消息类型。
            payload: 最多 65536 字节的 JSON 字典，脱敏前后均校验大小。
        返回:
            已持久化事件的独立快照，序号从 1 开始且仅在新增事件时递增。
        异常:
            TranscriptConflict: 标识已用于其他内容，包括仅敏感值不同的情况。
            TranscriptExpired: 原作用域已到期，不会隐式重建。
            TranscriptError: 参数或存储失败，失败写入不刷新期限。
        """
        return self._record(scope, event_id, kind, payload, None)

    def _record(self, scope, event_id, kind, payload, redactor) -> TranscriptEvent:
        """校验、脱敏后交给后端 ``_append``；``redactor`` 为 None 时只做默认脱敏。"""
        _scope(scope)
        _identifier(event_id, "event_id")
        _identifier(kind, "kind", 64)
        original, digest = _payload(payload)
        safe, _ = _payload(_sanitize(original, redactor))
        return self._append(scope, event_id, kind, safe, digest)

    def put_artifact(self, scope: TranscriptScope, text: str) -> ArchiveRef:
        """存入完整脱敏正文，按其 UTF-8 SHA-256 去重，空正文也可归档。"""
        return self._put_text(scope, text, None)

    def _put_text(self, scope, text, redactor) -> ArchiveRef:
        """校验并脱敏字符串后交给 ``_save_text``。"""
        _scope(scope)
        safe = _text(_sanitize(_text(text), redactor))
        return self._save_text(scope, safe)

    def _save_text(self, scope, text) -> ArchiveRef:
        """保存已经完成脱敏的正文，JSON 归档不能再次按字符串替换。"""
        _scope(scope)
        _text(text)
        artifact_id = "sha256_" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self._put_artifact(scope, artifact_id, text)


@dataclass
class _MemoryStream:
    """内存里一条记录流：事件、正文、独立锁与闲置到期时间。"""

    scope: TranscriptScope
    expires_at: float
    events: dict[str, tuple[TranscriptEvent, str]] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    lock: Any = field(default_factory=threading.RLock)


class MemoryTranscriptStore(_TranscriptStore):
    """线程安全的进程内存储，同一实例可被多个 Transcript 复用。

    参数:
        retention_days: 默认闲置 30 天，每次成功写入（含幂等重试）刷新。
            大于零，最多 36500 天，可用小数。读取不刷新，过期数据不删除。
    注意:
        仅短暂锁定作用域目录，每个流独立加锁。重建此存储实例会丢失内存
        数据，重建绑定同一实例的 Transcript 不会丢失。
    """

    def __init__(self, *, retention_days: float = 30) -> None:
        """见类文档 ``retention_days``。"""
        self._retention_seconds = _retention(retention_days)
        self._streams: dict[str, _MemoryStream] = {}
        self._streams_lock = threading.RLock()

    @contextmanager
    def _stream(self, scope: TranscriptScope, *, write: bool = False):
        """取出当前作用域的内存流；写路径可新建，过期显式报错。"""
        _scope(scope)
        with self._streams_lock:
            stream = self._streams.get(scope.stream_id)
            if stream is None and write:
                stream = _MemoryStream(scope, time.time() + self._retention_seconds)
                self._streams[scope.stream_id] = stream
        if stream is None:
            yield None
            return
        with stream.lock:
            if stream.scope != scope:
                raise TranscriptError("作用域校验失败")
            if stream.expires_at <= time.time():
                raise TranscriptExpired("记录流已闲置到期，请使用新的执行标识")
            yield stream
            if write:
                stream.expires_at = time.time() + self._retention_seconds

    def _append(self, scope, event_id, kind, payload, digest) -> TranscriptEvent:
        """内存追加：同标识同摘要幂等，不同则冲突。"""
        with self._stream(scope, write=True) as stream:
            existing = stream.events.get(event_id)
            if existing is not None:
                event, original_digest = existing
                if event.kind != kind or original_digest != digest or _canonical(event.payload) != _canonical(payload):
                    raise TranscriptConflict("事件标识已绑定不同种类或内容")
            else:
                event = TranscriptEvent(event_id, len(stream.events) + 1, kind, copy.deepcopy(payload))
                stream.events[event_id] = event, digest
            return copy.deepcopy(event)

    def get_event(self, scope: TranscriptScope, event_id: str) -> TranscriptEvent | None:
        """读取当前作用域的事件，流或事件不存在返回 None，过期显式报错。"""
        _identifier(event_id, "event_id")
        with self._stream(scope) as stream:
            entry = stream.events.get(event_id) if stream else None
            return copy.deepcopy(entry[0]) if entry else None

    def list_events(self, scope: TranscriptScope, *, after_seq: int = 0, limit: int = 100) -> list[TranscriptEvent]:
        """按序号升序读取，排除 after_seq 本身，limit 为 1～1000。"""
        _page(after_seq, limit)
        with self._stream(scope) as stream:
            if stream is None:
                return []
            ordered = sorted(
                (event for event, _ in stream.events.values() if event.seq > after_seq),
                key=lambda event: event.seq,
            )
            return copy.deepcopy(list(islice(ordered, limit)))

    def _put_artifact(self, scope, artifact_id, text) -> ArchiveRef:
        """按 artifact_id 写入内存正文；摘要冲突则拒绝。"""
        with self._stream(scope, write=True) as stream:
            existing = stream.artifacts.get(artifact_id)
            if existing is not None and existing != text:
                raise TranscriptConflict("正文内容摘要冲突")
            stream.artifacts[artifact_id] = text
            return _ref(scope, artifact_id, len(text), "memory")

    def read_artifact(self, scope: TranscriptScope, artifact_id: str) -> str:
        """读取本作用域正文，未找到统一抛出 FileNotFoundError。"""
        with self._stream(scope) as stream:
            if not isinstance(artifact_id, str) or not _ARTIFACT.fullmatch(artifact_id):
                raise FileNotFoundError("未找到当前作用域的归档正文")
            if stream is None or artifact_id not in stream.artifacts:
                raise FileNotFoundError("未找到当前作用域的归档正文")
            return stream.artifacts[artifact_id]

    def list_artifacts(self, scope: TranscriptScope) -> list[ArchiveRef]:
        """按正文标识排序返回所有引用，列表不依赖 Transcript 的本地缓存。"""
        with self._stream(scope) as stream:
            return [] if stream is None else [
                _ref(scope, key, len(value), "memory")
                for key, value in sorted(stream.artifacts.items())
            ]


class PostgresTranscriptStore(_TranscriptStore):
    """使用三张专用表的持久化存储，重建存储对象或连接池后可回读。

    参数:
        pool: 已打开的 psycopg 连接池，None 时首次操作才调用项目 get_pool。
            兼容自动提交连接，所有操作都使用显式事务与字典行工厂。
        retention_days: 默认闲置 30 天，范围与内存版本一致。
    注意:
        写操作仅锁当前 stream 行，提交后才返回成功。读操作使用共享行锁，
        保证校验期限与读取属于同一个事务。时间取数据库实际时钟，避免锁
        等待期间过期后又被刷新。过期记录不复活、不自动删除，也不自动迁移。
    """

    def __init__(self, *, pool=None, retention_days: float = 30) -> None:
        """见类文档 ``pool`` / ``retention_days``；此处不连库。"""
        self._pool = pool
        self._retention_seconds = _retention(retention_days)

    @property
    def pool(self):
        """显式注入的池或项目共享池，构造存储对象本身不访问数据库。"""
        return self._pool if self._pool is not None else get_pool()

    @contextmanager
    def _stream(self, scope: TranscriptScope, *, write: bool = False):
        """事务内锁定 stream 行；写路径可插入，过期按库时钟判定。"""
        _scope(scope)
        try:
            with self.pool.connection() as conn, conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
                if write:
                    cur.execute(
                        "INSERT INTO agent_transcript_streams "
                        "(stream_id, user_id, task_id, agent_id, execution_id, expires_at) "
                        "VALUES (%s, %s, %s, %s, %s, clock_timestamp() + %s * interval '1 second') "
                        "ON CONFLICT (stream_id) DO NOTHING",
                        (scope.stream_id, *scope._values, self._retention_seconds),
                    )
                lock = "UPDATE" if write else "SHARE"
                row = cur.execute(
                    "SELECT user_id, task_id, agent_id, execution_id, last_seq "
                    f"FROM agent_transcript_streams WHERE stream_id=%s FOR {lock}",
                    (scope.stream_id,),
                ).fetchone()
                if row is not None:
                    if tuple(row[key] for key in ("user_id", "task_id", "agent_id", "execution_id")) != scope._values:
                        raise TranscriptError("作用域校验失败")
                    # 单独查询实际时间，确保先拿到行锁再判定是否已经到期。
                    expired = cur.execute(
                        "SELECT expires_at <= clock_timestamp() AS expired "
                        "FROM agent_transcript_streams WHERE stream_id=%s",
                        (scope.stream_id,),
                    ).fetchone()["expired"]
                    if expired:
                        raise TranscriptExpired("记录流已闲置到期，请使用新的执行标识")
                yield cur, row
                if write:
                    cur.execute(
                        "UPDATE agent_transcript_streams "
                        "SET expires_at=clock_timestamp() + %s * interval '1 second' WHERE stream_id=%s",
                        (self._retention_seconds, scope.stream_id),
                    )
        except (TranscriptError, FileNotFoundError):
            raise
        except Exception as exc:
            raise TranscriptError("记录存储操作失败，事务未确认成功") from exc

    @staticmethod
    def _event(row) -> TranscriptEvent:
        """把事件表行收成快照；payload 深拷贝。"""
        return TranscriptEvent(row["event_id"], row["seq"], row["kind"], copy.deepcopy(row["payload"]))

    def _append(self, scope, event_id, kind, payload, digest) -> TranscriptEvent:
        """库内追加事件并推进 ``last_seq``；冲突规则同内存实现。"""
        with self._stream(scope, write=True) as (cur, stream):
            if stream is None:
                raise TranscriptError("无法创建或锁定记录流")
            row = cur.execute(
                "SELECT event_id, seq, kind, payload, payload_sha256 FROM agent_transcript_events "
                "WHERE stream_id=%s AND event_id=%s", (scope.stream_id, event_id),
            ).fetchone()
            if row is not None:
                if row["kind"] != kind or row["payload_sha256"] != digest or _canonical(row["payload"]) != _canonical(payload):
                    raise TranscriptConflict("事件标识已绑定不同种类或内容")
                event = self._event(row)
            else:
                seq = stream["last_seq"] + 1
                cur.execute(
                    "INSERT INTO agent_transcript_events "
                    "(stream_id, event_id, seq, kind, payload, payload_sha256) VALUES (%s, %s, %s, %s, %s, %s)",
                    (scope.stream_id, event_id, seq, kind, Jsonb(payload), digest),
                )
                cur.execute(
                    "UPDATE agent_transcript_streams SET last_seq=%s WHERE stream_id=%s",
                    (seq, scope.stream_id),
                )
                event = TranscriptEvent(event_id, seq, kind, copy.deepcopy(payload))
        return event

    def get_event(self, scope: TranscriptScope, event_id: str) -> TranscriptEvent | None:
        """读取完整作用域内的事件，未创建的流或不存在的事件返回 None。"""
        _identifier(event_id, "event_id")
        with self._stream(scope) as (cur, stream):
            row = None if stream is None else cur.execute(
                "SELECT event_id, seq, kind, payload FROM agent_transcript_events "
                "WHERE stream_id=%s AND event_id=%s", (scope.stream_id, event_id),
            ).fetchone()
            return self._event(row) if row is not None else None

    def list_events(self, scope: TranscriptScope, *, after_seq: int = 0, limit: int = 100) -> list[TranscriptEvent]:
        """按序号升序分页，after_seq 不包含在结果中，limit 为 1～1000。"""
        _page(after_seq, limit)
        with self._stream(scope) as (cur, stream):
            rows = [] if stream is None else cur.execute(
                "SELECT event_id, seq, kind, payload FROM agent_transcript_events "
                "WHERE stream_id=%s AND seq>%s ORDER BY seq LIMIT %s",
                (scope.stream_id, after_seq, limit),
            ).fetchall()
            return [self._event(row) for row in rows]

    def _put_artifact(self, scope, artifact_id, text) -> ArchiveRef:
        """写入 ``agent_transcript_artifacts``；同摘要冲突拒绝。"""
        with self._stream(scope, write=True) as (cur, _):
            row = cur.execute(
                "SELECT content FROM agent_transcript_artifacts WHERE stream_id=%s AND artifact_id=%s",
                (scope.stream_id, artifact_id),
            ).fetchone()
            if row is not None and row["content"] != text:
                raise TranscriptConflict("正文内容摘要冲突")
            if row is None:
                cur.execute(
                    "INSERT INTO agent_transcript_artifacts (stream_id, artifact_id, content, chars) "
                    "VALUES (%s, %s, %s, %s)", (scope.stream_id, artifact_id, text, len(text)),
                )
            ref = _ref(scope, artifact_id, len(text), "postgres")
        return ref

    def read_artifact(self, scope: TranscriptScope, artifact_id: str) -> str:
        """只查询当前作用域，错误不显示其他作用域是否持有相同正文。"""
        with self._stream(scope) as (cur, stream):
            if not isinstance(artifact_id, str) or not _ARTIFACT.fullmatch(artifact_id):
                raise FileNotFoundError("未找到当前作用域的归档正文")
            row = None if stream is None else cur.execute(
                "SELECT content FROM agent_transcript_artifacts WHERE stream_id=%s AND artifact_id=%s",
                (scope.stream_id, artifact_id),
            ).fetchone()
            if row is None:
                raise FileNotFoundError("未找到当前作用域的归档正文")
            return row["content"]

    def list_artifacts(self, scope: TranscriptScope) -> list[ArchiveRef]:
        """从数据库按正文标识排序列出引用，不依赖进程内清单。"""
        with self._stream(scope) as (cur, stream):
            rows = [] if stream is None else cur.execute(
                "SELECT artifact_id, chars FROM agent_transcript_artifacts WHERE stream_id=%s ORDER BY artifact_id",
                (scope.stream_id,),
            ).fetchall()
            return [_ref(scope, row["artifact_id"], row["chars"], "postgres") for row in rows]


class Transcript:
    """绑定一次完整作用域，同时实现事件入口和原有 Archive 协议。

    参数:
        store: MemoryTranscriptStore 或 PostgresTranscriptStore 实例。
        scope: 当前用户、任务、代理及执行轮次，不隐式推导或重建执行标识。
        redactor: 可选的业务脱敏函数，接收默认脱敏后的 JSON 或字符串副本，
            返回相同顶层类型。输出再次做默认脱敏与校验，异常阻止写入。
            同一原文应产生稳定结果，否则相同事件标识会冲突。
    注意:
        ``store_backend`` 保存后端，``store`` 留给 Archive 协议。归档按脱敏
        正文寻址，不按 ContextItem 的可重用标签覆盖。原始事件 JSON 的摘要
        在脱敏前计算，业务脱敏也不会使不同原始事件被误判为幂等重试。
    """

    def __init__(self, store: MemoryTranscriptStore | PostgresTranscriptStore, scope: TranscriptScope,
                 *, redactor: Callable[[Any], Any] | None = None) -> None:
        """见类文档；``redactor`` 非空时必须可调用。"""
        _scope(scope)
        if redactor is not None and not callable(redactor):
            raise TranscriptError("redactor 必须是可调用的业务脱敏函数")
        self.scope = scope
        self.store_backend = store
        self._redactor = redactor

    def record(self, event_id: str, kind: str, payload: dict) -> TranscriptEvent:
        """记录事件并返回已保存快照，不调用或跳过任何工具执行。"""
        if self._redactor is None:
            return self.store_backend.append(self.scope, event_id, kind, payload)
        return self.store_backend._record(self.scope, event_id, kind, payload, self._redactor)

    def get(self, event_id: str) -> TranscriptEvent | None:
        """读取当前执行内的事件，不存在返回 None。"""
        return self.store_backend.get_event(self.scope, event_id)

    def events(self, *, after_seq: int = 0, limit: int = 100) -> list[TranscriptEvent]:
        """按序号升序分页读取事件，读取不刷新闲置期限。"""
        return self.store_backend.list_events(self.scope, after_seq=after_seq, limit=limit)

    def put_text(self, text: str) -> ArchiveRef:
        """归档完整字符串，可用于 JSON 消息正文，不进行消息对象序列化。"""
        if self._redactor is None:
            return self.store_backend.put_artifact(self.scope, text)
        return self.store_backend._put_text(self.scope, text, self._redactor)

    def put_json(self, value: Any) -> ArchiveRef:
        """递归脱敏 JSON 后归档稳定编码，供完整消息和实际模型输入使用。

        参数:
            value: JSON 原生对象或标量，不接受配置实例或完整模型实例。
                先执行默认递归脱敏和本 Transcript 的业务脱敏，再按键排序、
                无额外空格、保留中文的方式编码。正文不受小事件大小限制。
        返回:
            完整 JSON 正文引用，可由 read_json 恢复脱敏后的 JSON 结构。
        注意:
            不对编码结果再做字符串脱敏，避免漏掉嵌套键或破坏转义换行。
        """
        safe = _sanitize(value, self._redactor)
        try:
            text = _canonical(safe)
        except (RecursionError, ValueError, OverflowError) as exc:
            raise TranscriptError("归档 JSON 无法完整编码") from exc
        return self.store_backend._save_text(self.scope, text)

    def read_json(self, artifact_id: str) -> Any:
        """读取当前作用域的 JSON 正文，无效 JSON 抛出 TranscriptError。"""
        text = self.read(artifact_id)
        try:
            return json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise TranscriptError("归档正文不是有效的 JSON") from exc

    def store(self, item: ContextItem) -> ArchiveRef:
        """按 Archive 协议归档 item.text，不保存其余对象字段。"""
        return self.put_text(item.text)

    def store_all(self, items: list[ContextItem]) -> list[ArchiveRef]:
        """依次归档并返回对应引用，失败时此前成功的单条写入不会撤销。"""
        return [self.store(item) for item in items]

    def read(self, artifact_id: str) -> str:
        """回读完整脱敏正文，未找到抛出 FileNotFoundError。"""
        return self.store_backend.read_artifact(self.scope, artifact_id)

    @property
    def refs(self) -> list[ArchiveRef]:
        """查询当前作用域的全部引用，重新绑定后仍可列出既有归档。"""
        return self.store_backend.list_artifacts(self.scope)
