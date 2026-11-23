"""Whole-thread authorization and work limits over a real local trace index."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from adaptive_rag.conversations import ConversationHistory, MAX_CONVERSATION_TURNS
from adaptive_rag.domain import AccessContext, ConflictError, ConversationFullError, NotFoundError
from adaptive_rag.http_inputs import QueryInput
from adaptive_rag.storage import SQLiteTraceSink


class ConversationLimitsTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sink = SQLiteTraceSink(Path(directory.name))
        self.access = AccessContext("tenant", "alice")
        self.body = QueryInput(question="follow up", conversation_id="group")
        self.revoked = set()
        self.read = Mock(wraps=self.sink.get)
        self.history = ConversationHistory(SimpleNamespace(trace_sink=self.sink, get_trace=self.read,
                                                          _source_checked_trace=self.check_sources))

    def check_sources(self, access, key, trace, scopes):
        if key in self.revoked:
            raise NotFoundError("trace not found")

    def save(self, key, parents=(), group="group", **fields):
        trace = dict(tenant_id="tenant", subject_id="alice", trace_id=key, question=key, persist_conversation=True,
                     status="answered", final_result={"answer": key}, history_trace_ids=list(parents),
                     history_turns=2 * len(parents))
        self.sink.save(trace | fields, conversation_id=group)

    def request(self):
        return self.history.request(self.access, self.body)

    def test_revoked_middle_hides_whole_group_without_losing_next_page(self):
        self.save("a")
        self.save("b", status="failed")
        self.save("c")  # No ancestry leads to the failed turn.
        self.save("z", group="other")
        self.revoked.add("b")
        first = self.history.page(self.access, 1, 0)
        self.assertEqual(first, {"items": [], "has_more": True, "next_offset": 1})
        following = self.history.page(self.access, 1, first["next_offset"])
        self.assertEqual([row["title"] for row in following["items"]], ["z"])
        with self.assertRaises(NotFoundError):
            self.history.turns(self.access, "group")
        with self.assertRaises(NotFoundError):
            self.request()
        self.assertEqual(self.history.checked(self.access, "a")["trace_id"], "a")

    def test_recheck_reauthorizes_new_descendants_and_requires_materialized_ids(self):
        self.save("a")
        self.save("b")
        ids = self.request().history_trace_ids
        self.assertEqual(ids, ("a", "b"))
        self.assertIsNone(self.history.recheck(self.access, "group", ids))
        self.save("c", ("b",))
        self.revoked.add("c")
        with self.assertRaises(NotFoundError):
            self.history.recheck(self.access, "group", ids)
        self.revoked.clear()
        with self.sink._connection(write=True) as conn:
            conn.execute("DELETE FROM traces WHERE tenant_id='tenant' AND trace_id='a'")
        self.assertIsNone(self.history.recheck(self.access, "group"))
        with self.assertRaises(NotFoundError):
            self.history.recheck(self.access, "group", ids)

    def test_page_budget_resumes_at_first_unchecked_candidate(self):
        for group in ("a", "b", "c"):
            self.save(group + "1", group=group)
            self.save(group + "2", group=group)
        with patch("adaptive_rag.conversations.MAX_PAGE_TURN_READS", 5):
            first = self.history.page(self.access, 3, 0)
            self.assertEqual(self.read.call_count, 4)
            second = self.history.page(self.access, 3, first["next_offset"])
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual([row["conversation_id"] for row in first["items"] + second["items"]], ["a", "b", "c"])

    def test_overlapping_dependencies_authorized_once_even_out_of_index_order(self):
        self.save("d")
        self.save("b", ("d",))
        self.save("c", ("d",))
        self.save("a", ("b", "c"))
        for operation in (lambda: self.history.checked(self.access, "a"), self.request,
                          lambda: self.history.turns(self.access, "group"),
                          lambda: self.history.page(self.access, 1, 0),
                          lambda: self.history.recheck(self.access, "group")):
            self.read.reset_mock()
            operation()
            self.assertCountEqual([call.args[1] for call in self.read.call_args_list], "abcd")
        self.revoked.add("d")
        with self.assertRaises(NotFoundError):
            self.history.checked(self.access, "a")

    def test_append_while_authorizing_does_not_release_a_checked_prefix(self):
        self.save("a")

        def append_during_read(tenant, key):
            self.save("b", ("a",))
            self.revoked.add("b")
            return self.sink.get(tenant, key)

        self.read.side_effect = append_during_read
        with self.assertRaises(NotFoundError):
            self.history.recheck(self.access, "group", ("a",))
        self.read.assert_called_once_with("tenant", "a")

    def test_missing_parent_and_overwide_metadata_do_not_trigger_unbounded_lookups(self):
        self.save("outside", group="other")
        self.save("a", ("outside",))
        self.save("b", ("a",) * 100)
        for key in ("a", "b"):
            self.read.reset_mock()
            with self.assertRaises(NotFoundError):
                self.history.checked(self.access, key)
            self.read.assert_called_once_with("tenant", key)
        with self.assertRaises(NotFoundError):
            self.history.recheck(self.access, "group")

    def test_concurrent_continuations_cannot_make_oversized_prefix_readable(self):
        # Both requests materialize their inputs before either result is saved.
        for i in range(MAX_CONVERSATION_TURNS - 1):
            self.save(f"a{i:03}")
        left, right = self.request(), self.request()
        self.save("left", left.history_trace_ids)
        self.assertEqual(len(self.history.turns(self.access, "group")), MAX_CONVERSATION_TURNS)
        with self.assertRaises(ConversationFullError):
            self.request()
        with self.assertRaises(ConflictError):
            self.save("right", right.history_trace_ids)
        with self.assertRaises(NotFoundError):
            self.sink.get("tenant", "right")
        # Simulate a database written before enforcement of the current cap.
        with patch("adaptive_rag.trace_history.MAX_CONVERSATION_TURNS", MAX_CONVERSATION_TURNS + 1):
            self.save("right", right.history_trace_ids)
        self.save("unrelated", group="other")
        self.read.reset_mock()
        with patch.object(self.sink, "conversation_turns", wraps=self.sink.conversation_turns) as members:
            for operation in (self.request, lambda: self.history.turns(self.access, "group"),
                              lambda: self.history.recheck(self.access, "group", right.history_trace_ids)):
                with self.assertRaises(NotFoundError):
                    operation()
            self.assertEqual(self.history.page(self.access, 1, 0)["items"], [])
            self.read.assert_not_called()
            with self.assertRaises(NotFoundError):
                self.history.checked(self.access, "left")
            self.read.assert_called_once_with("tenant", "left")
            self.assertTrue(all(call.kwargs["limit"] == MAX_CONVERSATION_TURNS + 1
                                for call in members.call_args_list))
