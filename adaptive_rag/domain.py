"""Immutable data contracts shared by adapters and the pipeline.

Tenant/subject fields are trusted caller context, never model outputs. HTTP
adapters must derive them from authentication rather than request bodies.
"""

from asyncio import CancelledError
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import math
import re
from typing import Any


class RagError(Exception):
    code = "rag_error"
    status_code = 400


class ValidationError(RagError):
    code = "validation_error"


class NotFoundError(RagError):
    code = "not_found"
    status_code = 404


class ConflictError(RagError):
    code = "conflict"
    status_code = 409


class ConversationFullError(ConflictError):
    code = "conversation_full"

    def __init__(self):
        super().__init__("这段对话已达到轮数上限，请新建对话。")


class ForbiddenError(RagError):
    code = "forbidden"
    status_code = 403


class CapabilityError(RagError):
    code = "capability_unavailable"
    status_code = 503

    def __init__(self, message, *, diagnostic_code="capability_unavailable"):
        super().__init__(message)
        self.diagnostic_code = diagnostic_code


_SAFE_ERRORS = {
    "conversation_full": "这段对话已达到轮数上限，请新建对话。",
    "capability_unavailable": "当前能力调用失败；本次未生成核验完成的回答。",
    "cloud_network_error": "与云模型的连接中断，本次调用未取得完整返回。可检查网络后手动重试。",
    "cloud_timeout": "等待云模型返回超时；为避免重复计费，本次没有自动重试。",
    "cloud_http_error": "云平台返回 HTTP 错误，请查看模型调用中的状态码并检查平台配置或服务状态。",
    "query_timeout": "本次查询或步骤超过时间限制，已停止等待；已完成的步骤仍可查看。",
    "query_cancelled": "查询已取消，此步骤没有完成；已完成的步骤仍可查看。",
}


def safe_error(exc: BaseException) -> dict[str, str]:
    """Return fixed diagnostics, never provider or plugin exception text."""
    code = (exc.diagnostic_code if isinstance(exc, CapabilityError) else
            exc.code if isinstance(exc, ConversationFullError) else
            "query_timeout" if isinstance(exc, TimeoutError) else
            "query_cancelled" if isinstance(exc, CancelledError) else "capability_unavailable")
    if not isinstance(code, str) or code not in _SAFE_ERRORS:
        code = "capability_unavailable"
    return {"code": code, "message": _SAFE_ERRORS[code]}


def identifier(value: str, name: str = "id") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[\w.:-]{1,128}", value, re.UNICODE):
        raise ValidationError(f"{name} must contain 1-128 letters, digits, _, ., : or -")
    return value


def nonempty(value: str, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValidationError(f"{name} must contain 1-{limit} characters without NUL")
    return value


def stable_id(*values: Any) -> str:
    data = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(data.encode("utf-8")).hexdigest()


def to_dict(value: Any) -> Any:
    """JSON-compatible public representation; no credential-bearing objects."""
    return asdict(value)


@dataclass(frozen=True)
class AccessContext:
    tenant_id: str
    subject_id: str = "local"
    groups: tuple[str, ...] = ()
    data_domains: tuple[str, ...] | None = None

    def __post_init__(self):
        identifier(self.tenant_id, "tenant_id")
        identifier(self.subject_id, "subject_id")
        if not isinstance(self.groups, tuple) or len(self.groups) > 1000:
            raise ValidationError("groups must be a tuple of at most 1000 subjects")
        for group in self.groups:
            identifier(group, "group")
        if self.data_domains is not None:
            from .data_access import normalize_data_domains
            object.__setattr__(self, "data_domains", normalize_data_domains(self.data_domains))

    @property
    def subjects(self) -> frozenset[str]:
        return frozenset((self.subject_id, *self.groups))


@dataclass(frozen=True)
class IngestRequest:
    tenant_id: str
    document_id: str
    version: str
    content: str
    media_type: str = "text/markdown"
    title: str = "Untitled"
    scope: str = "store"
    sku_ids: tuple[str, ...] = ()
    allowed_subjects: tuple[str, ...] = ()
    language: str = "zh"
    source_object_key: str | None = None
    source_parser: str | None = None
    data_domains: tuple[str, ...] = ()

    def __post_init__(self):
        for name in ("tenant_id", "document_id", "version", "language"):
            identifier(getattr(self, name), name)
        nonempty(self.content, "content", 2_000_000)
        nonempty(self.title, "title", 300)
        if self.source_object_key is not None and not re.fullmatch(r"[a-f0-9]{64}", self.source_object_key):
            raise ValidationError("source_object_key must be a SHA256 object key")
        if self.source_parser is not None:
            nonempty(self.source_parser, "source_parser", 100)
        if self.media_type not in ("text/plain", "text/markdown", "text/html"):
            raise ValidationError("media_type must be text/plain, text/markdown or text/html")
        if self.scope not in ("store", "sku"):
            raise ValidationError("scope must be store or sku")
        for name in ("sku_ids", "allowed_subjects"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or len(values) > 1000:
                raise ValidationError(f"{name} must be a tuple of at most 1000 identifiers")
        if (self.scope == "sku") != bool(self.sku_ids):
            raise ValidationError("sku scope requires sku_ids; store scope must not have sku_ids")
        for sku in self.sku_ids:
            identifier(sku, "sku_id")
        for subject in self.allowed_subjects:
            identifier(subject, "allowed_subject")
        from .data_access import normalize_data_domains
        object.__setattr__(self, "data_domains", normalize_data_domains(self.data_domains))


@dataclass(frozen=True)
class Section:
    heading_path: tuple[str, ...]
    text: str
    locator: str


@dataclass(frozen=True)
class ParentChunk:
    tenant_id: str
    document_id: str
    version: str
    revision_id: str
    parent_id: str
    heading_path: tuple[str, ...]
    text: str
    locator: str
    content_hash: str


@dataclass(frozen=True)
class ChildChunk:
    tenant_id: str
    document_id: str
    version: str
    revision_id: str
    chunk_id: str
    parent_id: str
    text: str
    retrieval_text: str
    locator: str
    scope: str
    sku_ids: tuple[str, ...]
    language: str

    @property
    def content_hash(self) -> str:
        return sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreparedDocument:
    request: IngestRequest
    revision_id: str
    processing_fingerprint: str
    content_hash: str
    raw_key: str
    parents: tuple[ParentChunk, ...]
    children: tuple[ChildChunk, ...]


@dataclass(frozen=True)
class IngestResult:
    job_id: str
    revision_id: str
    document_id: str
    version: str
    state: str
    parent_count: int
    child_count: int
    duplicate: bool = False


@dataclass(frozen=True)
class PdfIngestResult(IngestResult):
    # Counts of the figures the PDF parser handed over: referenced, stored, skipped.
    figures: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Snapshot:
    tenant_id: str
    generation: str
    revisions: tuple[tuple[str, str], ...]
    created_at: str


OPTIONAL_QUERY_STEPS = ("rewrite", "multi_query", "hyde")


@dataclass(frozen=True)
class QueryRequest:
    access: AccessContext
    question: str
    sku: str | None = None
    generation: str | None = None
    top_k: int = 5
    history: tuple[str, ...] = ()
    persist_conversation: bool = False
    conversation_id: str | None = None
    history_trace_ids: tuple[str, ...] = ()
    skip_query_steps: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.access, AccessContext):
            raise ValidationError("access must be a trusted AccessContext")
        nonempty(self.question, "question", 4000)
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int) or not 1 <= self.top_k <= 20:
            raise ValidationError("top_k must be an integer between 1 and 20")
        if (not isinstance(self.skip_query_steps, tuple) or len(self.skip_query_steps) > len(OPTIONAL_QUERY_STEPS)
                or any(step not in OPTIONAL_QUERY_STEPS for step in self.skip_query_steps)
                or len(set(self.skip_query_steps)) != len(self.skip_query_steps)):
            raise ValidationError("only optional query preprocessing steps may be skipped")
        if self.sku is not None:
            identifier(self.sku, "sku")
        if self.generation is not None:
            identifier(self.generation, "generation")
        if self.conversation_id is not None:
            identifier(self.conversation_id, "conversation_id")
        if not isinstance(self.history, tuple) or len(self.history) > 6:
            raise ValidationError("history must contain at most 6 turns")
        for turn in self.history:
            nonempty(turn, "history turn", 4000)


@dataclass(frozen=True)
class SubQuery:
    query_id: str
    text: str


@dataclass(frozen=True)
class Plan:
    queries: tuple[SubQuery, ...]
    sku: str | None = None
    action: str = "retrieve"
    message: str = ""
    original_question: str = ""


@dataclass(frozen=True)
class Hit:
    query_id: str
    chunk: ChildChunk
    lane: str
    score: float
    rank: int

    def __post_init__(self):
        if not math.isfinite(self.score) or self.rank < 1:
            raise ValidationError("hit score must be finite and rank positive")


@dataclass(frozen=True)
class EvidenceImage:
    """A figure that sits inside an evidence passage; the bytes live in the object store."""
    key: str
    caption: str = ""
    page: int | None = None


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    query_ids: tuple[str, ...]
    tenant_id: str
    generation: str
    document_id: str
    version: str
    revision_id: str
    parent_id: str
    text: str
    locator: str
    content_hash: str
    source_hash: str
    truncated: bool = False
    heading_path: tuple[str, ...] = ()
    images: tuple[EvidenceImage, ...] = ()


@dataclass(frozen=True)
class Coverage:
    covered_query_ids: tuple[str, ...]
    missing_query_ids: tuple[str, ...]
    basis: str = "lexical_presence_only"
    missing_queries: tuple[SubQuery, ...] = ()


@dataclass(frozen=True)
class Claim:
    text: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Draft:
    answer: str | None = None
    claims: tuple[Claim, ...] = ()
    mode: str = "disabled"


@dataclass(frozen=True)
class Verification:
    valid: bool
    errors: tuple[str, ...] = ()
    support_checked: bool = False
    supported_claim_indexes: tuple[int, ...] = ()
    missing_queries: tuple[SubQuery, ...] = ()
    completeness_checked: bool = False


@dataclass(frozen=True)
class QueryResult:
    trace_id: str
    status: str
    message: str
    generation: str | None
    evidence: tuple[Evidence, ...] = ()
    answer: str | None = None
    claims: tuple[Claim, ...] = ()
    missing_query_ids: tuple[str, ...] = ()
    capabilities: dict[str, Any] = field(default_factory=dict)
