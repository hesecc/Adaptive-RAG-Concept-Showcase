"""Content-free conversation index, derived from immutable server trace envelopes."""

from datetime import datetime, timezone
from itertools import islice
import json
from typing import Any

from .domain import ConversationFullError, ValidationError, identifier


_TABLE = """CREATE TABLE trace_history (
    tenant_id TEXT NOT NULL, trace_id TEXT NOT NULL, subject_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL, started_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, trace_id),
    FOREIGN KEY (tenant_id, trace_id) REFERENCES traces (tenant_id, trace_id) ON DELETE CASCADE
)"""
_INDEX = """CREATE INDEX trace_history_turns ON trace_history
    (tenant_id, subject_id, conversation_id, started_at, trace_id)"""
_INSERT = "INSERT INTO trace_history (tenant_id,trace_id,subject_id,conversation_id,started_at) VALUES (?,?,?,?,?)"
STORAGE_VERSION = 2
MAX_CONVERSATION_TURNS = 64


def with_conversation(trace, conversation_id):
    if conversation_id is not None:
        if trace.get("conversation_id") not in (None, conversation_id):
            raise ValidationError("conflicting conversation_id")
        trace = dict(trace, conversation_id=conversation_id)
    if trace.get("conversation_id") is not None:
        identifier(trace["conversation_id"], "conversation_id")
    return trace


def _metadata(trace, *, legacy=False):
    # Only the top-level subject stamped from AccessContext is authoritative.
    if trace.get("candidate_preview"):
        return None
    if legacy and "persist_conversation" not in trace:
        # V1 did not authenticate conversation groups or client-supplied context.
        if (trace.get("conversation_id", trace["trace_id"]) != trace["trace_id"]
                or trace.get("history_turns") or trace.get("history_used") or trace.get("history_trace_ids")):
            return None
    elif trace.get("persist_conversation") is not True:
        return None
    try:
        subject = identifier(trace.get("subject_id"), "subject_id")
    except ValidationError:
        return None
    conversation = trace.get("conversation_id")
    if conversation is None:
        conversation = trace["trace_id"]
    identifier(conversation, "conversation_id")
    # Undated legacy rows sort first. Never copy arbitrary strings into the index.
    started_at = ""
    try:
        instant = datetime.fromisoformat(trace["started_at"])
        if instant.utcoffset() is not None:
            started_at = instant.astimezone(timezone.utc).isoformat()
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return trace["tenant_id"], trace["trace_id"], subject, conversation, started_at


def index_trace(connection, trace, placeholder):
    row = _metadata(trace)
    if row is not None:
        # Both save paths already own a write lock (SQLite DB / PG tenant).
        # A conflicting append rolls back its envelope in this same transaction.
        full = """SELECT 1 FROM trace_history WHERE tenant_id=? AND subject_id=? AND conversation_id=?
                  LIMIT 1 OFFSET ?"""
        if connection.execute(full.replace("?", placeholder),
                              (row[0], row[2], row[3], MAX_CONVERSATION_TURNS - 1)).fetchone():
            raise ConversationFullError()
        connection.execute(_INSERT.replace("?", placeholder), row)


def _backfill_rows(rows):
    for row in rows:
        try:
            trace = json.loads(row["payload"])
            if not isinstance(trace, dict):
                continue
            if any(identifier(trace.get(k), k) != (row[k].decode("utf-8") if isinstance(row[k], bytes) else row[k])
                   for k in ("tenant_id", "trace_id")):
                continue
            metadata = _metadata(trace, legacy=True)
        except (ValidationError, ValueError, TypeError, RecursionError):
            # Isolate malformed envelopes; never log contents or suppress SQL errors.
            continue
        if metadata is not None:
            yield metadata


def backfill_history(connection, *, postgres, indexed_source=None):
    # Avoid fetching all historical payloads into the process on either backend.
    if indexed_source is not None:
        reader = indexed_source.cursor()
    else:
        reader = connection.cursor(name="history_backfill") if postgres else connection.cursor()
    writer = connection.cursor()
    try:
        # A v2 import preserves membership, but derives owner/group from the envelope.
        members = " JOIN trace_history USING (tenant_id,trace_id)" if indexed_source is not None else ""
        if postgres and indexed_source is None:
            reader.execute("SELECT tenant_id,trace_id,envelope_json AS payload FROM traces")
        else:
            # SQLite TEXT can hold invalid UTF-8. Decode per row, not during cursor iteration.
            reader.execute("SELECT CAST(tenant_id AS BLOB) AS tenant_id, CAST(trace_id AS BLOB) AS trace_id, "
                           "CAST(envelope_json AS BLOB) AS payload FROM traces" + members +
                           " WHERE typeof(traces.tenant_id)='text' AND typeof(traces.trace_id)='text' "
                           "AND typeof(envelope_json)='text'")
        rows = _backfill_rows(reader)
        statement = _INSERT.replace("?", "%s") if postgres else _INSERT
        while batch := list(islice(rows, 256)):
            writer.executemany(statement, batch)
    finally:
        reader.close()
        writer.close()


def initialize_history(connection, version, *, postgres=False):
    # Under the schema lock. A missing v2 index is not permission to backfill:
    # absence from it may be intentional even for an unmarked envelope.
    if version == STORAGE_VERSION:
        connection.execute("SELECT envelope_json FROM traces JOIN trace_history USING (tenant_id,trace_id) LIMIT 0")
        return
    # Baseline get/save/retention all read value_json, including on warm adapters.
    connection.execute("ALTER TABLE traces RENAME COLUMN value_json TO envelope_json")
    connection.execute("DROP TABLE IF EXISTS trace_history")  # pre-release v1 index
    connection.execute(_TABLE)
    backfill_history(connection, postgres=postgres)
    connection.execute(_INDEX)
    connection.execute(f"UPDATE schema_metadata SET version={STORAGE_VERSION} WHERE id=1")


class TraceHistory:
    _placeholder = "?"

    def list_conversations(self, tenant_id: str, subject_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        identifier(tenant_id, "tenant_id")
        identifier(subject_id, "subject_id")
        if any(type(n) is not int or not 0 <= n < 2**63 for n in (limit, offset)):
            raise ValidationError("limit and offset must be nonnegative integers")
        query = """
            WITH page AS (
                SELECT tenant_id, subject_id, conversation_id, MAX(started_at) AS updated_at
                FROM trace_history WHERE tenant_id=? AND subject_id=?
                GROUP BY tenant_id, subject_id, conversation_id
                ORDER BY updated_at DESC, conversation_id LIMIT ? OFFSET ?
            )
            SELECT conversation_id, (SELECT h.trace_id FROM trace_history h
                WHERE h.tenant_id=p.tenant_id AND h.subject_id=p.subject_id AND h.conversation_id=p.conversation_id
                ORDER BY h.started_at, h.trace_id LIMIT 1) AS trace_id, updated_at
            FROM page p ORDER BY updated_at DESC, conversation_id"""
        with self._connection() as connection:
            rows = connection.execute(query.replace("?", self._placeholder), (tenant_id, subject_id, limit, offset))
            return [dict(row) for row in rows]

    def conversation_turns(self, tenant_id: str, subject_id: str, conversation_id: str, *,
                           limit: int = MAX_CONVERSATION_TURNS + 1) -> list[str]:
        identifier(tenant_id, "tenant_id")
        identifier(subject_id, "subject_id")
        identifier(conversation_id, "conversation_id")
        if type(limit) is not int or not 0 <= limit <= MAX_CONVERSATION_TURNS + 1:
            raise ValidationError("invalid conversation turn read limit")
        query = """SELECT trace_id FROM trace_history WHERE tenant_id=? AND subject_id=? AND conversation_id=?
                   ORDER BY started_at, trace_id LIMIT ?"""
        with self._connection() as connection:
            rows = connection.execute(query.replace("?", self._placeholder), (tenant_id, subject_id, conversation_id, limit))
            return [row["trace_id"] for row in rows]
