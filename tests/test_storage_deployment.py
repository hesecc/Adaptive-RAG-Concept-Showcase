"""Safety properties of the explicit offline knowledge migration entry point."""

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
from io import StringIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "migrate_knowledge.py"
spec = importlib.util.spec_from_file_location("rag_migrate_knowledge", SCRIPT)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


class MigrationCommandTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "knowledge.sqlite3"
        self.source.write_bytes(b"synthetic-offline-copy")
        self.environment = self.root / "migration.env"

    def call(self):
        stdout, stderr = StringIO(), StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            result = migration.main(["--source", str(self.source), "--env-file", str(self.environment)])
        self.assertEqual(self.source.read_bytes(), b"synthetic-offline-copy")
        return result, stdout.getvalue(), stderr.getvalue()

    def test_refuses_implicit_target_and_does_not_begin_migration(self):
        self.environment.write_text("RAG_POSTGRES_DSN=postgresql://target/test\n", encoding="utf-8")
        with patch.object(migration, "migrate_sqlite_to_postgres") as migrate:
            code, _, error = self.call()
        self.assertEqual(code, 2)
        migrate.assert_not_called()
        self.assertIn("RAG_ENABLE_REMOTE_STORAGE=true", error)

    def test_explicit_target_delegates_readonly_source_and_reports_counts(self):
        self.environment.write_text("RAG_ENABLE_REMOTE_STORAGE=true\nRAG_POSTGRES_DSN=postgresql://target/test\n", encoding="utf-8")
        with patch.object(migration, "migrate_sqlite_to_postgres", return_value={"revisions": 2}) as migrate:
            code, output, error = self.call()
        self.assertEqual(code, 0)
        self.assertIn('"revisions": 2', output)
        self.assertEqual(error, "")
        migrate.assert_called_once_with(self.source, "postgresql://target/test", schema="adaptive_rag")

    def test_unexpected_driver_error_never_exposes_target_secret(self):
        secret = "postgresql://operator:synthetic-secret@target/test"
        self.environment.write_text("RAG_ENABLE_REMOTE_STORAGE=true\nRAG_POSTGRES_DSN=" + secret + "\n", encoding="utf-8")
        with patch.object(migration, "migrate_sqlite_to_postgres", side_effect=RuntimeError(secret)):
            code, output, error = self.call()
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertNotIn("synthetic-secret", error)
        self.assertIn("migration_error", error)


if __name__ == "__main__":
    unittest.main()
