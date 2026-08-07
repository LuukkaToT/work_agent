"""按资料目录组织的本地知识库。

目录名是第一层硬过滤：信道目录进入 channel 索引，其余资料进入 basic
索引。当前支持 Markdown/TXT；真实环境可保持原目录不变，只切换根路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
from pathlib import Path
import re
from typing import Iterable

from work_agent.analysis.schemas import EvidenceHit
from work_agent.core.config import get_settings

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}

CHANNEL_ALIASES = {
    "PUCCH": "PUCCH",
    "PUSCH": "PUSCH",
    "PRACH": "PRACH",
    "PDCCH": "PDCCH",
    "PDSCH": "PDSCH",
    "SRS": "SRS",
    "CSIRS": "CSI-RS",
    "CSI-RS": "CSI-RS",
    "CSI_RS": "CSI-RS",
    "SSB": "SSB",
    "PBCH": "PBCH",
    "DMRS": "DMRS",
    "PTRS": "PTRS",
    "RRC": "RRC",
    "MAC": "MAC",
}

BASIC_FOLDER_MARKERS = {
    "basic",
    "baseline",
    "common",
    "general",
    "testpoints",
    "testpoint",
    "basic_test_points",
    "基础测试点",
    "基础测试点资料库",
    "通用",
    "公共",
}

# 这里只定义允许扩展检索的邻接信道，不代表完整协议依赖关系。
CHANNEL_DEPENDENCIES = {
    "PUCCH": ["PUSCH", "SRS", "RRC", "MAC"],
    "PUSCH": ["PUCCH", "SRS", "RRC", "MAC"],
    "PRACH": ["PDCCH", "PDSCH", "RRC", "MAC"],
    "PDCCH": ["PDSCH", "RRC", "MAC"],
    "PDSCH": ["PDCCH", "RRC", "MAC"],
    "SRS": ["PUSCH", "PUCCH", "RRC", "MAC"],
    "CSI-RS": ["PDSCH", "PDCCH", "RRC"],
    "SSB": ["PBCH", "PRACH", "RRC"],
}


def _key(value: str) -> str:
    return re.sub(r"[\s_.-]+", "", value).upper()


_CHANNEL_KEYS = {_key(alias): canonical for alias, canonical in CHANNEL_ALIASES.items()}


def normalize_channel(value: str) -> str | None:
    """把大小写、连字符等不同写法归一为受支持的标准信道名。"""

    return _CHANNEL_KEYS.get(_key(value))


def _channel_from_folder(value: str) -> str | None:
    exact = normalize_channel(value)
    if exact:
        return exact
    folder_key = _key(value)
    # 兼容 "01_PUCCH_上行控制信道" 这类真实目录名；Tool 入参仍要求精确别名。
    matches = [
        (len(alias_key), canonical)
        for alias_key, canonical in _CHANNEL_KEYS.items()
        if alias_key and alias_key in folder_key
    ]
    return max(matches, default=(0, None))[1]


@dataclass(frozen=True)
class DocumentChunk:
    """检索内部使用的不可变资料分块。"""

    chunk_id: str
    doc_id: str
    source_kind: str
    channel: str | None
    title: str
    section: str
    relative_path: str
    content: str


@dataclass(frozen=True)
class CorpusDocument:
    """扫描阶段得到的资料文件及其信道范围元数据。"""

    path: Path
    relative_path: Path
    source_kind: str
    channel: str | None


def _document_title(path: Path, text: str) -> str:
    for line in text.splitlines():
        match = re.match(r"^\s*#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return path.stem.replace("_", " ").replace("-", " ")


def _split_long_section(text: str, *, max_chars: int = 1800) -> list[str]:
    """优先按段落切分长章节，超长单段再按字符窗口兜底。"""

    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []

    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if current and current_len + len(paragraph) + 2 > max_chars:
            pieces.append("\n\n".join(current))
            current = []
            current_len = 0
        if len(paragraph) > max_chars:
            if current:
                pieces.append("\n\n".join(current))
                current = []
                current_len = 0
            for start in range(0, len(paragraph), max_chars):
                pieces.append(paragraph[start : start + max_chars])
            continue
        current.append(paragraph)
        current_len += len(paragraph) + 2
    if current:
        pieces.append("\n\n".join(current))
    return pieces


def _split_markdown(text: str) -> list[tuple[str, str]]:
    """保留一至三级标题作为 section 元数据并切分正文。"""

    sections: list[tuple[str, list[str]]] = [("正文", [])]
    for line in text.splitlines():
        heading = re.match(r"^\s*#{1,3}\s+(.+?)\s*$", line)
        if heading:
            sections.append((heading.group(1).strip(), []))
        else:
            sections[-1][1].append(line)

    out: list[tuple[str, str]] = []
    for section, lines in sections:
        for piece in _split_long_section("\n".join(lines)):
            out.append((section, piece))
    return out


def _infer_scope(relative_path: Path) -> tuple[str, str | None]:
    """仅根据根目录内的相对路径判断资料属于基础库还是信道库。"""

    for part in relative_path.parts[:-1]:
        channel = _channel_from_folder(part)
        if channel:
            return "channel", channel
    lowered = {
        re.sub(r"[\s-]+", "_", part.strip().lower())
        for part in relative_path.parts[:-1]
    }
    if lowered & BASIC_FOLDER_MARKERS:
        return "basic", None
    # 未分类资料按通用资料处理，避免模型获得任意路径选择权。
    return "basic", None


class CorpusCatalog:
    """惰性扫描、按资料范围加载并缓存分块的本地目录索引。

    目录发现只记录元数据；正文直到某个 basic/channel 范围第一次被检索时才
    读取，避免单信道任务把整棵真实资料目录装入内存。
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._documents: list[CorpusDocument] | None = None
        self._chunks_by_scope: dict[tuple[str, str | None], list[DocumentChunk]] = {}

    def _discover(self) -> list[CorpusDocument]:
        """扫描受支持文件并缓存文件级元数据，不读取正文。"""

        if self._documents is not None:
            return list(self._documents)
        if not self.root.exists():
            self._documents = []
            return []

        documents: list[CorpusDocument] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            relative = path.relative_to(self.root)
            source_kind, channel = _infer_scope(relative)
            documents.append(
                CorpusDocument(
                    path=path,
                    relative_path=relative,
                    source_kind=source_kind,
                    channel=channel,
                )
            )
        self._documents = documents
        return list(documents)

    @staticmethod
    def _read_document(path: Path) -> str:
        """优先读取 UTF-8，并兼容常见的 GB18030 文本资料。"""

        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raw = path.read_bytes()
            try:
                return raw.decode("gb18030")
            except UnicodeDecodeError:
                return raw.decode("utf-8", errors="replace")

    def chunks(
        self,
        *,
        source_kind: str,
        channel: str | None = None,
    ) -> list[DocumentChunk]:
        """按 scope 惰性构建分块；调用方不能跨 scope 获得其他信道正文。"""

        scope = (source_kind, channel)
        if scope in self._chunks_by_scope:
            return list(self._chunks_by_scope[scope])

        chunks: list[DocumentChunk] = []
        for document in self._discover():
            if document.source_kind != source_kind:
                continue
            if channel is not None and document.channel != channel:
                continue
            text = self._read_document(document.path)
            if not text.strip():
                continue

            relative = document.relative_path
            title = _document_title(document.path, text)
            # ID 只依赖相对位置和章节序号，重复运行时可稳定追踪同一资料块。
            doc_id = hashlib.sha1(
                str(relative).replace("\\", "/").encode("utf-8")
            ).hexdigest()[:12]
            for index, (section, content) in enumerate(_split_markdown(text), 1):
                chunk_id = hashlib.sha1(
                    f"{doc_id}:{section}:{index}".encode("utf-8")
                ).hexdigest()[:16]
                chunks.append(
                    DocumentChunk(
                        chunk_id=chunk_id,
                        doc_id=doc_id,
                        source_kind=document.source_kind,
                        channel=document.channel,
                        title=title,
                        section=section,
                        relative_path=str(relative).replace("\\", "/"),
                        content=content,
                    )
                )
        self._chunks_by_scope[scope] = chunks
        return list(chunks)

    def scan(self) -> list[DocumentChunk]:
        """显式读取全部资料，主要用于诊断和小型语料测试。"""
        chunks = self.chunks(source_kind="basic")
        for channel in self.available_channels():
            chunks.extend(self.chunks(source_kind="channel", channel=channel))
        return chunks

    def available_channels(self) -> list[str]:
        """返回资料目录中实际存在且能够识别的信道。"""

        return sorted(
            {
                document.channel
                for document in self._discover()
                if document.source_kind == "channel" and document.channel
            }
        )


def _query_terms(values: Iterable[str]) -> list[str]:
    """从中英文查询中生成轻量关键词，并保持首次出现顺序。"""

    terms: list[str] = []
    for value in values:
        raw = value.strip().lower()
        if not raw:
            continue
        terms.append(raw)
        terms.extend(
            token.lower()
            for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{1,}|[\u4e00-\u9fff]{2,}", raw)
        )
    return list(dict.fromkeys(term for term in terms if len(term) >= 2))


def _score(chunk: DocumentChunk, terms: list[str]) -> float:
    """使用可解释的词项命中分数排序，不伪装成语义向量检索。"""

    haystack = " ".join(
        [
            chunk.title,
            chunk.section,
            chunk.relative_path,
            chunk.channel or "",
            chunk.content,
        ]
    ).lower()
    score = 0.0
    for term in terms:
        if term in haystack:
            score += 2.0 + min(len(term), 20) / 20
        else:
            words = [word for word in re.split(r"[\s,，。/;；:：]+", term) if word]
            score += 0.25 * sum(1 for word in words if len(word) >= 2 and word in haystack)
    return score


class ChannelKnowledgeRetriever:
    """在目录硬过滤之后执行轻量关键词检索的统一入口。"""

    def __init__(self, root: Path) -> None:
        self.catalog = CorpusCatalog(root)

    @property
    def root(self) -> Path:
        return self.catalog.root

    def available_channels(self) -> list[str]:
        return self.catalog.available_channels()

    def expand_allowed_channels(self, channels: list[str]) -> list[str]:
        """在实际存在的目录内扩展少量已声明依赖信道。"""

        available = set(self.available_channels())
        normalized = [
            channel
            for item in channels
            if (channel := normalize_channel(item)) and channel in available
        ]
        expanded = list(normalized)
        for channel in normalized:
            for dependency in CHANNEL_DEPENDENCIES.get(channel, []):
                if dependency in available and dependency not in expanded:
                    expanded.append(dependency)
        return expanded

    def _search(
        self,
        *,
        source_kind: str,
        queries: list[str],
        top_k: int,
        channel: str | None = None,
        dimensions: list[str] | None = None,
    ) -> list[EvidenceHit]:
        """在一个已确定的资料范围中检索并返回可追踪证据。"""

        top_k = max(1, min(int(top_k), 8))
        terms = _query_terms([*queries, *(dimensions or [])])
        candidates = self.catalog.chunks(
            source_kind=source_kind,
            channel=channel,
        )
        ranked = sorted(
            ((chunk, _score(chunk, terms)) for chunk in candidates),
            key=lambda item: (-item[1], item[0].relative_path, item[0].chunk_id),
        )
        # 即使关键词没有命中，也返回该受限目录的前几个 chunk，交给充分性节点判断。
        return [
            EvidenceHit(
                chunk_id=chunk.chunk_id,
                doc_id=chunk.doc_id,
                source_kind=chunk.source_kind,  # type: ignore[arg-type]
                channel=chunk.channel,
                title=chunk.title,
                section=chunk.section,
                relative_path=chunk.relative_path,
                content=chunk.content,
                score=round(score, 3),
            )
            for chunk, score in ranked[:top_k]
        ]

    def search_basic(
        self,
        *,
        queries: list[str],
        dimensions: list[str],
        top_k: int = 5,
    ) -> list[EvidenceHit]:
        """检索跨信道通用的基础测试点资料。"""

        return self._search(
            source_kind="basic",
            queries=queries,
            dimensions=dimensions,
            top_k=top_k,
        )

    def search_channel(
        self,
        *,
        channel: str,
        queries: list[str],
        top_k: int = 5,
    ) -> list[EvidenceHit]:
        """只检索指定标准信道的资料；未知信道不做宽泛回退。"""

        canonical = normalize_channel(channel)
        if not canonical:
            return []
        return self._search(
            source_kind="channel",
            channel=canonical,
            queries=queries,
            top_k=top_k,
        )


@lru_cache(maxsize=4)
def _retriever_for_root(root_text: str) -> ChannelKnowledgeRetriever:
    """按资料根目录复用索引，同时允许测试切换不同临时语料。"""

    return ChannelKnowledgeRetriever(Path(root_text))


def get_knowledge_retriever() -> ChannelKnowledgeRetriever:
    """根据当前配置获得知识检索器；未配置时立即显式失败。"""

    configured = get_settings().test_analysis_knowledge_root
    if configured is None:
        raise RuntimeError("未配置测试分析资料库根目录")
    return _retriever_for_root(str(configured.resolve()))
