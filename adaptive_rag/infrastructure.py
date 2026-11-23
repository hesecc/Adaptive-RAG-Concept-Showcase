"""Explicit storage selection, independent of paid model capabilities."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit

from .domain import CapabilityError, ValidationError


@dataclass(frozen=True)
class InfrastructureSettings:
    postgres_dsn: str = field(default="", repr=False)
    asset_store: str = "sqlite"
    vector_backend: str = "sqlite"
    s3_endpoint: str = ""
    s3_access_key: str = field(default="", repr=False)
    s3_secret_key: str = field(default="", repr=False)
    s3_bucket: str = "an-rag-assets"
    s3_region: str = "us-east-1"
    s3_prefix: str = "objects"
    milvus_endpoint: str = ""
    milvus_token: str = field(default="", repr=False)
    milvus_collection: str = "an_rag_asset_text_v1"
    embedding_dimensions: int | None = None

    def __post_init__(self):
        if self.asset_store not in {"sqlite", "postgres"} or self.vector_backend not in {"sqlite", "milvus"}:
            raise ValidationError("unknown asset storage backend")
        if self.asset_store == "postgres" and not self.postgres_dsn:
            raise ValidationError("PostgreSQL asset storage requires RAG_POSTGRES_DSN")
        if self.vector_backend == "milvus" and not self.milvus_endpoint:
            raise ValidationError("Milvus requires RAG_MILVUS_ENDPOINT")
        for value, name in ((self.s3_endpoint, "S3"), (self.milvus_endpoint, "Milvus")):
            if value:
                try:
                    url = urlsplit(value)
                    valid = url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password
                    valid = valid and not url.query and not url.fragment and url.path in {"", "/"}
                    _ = url.port
                except ValueError:
                    valid = False
                if not valid:
                    raise ValidationError(f"{name} endpoint must be an explicit HTTP(S) origin without credentials")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", self.milvus_collection):
            raise ValidationError("invalid Milvus collection name")
        if self.embedding_dimensions is not None and (type(self.embedding_dimensions) is not int or not 1 <= self.embedding_dimensions <= 16384):
            raise ValidationError("embedding dimensions must be between 1 and 16384")


def load_infrastructure(values: Mapping[str, str], defaults: Mapping[str, str] | None = None) -> tuple[InfrastructureSettings, dict[str, str]]:
    defaults = defaults or {}
    selectors = {
        "store": values.get("RAG_STORE_BACKEND", defaults.get("store", "sqlite")),
        "trace": values.get("RAG_TRACE_BACKEND", defaults.get("trace", "sqlite")),
        "objects": values.get("RAG_OBJECT_BACKEND", defaults.get("objects", "file")),
    }
    for role, variable, allowed in (("store", "RAG_STORE_BACKEND", {"sqlite", "postgres"}),
                                    ("trace", "RAG_TRACE_BACKEND", {"sqlite", "postgres"}),
                                    ("objects", "RAG_OBJECT_BACKEND", {"file", "s3"})):
        if variable in values and selectors[role] not in allowed:
            raise ValidationError("unknown remote storage selector")
    raw_dimension = values.get("RAG_ASSET_EMBEDDING_DIMENSIONS", "")
    try:
        dimension = int(raw_dimension) if raw_dimension else None
    except ValueError:
        raise ValidationError("invalid RAG_ASSET_EMBEDDING_DIMENSIONS") from None
    config = InfrastructureSettings(
        postgres_dsn=values.get("RAG_POSTGRES_DSN", ""),
        asset_store=values.get("RAG_ASSET_STORE_BACKEND", "sqlite"),
        vector_backend=values.get("RAG_ASSET_VECTOR_BACKEND", "sqlite"),
        s3_endpoint=values.get("RAG_S3_ENDPOINT", ""),
        s3_access_key=values.get("RAG_S3_ACCESS_KEY", ""),
        s3_secret_key=values.get("RAG_S3_SECRET_KEY", ""),
        s3_bucket=values.get("RAG_S3_BUCKET", "an-rag-assets"),
        s3_region=values.get("RAG_S3_REGION", "us-east-1"),
        s3_prefix=values.get("RAG_S3_PREFIX", "objects"),
        milvus_endpoint=values.get("RAG_MILVUS_ENDPOINT", ""),
        milvus_token=values.get("RAG_MILVUS_TOKEN", ""),
        milvus_collection=values.get("RAG_MILVUS_COLLECTION", "an_rag_asset_text_v1"),
        embedding_dimensions=dimension,
    )
    if "postgres" in selectors.values() and not config.postgres_dsn:
        raise ValidationError("PostgreSQL requires RAG_POSTGRES_DSN")
    if selectors["objects"] == "s3" and not all((config.s3_endpoint, config.s3_access_key, config.s3_secret_key, config.s3_bucket)):
        raise ValidationError("S3 requires endpoint, access key, secret key and bucket")
    return config, selectors


def remote_requested(config: InfrastructureSettings, selectors: Mapping[str, str]) -> bool:
    return ("postgres" in selectors.values() or selectors.get("objects") == "s3"
            or config.asset_store == "postgres" or config.vector_backend == "milvus")


def infrastructure(settings) -> InfrastructureSettings:
    value = settings.infrastructure
    if value is None:
        return InfrastructureSettings()
    if not isinstance(value, InfrastructureSettings):
        raise ValidationError("invalid infrastructure configuration")
    return value


def build_assets(settings, objects):
    from .assets import AssetService
    from .vector_store import SQLiteVectorIndex
    config = infrastructure(settings)
    if (config.asset_store == "postgres" or config.vector_backend == "milvus") and not settings.allow_network:
        raise CapabilityError("remote asset storage requires explicit network opt-in")
    if config.vector_backend == "milvus":
        from .remote import MilvusVectorIndex
        index = MilvusVectorIndex(config.milvus_endpoint, config.milvus_collection,
                                  token=config.milvus_token, dimension=config.embedding_dimensions)
    else:
        index = SQLiteVectorIndex(settings.data_dir)
    embedder = None
    profile = "lexical-only-v1"
    if settings.allow_models and settings.cloud is not None:
        from .cloud import CloudEmbedder, CloudTransport
        embedder = CloudEmbedder(CloudTransport(settings.cloud))
        profile = sha256(json.dumps(["asset-text-v1", settings.cloud.base_url,
                                    settings.cloud.embedding_model, settings.cloud.embedding_dimensions],
                                   separators=(",", ":")).encode()).hexdigest()
    return AssetService(settings.data_dir, objects, index, embedder=embedder,
                        embedding_profile=profile,
                        postgres_dsn=config.postgres_dsn if config.asset_store == "postgres" else None)
