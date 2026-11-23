"""Deployment artifacts and isolated backup/recovery failure contracts."""

from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from adaptive_rag.api import create_api
from adaptive_rag.app import RagApplication
from adaptive_rag.config import Settings
from adaptive_rag.domain import IngestRequest
from support import ADMIN_PASSWORD


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
spec = importlib.util.spec_from_file_location("rag_snapshot", DEPLOY / "snapshot.py")
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)
IMAGE_ID = "sha256:" + "a" * 64


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / "an-RAG"
        self.root.mkdir()
        for name in snapshot.TREES + ("backups",):
            (self.root / name).mkdir()
        (self.root / ".managed").write_text(snapshot.MARKER)
        (self.root / "compose.yaml").write_text("name: an-rag\n")
        (self.root / "deployment.env").write_text("RAG_IMAGE=an-rag:test\n")
        (self.root / "config" / "app.env").write_text("RAG_HTTP_API_TOKEN=synthetic-private-token\n")
        self.application = RagApplication(Settings(data_dir=self.root / "data"))
        result = self.application.ingest(IngestRequest("demo", "doc", "v1", "# Warranty\nWarranty lasts one year."))
        self.application.publish("demo", result.job_id, None)

    def backup(self, name="complete"):
        destination = self.root / "backups" / name
        destination.mkdir()
        snapshot.create_backup(self.root, destination, IMAGE_ID)
        return destination

    def test_cold_snapshot_restores_database_objects_and_config_in_isolation(self):
        backup = self.backup()
        before = snapshot.inventory(self.root)
        result = snapshot.restore_check(self.root, backup)
        self.assertEqual(result["sqlite_databases"], 1)
        self.assertGreater(result["objects"], 0)
        restored = Path(result["restore_directory"])
        self.assertTrue(restored.is_relative_to(self.root / "backups"))
        self.assertNotEqual(restored / "data", self.root / "data")
        self.assertEqual((restored / "config" / "app.env").read_bytes(),
                         (self.root / "config" / "app.env").read_bytes())
        self.assertEqual(before, snapshot.inventory(self.root))
        with closing(sqlite3.connect(restored / "data" / "knowledge.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM current_snapshots").fetchone()[0], 1)

    def test_corrupt_archive_is_rejected_before_restore_directory_creation(self):
        backup = self.backup()
        archive = backup / "snapshot.tar.gz"
        value = bytearray(archive.read_bytes())
        value[len(value) // 2] ^= 1
        archive.write_bytes(value)
        with self.assertRaisesRegex(ValueError, "checksum"):
            snapshot.restore_check(self.root, backup)
        self.assertEqual([], list((self.root / "backups").glob(".restore-check-*")))

    def test_path_traversal_manifest_is_rejected_even_with_updated_checksums(self):
        backup = self.backup()
        path = backup / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["entries"]["../escape"] = {"kind": "directory", "mode": 0o700}
        path.write_text(json.dumps(manifest), encoding="utf-8")
        (backup / "SHA256SUMS").write_text(
            f"{snapshot.digest(backup / 'snapshot.tar.gz')}  snapshot.tar.gz\n{snapshot.digest(path)}  manifest.json\n")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            snapshot.restore_check(self.root, backup)
        self.assertFalse((self.root / "backups" / "escape").exists())

    def test_corrupt_content_addressed_object_fails_recovery_and_preserves_source(self):
        object_path = next(p for p in (self.root / "data" / "objects").rglob("*") if p.is_file())
        object_path.write_bytes(b"corrupt synthetic object")
        backup = self.backup()
        before = snapshot.inventory(self.root)
        with self.assertRaisesRegex(ValueError, "object"):
            snapshot.restore_check(self.root, backup)
        self.assertEqual(before, snapshot.inventory(self.root))
        evidence = list((self.root / "backups").glob(".restore-check-*/RESTORE-FAILED"))
        self.assertEqual(len(evidence), 1)

    def test_missing_object_reference_fails_recovery(self):
        path = next(p for p in (self.root / "data" / "objects").rglob("*") if p.is_file())
        path.unlink()
        backup = self.backup()
        with self.assertRaisesRegex((ValueError, FileNotFoundError), "object|No such|cannot find"):
            snapshot.restore_check(self.root, backup)

    def test_wrong_marker_and_destination_outside_backup_root_are_refused(self):
        destination = self.root / "backups" / "partial"
        destination.mkdir()
        (self.root / ".managed").write_text("another-project")
        with self.assertRaisesRegex(ValueError, "marker"):
            snapshot.create_backup(self.root, destination, IMAGE_ID)
        (self.root / ".managed").write_text(snapshot.MARKER)
        with self.assertRaisesRegex(ValueError, "outside"):
            snapshot.create_backup(self.root, self.root.parent, IMAGE_ID)

    def test_changed_inputs_leave_backup_incomplete(self):
        destination = self.root / "backups" / ".partial-test"
        destination.mkdir()
        first = snapshot.inventory(self.root)
        with patch.object(snapshot, "inventory", side_effect=[first, {}]):
            with self.assertRaisesRegex(ValueError, "changed"):
                snapshot.create_backup(self.root, destination, IMAGE_ID)
        self.assertTrue((destination / "snapshot.tar.gz").exists())
        self.assertFalse((destination / "manifest.json").exists())
        with self.assertRaisesRegex(ValueError, "completed"):
            snapshot.restore_check(self.root, destination)


class BackupShellTests(unittest.TestCase):
    """Run the actual trap/control flow with a scoped synthetic Docker command.

    The root literal is replaced only in a disposable script copy. Docker,
    Python and flock are fixtures; this does not claim real Docker/lock QA.
    """
    def setUp(self):
        candidate = Path("C:/Program Files/Git/bin/bash.exe")
        self.bash = str(candidate) if candidate.exists() else shutil.which("bash")
        if not self.bash:
            self.skipTest("Bash unavailable")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "an-RAG"
        self.root.mkdir()
        for directory in ("config", "data", "secrets", "tls", "backups"):
            (self.root / directory).mkdir()
        for name, value in ((".managed", snapshot.MARKER), ("compose.yaml", ""), ("deployment.env", ""), ("snapshot.py", "")):
            (self.root / name).write_text(value)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "calls"
        self.state = self.base / "state"
        self.state.write_text("running")
        self.posix_root = self.posix(self.root)
        source = (DEPLOY / "backup.sh").read_text(encoding="utf-8")
        self.script = self.root / "backup.sh"
        self.script.write_text(source.replace("ROOT=/opt/adaptive-rag", f"ROOT='{self.posix_root}'"), encoding="utf-8", newline="\n")
        docker = '''#!/usr/bin/env bash
printf '%s\n' "$*" >>"$TEST_LOG"
case "$*" in
  inspect*) cat "$TEST_STATE" ;;
  *'config --quiet'*) exit 0 ;;
  *'ps --all --quiet api'*) printf '%064d\n' 1 ;;
  *'images -q api'*) printf '%064d\n' 2 ;;
  *'stop --timeout 150 api'*) printf exited >"$TEST_STATE"; [[ ${TEST_FAIL:-} != stop ]] || exit 19 ;;
  *'up -d --no-recreate --wait --wait-timeout 180 api'*) [[ ${TEST_FAIL:-} != start ]] || exit 18; printf running >"$TEST_STATE" ;;
  *) exit 20 ;;
esac
'''
        python = '''#!/usr/bin/env bash
printf 'snapshot\n' >>"$TEST_LOG"
[[ ${TEST_FAIL:-} != snapshot ]] || exit 42
printf synthetic >"$3/snapshot.tar.gz"
'''
        for name, content in (("docker", docker), ("python3", python), ("flock", "#!/usr/bin/env bash\nexit 0\n")):
            path = self.bin / name
            path.write_text(content, encoding="utf-8", newline="\n")
            path.chmod(0o755)

    def posix(self, path):
        if os.name != "nt":
            return str(path)
        return subprocess.check_output([self.bash, "-c", 'cygpath -u "$1"', "bash", str(path)], text=True).strip()

    def run_backup(self, failure=""):
        env = dict(os.environ, TEST_FAIL=failure, TEST_LOG=self.posix(self.log), TEST_STATE=self.posix(self.state))
        return subprocess.run([self.bash, "-c", 'export PATH="$1:$PATH"; exec bash "$2"',
                               "bash", self.posix(self.bin), self.posix(self.script)],
                              env=env, text=True, capture_output=True, timeout=20)

    def test_snapshot_failure_restarts_previously_running_app(self):
        result = self.run_backup("snapshot")
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertEqual(self.state.read_text(), "running")
        self.assertIn("up -d --no-recreate --wait --wait-timeout 180 api", self.log.read_text())
        self.assertEqual(1, len(list((self.root / "backups").glob(".partial-*"))))

    def test_partial_stop_failure_still_attempts_restart(self):
        result = self.run_backup("stop")
        self.assertEqual(result.returncode, 19, result.stderr)
        self.assertEqual(self.state.read_text(), "running")
        self.assertNotIn("snapshot\n", self.log.read_text())

    def test_success_restores_running_state_and_finalizes_backup(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text(), "running")
        backups = list((self.root / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertFalse(backups[0].name.startswith(".partial-"))

    def test_stopped_app_is_not_started_by_backup(self):
        self.state.write_text("exited")
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state.read_text(), "exited")
        calls = self.log.read_text()
        self.assertNotIn("up -d --no-recreate", calls)
        self.assertNotIn("stop --timeout", calls)

    def test_restart_failure_is_visible_even_when_archive_succeeds(self):
        result = self.run_backup("start")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CRITICAL", result.stderr)


def nginx_locations(config):
    """Every `location` block as (match rule, body). The body keeps nested blocks such as limit_except."""
    blocks = []
    for opening in re.finditer(r"^[ \t]*location[ \t]+([^{;\n]+?)[ \t]*\{", config, re.MULTILINE):
        depth, end = 1, opening.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(config[end], 0)
            end += 1
        blocks.append((opening.group(1), config[opening.end():end - 1]))
    return blocks


def guarding_zone(config, path):
    """The limit_req zone nginx applies to a path: the first matching regex location, else the plain `/` one."""
    blocks = nginx_locations(config)
    for rule, body in blocks:
        if rule.startswith("~ ") and re.search(rule[2:], path):
            return re.search(r"limit_req zone=(\w+)", body).group(1)
    return re.search(r"limit_req zone=(\w+)", dict(blocks)["/"]).group(1)


class DeploymentTemplateTests(unittest.TestCase):
    def test_server_resources_are_scoped_and_backend_is_loopback(self):
        compose = (DEPLOY / "compose.server.yaml").read_text()
        for expected in ("name: an-rag", "127.0.0.1:18000:8000", "/opt/adaptive-rag/data",
                         "create_host_path: false", "read_only: true", "format: raw",
                         "10001:10001", "pids_limit: 128", "max-size:", "RAG_IMAGE:?"):
            self.assertIn(expected, compose)
        self.assertNotIn("shangjing", compose)
        self.assertNotIn("container_name", compose)
        self.assertNotIn("18443", compose)

    def test_independent_tls_rate_limits_and_no_cookie_overwrite(self):
        nginx = (DEPLOY / "nginx.server.conf").read_text()
        for expected in ("listen 8443 ssl", "127.0.0.1:18000", "/opt/adaptive-rag/tls/server.key",
                         "limit_req zone=an_rag_requests", "client_max_body_size 16m",
                         "proxy_set_header Authorization $http_authorization"):
            self.assertIn(expected, nginx)
        self.assertNotIn("proxy_hide_header Set-Cookie", nginx)
        self.assertNotIn("sj_session", nginx)
        self.assertNotIn("18443", nginx)

    def test_image_bytes_have_their_own_limit_and_other_routes_keep_the_strict_one(self):
        nginx = (DEPLOY / "nginx.server.conf").read_text()
        # One zone per purpose; the strict zone must still guard the catch-all.
        self.assertIn("zone=an_rag_media:10m rate=300r/m", nginx)
        blocks = dict(nginx_locations(nginx))
        media_block = blocks["~ ^(/assets/[^/]+/(thumbnail|content)|/evidence/[^/]+/[^/]+/images/[^/]+)$"]
        catch_block = blocks["/"]
        self.assertIn("limit_req zone=an_rag_media burst=120 nodelay", media_block)
        self.assertNotIn("an_rag_requests", media_block)
        self.assertIn("limit_req zone=an_rag_requests burst=10 nodelay", catch_block)
        self.assertNotIn("an_rag_media", catch_block)
        # The relaxed route is read-only and must forward the same identity headers.
        self.assertIn("limit_except GET { deny all; }", media_block)
        for header in ("proxy_set_header Authorization $http_authorization",
                       "proxy_set_header X-Forwarded-Proto https", "proxy_pass http://127.0.0.1:18000"):
            self.assertIn(header, media_block)
            self.assertIn(header, catch_block)

    def test_page_loads_have_their_own_generous_read_only_limit(self):
        nginx = (DEPLOY / "nginx.server.conf").read_text()
        # nginx refuses to reload when a zone that already exists changes its key, so the running zones stay as they were.
        self.assertIn("limit_req_zone $binary_remote_addr zone=an_rag_requests:10m rate=30r/m;", nginx)
        self.assertIn("limit_req_zone $binary_remote_addr zone=an_rag_media:10m rate=300r/m;", nginx)
        # Page shells, scripts and the session reads get a zone of their own, so a quick switch between pages
        # cannot drain the 30 requests per minute that guard login and the model-backed routes.
        self.assertIn("limit_req_zone $binary_remote_addr zone=an_rag_pages:10m rate=300r/m;", nginx)
        blocks = nginx_locations(nginx)
        self.assertEqual(len(blocks), 3, [rule for rule, _ in blocks])
        pages = [body for _, body in blocks if "an_rag_pages" in body]
        self.assertEqual(len(pages), 1)
        self.assertIn("limit_req zone=an_rag_pages burst=60 nodelay", pages[0])
        self.assertIn("limit_except GET { deny all; }", pages[0])
        self.assertNotIn("an_rag_requests", pages[0])
        self.assertNotIn("an_rag_media", pages[0])
        for _, body in blocks:
            self.assertEqual(body.count("limit_req zone="), 1)
            for line in ("proxy_pass http://127.0.0.1:18000", "proxy_set_header Authorization $http_authorization",
                         "proxy_set_header X-Real-IP $remote_addr", "proxy_set_header X-Forwarded-For $remote_addr",
                         "proxy_set_header X-Forwarded-Proto https", 'proxy_set_header Connection ""'):
                self.assertIn(line, body)
            # A location that declares its own add_header drops the security headers set once on the server.
            self.assertNotIn("add_header", body)
        for header in ("X-Content-Type-Options nosniff", "Referrer-Policy no-referrer", 'Cache-Control "no-store"'):
            self.assertEqual(nginx.count(f"add_header {header} always;"), 1)

    def test_only_cheap_reads_are_relaxed_and_every_write_stays_strict(self):
        nginx = (DEPLOY / "nginx.server.conf").read_text()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as data:
            routes = create_api(Settings(data_dir=Path(data), admin_password=ADMIN_PASSWORD)).routes
        pages = {"/", "/images", "/knowledge", "/evaluation", "/console/session", "/access/domains"}
        relaxed = set()
        for route in routes:
            methods = getattr(route, "methods", None)
            if not methods:
                continue  # the /static mount has no methods; it is checked below
            path = re.sub(r"\{[^}]+\}", "x1", route.path)
            zone = guarding_zone(nginx, path)
            label = f"{sorted(methods)} {path}"
            if methods & {"POST", "PUT", "PATCH", "DELETE"}:
                self.assertEqual(zone, "an_rag_requests", label)
            elif path in pages:
                self.assertEqual(zone, "an_rag_pages", label)
                relaxed.add(path)
            elif re.fullmatch(r"/assets/x1/(thumbnail|content)|/evidence/x1/x1/images/x1", path):
                self.assertEqual(zone, "an_rag_media", label)
            else:
                self.assertEqual(zone, "an_rag_requests", label)
        self.assertEqual(relaxed, pages)
        for path in ("/static/app.js", "/static/domain-controls.js", "/static/mark.svg"):
            self.assertEqual(guarding_zone(nginx, path), "an_rag_pages", path)
        # Lookalikes of the relaxed paths do not slip into the relaxed zone.
        for path in ("/images/", "/images/x1", "/static", "/static/", "/console/session/x1", "/health",
                     "/evaluation/reports", "/auth/login", "/capabilities",
                     "/evidence/x1/x1", "/evidence/x1/x1/images", "/evidence/x1/x1/images/x1/x1"):
            self.assertEqual(guarding_zone(nginx, path), "an_rag_requests", path)


if __name__ == "__main__":
    unittest.main()
