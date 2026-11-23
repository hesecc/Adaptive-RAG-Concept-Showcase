"""Validated configuration; external capabilities require explicit opt-in."""

from dataclasses import dataclass, field
import ipaddress
import math
from pathlib import Path
import tomllib
from typing import Any

from .domain import AccessContext, ValidationError, identifier


DEFAULT_PLUGINS = {
    "parser": "structured", "cleaner": "normalize", "chunker": "parent_child",
    "store": "sqlite", "objects": "file", "trace": "sqlite",
    "decision": "rules", "planner": "rules", "rewrite": "disabled",
    "multi_query": "disabled", "hyde": "disabled", "dense": "disabled",
    "lexical": "bm25", "fusion": "rrf", "reranker": "passthrough",
    "judge": "lexical_coverage", "gap": "disabled", "generator": "disabled",
    "verifier": "citations",
}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    subject_id: str = "local"
    groups: tuple[str, ...] = ()
    admin: bool = False
    data_domains: tuple[str, ...] | None = None

    def __post_init__(self):
        access = AccessContext(self.tenant_id, self.subject_id, self.groups, self.data_domains)
        object.__setattr__(self, "data_domains", access.data_domains)
        if type(self.admin) is not bool:
            raise ValidationError("principal.admin must be boolean")

    @property
    def access(self) -> AccessContext:
        return AccessContext(self.tenant_id, self.subject_id, self.groups, self.data_domains)


def _integer(value: int, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be an integer between {minimum} and {maximum}")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(".rag-data")
    parent_chars: int = 1600
    child_chars: int = 320
    overlap_chars: int = 40
    candidate_limit: int = 50
    max_subqueries: int = 3
    max_lanes: int = 12
    max_retries: int = 1
    evidence_chars: int = 12000
    relevance_gate: bool = True
    relevance_lexical_floor: float = 0.3
    relevance_rerank_floor: float = 0.05
    timeout_seconds: float = 12.0
    stage_timeout_seconds: float = 5.0
    host: str = "127.0.0.1"
    port: int = 8000
    plugins: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PLUGINS))
    api_tokens: dict[str, Principal] = field(default_factory=dict, repr=False)
    browser_tenant: str | None = None
    admin_password: str | None = field(default=None, repr=False)
    allow_models: bool = False
    allow_network: bool = False
    cloud: Any = field(default=None, repr=False)
    infrastructure: Any = field(default=None, repr=False)
    intent: Any = field(default=None, repr=False)
    deepseek_answers: Any = field(default=None, repr=False)

    def __post_init__(self):
        object.__setattr__(self, "data_dir", Path(self.data_dir).resolve())
        for name in ("allow_models", "allow_network", "relevance_gate"):
            if type(getattr(self, name)) is not bool:
                raise ValidationError(f"{name} must be boolean")
        for name in ("relevance_lexical_floor", "relevance_rerank_floor"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValidationError(f"{name} must be a finite number between 0 and 1")
        if self.allow_models and not self.allow_network:
            raise ValidationError("cloud models require allow_network=true")
        limits = {
            "parent_chars": (64, 16000), "child_chars": (16, 4000),
            "overlap_chars": (0, 1000), "candidate_limit": (1, 100),
            "max_subqueries": (1, 3), "max_lanes": (1, 12),
            "max_retries": (0, 1), "evidence_chars": (64, 40000), "port": (1, 65535),
        }
        for name, (minimum, maximum) in limits.items():
            _integer(getattr(self, name), name, minimum, maximum)
        if self.child_chars > self.parent_chars or self.overlap_chars >= self.child_chars:
            raise ValidationError("require overlap_chars < child_chars <= parent_chars")
        for name in ("timeout_seconds", "stage_timeout_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0.01 <= value <= 120:
                raise ValidationError(f"{name} must be finite and between 0.01 and 120")
        try:
            loopback = isinstance(self.host, str) and (self.host == "localhost" or ipaddress.ip_address(self.host).is_loopback)
        except (ValueError, TypeError):
            loopback = False
        if not loopback:
            raise ValidationError("foundation HTTP host must be loopback; configure a production gateway later")
        if not isinstance(self.plugins, dict) or set(self.plugins) - set(DEFAULT_PLUGINS):
            raise ValidationError("plugins contains unknown role")
        merged = DEFAULT_PLUGINS | self.plugins
        if any(not isinstance(v, str) or not v for v in merged.values()):
            raise ValidationError("plugin selectors must be nonempty strings")
        object.__setattr__(self, "plugins", merged)
        if not isinstance(self.api_tokens, dict):
            raise ValidationError("api_tokens must be a token-to-Principal mapping")
        for token, principal in self.api_tokens.items():
            if not isinstance(token, str) or len(token) < 24 or any(c.isspace() for c in token):
                raise ValidationError("API tokens must have at least 24 non-whitespace characters")
            if not isinstance(principal, Principal):
                raise ValidationError("api_tokens values must be Principal instances")
        if self.browser_tenant is not None:
            identifier(self.browser_tenant, "browser_tenant")


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        return Settings()
    try:
        source = Path(path).resolve()
        with source.open("rb") as handle:
            values: dict[str, Any] = tomllib.load(handle)
        known = set(Settings.__dataclass_fields__)
        known.remove("cloud")  # Credentials are injected, never serialized in TOML.
        known.remove("infrastructure")
        known.remove("intent")
        known.remove("deepseek_answers")
        known.remove("admin_password")
        if set(values) - known:
            raise ValidationError(f"unknown settings: {', '.join(sorted(set(values) - known))}")
        if "data_dir" in values:
            values["data_dir"] = source.parent / values["data_dir"]
        principals = values.get("api_tokens", {})
        if not isinstance(principals, dict):
            raise ValidationError("api_tokens must be a table")
        parsed = {}
        for token, entry in principals.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("groups", []), list):
                raise ValidationError("API principal must be a table with an optional groups array")
            principal_values = entry | {"groups": tuple(entry.get("groups", ()))}
            if "data_domains" in entry:
                if not isinstance(entry["data_domains"], list):
                    raise ValidationError("API principal data_domains must be an array")
                principal_values["data_domains"] = tuple(entry["data_domains"])
            parsed[token] = Principal(**principal_values)
        values["api_tokens"] = parsed
        return Settings(**values)
    except (OSError, TypeError, ValueError) as exc:
        raise ValidationError("invalid configuration file; check paths, keys and value types") from exc
