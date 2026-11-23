"""Storage configuration must not implicitly enable paid model calls."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from adaptive_rag.config import Settings
from adaptive_rag.domain import ValidationError
from adaptive_rag.environment import apply_environment
from adaptive_rag.infrastructure import InfrastructureSettings


class InfrastructureTests(unittest.TestCase):
    def test_remote_requires_explicit_permission_but_does_not_enable_models(self):
        with TemporaryDirectory() as directory:
            base = Settings(data_dir=Path(directory))
            values = {"RAG_STORE_BACKEND": "postgres", "RAG_POSTGRES_DSN": "postgresql://rag:synthetic-secret@localhost/rag"}
            with self.assertRaises(ValidationError):
                apply_environment(base, values)
            result = apply_environment(base, values | {"RAG_ENABLE_REMOTE_STORAGE": "true"})
            self.assertTrue(result.allow_network)
            self.assertFalse(result.allow_models)
            self.assertEqual(result.plugins["store"], "postgres")
            self.assertEqual(result.plugins["dense"], "disabled")
            self.assertNotIn("synthetic-secret", repr(result))
            self.assertNotIn("synthetic-secret", repr(result.infrastructure))

    def test_invalid_endpoint_does_not_echo_secret(self):
        for value in ("http://user:synthetic-secret@localhost", "https://localhost/path", "file:///data"):
            with self.assertRaises(ValidationError) as caught:
                InfrastructureSettings(milvus_endpoint=value)
            self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_local_selection_keeps_network_disabled(self):
        with TemporaryDirectory() as directory:
            result = apply_environment(Settings(data_dir=Path(directory)), {"RAG_ASSET_VECTOR_BACKEND": "sqlite"})
            self.assertFalse(result.allow_network)
            self.assertFalse(result.allow_models)

    def test_storage_credentials_do_not_silently_replace_configured_backends(self):
        base = Settings(plugins={"store": "postgres", "trace": "postgres"})
        result = apply_environment(base, {"RAG_POSTGRES_DSN": "postgresql://localhost/test",
                                          "RAG_ENABLE_REMOTE_STORAGE": "true"})
        self.assertEqual(result.plugins["store"], "postgres")
        self.assertEqual(result.plugins["trace"], "postgres")
        self.assertEqual(result.plugins["objects"], "file")

    def test_remote_switch_is_validated_even_without_storage_configuration(self):
        with self.assertRaises(ValidationError):
            apply_environment(Settings(), {"RAG_ENABLE_REMOTE_STORAGE": "maybe"})

    def test_asset_configuration_preserves_trusted_custom_store(self):
        base = Settings(plugins={"store": "custom"})
        result = apply_environment(base, {"RAG_ASSET_VECTOR_BACKEND": "sqlite"})
        self.assertEqual(result.plugins["store"], "custom")

    def test_storage_selections_survive_explicit_cloud_activation(self):
        with TemporaryDirectory() as directory:
            result = apply_environment(Settings(data_dir=Path(directory)), {
                "RAG_ENABLE_REMOTE_STORAGE": "true", "RAG_STORE_BACKEND": "postgres",
                "RAG_POSTGRES_DSN": "postgresql://localhost/rag", "RAG_ENABLE_CLOUD": "true",
                "RAG_CLOUD_BASE_URL": "https://models.example.test/v1", "RAG_CLOUD_API_KEY": "synthetic-key",
                "RAG_LLM_MODEL": "llm", "RAG_EMBEDDING_MODEL": "embed", "RAG_RERANK_MODEL": "rank"})
            self.assertEqual(result.plugins["store"], "postgres")
            self.assertEqual(result.plugins["dense"], "cloud")


if __name__ == "__main__":
    unittest.main()
