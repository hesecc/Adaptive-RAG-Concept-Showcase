"""Browser login and account management through their real HTTP interface."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from adaptive_rag.api import create_api
from adaptive_rag.auth import COOKIE_NAME
from adaptive_rag.config import Principal, Settings, load_settings
from adaptive_rag.domain import ConflictError, IngestRequest, ValidationError
from support import ADMIN_PASSWORD


class AccountHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(data_dir=self.root, browser_tenant="alpha", admin_password=ADMIN_PASSWORD)
        self.api = create_api(self.settings)
        self.admin = self.client()
        self.login(self.admin)

    def client(self, api=None):
        client = TestClient(api or self.api, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def login(self, client, username="ragadmin", password=ADMIN_PASSWORD):
        response = client.post("/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        return response

    def create_user(self, username="alice", password="alice-password"):
        response = self.admin.post("/admin/users", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(set(response.json()), {"username", "admin", "created_at", "data_domains"})
        self.assertFalse(response.json()["admin"])
        self.assertEqual(response.json()["data_domains"], ["public"])
        return response.json()

    def publish(self, allowed=()):
        app = self.api.state.application
        job = app.ingest(IngestRequest("alpha", "warranty", "v1", "# Warranty\nAmber warranty provides twelve months of coverage.", allowed_subjects=allowed,
                                         data_domains=("public",)))
        return app.publish("alpha", job.job_id, None)

    def test_fixed_admin_cookie_login_logout_and_no_password_disclosure(self):
        other = self.client()
        self.assertEqual(other.get("/console/session").status_code, 401)
        self.assertEqual(other.post("/query", json={"question": "warranty"}).status_code, 401)
        response = self.login(other, " RAGADMIN ")
        session = response.json()
        self.assertEqual(session["username"], "ragadmin")
        self.assertEqual(session["tenant_id"], "alpha")
        self.assertTrue(session["admin"])
        self.assertIn("capabilities", session)
        cookie = response.headers["set-cookie"]
        self.assertIn(COOKIE_NAME + "=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn("sj_session", cookie)
        token = other.cookies.get(COOKIE_NAME)
        self.assertNotIn(token, json.dumps(session))
        self.assertNotIn("password", response.text)
        listed = other.get("/admin/users").json()["users"]
        self.assertEqual([user["username"] for user in listed], ["ragadmin"])
        response = other.post("/auth/logout")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(other.get("/console/session").status_code, 401)
        other.cookies.set(COOKIE_NAME, token)
        self.assertEqual(other.get("/console/session").status_code, 401)
        self.assertEqual(other.post("/auth/logout").status_code, 204)

    def test_admin_create_reader_stream_trace_evidence_and_source_revocation(self):
        self.create_user()
        snapshot = self.publish()
        reader = self.client()
        session = self.login(reader, "alice", "alice-password").json()
        self.assertFalse(session["admin"])
        self.assertEqual(reader.get("/console/session").json(), session)
        self.assertEqual(reader.get("/capabilities").status_code, 200)
        response = reader.post("/query/stream", json={"question": "amber warranty"})
        self.assertEqual(response.status_code, 200, response.text)
        blocks = response.text.strip().split("\n\n")
        final = json.loads(blocks[-1].splitlines()[1].removeprefix("data: "))
        self.assertEqual(final["result"]["status"], "evidence_preview")
        self.assertTrue(final["result"]["evidence"])
        trace_path = "/traces/" + final["trace"]["trace_id"]
        self.assertEqual(reader.get(trace_path).status_code, 200)
        evidence = final["result"]["evidence"][0]
        source_path = "/evidence/" + snapshot.generation + "/" + evidence["evidence_id"]
        self.assertEqual(reader.get(source_path).status_code, 200)
        self.api.state.application.set_access("alpha", "warranty", (), True)
        self.assertEqual(reader.get(trace_path).status_code, 404)
        self.assertEqual(reader.get(source_path).status_code, 404)

    def test_readers_cannot_manage_accounts_or_documents(self):
        self.create_user()
        reader = self.client()
        self.login(reader, "alice", "alice-password")
        for method, path, body in (
            ("GET", "/admin/users", None),
            ("POST", "/admin/users", {"username": "other", "password": "another-password"}),
            ("DELETE", "/admin/users/ragadmin", None),
            ("POST", "/documents", {"document_id": "bad", "version": "v1", "content": "test"}),
            ("POST", "/publish", {"job_id": "job"}),
            ("POST", "/access", {"document_id": "doc", "revoked": True}),
        ):
            response = reader.request(method, path, **({"json": body} if body else {}))
            self.assertEqual(response.status_code, 403, (path, response.text))

    def test_deleted_account_sessions_stop_and_recreated_name_has_new_subject(self):
        self.create_user()
        one, two = self.client(), self.client()
        first = self.login(one, "alice", "alice-password").json()["subject_id"]
        self.login(two, "alice", "alice-password")
        self.publish((first,))
        response = one.post("/query", json={"question": "amber warranty"})
        self.assertTrue(response.json()["evidence"])
        self.assertEqual(self.admin.delete("/admin/users/alice").status_code, 204)
        for client in (one, two):
            self.assertEqual(client.get("/console/session").status_code, 401)
            self.assertEqual(client.post("/query", json={"question": "amber warranty"}).status_code, 401)
        self.assertEqual(self.admin.delete("/admin/users/alice").status_code, 404)
        self.assertEqual(one.post("/auth/login", json={"username": "alice", "password": "alice-password"}).status_code, 401)
        self.create_user()
        second = self.login(one, "alice", "alice-password").json()["subject_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(one.post("/query", json={"question": "amber warranty"}).json()["evidence"], [])

    def test_accounts_and_sessions_survive_api_restart_without_reseeding(self):
        self.create_user()
        reader = self.client()
        session = self.login(reader, "alice", "alice-password").json()
        restarted = self.client(create_api(self.settings))
        restarted.cookies.update(reader.cookies)
        self.assertEqual(restarted.get("/console/session").json(), session)
        self.assertEqual(self.login(restarted, "alice", "alice-password").json()["subject_id"], session["subject_id"])
        self.assertEqual(self.admin.delete("/admin/users/ragadmin").status_code, 409)
        self.assertEqual(self.admin.delete("/admin/users/RAGADMIN").status_code, 409)
        self.assertEqual(len(self.admin.get("/admin/users").json()["users"]), 2)

    def test_input_boundaries_duplicates_and_login_errors(self):
        for username in ("ab", "a" * 33, "用户", "<script>", "a/b", "a\nb"):
            response = self.admin.post("/admin/users", json={"username": username, "password": "good-password"})
            self.assertIn(response.status_code, (400, 422), response.text)
        for password in ("xyz12", "p" * 129, "      ", "hello\nworld", "hello\x00world"):
            response = self.admin.post("/admin/users", json={"username": "valid", "password": password})
            self.assertIn(response.status_code, (400, 422), response.text)
            self.assertNotIn(password, response.text)
        self.create_user(" Alice ", "密码 Unicode 🔑")
        self.assertEqual(self.admin.post("/admin/users", json={"username": "ALICE", "password": "other-password"}).status_code, 409)
        reader = self.client()
        self.login(reader, "ALICE", "密码 Unicode 🔑")
        for username, password in (("unknown", "valid-password"), ("alice", "wrong-password"), ("alice", "bad\npassword")):
            response = self.client().post("/auth/login", json={"username": username, "password": password})
            self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(response.json()["error"]["message"], "账号或密码不正确。")
        for body in ({"username": "evil", "password": "good-password", "admin": True},
                     {"username": True, "password": "good-password"}):
            self.assertEqual(self.admin.post("/admin/users", json=body).status_code, 422)
        # Include both lower and upper allowed username/password lengths.
        for username, password in (("abc", "123456"), ("a" * 32, "p" * 128)):
            self.create_user(username, password)
            self.login(self.client(), username, password)

    def test_expired_sessions_are_rejected_and_storage_contains_no_raw_credentials(self):
        self.create_user()
        reader = self.client()
        self.login(reader, "alice", "alice-password")
        token = reader.cookies.get(COOKIE_NAME)
        db = self.root / "accounts.sqlite3"
        with closing(sqlite3.connect(db)) as connection:
            with connection:
                connection.execute("UPDATE sessions SET expires_at=?", (time.time() - 1,))
        self.assertEqual(reader.get("/console/session").status_code, 401)
        stored = db.read_bytes()
        for value in (ADMIN_PASSWORD.encode(), b"alice-password", token.encode()):
            self.assertNotIn(value, stored)

    def test_unique_admin_tenant_and_explicit_tenant_selection_preserve_bearer(self):
        token = "legacy-admin-token-0000000000"
        config = Settings(data_dir=self.root / "legacy", admin_password=ADMIN_PASSWORD,
                          api_tokens={token: Principal("existing", "operator", admin=True)})
        client = self.client(create_api(config))
        self.assertEqual(self.login(client).json()["tenant_id"], "existing")
        self.assertEqual(client.get("/console/session", headers={"Authorization": "Bearer " + token}).json()["subject_id"], "operator")
        self.assertEqual(client.get("/console/session", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        with self.assertRaises(ValidationError):
            create_api(replace(config, browser_tenant="changed"))

    def test_cookie_is_secure_on_https_and_does_not_touch_other_project_cookie(self):
        client = TestClient(self.api, base_url="https://testserver")
        self.addCleanup(client.close)
        client.cookies.set("sj_session", "unrelated-session")
        response = self.login(client)
        self.assertIn("Secure", response.headers["set-cookie"])
        self.assertEqual(client.cookies.get("sj_session"), "unrelated-session")
        self.assertEqual(client.get("/console/session").status_code, 200)
        response = client.post("/auth/logout")
        self.assertNotIn("sj_session", response.headers["set-cookie"])
        self.assertEqual(client.cookies.get("sj_session"), "unrelated-session")

    def test_concurrent_account_creation_has_one_winner(self):
        principal = Principal("alpha", "test-admin", admin=True)
        store = self.api.state.accounts
        def attempt():
            try:
                return store.create(principal, "same-user", "same-password")["username"]
            except ConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))
        self.assertCountEqual(results, ["same-user", "conflict"])
        self.assertEqual([row["username"] for row in store.users(principal)].count("same-user"), 1)


class AdminPasswordTests(unittest.TestCase):
    """RAG_ADMIN_PASSWORD is the only source of the fixed administrator's password."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def start(self, password=None):
        settings = Settings(data_dir=self.root, browser_tenant="alpha", admin_password=password)
        client = TestClient(create_api(settings), raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    @staticmethod
    def login(client, password):
        return client.post("/auth/login", json={"username": "ragadmin", "password": password})

    def test_new_database_requires_the_password_and_creates_no_account_without_it(self):
        with self.assertRaisesRegex(ValidationError, "RAG_ADMIN_PASSWORD is required"):
            self.start()
        client = self.start("first-admin-password")
        self.assertEqual(self.login(client, "first-admin-password").status_code, 200)
        self.assertEqual(self.login(client, "wrong-local-password").status_code, 401, "only the configured password is accepted")

    def test_existing_account_is_untouched_when_the_variable_is_absent(self):
        self.start("kept-admin-password")
        client = self.start()
        self.assertEqual(self.login(client, "kept-admin-password").status_code, 200)

    def test_changed_password_replaces_the_stored_one_and_ends_old_sessions(self):
        old = self.start("old-admin-password")
        self.assertEqual(self.login(old, "old-admin-password").status_code, 200)
        self.assertEqual(old.get("/console/session").status_code, 200)
        same = self.start("old-admin-password")
        same.cookies.update(old.cookies)
        self.assertEqual(same.get("/console/session").status_code, 200, "an unchanged password keeps sessions")
        new = self.start("new-admin-password")
        new.cookies.update(old.cookies)
        self.assertEqual(new.get("/console/session").status_code, 401)
        self.assertEqual(self.login(new, "old-admin-password").status_code, 401)
        self.assertEqual(self.login(new, "new-admin-password").status_code, 200)

    def test_invalid_password_is_rejected_without_changing_the_database(self):
        self.start("valid-admin-password")
        for value in ("short", "x" * 129, "tab\tinside", "      "):
            with self.assertRaisesRegex(ValidationError, "RAG_ADMIN_PASSWORD must be"):
                self.start(value)
        self.assertEqual(self.login(self.start(), "valid-admin-password").status_code, 200)

    def test_password_is_not_printed_by_settings_and_cannot_come_from_toml(self):
        settings = Settings(admin_password="never-printed-password")
        self.assertNotIn("never-printed-password", repr(settings))
        config = self.root / "settings.toml"
        config.write_text('admin_password = "from-a-file"\n', encoding="utf-8")
        with self.assertRaises(ValidationError):
            load_settings(config)


if __name__ == "__main__":
    unittest.main()
