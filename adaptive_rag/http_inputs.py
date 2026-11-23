"""Strict request bodies shared by the HTTP adapters."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DocumentInput(Input):
    document_id: str
    version: str
    content: str = Field(min_length=1, max_length=2_000_000)
    media_type: str = "text/markdown"
    title: str = "Untitled"
    scope: str = "store"
    sku_ids: list[str] = Field(default_factory=list, max_length=1000)
    allowed_subjects: list[str] = Field(default_factory=list, max_length=1000)
    data_domains: list[str] = Field(default_factory=list, max_length=6)
    language: str = "zh"


class PublishInput(Input):
    job_id: str
    expected_generation: str | None = None


class QueryInput(Input):
    persist_conversation: bool = False
    conversation_id: str | None = Field(default=None, min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=4000)
    sku: str | None = None
    generation: str | None = None
    top_k: int = Field(default=5, ge=1, le=20)
    history: list[str] = Field(default_factory=list, max_length=6)
    skip_query_steps: list[Literal["rewrite", "multi_query", "hyde"]] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def server_history_only(self):
        if self.history and (self.persist_conversation or self.conversation_id):
            raise ValueError("persistent conversations use server-managed history")
        return self


class DeleteInput(Input):
    document_id: str
    expected_generation: str | None = None


class RollbackInput(Input):
    generation: str
    expected_generation: str | None = None


class AccessInput(Input):
    document_id: str
    allowed_subjects: list[str] | None = Field(default=None, max_length=1000)
    revoked: bool | None = None
    data_domains: list[str] | None = Field(default=None, max_length=6)


class LoginInput(Input):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class AccountInput(Input):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    data_domains: list[str] = Field(default_factory=lambda: ["public"], max_length=6)


class AccountDomainsInput(Input):
    data_domains: list[str] = Field(max_length=6)


class AssetInput(Input):
    asset_id: str = Field(min_length=1, max_length=128)
    content_base64: str = Field(min_length=1, max_length=14_000_000)
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=16000)
    ocr_text: str = Field(default="", max_length=32000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    allowed_subjects: list[str] = Field(default_factory=list, max_length=1000)
    data_domains: list[str] = Field(default_factory=list, max_length=6)
    source_document_id: str | None = None
    source_page: int | None = Field(default=None, ge=1)


class AssetUpdate(Input):
    expected_revision: str
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=16000)
    ocr_text: str = Field(default="", max_length=32000)
    tags: list[str] = Field(default_factory=list, max_length=50)
    allowed_subjects: list[str] = Field(default_factory=list, max_length=1000)
    data_domains: list[str] | None = Field(default=None, max_length=6)


class AssetSearch(Input):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)


class AssetIndex(Input):
    limit: int = Field(default=10, ge=1, le=100)
