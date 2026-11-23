"""Permission matrix: every identity must retrieve exactly the documents it is entitled to.

The check runs on a throwaway knowledge base seeded with a small synthetic corpus, using a
model-free application built from default settings (never from the environment), so it
makes no model or network calls. Which documents each identity may see is written out by
hand below from docs/data-permissions.md. It never calls the code under test, so a fault
in that code cannot also move the expected answer.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .app import RagApplication
from .config import Settings
from .domain import AccessContext, IngestRequest, QueryRequest, ValidationError

SHARED_TOKEN = "quasarcommon"
TARGETED_TOP_K = 5
BROAD_TOP_K = 20


@dataclass(frozen=True)
class MatrixDocument:
    document_id: str
    label: str
    marker: str  # a word that appears in this document only
    data_domains: tuple[str, ...]
    allowed_subjects: tuple[str, ...] = ()
    revoked: bool = False


@dataclass(frozen=True)
class MatrixIdentity:
    identity: str
    label: str
    subject_id: str
    data_domains: tuple[str, ...] | None  # None is unrestricted, like the fixed administrator
    sees: frozenset[str]  # the documents this identity may retrieve, written by hand


DOCUMENTS = (
    MatrixDocument("public-handbook", "公共手册", "amberlark", ("public",)),
    MatrixDocument("ops-runbook", "运营手册", "bronzefinch", ("operations",)),
    MatrixDocument("finance-budget", "财务预算", "cobaltheron", ("finance",)),
    MatrixDocument("product-spec", "产品规格", "duskwren", ("product",)),
    MatrixDocument("sales-playbook", "销售手册", "emberswift", ("sales",)),
    MatrixDocument("hr-policy", "人事制度", "fernplover", ("hr",)),
    MatrixDocument("ops-finance-joint", "运营财务联合", "garnetkite", ("operations", "finance")),
    MatrixDocument("unclassified-memo", "未分类备忘", "hazelstork", ()),
    MatrixDocument("acl-public-note", "指名可读的公共说明", "indigoteal", ("public",), ("reviewer-1",)),
    MatrixDocument("revoked-ops-notice", "已撤销的运营通知", "jadepetrel", ("operations",), (), True),
)

# A scoped account may read a document that is public, or shares a classification with it.
# Unclassified, revoked, and ACL-restricted documents follow their own rules (see the doc).
IDENTITIES = (
    MatrixIdentity("admin", "管理员（不限分类）", "matrix-admin", None, frozenset({
        "public-handbook", "ops-runbook", "finance-budget", "product-spec", "sales-playbook",
        "hr-policy", "ops-finance-joint", "unclassified-memo",
    })),
    MatrixIdentity("public-only", "仅公共", "matrix-public", ("public",), frozenset({
        "public-handbook",
    })),
    MatrixIdentity("operations", "运营", "matrix-operations", ("public", "operations"), frozenset({
        "public-handbook", "ops-runbook", "ops-finance-joint",
    })),
    MatrixIdentity("finance", "财务", "matrix-finance", ("public", "finance"), frozenset({
        "public-handbook", "finance-budget", "ops-finance-joint",
    })),
    MatrixIdentity("product", "产品", "matrix-product", ("public", "product"), frozenset({
        "public-handbook", "product-spec",
    })),
    MatrixIdentity("sales", "销售", "matrix-sales", ("public", "sales"), frozenset({
        "public-handbook", "sales-playbook",
    })),
    MatrixIdentity("hr", "人事", "matrix-hr", ("public", "hr"), frozenset({
        "public-handbook", "hr-policy",
    })),
    MatrixIdentity("ops-finance", "运营+财务", "matrix-ops-finance", ("public", "operations", "finance"), frozenset({
        "public-handbook", "ops-runbook", "finance-budget", "ops-finance-joint",
    })),
    MatrixIdentity("reviewer", "指名审阅人", "reviewer-1", ("public",), frozenset({
        "public-handbook", "acl-public-note",
    })),
)


def _content(document: MatrixDocument) -> str:
    return (f"# {document.label}\n\nSynthetic evaluation note. Shared word {SHARED_TOKEN}. "
            f"Unique word {document.marker}.")


def _verdict(expected: bool, visible: bool) -> str:
    if visible and not expected:
        return "leak"
    if expected and not visible:
        return "missing"
    return "ok"


def seed_corpus(application: RagApplication, tenant_id: str) -> None:
    """Publish the synthetic documents, then read them back to confirm they were stored as designed."""
    generation = None
    for document in DOCUMENTS:
        staged = application.ingest(IngestRequest(
            tenant_id, document.document_id, "v1", _content(document), title=document.label,
            allowed_subjects=document.allowed_subjects, data_domains=document.data_domains,
        ))
        generation = application.publish(tenant_id, staged.job_id, generation).generation
        if document.revoked:
            application.set_access(tenant_id, document.document_id, None, True, None)
    stored = {item["document_id"]: item for item in application.store.list_documents(tenant_id)}
    for document in DOCUMENTS:
        item = stored.get(document.document_id)
        if (item is None or not item["published"] or item["revoked"] != document.revoked
                or sorted(item["data_domains"]) != sorted(document.data_domains)
                or sorted(item["allowed_subjects"]) != sorted(document.allowed_subjects)):
            raise ValidationError(f"The permission corpus was not stored as designed: {document.document_id}")


async def run_permission_matrix(tenant_id: str, work_dir: str | Path) -> dict[str, Any]:
    """Seed the corpus in work_dir, query it as every identity, and compare with the hand-written expectation."""
    application = RagApplication(Settings(data_dir=Path(work_dir)))
    seed_corpus(application, tenant_id)
    cells: list[dict[str, Any]] = []
    broad: list[dict[str, Any]] = []
    model_calls = 0
    for identity in IDENTITIES:
        access = AccessContext(tenant_id, identity.subject_id, (), identity.data_domains)
        for document in DOCUMENTS:
            result = await application.query(QueryRequest(access, document.marker, top_k=TARGETED_TOP_K))
            model_calls += result.capabilities.get("usage", {}).get("model_calls", 0)
            expected = document.document_id in identity.sees
            visible = any(item.document_id == document.document_id for item in result.evidence)
            cells.append({"identity": identity.identity, "document_id": document.document_id,
                          "expected": expected, "visible": visible, "result": _verdict(expected, visible)})
        # One question every document answers: the whole visible set must equal the expectation.
        result = await application.query(QueryRequest(access, SHARED_TOKEN, top_k=BROAD_TOP_K))
        model_calls += result.capabilities.get("usage", {}).get("model_calls", 0)
        returned = {item.document_id for item in result.evidence}
        broad.append({"identity": identity.identity, "expected_ids": sorted(identity.sees),
                      "returned_ids": sorted(returned), "leaks": sorted(returned - identity.sees),
                      "missing": sorted(identity.sees - returned), "ok": returned == identity.sees})
    counts = {name: sum(cell["result"] == name for cell in cells) for name in ("ok", "leak", "missing")}
    broad_failures = sum(not item["ok"] for item in broad)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "parameters": {"tenant_id": tenant_id, "targeted_top_k": TARGETED_TOP_K, "broad_top_k": BROAD_TOP_K,
                       "corpus": "synthetic", "shared_token": SHARED_TOKEN},
        "capabilities": application.capabilities(),
        "model_calls": model_calls,
        "basis": "retrieval evidence only, on a synthetic corpus; no model calls and no real documents",
        "documents": [{"document_id": item.document_id, "label": item.label, "data_domains": list(item.data_domains),
                       "allowed_subjects": list(item.allowed_subjects), "revoked": item.revoked}
                      for item in DOCUMENTS],
        "identities": [{"identity": item.identity, "label": item.label, "subject_id": item.subject_id,
                        "data_domains": None if item.data_domains is None else list(item.data_domains)}
                       for item in IDENTITIES],
        "cells": cells,
        "broad": broad,
        "summary": {"cells": len(cells), "ok": counts["ok"], "leaks": counts["leak"], "missing": counts["missing"],
                    "broad_checks": len(broad), "broad_failures": broad_failures,
                    "passed": counts["ok"] == len(cells) and broad_failures == 0},
    }


def failures(report: dict[str, Any], limit: int = 20) -> list[dict[str, Any]]:
    """The first cells and broad checks that did not match, for a short command-line summary."""
    items: list[dict[str, Any]] = [
        {"identity": cell["identity"], "document_id": cell["document_id"], "result": cell["result"]}
        for cell in report["cells"] if cell["result"] != "ok"
    ]
    items += [{"identity": item["identity"], "document_id": None, "result": "broad_mismatch",
               "leaks": item["leaks"], "missing": item["missing"]}
              for item in report["broad"] if not item["ok"]]
    return items[:limit]
