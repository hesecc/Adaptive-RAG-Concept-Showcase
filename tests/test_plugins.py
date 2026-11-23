"""Exercise replacement adapters through the public application interface."""

from dataclasses import replace
from hashlib import sha256
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from adaptive_rag.app import RagApplication
from adaptive_rag.config import Settings
from adaptive_rag.domain import AccessContext, IngestRequest, QueryRequest, ValidationError
from adaptive_rag.plugins import PluginRegistry, default_registry


class RecordingAdapter:
    """Delegate to real adapters while exposing which seam was crossed."""

    def __init__(self, role, delegate, calls):
        self.role = role
        self.delegate = delegate
        self.calls = calls

    def __getattr__(self, name):
        member = getattr(self.delegate, name)
        if not callable(member):
            return member
        if inspect.iscoroutinefunction(member):
            async def invoke_async(*args, **kwargs):
                self.calls.append((self.role, name))
                return await member(*args, **kwargs)
            return invoke_async

        def invoke(*args, **kwargs):
            self.calls.append((self.role, name))
            return member(*args, **kwargs)
        return invoke


class PluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(data_dir=Path(self.directory.name))

    async def test_every_required_role_runs_through_its_injected_adapter(self):
        expected_methods = {
            "parser": {"parse"},
            "cleaner": {"clean"},
            "chunker": {"chunk"},
            "store": {"stage", "get_job", "publish", "open_snapshot", "list_chunks", "get_parents"},
            "objects": {"put", "get"},
            "trace": {"save", "get", "list_conversations", "conversation_turns"},
            "decision": {"decide"},
            "planner": {"plan"},
            "lexical": {"search"},
            "fusion": {"fuse"},
            "reranker": {"rank"},
            "judge": {"assess"},
            "verifier": {"verify"},
        }
        calls, created, adapters = [], [], {}
        registry = default_registry()
        for role in expected_methods:
            default = registry.spec(role, self.settings.plugins[role])

            def factory(settings, selected_role=role, selected_spec=default):
                created.append(selected_role)
                adapter = RecordingAdapter(selected_role, selected_spec.factory(settings), calls)
                adapters[selected_role] = adapter
                return adapter

            registry.register(role, "recording", factory)
        settings = replace(self.settings, plugins={role: "recording" for role in expected_methods})
        application = RagApplication(settings, registry)
        self.assertCountEqual(created, expected_methods)
        for role in expected_methods:
            with self.subTest(role=role):
                self.assertIs(application.components[role], adapters[role])

        source = "# Amber warranty\nThe amber warranty lasts twelve months."
        staged = application.ingest(IngestRequest("shop", "warranty", "v1", source))
        self.assertEqual(application.get_job("shop", staged.job_id)["job_id"], staged.job_id)
        snapshot = application.publish("shop", staged.job_id, None)
        self.assertEqual(application.objects.get(sha256(source.encode("utf-8")).hexdigest()), source.encode("utf-8"))
        access = AccessContext("shop", "reader")
        result = await application.query(QueryRequest(access, "amber warranty", persist_conversation=True))
        self.assertEqual(result.status, "evidence_preview")
        self.assertEqual(result.generation, snapshot.generation)
        self.assertIsNone(result.answer)
        self.assertTrue(result.evidence)
        self.assertEqual(result.evidence[0].document_id, "warranty")
        evidence = application.get_evidence(access, snapshot.generation, result.evidence[0].evidence_id)
        self.assertEqual(evidence.text, result.evidence[0].text)
        self.assertEqual(evidence.source_hash, result.evidence[0].source_hash)
        trace = application.get_trace("shop", result.trace_id)
        self.assertEqual(application.conversations.page(access, 1, 0)["items"][0]["conversation_id"], result.trace_id)
        self.assertEqual(trace["status"], result.status)
        self.assertEqual(trace["generation"], snapshot.generation)
        self.assertTrue(trace["retrieved_chunk_ids"])
        self.assertEqual((trace["model_calls"], trace["network_calls"]), (0, 0))
        self.assertFalse(trace["verification"]["support_checked"])
        for role, methods in expected_methods.items():
            with self.subTest(role=role):
                self.assertTrue(methods.issubset({method for called_role, method in calls if called_role == role}),
                                f"Injected {role} did not receive expected calls: {methods}")

    def test_incomplete_persistence_adapters_fail_during_application_initialization(self):
        required = {
            "store": ("stage", "get_job", "publish", "open_snapshot", "list_chunks",
                      "get_parents", "delete_document", "rollback", "set_access"),
            "objects": ("put", "get"),
            "trace": ("save", "get", "list_conversations", "conversation_turns"),
        }
        for role, methods in required.items():
            for missing in methods:
                with self.subTest(role=role, missing=missing):
                    def unexpected_call(*args, **kwargs):
                        self.fail("Adapter methods must not run while their interface is validated")

                    partial = type("IncompleteAdapter", (), {
                        method: unexpected_call for method in methods if method != missing
                    })()
                    registry = default_registry()
                    registry.register(role, "incomplete", lambda settings, value=partial: value)
                    with self.assertRaises(ValidationError) as caught:
                        RagApplication(replace(self.settings, plugins={role: "incomplete"}), registry)
                    self.assertIn(role, str(caught.exception))
                    self.assertIn(missing, str(caught.exception))

    def test_duplicate_registration_is_rejected(self):
        registry = PluginRegistry()
        registry.register("parser", "fixture", lambda settings: object())
        with self.assertRaises(ValidationError):
            registry.register("parser", "fixture", lambda settings: object())

    def test_unknown_selector_is_rejected_before_any_factory_runs(self):
        registry = default_registry()

        def unexpected_factory(settings):
            self.fail("All selectors must be checked before the first factory runs")

        registry.register("parser", "observed", unexpected_factory)
        with self.assertRaises(ValidationError) as caught:
            RagApplication(replace(self.settings, plugins={"parser": "observed", "verifier": "missing"}), registry)
        self.assertIn("unknown plugin", str(caught.exception))

    def test_required_roles_cannot_be_disabled(self):
        for role in ("parser", "cleaner", "chunker", "store", "objects", "trace",
                     "decision", "planner", "lexical", "fusion", "reranker", "judge", "verifier"):
            with self.subTest(role=role):
                with self.assertRaises(ValidationError):
                    PluginRegistry().register(role, "disabled", lambda settings: None, enabled=False)


if __name__ == "__main__":
    unittest.main()
