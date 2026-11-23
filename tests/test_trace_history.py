"""Conversation index contract; only temporary SQLite stores or isolated PG test schemas."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from adaptive_rag.config import Principal
from adaptive_rag.domain import CapabilityError, ConflictError, ConversationFullError, NotFoundError, ValidationError
from adaptive_rag.postgres import PostgresKnowledgeStore, PostgresTraceSink, _driver, migrate_sqlite_to_postgres
from adaptive_rag.retention import Database, QUEUE, purge_docs
from adaptive_rag.storage import SQLiteKnowledgeStore, SQLiteTraceSink
from test_storage import prepared


FIRST = "2025-04-01T00:00:00+00:00"
LAST = "2025-04-02T00:00:00+00:00"


def turn(tid, **kw):
    return dict(tenant_id="A", subject_id="alice", trace_id=tid, started_at=FIRST, persist_conversation=True,
                question="private question", answer="private answer") | kw


class HistoryContract:
    def v1_layout(self, con):
        con.execute("DROP TABLE trace_history")
        con.execute("ALTER TABLE traces RENAME COLUMN envelope_json TO value_json")
        con.execute("UPDATE schema_metadata SET version=1 WHERE id=1")

    def test_baseline_adapter_is_fenced_at_startup_and_after_warm_upgrade(self):
        # Exercise actual pre-conversation adapter code when repository history is available.
        module = "storage" if self.ph == "?" else "postgres"
        path = f"80cfc7c:adaptive_rag/{module}.py"
        try:
            source = subprocess.check_output(["git", "show", path], cwd=Path(__file__).resolve().parents[1],
                                             stderr=subprocess.DEVNULL)
        except (FileNotFoundError, subprocess.CalledProcessError):
            self.skipTest("baseline commit unavailable")
        baseline = {"__name__": f"adaptive_rag.baseline_{module}", "__package__": "adaptive_rag"}
        exec(compile(source, path, "exec"), baseline)
        if module == "storage":
            old = lambda: baseline["SQLiteTraceSink"](self.sink.path.parent)
            read_error = sqlite3.OperationalError
        else:
            old = lambda: baseline["PostgresTraceSink"](self.dsn, schema=self.schema)
            read_error = CapabilityError
        with self.write() as con:
            self.v1_layout(con)
        warm = old()
        v1 = {"tenant_id": "A", "trace_id": "before", "subject_id": "alice"}
        warm.save(v1)
        self.assertEqual(warm.get("A", "before"), v1)
        current = self.reopen()
        current.save(turn("after"))
        self.assertEqual(current.conversation_turns("A", "alice", "before"), ["before"])
        with self.assertRaises(ConflictError):
            old()
        with self.assertRaises(read_error):
            warm.get("A", "after")
        with self.assertRaises(read_error):
            warm.save(turn("stale"))
        with self.assertRaises(NotFoundError):
            current.get("A", "stale")

    def test_opt_out_and_unmarked_live_writes_remain_diagnostic_only(self):
        for tid, marker in [("false", False), ("null", None), ("string", "true"), ("one", 1), ("absent", None)]:
            trace = turn(tid, persist_conversation=marker, conversation_id="private")
            if tid == "absent":
                del trace["persist_conversation"]
            self.sink.save(trace)
            self.assertEqual(self.sink.get("A", tid), trace)
        self.assertEqual(self.reopen().list_conversations("A", "alice", 10, 0), [])
        self.assertEqual(self.sink.conversation_turns("A", "alice", "private"), [])

    def test_failed_backfill_leaves_old_schema_and_payload_intact(self):
        trace = {"tenant_id": "A", "trace_id": "before", "subject_id": "alice"}
        with self.write() as con:
            self.v1_layout(con)
            con.execute("INSERT INTO traces VALUES (?,?,?)".replace("?", self.ph), ("A", "before", json.dumps(trace)))
        with patch("adaptive_rag.trace_history.backfill_history", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.reopen()
        with self.write() as con:
            self.assertEqual(con.execute("SELECT version FROM schema_metadata").fetchone()["version"], 1)
            self.assertEqual(json.loads(con.execute("SELECT value_json FROM traces").fetchone()["value_json"]), trace)
        self.assertEqual(self.reopen().conversation_turns("A", "alice", "before"), ["before"])

    def test_racing_last_turn_is_atomic_and_idempotent(self):
        for n in range(63):
            self.sink.save(turn(f"t{n:02}", conversation_id="chat"))
        ready = Barrier(2)

        def append(key):
            ready.wait(timeout=10)
            try:
                self.sink.save(turn(key, conversation_id="chat"))
            except ConversationFullError:
                return False
            return True

        keys = ("x", "y")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(append, keys))
        self.assertCountEqual(results, [True, False])
        self.sink.save(turn(keys[results.index(True)], conversation_id="chat"))
        with self.assertRaises(NotFoundError):
            self.sink.get("A", keys[results.index(False)])
        self.assertEqual(len(self.sink.conversation_turns("A", "alice", "chat", limit=65)), 64)
        self.assertEqual(self.sink.conversation_turns("A", "alice", "chat", limit=1), ["t00"])
        self.sink.save(turn("other", subject_id="bob", conversation_id="chat"))

    def test_groups_in_time_order_without_copying_content(self):
        sink = self.sink
        sink.save(turn("last", started_at=LAST), conversation_id="group")
        sink.save(turn("title", conversation_id="group", started_at="2025-04-01T01:00:00+02:00"))
        sink.save(turn("middle", conversation_id="group"))
        sink.save(turn("middle"), conversation_id="group")  # same immutable trace
        with self.assertRaises(ConflictError):
            sink.save(turn("middle"), conversation_id="other")
        sink.save(turn("single"))
        sink.save(turn("z", conversation_id="z", started_at=LAST))
        expected = [dict(conversation_id="group", trace_id="title", updated_at=LAST),
                    dict(conversation_id="z", trace_id="z", updated_at=LAST),
                    dict(conversation_id="single", trace_id="single", updated_at=FIRST)]
        self.assertEqual(sink.list_conversations("A", "alice", 3, 0), expected)
        self.assertEqual(sink.list_conversations("A", "alice", 1, 1), expected[1:2])
        self.assertEqual(sink.list_conversations("A", "alice", 5, 3), [])
        self.assertEqual(sink.conversation_turns("A", "alice", "group"), ["title", "middle", "last"])
        with sink._connection() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM trace_history")]
        self.assertEqual(len(rows), 5)
        for row in rows:
            self.assertEqual(set(row), {"tenant_id", "trace_id", "subject_id", "conversation_id", "started_at"})
            self.assertNotIn("private", repr(row))

    def test_same_group_is_scoped_and_previews_never_count(self):
        sink = self.sink
        for trace in [turn("one"), turn("two", subject_id="bob"), turn("three", tenant_id="B"),
                      turn("preview", candidate_preview=True), turn("blank", subject_id=""),
                      turn("invalid", subject_id=["alice"])]:
            sink.save(trace, conversation_id="same")
        trace = turn("forged", request={"subject_id": "alice"}, username="alice")
        del trace["subject_id"]
        sink.save(trace, conversation_id="same")
        for tenant, subject, ids in [("A", "alice", ["one"]), ("A", "bob", ["two"]),
                                     ("B", "alice", ["three"]), ("B", "bob", [])]:
            self.assertEqual(sink.conversation_turns(tenant, subject, "same"), ids)
            self.assertEqual([r["trace_id"] for r in sink.list_conversations(tenant, subject, 10, 0)], ids)

    def test_cascade_promotes_surviving_title_and_drops_empty_group(self):
        sink = self.sink
        for tid in ["b", "a"]:  # identical timestamp; trace_id breaks ties
            sink.save(turn(tid, conversation_id="same"))
        self.assertEqual(sink.conversation_turns("A", "alice", "same"), ["a", "b"])
        for tid, survivors in [("a", ["b"]), ("b", [])]:
            with self.write() as con:
                con.execute("DELETE FROM traces WHERE tenant_id='A' AND trace_id=?".replace("?", self.ph), (tid,))
            self.assertEqual(sink.conversation_turns("A", "alice", "same"), survivors)
            self.assertEqual([r["trace_id"] for r in sink.list_conversations("A", "alice", 9, 0)], survivors)

    def test_old_schema_backfills_only_authoritative_subjects_on_first_open(self):
        with self.write() as con:
            self.v1_layout(con)
            for trace in [turn("old"), turn("draft", candidate_preview=True), turn("nested", user={"subject_id":"alice"}),
                          turn("wrong", tenant_id="B"), turn("undated", started_at="private note"),
                          turn("manual", history_turns=2), turn("foreign", conversation_id="old"),
                          turn("optout", persist_conversation=False), turn("bad id")]:
                if trace["trace_id"] != "optout":
                    del trace["persist_conversation"]
                if trace["trace_id"] == "nested":
                    del trace["subject_id"]
                con.execute("INSERT INTO traces VALUES (?,?,?)".replace("?", self.ph),
                            ("A", trace["trace_id"], json.dumps(trace)))
            for key, payload in [("invalid", "not json"), ("array", "[]"), ("deep", "[" * 2000 + "]" * 2000)]:
                con.execute("INSERT INTO traces VALUES (?,?,?)".replace("?", self.ph), ("A", key, payload))
        sink = self.reopen()
        self.assertEqual(sink.list_conversations("A", "alice", 10, 0), [
            dict(conversation_id="old", trace_id="old", updated_at=FIRST),
            dict(conversation_id="undated", trace_id="undated", updated_at="")])
        with patch("adaptive_rag.trace_history.backfill_history", side_effect=AssertionError("duplicate backfill")):
            self.assertEqual(self.reopen().conversation_turns("A", "alice", "old"), ["old"])

    def test_document_retention_removes_only_affected_history(self):
        store = self.knowledge_store()
        store.stage(prepared(tenant="A", doc="expired"))
        trash = store.trash_document(Principal("A", "admin", admin=True), "expired", None)
        self.sink.save(turn("title", document_id="expired"), conversation_id="chat")
        self.sink.save(turn("survivor", started_at=LAST), conversation_id="chat")
        with self.write() as con:
            db = Database(con, postgres=self.ph == "%s")
            db.sql(f"CREATE TABLE {QUEUE} (digest TEXT PRIMARY KEY)")
            self.assertEqual(purge_docs(db, None, datetime.fromisoformat(trash["purge_after"]).timestamp()), 1)
        self.assertEqual(self.sink.conversation_turns("A", "alice", "chat"), ["survivor"])
        self.assertEqual(self.sink.list_conversations("A", "alice", 3, 0), [
            dict(conversation_id="chat", trace_id="survivor", updated_at=LAST)])

    def test_invalid_input_does_not_change_history(self):
        for kw in [{"conversation_id": "not an id"}, {"conversation_id": "different"}]:
            with self.assertRaises(ValidationError):
                self.sink.save(turn("t", conversation_id="original"), **kw)
        with self.assertRaises(ValidationError):
            self.sink.save(turn("t", conversation_id="not an id"))
        for limit, offset in [(-1, 0), (1, -1), (True, 0)]:
            with self.assertRaises(ValidationError):
                self.sink.list_conversations("A", "alice", limit, offset)
        self.assertEqual(self.sink.list_conversations("A", "alice", 10, 0), [])


class SQLiteHistoryTests(HistoryContract, unittest.TestCase):
    ph = "?"

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.reopen = lambda: SQLiteTraceSink(Path(temp.name))
        self.knowledge_store = lambda: SQLiteKnowledgeStore(Path(temp.name))
        self.sink = self.reopen()
        self.write = lambda: self.sink._connection(write=True)

    def test_invalid_utf8_in_legacy_text_does_not_prevent_startup(self):
        with self.write() as con:
            self.v1_layout(con)
            con.execute("INSERT INTO traces VALUES ('A','corrupt',CAST(x'ff' AS TEXT))")
            con.execute("INSERT INTO traces VALUES ('A',CAST(x'ff' AS TEXT),?)", (json.dumps(turn("other")),))
            con.execute("INSERT INTO traces VALUES ('A','valid',?)", (json.dumps(turn("valid")),))
        self.assertEqual([r["trace_id"] for r in self.reopen().list_conversations("A", "alice", 10, 0)], ["valid"])

    def test_history_queries_use_covering_index_without_reading_traces(self):
        connect = self.sink._connection
        statements = []

        @contextmanager
        def capture():
            with connect() as con:
                con.set_trace_callback(statements.append)
                yield con

        with patch.object(self.sink, "_connection", capture):
            self.sink.list_conversations("A", "alice", 5, 0)
            self.sink.conversation_turns("A", "alice", "same")
        queries = [s for s in statements if "SELECT" in s]
        self.assertEqual(len(queries), 2)
        with connect() as con:
            for query in queries:
                plan = [r["detail"] for r in con.execute("EXPLAIN QUERY PLAN " + query)]
                self.assertTrue(any("SEARCH" in p and "COVERING INDEX trace_history_turns" in p for p in plan), plan)
                self.assertNotIn("traces", query)


@unittest.skipUnless(os.environ.get("RAG_TEST_POSTGRES_DSN"), "requires isolated PostgreSQL test DSN")
class PostgresHistoryTests(HistoryContract, unittest.TestCase):
    ph = "%s"

    def setUp(self):
        self.dsn = os.environ["RAG_TEST_POSTGRES_DSN"]
        self.schema = "rag_test_" + uuid4().hex
        self.reopen = lambda: PostgresTraceSink(self.dsn, schema=self.schema)
        self.knowledge_store = lambda: PostgresKnowledgeStore(self.dsn, schema=self.schema)
        self.sink = self.reopen()
        self.addCleanup(self.drop_schema)
        self.write = self.sink._connection

    def drop_schema(self):
        psycopg, sql, _ = _driver()
        with psycopg.connect(self.dsn) as con:
            con.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema)))

    def test_import_rebuilds_history_from_legacy_sqlite(self):
        with TemporaryDirectory() as temp:
            source = SQLiteTraceSink(Path(temp))
            source.save(turn("first"))
            source.save(turn("second", started_at=LAST), conversation_id="first")
            with source._connection(write=True) as con:
                self.v1_layout(con)
                legacy = turn("old")
                del legacy["persist_conversation"]
                con.execute("INSERT INTO traces VALUES (?,?,?)", ("A", "old", json.dumps(legacy)))
            migrate_sqlite_to_postgres(source.path, self.dsn, schema=self.schema)
        self.assertEqual(self.sink.conversation_turns("A", "alice", "first"), ["first", "second"])
        self.assertEqual(self.sink.conversation_turns("A", "alice", "old"), ["old"])

    def test_v2_import_does_not_reclassify_unmarked_diagnostics(self):
        with TemporaryDirectory() as temp:
            source = SQLiteTraceSink(Path(temp))
            unmarked = turn("diagnostic")
            del unmarked["persist_conversation"]
            source.save(unmarked)
            source.save(turn("group"))
            source.save(turn("follow", conversation_id="group"))
            migrate_sqlite_to_postgres(source.path, self.dsn, schema=self.schema)
        self.assertEqual(self.sink.conversation_turns("A", "alice", "group"), ["follow", "group"])
        self.assertEqual(self.sink.conversation_turns("A", "alice", "diagnostic"), [])
        self.assertEqual(self.sink.get("A", "diagnostic"), unmarked)
