"""Explicit environment loading. Values are never logged, evaluated or expanded."""

from collections.abc import Mapping
from dataclasses import replace
import os
from pathlib import Path
import re

from .config import Principal, Settings
from .domain import ValidationError


def read_environment(path: str | Path | None = None) -> dict[str, str]:
    values: dict[str, str] = {}
    if path is not None:
        try:
            lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError):
            raise ValidationError("cannot read the selected environment file") from None
        for number, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            name, value = name.strip(), value.strip()
            if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
                raise ValidationError(f"invalid or duplicate environment variable at line {number}")
            if value[:1] in (chr(34), chr(39)):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ValidationError(f"invalid quoted environment value at line {number}")
                value = value[1:-1]
            if "\x00" in value:
                raise ValidationError(f"invalid environment value at line {number}")
            values[name] = value
    # Process-level secrets injected by Docker take precedence over local files.
    return values | dict(os.environ)


def _enabled(values: Mapping[str, str], name: str) -> bool:
    value = values.get(name, "false").lower()
    if value not in ("true", "false"):
        raise ValidationError(f"{name} must be true or false")
    return value == "true"


def _fraction(values: Mapping[str, str], name: str) -> float | None:
    """An optional number between 0 and 1; an empty value counts as unset."""
    text = values.get(name, "")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise ValidationError(f"{name} must be a number between 0 and 1") from None
    if not 0 <= value <= 1:
        raise ValidationError(f"{name} must be a number between 0 and 1")
    return value


def apply_environment(settings: Settings, values: Mapping[str, str]) -> Settings:
    updates = {}
    if values.get("RAG_RELEVANCE_GATE"):
        gate = values["RAG_RELEVANCE_GATE"].lower()
        if gate not in ("true", "false"):
            raise ValidationError("RAG_RELEVANCE_GATE must be true or false")
        updates["relevance_gate"] = gate == "true"
    for name, field in (("RAG_RELEVANCE_LEXICAL_FLOOR", "relevance_lexical_floor"),
                        ("RAG_RELEVANCE_RERANK_FLOOR", "relevance_rerank_floor")):
        value = _fraction(values, name)
        if value is not None:
            updates[field] = value
    remote_enabled = _enabled(values, "RAG_ENABLE_REMOTE_STORAGE")
    storage_names = {name for name in values if name.startswith(("RAG_STORE_", "RAG_TRACE_", "RAG_OBJECT_", "RAG_ASSET_", "RAG_POSTGRES_", "RAG_S3_", "RAG_MILVUS_"))}
    if storage_names:
        from .infrastructure import load_infrastructure, remote_requested
        storage, selectors = load_infrastructure(values, settings.plugins)
        if remote_requested(storage, selectors):
            if not remote_enabled:
                raise ValidationError("remote storage requires RAG_ENABLE_REMOTE_STORAGE=true")
            updates["allow_network"] = True
        updates.update(infrastructure=storage, plugins=settings.plugins | selectors)
    cloud_enabled = _enabled(values, "RAG_ENABLE_CLOUD")
    optional = {name: _enabled(values, name) for name in (
        "RAG_ENABLE_QUERY_PREPARATION", "RAG_ENABLE_QUALITY_CHECKS",
        "RAG_ENABLE_MULTI_QUERY", "RAG_ENABLE_HYDE")}
    deepseek_answers = _enabled(values, "RAG_ENABLE_DEEPSEEK_ANSWERS")
    if any(optional.values()) and not (cloud_enabled or deepseek_answers):
        raise ValidationError("query preparation and quality checks require RAG_ENABLE_CLOUD or RAG_ENABLE_DEEPSEEK_ANSWERS")
    if values.get("RAG_DATA_DIR"):
        updates["data_dir"] = Path(values["RAG_DATA_DIR"])
    if cloud_enabled:
        from .cloud import load_cloud_settings
        selected = updates.get("plugins", settings.plugins) | {"dense": "cloud", "reranker": "cloud", "generator": "cloud"}
        if optional["RAG_ENABLE_QUERY_PREPARATION"]:
            selected.update(planner="cloud", rewrite="cloud")
        if optional["RAG_ENABLE_QUALITY_CHECKS"]:
            selected.update(judge="cloud", verifier="cloud", gap="cloud")
        if optional["RAG_ENABLE_MULTI_QUERY"]:
            selected["multi_query"] = "cloud"
        if optional["RAG_ENABLE_HYDE"]:
            selected["hyde"] = "cloud"
        updates.update(allow_models=True, allow_network=True, cloud=load_cloud_settings(values),
                       plugins=selected,
                       timeout_seconds=120.0, stage_timeout_seconds=90.0)
    if _enabled(values, "RAG_ENABLE_DEEPSEEK_INTENT"):
        from .intent import DeepSeekSettings
        intent = DeepSeekSettings(values.get("DEEPSEEK_API_KEY", ""), values.get("RAG_DEEPSEEK_INTENT_MODEL") or "deepseek-flash")
        updates.update(intent=intent, allow_models=True, allow_network=True,
                       plugins=updates.get("plugins", settings.plugins) | {"decision": "deepseek"})
    if deepseek_answers:
        from .intent import AnswerSettings
        answers = AnswerSettings(values.get("DEEPSEEK_API_KEY", ""), values.get("RAG_DEEPSEEK_ANSWER_MODEL") or "deepseek-flash")
        selected = updates.get("plugins", settings.plugins) | dict.fromkeys(("generator", "judge", "verifier", "gap"), "deepseek")
        if optional["RAG_ENABLE_QUERY_PREPARATION"]:
            selected.update(planner="deepseek", rewrite="deepseek")
        if optional["RAG_ENABLE_MULTI_QUERY"]:
            selected["multi_query"] = "deepseek"
        if optional["RAG_ENABLE_HYDE"]:
            selected["hyde"] = "deepseek"
        updates.update(deepseek_answers=answers, allow_models=True, allow_network=True, plugins=selected,
                       timeout_seconds=120.0, stage_timeout_seconds=90.0)
    if values.get("RAG_HTTP_API_TOKEN"):
        principal = Principal(values.get("RAG_HTTP_TENANT", "demo"), values.get("RAG_HTTP_SUBJECT", "admin"), admin=True)
        updates["api_tokens"] = settings.api_tokens | {values["RAG_HTTP_API_TOKEN"]: principal}
    if values.get("RAG_HTTP_TENANT"):
        updates["browser_tenant"] = values["RAG_HTTP_TENANT"]
    if values.get("RAG_ADMIN_PASSWORD"):
        updates["admin_password"] = values["RAG_ADMIN_PASSWORD"]
    return replace(settings, **updates)


def pdf_settings(values: Mapping[str, str]):
    from .pdf import PdfSettings
    return PdfSettings(api_key=values.get("MINERU_API_KEY", ""),
                       base_url=values.get("MINERU_BASE_URL", "https://mineru.net/api/v4"))
