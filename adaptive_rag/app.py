"""One application facade shared by trusted local CLI and authenticated HTTP."""

from dataclasses import asdict
import re

from .config import Settings
from .conversations import ConversationHistory
from .domain import AccessContext, NotFoundError, PdfIngestResult, ValidationError, identifier, stable_id
from .figures import ParsedFigureRequest, attach_figures, figure_binding, read_bound_figure
from .ingestion import IngestionService
from .pipeline import QueryPipeline, digest, make_evidence
from .plugins import PluginRegistry, default_registry


class RagApplication:
    def __init__(self, settings: Settings, registry: PluginRegistry | None = None):
        self.settings = settings
        self.registry = registry or default_registry()
        self.descriptions = self.registry.describe(settings)
        # Validate every selection before invoking any factory.
        for role, description in self.descriptions.items():
            if (description["requires_model"] and not settings.allow_models) or (description["uses_network"] and not settings.allow_network):
                from .domain import CapabilityError
                raise CapabilityError(f"model/network plugin forbidden without explicit opt-in: {role}")
        self.components = {role: self.registry.build(role, settings) for role in settings.plugins}
        self.store = self.components["store"]
        self.objects = self.components["objects"]
        self.trace_sink = self.components["trace"]
        self.conversations = ConversationHistory(self)
        fingerprint = stable_id("foundation-v1", {role: self.descriptions[role] for role in ("parser", "cleaner", "chunker")})
        self.ingestion = IngestionService(self.store, self.objects, self.components["parser"],
                                          self.components["cleaner"], self.components["chunker"], fingerprint)
        self.pipeline = QueryPipeline(settings, self.components, self.descriptions)
        self._assets = None

    @property
    def assets(self):
        if self._assets is None:
            from .infrastructure import build_assets
            self._assets = build_assets(self.settings, self.objects)
        return self._assets

    def ingest(self, request, *, principal=None):
        return self.ingestion.ingest(request, **({"principal": principal} if principal is not None else {}))

    def ingest_pdf(self, path, *, backend="local", pdf_settings=None, pdf_checkpoint=None, principal=None, **metadata):
        from pathlib import Path
        from hashlib import sha256
        from .mineru_formats import ARCHIVE_INPUT_LIMIT, read_source
        from .pdf import parse_pdf
        source = Path(path)
        parsed = parse_pdf(source, backend, pdf_settings,
                           **({"checkpoint_path": pdf_checkpoint} if pdf_checkpoint is not None else {}))
        raw = read_source(source, ARCHIVE_INPUT_LIMIT, "PDF")
        if sha256(raw).hexdigest() != parsed.metadata.get("source_sha256"):
            raise ValidationError("PDF source changed during parsing; retry with a stable source")
        return self._stage_parsed(parsed, raw, principal, metadata)

    def ingest_mineru(self, path, *, pdf_settings, principal=None, before_stage=None, **metadata):
        """Upload once. A synchronous authorization callback must return None or raise."""
        from .pdf import MinerUPdfParser
        if before_stage is not None and not callable(before_stage):
            raise ValidationError("before_stage must be a callable")
        parsed, raw = MinerUPdfParser(pdf_settings).parse_original(path)
        if before_stage is not None:
            authorization = before_stage()
            if authorization is not None:
                from inspect import iscoroutine
                if iscoroutine(authorization):
                    authorization.close()
                raise ValidationError("before_stage must return None synchronously or raise")
        return self._stage_parsed(parsed, raw, principal, metadata)

    def _stage_parsed(self, parsed, raw, principal, metadata):
        from .domain import IngestRequest
        raw_key = self.objects.put(raw)
        # Figures are stored before the document, so a stored document never points at a missing picture.
        markdown, figures, validated_keys = attach_figures(parsed.markdown, parsed.figures, self.objects.put)
        request = IngestRequest(content=markdown, media_type="text/markdown",
            source_object_key=raw_key, source_parser=parsed.backend, **metadata)
        if validated_keys:
            request = ParsedFigureRequest(**asdict(request), parser_figure_keys=validated_keys)
        result = self.ingest(request, **({"principal": principal} if principal is not None else {}))
        # A retry recreates missing entries, even for a duplicate job.
        for key in validated_keys:
            entry = figure_binding(request.tenant_id, request.document_id, request.version, result.revision_id, key)
            self.objects.put(entry)
        return PdfIngestResult(**asdict(result), figures=figures)

    def publish(self, tenant_id, job_id, expected_generation, *, principal=None):
        return self.store.publish(tenant_id, job_id, expected_generation, principal=principal)

    def delete_document(self, tenant_id, document_id, expected_generation, *, principal=None):
        return self.store.delete_document(tenant_id, document_id, expected_generation, principal=principal)

    def rollback(self, tenant_id, generation, expected_generation):
        return self.store.rollback(tenant_id, generation, expected_generation)

    def set_access(self, tenant_id, document_id, allowed_subjects=None, revoked=None, data_domains=None, *, principal=None):
        return self.store.set_access(tenant_id, document_id, allowed_subjects, revoked, data_domains, principal=principal)

    async def query(self, request, observer=None):
        return await self.pipeline.run(request, self.capabilities(), observer)

    async def preview_candidate(self, request):
        """Trusted operator only. Preview is lexical even in a cloud-enabled deployment."""
        from .domain import CapabilityError
        if any(d["requires_model"] for d in self.descriptions.values()):
            raise CapabilityError("candidate preview requires a model-free query configuration")
        if not self.store.is_candidate_snapshot(request.access.tenant_id, request.generation):
            raise NotFoundError("unactivated candidate not found")
        return await self.pipeline.run(request, self.capabilities(), candidate_preview=True)

    async def prewarm(self, access, generation, *, sku=None, dry_run=True, max_chunks=None, candidate=False,
                      all_scopes=False):
        from .domain import CapabilityError
        from .prewarm import prewarm_generation
        identifier(generation, "generation")
        if any(type(flag) is not bool for flag in (dry_run, candidate, all_scopes)):
            raise ValidationError("dry_run, candidate and all_scopes must be booleans")
        if all_scopes and (sku is not None or access != AccessContext(access.tenant_id)):
            raise ValidationError("all-scopes prewarm cannot be combined with identity or SKU filters")
        if max_chunks is not None and (type(max_chunks) is not int or max_chunks < 1):
            raise ValidationError("max_chunks must be a positive integer")
        if candidate and not self.store.is_candidate_snapshot(access.tenant_id, generation):
            raise NotFoundError("unactivated candidate not found")
        if not self.descriptions["dense"]["enabled"]:
            if not dry_run:
                raise CapabilityError("real prewarm requires a configured cloud document dense retriever")
            # No namespace exists to inspect; report scope without claiming any cache readiness.
            flags = {"allow_candidate": True} if candidate else {}
            snapshot = self.store.open_snapshot(access.tenant_id, generation, **flags)
            if snapshot is None:
                raise NotFoundError("snapshot not found")
            chunks = (self.store.list_prewarm_chunks(snapshot, **flags) if all_scopes else
                      self.store.list_chunks(snapshot, access, sku, **flags))
            return {"generation": generation, "dry_run": True, "status": "dense_disabled",
                    "authorized_chunks": len(chunks), "scope": "all_scopes" if all_scopes else "principal",
                    "embedded": 0, "model_calls": 0}
        return await prewarm_generation(self.store, self.components["dense"], access, generation,
                                        sku=sku, dry_run=dry_run, max_chunks=max_chunks,
                                        allow_candidate=candidate, all_scopes=all_scopes)

    def get_job(self, tenant_id, job_id):
        return self.store.get_job(tenant_id, job_id)

    def get_trace(self, tenant_id, trace_id):
        return self.trace_sink.get(tenant_id, trace_id)

    def get_accessible_trace(self, access: AccessContext, trace_id: str):
        trace = self.get_trace(access.tenant_id, trace_id)
        if "conversation_id" in trace:
            return self.conversations.checked(access, trace_id, stored=trace)
        return self._source_checked_trace(access, trace_id, trace)

    def _source_checked_trace(self, access, trace_id, trace=None, chunk_scopes=None):
        """Administrative diagnostics still obey current source permissions.

        A trace can mention retrieval hits absent from the final evidence, and
        can survive a failed query. Check all those references, not only the
        final excerpts. The trusted local CLI retains its existing raw reader.
        """
        if trace is None:
            trace = self.get_trace(access.tenant_id, trace_id)
        if trace.get("candidate_preview"):
            raise NotFoundError("trace not found")
        generation = trace.get("generation")
        if generation is None:
            return trace
        snapshot = self.store.open_snapshot(access.tenant_id, generation)
        if snapshot is None:
            raise NotFoundError("trace sources are no longer accessible")
        chunk_ids = set(trace.get("retrieved_chunk_ids", ()))
        for lane in trace.get("retrieval_lanes", ()):
            chunk_ids.update(hit["chunk_id"] for hit in lane.get("hits", ()))
        if chunk_ids:
            scope = (generation, access, trace.get("sku"))
            allowed = chunk_scopes.get(scope) if chunk_scopes is not None else None
            if allowed is None:
                allowed = {chunk.chunk_id for chunk in self.store.list_chunks(snapshot, access, trace.get("sku"))}
                if chunk_scopes is not None:
                    # Request-local, bounded cache; never reuse an ACL decision on a later request.
                    if len(chunk_scopes) >= 8:
                        chunk_scopes.clear()
                    chunk_scopes[scope] = allowed
            if not chunk_ids <= allowed:
                raise NotFoundError("trace sources are no longer accessible")
        parent_ids = set(trace.get("source_parent_ids", ()))
        parent_ids.update(item["parent_id"] for item in trace.get("evidence", ()))
        if parent_ids:
            parents = self.store.get_parents(snapshot, tuple(parent_ids), access)
            if {parent.parent_id for parent in parents} != parent_ids:
                raise NotFoundError("trace sources are no longer accessible")
        return trace

    def get_evidence(self, access: AccessContext, generation: str, evidence_id: str):
        identifier(generation, "generation")
        try:
            parent_id, length, short_hash = evidence_id.rsplit(".", 2)
            identifier(parent_id, "parent_id")
            length = int(length)
            if not 1 <= length <= self.settings.evidence_chars:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ValidationError("invalid evidence identifier") from exc
        snapshot = self.store.open_snapshot(access.tenant_id, generation)
        if snapshot is None:
            raise NotFoundError("snapshot not found")
        parents = self.store.get_parents(snapshot, (parent_id,), access)
        if len(parents) != 1:
            raise NotFoundError("evidence not found or no longer accessible")
        if digest(parents[0].text) != parents[0].content_hash:
            raise ValidationError("source integrity check failed")
        evidence = make_evidence(parents[0], generation, length)
        if evidence.evidence_id != evidence_id:
            raise NotFoundError("evidence identifier integrity check failed")
        return evidence

    def get_evidence_image(self, access: AccessContext, generation: str, evidence_id: str, key: str):
        """Read a parser-bound figure shown by evidence the caller can currently read."""
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValidationError("invalid image key")
        evidence = self.get_evidence(access, generation, evidence_id)
        return read_bound_figure(evidence, key, self.objects.get)

    def capabilities(self):
        cloud_mode = any(d["requires_model"] for d in self.descriptions.values())
        return {"mode": "cloud" if cloud_mode else "model_free",
                "model_calls": None if cloud_mode else 0, "network_calls": None if cloud_mode else 0,
                "call_counts_basis": "per-request trace" if cloud_mode else "disabled",
                "image_semantic_search": bool(self.settings.allow_models and self.settings.cloud is not None),
                "generation_enabled": self.descriptions["generator"]["enabled"],
                "semantic_support_verification": self.settings.plugins["verifier"] in ("cloud", "deepseek"),
                "history_resolution": any(self.settings.plugins[role] in ("cloud", "deepseek") for role in ("planner", "rewrite")),
                "plugins": self.descriptions}
