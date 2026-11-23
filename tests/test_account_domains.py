"""Account grants through the same persistent interface used by HTTP auth."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from adaptive_rag.auth import AccountStore
from adaptive_rag.config import Principal
from adaptive_rag.domain import ConflictError, ForbiddenError, NotFoundError, ValidationError
from support import ADMIN_PASSWORD


class AccountDomainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AccountStore(self.root, "company", ADMIN_PASSWORD)
        self.admin = Principal("company", "test-admin", admin=True)

    def create(self, username="alice", domains=("operations",)):
        return self.store.create(self.admin, username, "account-password", domains)

    def login(self, username="alice"):
        return self.store.login(username, "account-password")

    def test_admin_is_unrestricted_and_new_readers_default_to_public_only(self):
        _, admin = self.store.login("ragadmin", ADMIN_PASSWORD)
        self.assertIsNone(admin.principal.data_domains)
        self.assertIsNone(admin.principal.access.data_domains)
        self.assertIsNone(admin.public()["data_domains"])
        created = self.store.create(self.admin, "reader", "account-password")
        self.assertEqual(created["data_domains"], ["public"])
        _, reader = self.login("reader")
        self.assertEqual(reader.principal.data_domains, ("public",))
        self.assertFalse(reader.principal.admin)

    def test_multidomain_grants_are_normalized_and_do_not_become_identity_groups(self):
        created = self.create(domains=("operations", "finance", "operations"))
        self.assertEqual(created["data_domains"], ["finance", "operations", "public"])
        _, account = self.login()
        self.assertEqual(account.principal.access.data_domains, ("finance", "operations", "public"))
        self.assertEqual(account.principal.groups, ())
        self.assertEqual(account.principal.access.subjects, frozenset((account.principal.subject_id,)))

    def test_all_six_classifications_can_be_granted_together(self):
        domains = ("public", "operations", "finance", "product", "sales", "hr")
        created = self.create(domains=domains)
        self.assertEqual(set(created["data_domains"]), set(domains))
        self.assertEqual(set(self.login()[1].principal.data_domains), set(domains))

    def test_empty_grants_mean_public_and_invalid_grants_do_not_create_accounts(self):
        self.assertEqual(self.create(domains=())["data_domains"], ["public"])
        for domains in (("unknown",), ("*",), ("运营",), ("finance ",), (True,), "finance", ["finance"], None):
            with self.subTest(domains=domains), self.assertRaises(ValidationError):
                self.create("invalid-user", domains)
        self.assertNotIn("invalid-user", [row["username"] for row in self.store.users(self.admin)])

    def test_every_existing_session_reads_updated_grants_without_logging_in_again(self):
        self.create(domains=("operations", "finance"))
        first_token, first = self.login()
        second_token, _ = self.login()
        updated = self.store.set_domains(self.admin, "ALICE", ("sales",))
        self.assertEqual(updated["data_domains"], ["public", "sales"])
        for token in (first_token, second_token):
            account = self.store.session(token)
            self.assertEqual(account.principal.data_domains, ("public", "sales"))
            self.assertEqual(account.principal.subject_id, first.principal.subject_id)
        self.store.set_domains(self.admin, "alice", ())
        self.assertEqual(self.store.session(first_token).principal.data_domains, ("public",))

    def test_invalid_update_keeps_existing_grants_and_sessions(self):
        self.create()
        token, _ = self.login()
        with self.assertRaises(ValidationError):
            self.store.set_domains(self.admin, "alice", ("owner",))
        self.assertEqual(self.store.session(token).principal.data_domains, ("operations", "public"))

    def test_readers_and_other_tenants_cannot_assign_grants(self):
        self.create()
        _, reader = self.login()
        for principal in (reader.principal, Principal("another-company", "admin", admin=True)):
            with self.subTest(principal=principal), self.assertRaises(ForbiddenError):
                self.store.set_domains(principal, "alice", ("finance",))
            with self.assertRaises(ForbiddenError):
                self.store.create(principal, "bob", "account-password", ("finance",))
        self.assertEqual(self.login()[1].principal.data_domains, ("operations", "public"))

    def test_fixed_admin_cannot_be_restricted_and_missing_account_is_reported(self):
        for username in ("ragadmin", " RAGADMIN "):
            with self.subTest(username=username), self.assertRaises(ConflictError):
                self.store.set_domains(self.admin, username, ("operations",))
        with self.assertRaises(NotFoundError):
            self.store.set_domains(self.admin, "missing", ("operations",))
        self.assertIsNone(self.store.login("ragadmin", ADMIN_PASSWORD)[1].principal.data_domains)

    def test_grants_and_same_session_survive_store_restart(self):
        self.create()
        token, previous = self.login()
        self.store.set_domains(self.admin, "alice", ("hr", "product"))
        restarted = AccountStore(self.root, "company")
        account = restarted.session(token)
        self.assertEqual(account.principal.subject_id, previous.principal.subject_id)
        self.assertEqual(account.principal.data_domains, ("hr", "product", "public"))
        self.assertEqual([row["username"] for row in restarted.users(self.admin)].count("ragadmin"), 1)

    def legacy_store(self):
        """Materialize the old schema with real pre-existing credentials/session."""
        self.create()
        token, account = self.login()
        with closing(sqlite3.connect(self.store.path)) as source:
            account_rows = source.execute(
                "SELECT account_id,username,tenant_id,admin,password_salt,password_hash,created_at FROM accounts"
            ).fetchall()
            session_rows = source.execute("SELECT session_hash,account_id,expires_at FROM sessions").fetchall()
        legacy = self.root / "legacy"
        legacy.mkdir()
        with closing(sqlite3.connect(legacy / "accounts.sqlite3")) as connection:
            with connection:
                connection.executescript("""
                    CREATE TABLE accounts (
                        account_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                        tenant_id TEXT NOT NULL, admin INTEGER NOT NULL CHECK(admin IN (0,1)),
                        password_salt TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL
                    );
                    CREATE TABLE sessions (
                        session_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL, expires_at REAL NOT NULL,
                        FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
                    );
                """)
                connection.executemany("INSERT INTO accounts VALUES(?,?,?,?,?,?,?)", account_rows)
                connection.executemany("INSERT INTO sessions VALUES(?,?,?)", session_rows)
        return legacy, token, account.principal.subject_id

    def test_legacy_schema_upgrade_keeps_credentials_subjects_and_sessions(self):
        legacy, token, subject_id = self.legacy_store()
        upgraded = AccountStore(legacy, "company")
        account = upgraded.session(token)
        self.assertEqual(account.principal.subject_id, subject_id)
        self.assertEqual(account.principal.data_domains, ("public",))
        self.assertEqual(upgraded.login("alice", "account-password")[1].principal.subject_id, subject_id)
        self.assertIsNone(upgraded.login("ragadmin", ADMIN_PASSWORD)[1].principal.data_domains)
        self.assertEqual(len(upgraded.users(self.admin)), 2)

    def test_concurrent_legacy_upgrade_is_idempotent(self):
        legacy, token, subject_id = self.legacy_store()
        with ThreadPoolExecutor(max_workers=2) as executor:
            stores = list(executor.map(lambda _: AccountStore(legacy, "company"), range(2)))
        for store in stores:
            self.assertEqual(store.session(token).principal.subject_id, subject_id)
            self.assertEqual(store.session(token).principal.data_domains, ("public",))
        with closing(sqlite3.connect(legacy / "accounts.sqlite3")) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(accounts)")]
        self.assertEqual(columns.count("data_domains"), 1)

    def test_public_account_metadata_contains_no_password_session_or_private_identity(self):
        created = self.create(domains=("operations", "finance"))
        token, account = self.login()
        payload = json.dumps(created)
        for private in ("account-password", token, account.principal.subject_id):
            self.assertNotIn(private, payload)
        self.assertEqual(set(created), {"username", "admin", "created_at", "data_domains"})

    def test_corrupt_persisted_domain_json_does_not_grant_access(self):
        self.create()
        token, _ = self.login()
        for value in ("null", '"finance"', '{"finance": true}', "broken-json"):
            with closing(sqlite3.connect(self.store.path)) as connection:
                with connection:
                    connection.execute("UPDATE accounts SET data_domains=? WHERE username='alice'", (value,))
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.store.session(token)


if __name__ == "__main__":
    unittest.main()
