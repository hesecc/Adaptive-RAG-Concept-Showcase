"""Owner-scoped conversation views over immutable, source-checked traces."""

from collections import deque

from .domain import ConversationFullError, NotFoundError, QueryRequest, ValidationError, identifier
from .trace_history import MAX_CONVERSATION_TURNS


MAX_PAGE_SIZE = 50
MAX_PAGE_TURN_READS = 2 * MAX_CONVERSATION_TURNS
HISTORY_PAIRS = 3


class ConversationHistory:
    def __init__(self, app):
        self.app = app

    def _members(self, access, group):
        identifier(group, "conversation_id")
        ids = self.app.trace_sink.conversation_turns(access.tenant_id, access.subject_id, group,
                                                     limit=MAX_CONVERSATION_TURNS + 1)
        if not ids or len(ids) > MAX_CONVERSATION_TURNS:
            raise NotFoundError("conversation not found")
        return ids

    def _validate(self, access, group, key, trace, members, scopes):
        parents = trace.get("history_trace_ids", ())
        if (trace.get("trace_id") != key or trace.get("tenant_id") != access.tenant_id or
                trace.get("subject_id") != access.subject_id or trace.get("conversation_id", key) != group or
                not isinstance(parents, (list, tuple)) or len(parents) > HISTORY_PAIRS or
                trace.get("history_turns", 2 * len(parents)) != 2 * len(parents) or
                any(not isinstance(p, str) or p not in members for p in parents)):
            raise NotFoundError("conversation not found")
        self.app._source_checked_trace(access, key, trace, scopes)
        return parents

    def checked(self, access, trace_id, *, stored=None):
        """Authorize this trace and its dependencies, not unrelated later turns."""
        trace = stored if stored is not None else self.app.get_trace(access.tenant_id, trace_id)
        group = trace.get("conversation_id", trace_id)
        ids = self._members(access, group)
        members = set(ids)
        if trace_id not in members:
            raise NotFoundError("conversation not found")
        scopes, seen = {}, {trace_id}
        pending = list(self._validate(access, group, trace_id, trace, members, scopes))
        while pending:
            key = pending.pop()
            if key not in seen:
                source = self.app.get_trace(access.tenant_id, key)
                pending.extend(self._validate(access, group, key, source, members, scopes))
                seen.add(key)
        if self._members(access, group) != ids:
            raise NotFoundError("conversation not found")
        return trace

    def _thread(self, access, group, *, required_ids=(), scopes=None, ids=None):
        if ids is None:
            ids = self._members(access, group)
        members = set(ids)
        if any(key not in members for key in required_ids):
            raise NotFoundError("conversation not found")
        scopes = {} if scopes is None else scopes
        # Check every member, including non-answer turns. Parent membership means
        # this single pass covers the full dependency graph in any storage order.
        for key in ids:
            trace = self.app.get_trace(access.tenant_id, key)
            self._validate(access, group, key, trace, members, scopes)
            yield trace
        # Membership can change while the payloads are being authorized. Do
        # not release the checked prefix or retry indefinitely under churn.
        if self._members(access, group) != ids:
            raise NotFoundError("conversation not found")

    def recheck(self, access, group, required_ids=()) -> None:
        """Reauthorize the current group and ensure materialized IDs still exist."""
        for _ in self._thread(access, group, required_ids=required_ids):
            pass

    def page(self, access, limit, offset):
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValidationError(f"limit must be between 1 and {MAX_PAGE_SIZE}")
        rows = self.app.trace_sink.list_conversations(access.tenant_id, access.subject_id, limit + 1, offset)
        items, scopes, consumed = [], {}, 0
        remaining = MAX_PAGE_TURN_READS
        for row in rows[:limit]:
            group = row["conversation_id"]
            try:
                ids = self._members(access, group)
                if len(ids) > remaining:
                    break  # Leave this candidate at next_offset for the next page.
                remaining -= len(ids)
                thread = self._thread(access, group, required_ids=(row["trace_id"],), scopes=scopes, ids=ids)
                question = next(thread).get("question") or "历史提问"
                for _ in thread:
                    pass
            except NotFoundError:
                pass
            else:
                items.append({"conversation_id": group, "updated_at": row["updated_at"],
                              "title": " ".join(question.split())[:80]})
            consumed += 1
        return {"items": items, "has_more": len(rows) > consumed, "next_offset": offset + consumed}

    def turns(self, access, group):
        turns = []
        for value in self._thread(access, group):
            result = value.get("final_result") or {}
            turns.append({"trace_id": value["trace_id"], "question": value.get("question", ""),
                          "answer": result.get("answer"), "status": value.get("status"),
                          "message": result.get("message", "")})
        return turns

    def request(self, access, body):
        values = body.model_dump()
        group = values["conversation_id"]
        if group:
            values["persist_conversation"] = True
            recent = deque(maxlen=HISTORY_PAIRS)
            ids = self._members(access, group)
            for trace in self._thread(access, group, ids=ids):
                answer = (trace.get("final_result") or {}).get("answer")
                question = trace.get("question")
                if trace.get("status") in {"answered", "partial"} and question and answer:
                    recent.append((trace["trace_id"], question[:4000], answer[:4000]))
            if len(ids) == MAX_CONVERSATION_TURNS:
                raise ConversationFullError()
            values["history_trace_ids"] = tuple(key for key, _, _ in recent)
            values["history"] = [text for _, question, answer in recent for text in (question, answer)]
        values["history"] = tuple(values["history"])
        values["skip_query_steps"] = tuple(values["skip_query_steps"])
        return QueryRequest(access=access, **values)
