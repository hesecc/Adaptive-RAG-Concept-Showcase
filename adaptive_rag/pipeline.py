"""Bounded orchestration with honest, deterministic model-free defaults.

Async plugins must cooperate with cancellation. Plugin code is trusted Python,
not a security sandbox; declarations prevent accidental model adapter loading.
"""

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
import inspect
import re
from time import monotonic
from uuid import uuid4

from .domain import (
    CapabilityError, ConversationFullError, Coverage, Draft, Evidence, Hit, NotFoundError, Plan,
    QueryRequest, QueryResult, RagError, SubQuery, ValidationError, Verification, safe_error,
)
from .figures import cut_before_figure, find_figures
from .relevance import RelevanceGate
from .intent import Intent, TERMINAL, exact_identifier, matches_identifier, product_code_hint
from .retrieval import tokenize
from .cloud import check_history_access, start_usage, usage_snapshot, reset_usage

NOT_FOUND = "在你有权访问的知识库中，没有找到与问题相同或相似的内容。"
NOT_FOUND_BY_WORDS = NOT_FOUND + "当前仅按词面匹配判断，换一种说法可能命中。"


def digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def make_evidence(parent, generation: str, length: int, query_ids=()) -> Evidence:
    # The cut may move back to a figure line's start; the id records the length actually used,
    # and cutting again at that length gives the same text, so `get_evidence` can rebuild it.
    text = parent.text[:cut_before_figure(parent.text, length)]
    excerpt_hash = digest(text)
    evidence_id = f"{parent.parent_id}.{len(text)}.{excerpt_hash[:16]}"
    return Evidence(
        evidence_id, tuple(query_ids), parent.tenant_id, generation,
        parent.document_id, parent.version, parent.revision_id, parent.parent_id,
        text, parent.locator, excerpt_hash, parent.content_hash, len(text) < len(parent.text),
        parent.heading_path, find_figures(text),
    )


class RulePlanner:
    """Small documented routing rules, not semantic intent understanding."""
    def __init__(self, max_subqueries=3):
        self.max_subqueries = max_subqueries

    @staticmethod
    def guard_route(question: str, has_sku: bool) -> str | None:
        text = question.strip()
        if re.fullmatch(r"(?:你好|您好|hi|hello|谢谢|thanks)[!！。 .]*", text, re.I):
            return "direct"
        if re.fullmatch(r"(?:请)?(?:帮我)?(?:转(?:接)?人工(?:客服)?|联系人工客服)[。！! .]*", text):
            return "handoff"
        # A documented procedure is not a request to execute a live lookup.
        live = re.search(r"实时库存|当前库存|订单状态|实时价格|物流状态|(?:包裹|快递|订单|物流)(?:现在|目前|已经|到底)?(?:到哪|发货了[吗没])|current stock|order status|where is my (?:package|order)", text, re.I)
        policy = re.search(r"有哪些|的含义|是什么意思|字段说明|定义|操作流程|操作指南|如何查询|怎么查询|how (?:do i|to) (?:check|track)", text, re.I)
        specific = re.search(r"订单号|[A-Za-z]*\d{5,}", text)
        if live and (not policy or specific):
            return "realtime"
        if (not has_sku and not product_code_hint(text)
                and re.search(r"这款|该款|这个商品|这件商品|this product|this item", text, re.I)):
            return "clarify"
        return None

    @staticmethod
    def explicit_parts(question: str) -> tuple[str, ...]:
        return tuple(part.strip() for part in re.split(r"[？?；;\n]+", question) if part.strip())

    async def plan(self, request: QueryRequest) -> Plan:
        text = request.question.strip()
        guarded = self.guard_route(text, request.sku is not None)
        if guarded:
            if guarded == "direct":
                return Plan((), request.sku, "capability_unavailable", "无模型模式不生成闲聊回复。")
            action, message = TERMINAL[guarded]
            return Plan((), request.sku, action, message)
        parts = self.explicit_parts(text)
        if len(parts) > self.max_subqueries:
            raise ValidationError(f"question exceeds {self.max_subqueries} explicit subquestions; split the request")
        return Plan(tuple(SubQuery(f"q{i}", part) for i, part in enumerate(parts or (text,), 1)), request.sku)


class RuleDecisionPolicy:
    async def decide(self, state: dict) -> dict:
        return {"action": "retry" if state.get("checkpoint") == "gap" and state.get("gap_enabled") and state.get("missing_query_ids") else "continue"}


class LexicalCoverageJudge:
    """Token overlap is only a retrieval diagnostic, never factual support."""
    async def assess(self, queries, evidence) -> Coverage:
        covered = tuple(q.query_id for q in queries if any(
            q.query_id in item.query_ids and set(tokenize(q.text)).intersection(tokenize(item.text))
            for item in evidence
        ))
        return Coverage(covered, tuple(q.query_id for q in queries if q.query_id not in covered))


class DisabledGenerator:
    async def generate(self, plan, evidence) -> Draft:
        return Draft()


class CitationVerifier:
    """Verify citation existence and excerpt integrity; not entailment."""
    async def verify(self, draft: Draft, evidence) -> Verification:
        errors = []
        known = {item.evidence_id: item for item in evidence}
        for item in evidence:
            if digest(item.text) != item.content_hash:
                errors.append("evidence_hash_mismatch")
        if draft.answer is None and draft.claims:
            errors.append("claims_without_answer")
        if draft.answer is not None and (not draft.answer.strip() or not draft.claims):
            errors.append("answer_requires_cited_claims")
        for claim in draft.claims:
            if not claim.text.strip() or not claim.evidence_ids:
                errors.append("claim_requires_text_and_citations")
            if any(key not in known for key in claim.evidence_ids):
                errors.append("unknown_evidence_reference")
        return Verification(not errors, tuple(dict.fromkeys(errors)), support_checked=False)


class QueryPipeline:
    def __init__(self, settings, components, descriptions):
        self.settings, self.c, self.descriptions = settings, components, descriptions
        self.gate = (RelevanceGate(settings.relevance_lexical_floor, settings.relevance_rerank_floor,
                                   dense=bool(descriptions["dense"]["enabled"]))
                     if settings.relevance_gate else None)

    async def run(self, request: QueryRequest, capabilities: dict, observer=None, *, candidate_preview=False) -> QueryResult:
        store = self.c["store"]
        open_snapshot, list_chunks, get_parents = store.open_snapshot, store.list_chunks, store.get_parents
        if candidate_preview:
            if not request.generation:
                raise ValidationError("candidate preview requires a generation")
            open_snapshot = partial(open_snapshot, allow_candidate=True)
            list_chunks = partial(list_chunks, allow_candidate=True)
            get_parents = partial(get_parents, allow_candidate=True)
        usage_token = start_usage()
        started = monotonic()
        deadline = started + self.settings.timeout_seconds
        save_reserve = min(self.settings.stage_timeout_seconds, self.settings.timeout_seconds * 0.15)
        failed = False
        trace = {
            "trace_id": uuid4().hex, "tenant_id": request.access.tenant_id,
            "subject_id": request.access.subject_id, "started_at": datetime.now(timezone.utc).isoformat(),
            "question": request.question, "sku": request.sku, "history_turns": len(request.history),
            "retrieval_preferences": {"top_k": request.top_k, "skip_query_steps": list(request.skip_query_steps)},
            "history_used": False, "persist_conversation": request.persist_conversation, "generation": None, "stages": [],
            "plugins": self.descriptions, "model_calls": 0, "network_calls": 0,
            "retrieved_chunk_ids": [], "retry_count": 0,
            "decisions": [], "retrieval_lanes": [], "rankings": [], "source_parent_ids": [],
        }

        if request.persist_conversation:
            trace["conversation_id"] = request.conversation_id or trace["trace_id"]
            trace["history_trace_ids"] = list(request.history_trace_ids)

        if candidate_preview:
            # Keep even failed/early-stop diagnostics private, including after activation.
            trace["candidate_preview"] = True

        def emit(event, value):
            # The observer is trusted transport code. Only timing metadata is
            # published before the result's final permission checks complete.
            if observer is not None:
                observer(event, value)

        emit("trace", {key: trace[key] for key in ("trace_id", "conversation_id") if key in trace})

        def stage_event(record):
            emit("stage", {key: record[key] for key in (
                "index", "name", "status", "started_ms", "duration_ms", "error_type", "reason"
            ) if key in record})

        def output(name, value):
            record = next(row for row in reversed(trace["stages"]) if row["name"] == name)
            record["output"] = value

        def sources(parent_ids):
            trace["source_parent_ids"] = list(dict.fromkeys((*trace["source_parent_ids"], *parent_ids)))

        def hit_output(hits):
            sources(hit.chunk.parent_id for hit in hits)
            return {"hit_count": len(hits), "hits": [
                {"chunk_id": hit.chunk.chunk_id, "parent_id": hit.chunk.parent_id,
                 "document_id": hit.chunk.document_id, "rank": hit.rank, "score": hit.score,
                 "text_preview": hit.chunk.text[:320], "text_chars": len(hit.chunk.text),
                 "preview_truncated": len(hit.chunk.text) > 320}
                for hit in hits
            ]}

        async def stage(name, function, *args, validate=None):
            begin = monotonic()
            before_calls = usage_snapshot()["model_calls"]
            record = {"index": len(trace["stages"]), "name": name, "status": "running",
                      "started_ms": round((begin - started) * 1000, 3)}
            trace["stages"].append(record)
            stage_event(record)
            try:
                async with asyncio.timeout(self.settings.stage_timeout_seconds):
                    plugin = self.descriptions.get(name.partition(".")[0], {})
                    # Built-in cloud transport checks each send, including retries. External
                    # plugins may own their transport: check before handing data to them.
                    if plugin.get("plugin") != "cloud" and (plugin.get("uses_network") or plugin.get("requires_model")):
                        await check_history_access()
                    if inspect.iscoroutinefunction(function):
                        value = await function(*args)
                    else:
                        value = await asyncio.to_thread(function, *args)
                        if inspect.isawaitable(value):
                            value = await value
                    if validate is not None:
                        try:
                            value = validate(value)
                            if inspect.isawaitable(value):
                                value = await value
                        except Exception:
                            record["reason"] = "output_validation_failed"
                            raise
                record["status"] = "ok"
                return value
            except BaseException as exc:
                record["status"] = ("cancelled" if isinstance(exc, asyncio.CancelledError) else
                                    "timeout" if isinstance(exc, TimeoutError) else "error")
                record["error_type"] = type(exc).__name__
                record["error"] = safe_error(exc)
                raise
            finally:
                owner = getattr(function, "__self__", None)
                if (request.history and name in {"planner", "rewrite", "multi_query", "hyde"} and
                        getattr(owner, "uses_history", False) and usage_snapshot()["model_calls"] > before_calls):
                    trace["history_used"] = True
                record["duration_ms"] = round((monotonic() - begin) * 1000, 3)
                stage_event(record)

        def disabled(role, *, reason=None):
            record = {"index": len(trace["stages"]), "name": role,
                      "status": "skipped" if reason else "disabled", "duration_ms": 0,
                      "started_ms": round((monotonic() - started) * 1000, 3),
                      "output": {"executed": False}}
            if reason:
                record["reason"] = reason
            trace["stages"].append(record)
            stage_event(record)

        def result(status, message, evidence=(), missing=(), draft=None):
            trace["status"] = status
            trace["evidence"] = [asdict(item) for item in evidence]
            value = QueryResult(trace["trace_id"], status, message, trace["generation"],
                               tuple(evidence), draft.answer if draft else None, draft.claims if draft else (),
                               tuple(missing), capabilities | {"usage": usage_snapshot()})
            trace["final_result"] = asdict(value)
            return value

        intent = None  # Request-local. Never mutate the registry or the user's preferences.

        async def decision(checkpoint, missing=()):
            nonlocal intent
            def checked(value):
                if not isinstance(value, dict) or value.get("action") not in {"continue", "retry", "stop"}:
                    raise ValidationError("decision plugin returned an invalid action")
                return value
            value = await stage(f"decision.{checkpoint}", self.c["decision"].decide, {
                **({"question": request.question, "has_history": bool(request.history), "has_sku": request.sku is not None}
                   if checkpoint == "preflight" and self.descriptions["decision"]["plugin"] == "deepseek" else {}),
                "checkpoint": checkpoint, "missing_query_ids": tuple(missing),
                "gap_enabled": self.descriptions["gap"]["enabled"],
                "retry_count": trace["retry_count"],
            }, validate=checked)
            detail = {"checkpoint": checkpoint, "action": value["action"]}
            if checkpoint == "preflight" and isinstance(value.get("intent"), Intent):
                intent = value["intent"]
                detail.update(route=intent.route, selected_steps=list(intent.steps), source=intent.source)
                if intent.reason:
                    detail["fallback_reason"] = intent.reason
            trace["decisions"].append(detail)
            output(f"decision.{checkpoint}", {**detail,
                   "missing_query_ids": list(missing), "retry_count": trace["retry_count"]})
            return value["action"]

        async def execute():
            async def resolve_snapshot():
                value = await stage("snapshot", open_snapshot, request.access.tenant_id, request.generation)
                output("snapshot", {"generation": value.generation if value else None})
                if request.generation is not None and value is None:
                    raise NotFoundError("snapshot not found")
                if value is not None:
                    trace["generation"] = value.generation
                return value

            # Reject unavailable explicit generations before invoking any model-backed planner.
            snapshot = await resolve_snapshot() if request.generation is not None else None
            if await decision("preflight") == "stop":
                return result("policy_stopped", "规则策略停止本次查询。")
            if intent and intent.route in TERMINAL:
                return result(*TERMINAL[intent.route])

            identifier_filter = None
            if intent and intent.route == "exact":
                identifier_filter = exact_identifier(request.question, request.sku)
                if not identifier_filter:
                    return result("clarify", "请提供一个明确的商品型号或 SKU，再查找对应资料。")

            def checked_plan(value):
                if not isinstance(value, Plan) or value.sku != request.sku or value.action not in {"retrieve", "clarify", "capability_unavailable"}:
                    raise ValidationError("planner returned an invalid plan or changed trusted SKU")
                return replace(value, queries=self._queries(value.queries)) if value.action == "retrieve" else value
            planner = self.c["planner"]
            if intent and ("decompose" not in intent.steps or len(RulePlanner.explicit_parts(request.question)) > 1):
                # Never let a semantic planner merge or silently drop an already explicit obligation.
                planner = RulePlanner(self.settings.max_subqueries)
            plan = await stage("planner", planner.plan, request, validate=checked_plan)
            if plan.action != "retrieve":
                output("planner", {"action": plan.action, "queries": []})
                return result(plan.action, plan.message)
            queries = plan.queries
            output("planner", {"action": plan.action, "queries": [asdict(q) for q in queries],
                               "implementation": "rules" if isinstance(planner, RulePlanner) else self.descriptions["planner"]["plugin"]})
            original = {q.query_id: q for q in queries}
            trace["subqueries"] = [asdict(q) for q in queries]
            variants = queries
            role_count = sum(bool(self.descriptions[r]["enabled"]) for r in ("lexical", "dense"))
            minimum_lanes = len(queries) * role_count
            if minimum_lanes > self.settings.max_lanes:
                raise ValidationError("retrieval lane budget cannot cover original questions")
            reserve = (min(minimum_lanes, self.settings.max_lanes - minimum_lanes)
                       if self.descriptions["gap"]["enabled"] and self.settings.max_retries else 0)
            variant_budget = ((self.settings.max_lanes - reserve) // role_count
                              if role_count else len(queries))
            trace["reserved_repair_lanes"] = reserve
            for role in ("rewrite", "multi_query", "hyde"):
                if self.descriptions[role]["enabled"]:
                    if role in request.skip_query_steps:
                        disabled(role, reason="user_preference")
                        continue
                    if intent and role not in intent.steps:
                        disabled(role, reason="intent_not_selected")
                        continue
                    if variant_budget <= len(variants):
                        disabled(role, reason="retrieval_lane_budget")
                        continue
                    variants = await stage(role, self.c[role].transform, variants, request,
                                           validate=lambda value: self._variants(value, original))
                    variants = self._budget_variants(variants, queries, variant_budget)
                    output(role, {"queries": [asdict(q) for q in variants], "variant_budget": variant_budget})
                else:
                    disabled(role)
            trace["query_variants"] = [asdict(q) for q in variants]
            if request.generation is None:
                snapshot = await resolve_snapshot()
            if snapshot is None:
                return result("no_index", "当前租户尚无已发布知识版本。")
            chunks = await stage("authorized_chunks", list_chunks, snapshot, request.access, request.sku)
            authorized_count = len(chunks)
            if identifier_filter is not None:
                chunks = tuple(chunk for chunk in chunks if matches_identifier(chunk, identifier_filter))
            allowed = {chunk.chunk_id: chunk for chunk in chunks}
            trace["authorized_chunk_count"] = authorized_count
            output("authorized_chunks", {"authorized_chunk_count": authorized_count, "sku": request.sku,
                   **({"identifier_match_count": len(chunks)} if identifier_filter is not None else {})})
            if identifier_filter is not None and not chunks:
                return result("empty", "在你有权访问的资料中，未找到与该型号或 SKU 完整匹配的内容。", missing=tuple(original))
            for role in ("dense",):
                if not self.descriptions[role]["enabled"]:
                    disabled(role)
            if self.gate is None:
                disabled("relevance")
            relevance_bases = {}
            wordings = {qid: [query.text] for qid, query in original.items()}
            lanes = {qid: [] for qid in original}
            lane_count = 0

            async def retrieve(batch):
                nonlocal lane_count
                for variant in batch:
                    if variant.text not in wordings[variant.query_id]:
                        wordings[variant.query_id].append(variant.text)
                    for role in ("lexical", "dense"):
                        if not self.descriptions[role]["enabled"]:
                            continue
                        if lane_count >= self.settings.max_lanes:
                            raise ValidationError("retrieval exceeds configured lane budget")
                        lane_count += 1
                        checked = await stage(f"{role}.{variant.query_id}.{lane_count}", self.c[role].search,
                                              variant, chunks, self.settings.candidate_limit,
                                              validate=lambda value: self._hits(value, variant.query_id, allowed, self.settings.candidate_limit))
                        output(f"{role}.{variant.query_id}.{lane_count}",
                               {"query_id": variant.query_id, "query": variant.text, **hit_output(checked)})
                        lanes[variant.query_id].append(checked)
                        trace["retrieval_lanes"].append({"role": role, "query_id": variant.query_id,
                            "text": variant.text, "hits": [{"chunk_id": h.chunk.chunk_id, "score": h.score, "rank": h.rank} for h in checked]})
                trace["lane_count"] = lane_count

            async def assemble():
                ranked = {}
                for qid, query in original.items():
                    lane_candidates = {hit.chunk.chunk_id: hit.chunk for lane in lanes[qid] for hit in lane}
                    fused = await stage(f"fusion.{qid}", self.c["fusion"].fuse, query, lanes[qid], self.settings.candidate_limit,
                                        validate=lambda value: self._hits(value, qid, lane_candidates, self.settings.candidate_limit))
                    output(f"fusion.{qid}", {"query_id": qid, **hit_output(fused)})
                    candidates = {hit.chunk.chunk_id: hit.chunk for hit in fused}
                    ranked[qid] = await stage(f"reranker.{qid}", self.c["reranker"].rank, query, fused, self.settings.candidate_limit,
                                              validate=lambda value: self._hits(value, qid, candidates, self.settings.candidate_limit))
                    output(f"reranker.{qid}", {"query_id": qid, **hit_output(ranked[qid])})
                    related = ranked[qid]
                    if self.gate is not None:
                        # Retrieval ranks something for any question; only similar chunks may become sources.
                        judgement = await stage(f"relevance.{qid}", self.gate.assess, query, ranked[qid], tuple(wordings[qid][1:]))
                        output(f"relevance.{qid}", judgement.detail)
                        relevance_bases[qid] = judgement.detail["basis"]
                        related = judgement.hits
                    trace["rankings"].append({"query_id": qid, "retry": trace["retry_count"],
                        "fused_chunk_ids": [h.chunk.chunk_id for h in fused],
                        "ranked_chunk_ids": [h.chunk.chunk_id for h in ranked[qid]],
                        "related_chunk_ids": [h.chunk.chunk_id for h in related]})
                    ranked[qid] = related
                ordered = []
                for rank in range(max((len(v) for v in ranked.values()), default=0)):
                    for hits in ranked.values():
                        if rank < len(hits):
                            ordered.append(hits[rank])
                trace["retrieved_chunk_ids"] = list(dict.fromkeys(hit.chunk.chunk_id for hit in ordered))
                associations = {}
                for hit in ordered:
                    associations.setdefault(hit.chunk.parent_id, set()).add(hit.query_id)
                selected = tuple(associations)[:request.top_k]
                def checked_parents(values):
                    if {parent.parent_id for parent in values} != set(selected):
                        # Revocation between retrieval and backfill is fail-closed.
                        raise NotFoundError("evidence became unavailable; retry the query")
                    for parent in values:
                        if parent.tenant_id != request.access.tenant_id or digest(parent.text) != parent.content_hash:
                            raise ValidationError("parent adapter returned invalid source integrity")
                    return values
                parents = await stage("parent_backfill", get_parents, snapshot, selected, request.access,
                                      validate=checked_parents)
                by_id = {parent.parent_id: parent for parent in parents}
                sources(selected)
                budget = self.settings.evidence_chars
                items = []
                for index, parent_id in enumerate(selected):
                    parent = by_id[parent_id]
                    length = min(len(parent.text), max(1, budget // (len(selected) - index)))
                    items.append(make_evidence(parent, snapshot.generation, length, sorted(associations[parent_id])))
                    budget -= length
                output("parent_backfill", {"parent_count": len(parents), "parents": [
                    {"parent_id": parent.parent_id, "document_id": parent.document_id,
                     "heading_path": list(parent.heading_path), "text_preview": parent.text[:600],
                     "text_chars": len(parent.text), "preview_truncated": len(parent.text) > 600,
                     "content_chars": len(parent.text)} for parent in parents
                ], "evidence_chars": sum(len(item.text) for item in items)})
                return tuple(items)

            async def finish(status, message, evidence, missing=(), draft=None):
                # All exits exposing evidence or claims recheck current permissions.
                def checked_access(values):
                    if {p.parent_id for p in values} != {e.parent_id for e in evidence}:
                        raise NotFoundError("evidence access changed during query; retry")
                    return values
                parents = await stage("final_access_check", get_parents, snapshot,
                                      tuple(e.parent_id for e in evidence), request.access, validate=checked_access)
                output("final_access_check", {"authorized_parent_count": len(parents), "valid": True})
                return result(status, message, evidence, missing, draft)

            async def recover(missing, evidence, label):
                # Initial evidence repair and post-generation repair share one budget.
                nonlocal coverage
                if not (chunks and missing and self.descriptions["gap"]["enabled"] and
                        trace["retry_count"] < self.settings.max_retries):
                    return evidence, False
                role_count = sum(bool(self.descriptions[r]["enabled"]) for r in ("lexical", "dense"))
                if lane_count + len(missing) * role_count > self.settings.max_lanes:
                    trace["repair_skipped"] = "retrieval_lane_budget"
                    return evidence, False
                missing = self._missing_queries(missing, original)
                gap = await stage("gap." + label, self.c["gap"].transform, missing, request,
                                  validate=lambda value: self._variants(value, {q.query_id: q for q in missing}))
                output("gap." + label, {"queries": [asdict(q) for q in gap], "checkpoint": label})
                if lane_count + len(gap) * role_count > self.settings.max_lanes:
                    trace["repair_skipped"] = "retrieval_lane_budget"
                    return evidence, False
                before = {(item.parent_id, item.source_hash): item.text for item in evidence}
                trace["retry_count"] += 1
                trace["repair_checkpoint"] = label
                await retrieve(gap)
                updated = await assemble()
                coverage = await stage("judge.retry", self.c["judge"].assess, queries, updated,
                                       validate=lambda value: self._coverage(value, original))
                output("judge.retry", asdict(coverage))
                trace["coverage"] = asdict(coverage)
                changed = any((item.parent_id, item.source_hash) not in before or
                              len(item.text) > len(before[item.parent_id, item.source_hash])
                              for item in updated)
                trace["repair_new_evidence"] = changed
                return updated, changed

            await retrieve(variants)
            evidence = await assemble()
            coverage = await stage("judge", self.c["judge"].assess, queries, evidence,
                                   validate=lambda value: self._coverage(value, original))
            output("judge", asdict(coverage))
            action = await decision("gap", coverage.missing_query_ids)
            if action == "stop":
                return result("policy_stopped", "策略在证据检查后停止查询。", missing=coverage.missing_query_ids)
            if self.descriptions["gap"]["enabled"] and action == "retry" and coverage.missing_query_ids and self.settings.max_retries:
                missing = coverage.missing_queries or tuple(original[qid] for qid in coverage.missing_query_ids)
                evidence, _ = await recover(missing, evidence, "evidence")
            elif not self.descriptions["gap"]["enabled"]:
                disabled("gap")
            trace["coverage"] = asdict(coverage)
            if not evidence:
                semantic = self.descriptions["dense"]["enabled"] or "rerank" in relevance_bases.values()
                return result("empty", NOT_FOUND if semantic else NOT_FOUND_BY_WORDS, missing=coverage.missing_query_ids)
            semantic_coverage = coverage.basis == "cloud_semantic_sufficiency"
            if semantic_coverage and not coverage.covered_query_ids:
                return await finish("insufficient", "找到了相关内容，但不足以完整回答这个问题，未生成答案。",
                                    evidence, coverage.missing_query_ids)
            if await decision("generate", coverage.missing_query_ids) == "stop":
                return result("policy_stopped", "策略停止生成阶段。", missing=coverage.missing_query_ids)
            async def checked_draft(value, items):
                if not isinstance(value, Draft):
                    raise ValidationError("generator returned an invalid draft")
                if not (await CitationVerifier().verify(value, items)).valid:
                    raise ValidationError("citation verification failed")
                return value
            generation_plan = replace(plan, original_question=trace["question"])
            if self.descriptions["generator"]["enabled"]:
                generation_queries = tuple(q for q in queries if not semantic_coverage or q.query_id in coverage.covered_query_ids)
                draft = await stage("generator", self.c["generator"].generate, replace(generation_plan, queries=generation_queries), evidence,
                                    validate=lambda value: checked_draft(value, evidence))
                output("generator", {"answer_present": draft.answer is not None,
                       "claim_count": len(draft.claims), "verification_pending": True})
            else:
                disabled("generator")
                draft = Draft()
            async def verify(current, items, label):
                structural = await CitationVerifier().verify(current, items)
                if not structural.valid:
                    raise ValidationError("citation verification failed")
                def checked_verification(value):
                    self._verification(value, current, original)
                    if not value.valid and not value.support_checked:
                        raise ValidationError("citation verification failed")
                    return value
                evaluator = getattr(self.c["verifier"], "evaluate", None)
                if callable(evaluator):
                    checked = await stage(label, evaluator, replace(plan, queries=queries), current, items,
                                          validate=checked_verification)
                else:
                    checked = await stage(label, self.c["verifier"].verify, current, items, validate=checked_verification)
                output(label, asdict(checked))
                return checked

            verification = await verify(draft, evidence, "verifier")
            missing = tuple(dict.fromkeys((*coverage.missing_query_ids,
                                          *(q.query_id for q in verification.missing_queries))))
            if verification.support_checked and draft.answer is not None and (not verification.valid or missing):
                trace["verification_initial"] = asdict(verification)
                gap_queries = verification.missing_queries or coverage.missing_queries
                if not gap_queries:
                    gap_queries = tuple(original[qid] for qid in (missing or tuple(original)))
                previous_draft, previous_verification = draft, verification
                evidence, changed = await recover(gap_queries, evidence, "answer")
                if changed and (coverage.basis != "cloud_semantic_sufficiency" or coverage.covered_query_ids):
                    generation_queries = tuple(q for q in queries if coverage.basis != "cloud_semantic_sufficiency" or q.query_id in coverage.covered_query_ids)
                    draft = await stage("generator.retry", self.c["generator"].generate, replace(generation_plan, queries=generation_queries), evidence,
                                        validate=lambda value: checked_draft(value, evidence))
                    output("generator.retry", {"answer_present": draft.answer is not None,
                           "claim_count": len(draft.claims), "verification_pending": True})
                    verification = await verify(draft, evidence, "verifier.retry")
                else:
                    # Retry may reorder/truncate parents. Keep only claims with exactly
                    # the same cited excerpts; never transplant a prior verdict.
                    current_ids = {e.evidence_id for e in evidence}
                    indexes = tuple(i for i in previous_verification.supported_claim_indexes
                                    if set(previous_draft.claims[i].evidence_ids) <= current_ids)
                    verification = replace(previous_verification, supported_claim_indexes=indexes)
                missing = tuple(dict.fromkeys((*coverage.missing_query_ids,
                                              *(q.query_id for q in verification.missing_queries))))
            trace["verification"] = asdict(verification)
            if verification.support_checked:
                safe_claims = tuple(draft.claims[i] for i in verification.supported_claim_indexes)
                safe_answer = "\n".join(claim.text + " " + " ".join(f"[{key}]" for key in claim.evidence_ids)
                                         for claim in safe_claims) or None
                draft = Draft(safe_answer, safe_claims, draft.mode)
                if not safe_claims:
                    return await finish("insufficient", "未获得可通过事实支持性检查的答案，需补充权威资料。",
                                        evidence, missing or tuple(original), draft)
                if not verification.valid or missing:
                    return await finish("partial", "仅返回通过支持性检查的部分；仍有缺项需要确认。",
                                        evidence, missing or tuple(original), draft)
                message = ("回答已通过云端事实支持性与完整性检查；模型判断仍可能出错。"
                           if verification.completeness_checked else
                           "回答已通过事实支持性检查，尚未检查完整性；模型判断仍可能出错。")
                return await finish("answered", message,
                                    evidence, (), draft)
            return await finish("evidence_preview" if draft.answer is None else "answered",
                                "证据预览，未生成答案，未验证语义支持。" if draft.answer is None else "已生成带引用回答；出处完整性检查通过，未验证语义支持。",
                                evidence, coverage.missing_query_ids, draft)

        try:
            async with asyncio.timeout(self.settings.timeout_seconds - save_reserve):
                return await execute()
        except TimeoutError as exc:
            failed = True
            trace["status"] = "timeout"
            trace["error"] = safe_error(exc)
            # The outer query deadline cancels its active stage before the
            # timeout context translates that cancellation into TimeoutError.
            if trace["stages"] and trace["stages"][-1]["status"] == "cancelled":
                record = trace["stages"][-1]
                record.update(status="timeout", error_type="TimeoutError", error=safe_error(exc))
                stage_event(record)
            raise CapabilityError(f"query timed out; trace_id={trace['trace_id']}",
                                  diagnostic_code="query_timeout") from exc
        except BaseException as exc:
            failed = True
            trace["status"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            trace["error_type"] = type(exc).__name__
            trace["error"] = safe_error(exc)
            if isinstance(exc, (RagError, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise CapabilityError(f"query plugin failed; trace_id={trace['trace_id']}") from exc
        finally:
            usage = usage_snapshot()
            reset_usage(usage_token)
            trace.update(model_calls=usage["model_calls"], network_calls=usage["network_calls"], usage=usage)
            trace["duration_ms"] = round((monotonic() - started) * 1000, 3)
            # Persistence is required for success, bounded, and never blocks the
            # event loop. A failed sink cannot record its own failure; surface it.
            try:
                async with asyncio.timeout(max(0.001, min(self.settings.stage_timeout_seconds, deadline - monotonic()))):
                    await asyncio.to_thread(self.c["trace"].save, trace)
            except ConversationFullError:
                if not failed:
                    raise
            except Exception as exc:
                if not failed:
                    raise CapabilityError(f"trace persistence failed; trace_id={trace['trace_id']}") from exc

    def _queries(self, queries):
        if not isinstance(queries, (tuple, list)) or not 1 <= len(queries) <= self.settings.max_subqueries:
            raise ValidationError("planner exceeded subquery budget")
        if len({q.query_id for q in queries if isinstance(q, SubQuery)}) != len(queries):
            raise ValidationError("subquery IDs must be unique")
        return self._variants(queries, {q.query_id: q for q in queries})

    def _variants(self, variants, originals):
        if not isinstance(variants, (tuple, list)) or not 1 <= len(variants) <= self.settings.max_lanes:
            raise ValidationError("transform exceeded variant budget")
        for query in variants:
            if not isinstance(query, SubQuery) or query.query_id not in originals or not isinstance(query.text, str) or not query.text.strip() or len(query.text) > 4000:
                raise ValidationError("transform returned invalid or reassigned subquery")
        if {q.query_id for q in variants} != set(originals):
            raise ValidationError("transform dropped an original subquery")
        return tuple(variants)

    @staticmethod
    def _budget_variants(variants, originals, limit):
        """Keep each original first, then distribute extension slots fairly."""
        selected = list(originals)
        seen = {(query.query_id, query.text) for query in selected}
        extras = {query.query_id: [] for query in originals}
        for query in variants:
            identity = (query.query_id, query.text)
            if identity not in seen:
                extras[query.query_id].append(query)
                seen.add(identity)
        for rank in range(max((len(items) for items in extras.values()), default=0)):
            for items in extras.values():
                if len(selected) >= limit:
                    return tuple(selected)
                if rank < len(items):
                    selected.append(items[rank])
        return tuple(selected)

    @staticmethod
    def _hits(hits, query_id, allowed, limit):
        if not isinstance(hits, (tuple, list)) or len(hits) > limit:
            raise ValidationError("retriever exceeded candidate budget")
        seen = set()
        for hit in hits:
            if not isinstance(hit, Hit) or hit.query_id != query_id or allowed.get(hit.chunk.chunk_id) != hit.chunk or hit.chunk.chunk_id in seen:
                raise ValidationError("plugin returned duplicate, unauthorized or mutated chunks")
            seen.add(hit.chunk.chunk_id)
        return tuple(hits)

    @staticmethod
    def _coverage(coverage, originals):
        if not isinstance(coverage, Coverage):
            raise ValidationError("judge returned invalid coverage")
        covered, missing = set(coverage.covered_query_ids), set(coverage.missing_query_ids)
        if covered & missing or covered | missing != set(originals):
            raise ValidationError("judge coverage must partition original subqueries")
        if len(covered) != len(coverage.covered_query_ids) or len(missing) != len(coverage.missing_query_ids):
            raise ValidationError("judge coverage must not repeat subqueries")
        if coverage.missing_queries:
            QueryPipeline._missing_queries(coverage.missing_queries, {qid: originals[qid] for qid in missing})
        return coverage

    @staticmethod
    def _missing_queries(queries, originals):
        if not isinstance(queries, (tuple, list)) or not 1 <= len(queries) <= len(originals):
            raise ValidationError("invalid missing-aspect queries")
        seen = set()
        for query in queries:
            if not isinstance(query, SubQuery) or query.query_id not in originals or query.query_id in seen or not isinstance(query.text, str) or not query.text.strip() or len(query.text) > 4000 or "\x00" in query.text:
                raise ValidationError("invalid missing-aspect queries")
            seen.add(query.query_id)
        return tuple(queries)

    @staticmethod
    def _verification(value, draft, originals):
        if not isinstance(value, Verification) or any(type(flag) is not bool for flag in
                (value.valid, value.support_checked, value.completeness_checked)):
            raise ValidationError("verifier returned invalid result")
        indexes = value.supported_claim_indexes
        if not isinstance(indexes, tuple) or any(type(i) is not int or not 0 <= i < len(draft.claims) for i in indexes) or len(set(indexes)) != len(indexes):
            raise ValidationError("verifier returned invalid supported claims")
        if value.support_checked and value.valid and set(indexes) != set(range(len(draft.claims))):
            raise ValidationError("verifier passed claims it did not check")
        if value.missing_queries:
            QueryPipeline._missing_queries(value.missing_queries, originals)
