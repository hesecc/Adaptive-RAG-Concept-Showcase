"""Explicit generation prewarm for the configured document dense adapter."""

import asyncio

from .cloud import CloudDenseRetriever
from .domain import AccessContext, CapabilityError, ForbiddenError, NotFoundError, ValidationError, identifier
from .ports import KnowledgeStore


async def prewarm_generation(store: KnowledgeStore, dense: CloudDenseRetriever, access: AccessContext,
                             generation: str, *, sku: str | None = None, dry_run: bool = True,
                             max_chunks: int | None = None, allow_candidate: bool = False,
                             all_scopes: bool = False) -> dict:
    """Survey or fill the document cache, never selecting the current generation implicitly.

    Pass the dense role already selected under the application's model/network policy.
    Counts other than authorized_chunks refer to unique cache keys. cached/missing are
    measured at planning time; planned includes only keys within this run's budget.
    Dry runs never embed or write vectors. Retry with the same arguments to continue.
    all_scopes is a trusted operator opt-in, not an authorization grant for a query.
    """
    identifier(generation, "generation")
    if type(allow_candidate) is not bool or type(all_scopes) is not bool:
        raise ValidationError("allow_candidate and all_scopes must be boolean")
    if not isinstance(access, AccessContext):
        raise ValidationError("access context is required")
    if all_scopes and (sku is not None or access != AccessContext(access.tenant_id)):
        raise ValidationError("all_scopes cannot be combined with subject, group, domain or SKU filters")
    if not isinstance(dense, CloudDenseRetriever):
        raise CapabilityError("prewarm requires the configured cloud document dense retriever")
    flags = {"allow_candidate": True} if allow_candidate else {}
    snapshot = await asyncio.to_thread(store.open_snapshot, access.tenant_id, generation, **flags)
    if snapshot is None or snapshot.generation != generation or snapshot.tenant_id != access.tenant_id:
        raise NotFoundError("snapshot not found")
    if all_scopes:
        chunks = await asyncio.to_thread(store.list_prewarm_chunks, snapshot, **flags)
    else:
        chunks = await asyncio.to_thread(store.list_chunks, snapshot, access, sku, **flags)

    def authorize_batch(batch):
        # The revision manifest is immutable; its document ACLs are not.
        document_ids = tuple(dict.fromkeys(chunk.document_id for chunk in batch))
        if not store.check_prewarm_access(access.tenant_id, document_ids, access=None if all_scopes else access):
            raise ForbiddenError("document access changed during prewarm; rerun with current access")

    counts = await dense.prewarm_documents(chunks, dry_run=dry_run, max_chunks=max_chunks,
                                          authorize_batch=authorize_batch)
    return {"generation": generation, "all_scopes": all_scopes, "scope": "all_scopes" if all_scopes else "principal",
            "dry_run": dry_run, **counts}
