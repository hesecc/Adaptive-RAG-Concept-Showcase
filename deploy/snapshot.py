#!/usr/bin/env python3
"""Cold project snapshots and isolated restore checks; never restores production.

CLI is intentionally fixed to /opt/adaptive-rag. backup.sh owns stopping/restarting
the sole writer and the maintenance lock. Partial backups are retained on error.
An archive includes private configuration/keys: keep backups root-only and copy
off-host only through an approved encrypted backup channel.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile


ROOT = Path("/opt/adaptive-rag")
MARKER = "an-rag:standalone:v1"
TREES = ("config", "data", "secrets", "tls")
FILES = (".managed", "compose.yaml", "deployment.env")
HASH = re.compile(r"[0-9a-f]{64}")


def digest(path):
    value = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def regular(path):
    return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()


def managed(root):
    root = Path(root).absolute()
    if root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise ValueError("unsafe managed root")
    marker = root / ".managed"
    if not regular(marker) or marker.read_text(encoding="utf-8").strip() != MARKER:
        raise ValueError("wrong managed marker")
    return root


def contained(path, root):
    path = Path(path).absolute()
    if path.is_symlink() or path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("path is outside managed directory or uses symlinks")
    return path


def inventory(root):
    entries = {}
    paths = [root / name for name in FILES]
    for tree in TREES:
        base = contained(root / tree, root)
        if not base.is_dir():
            raise ValueError("missing snapshot directory")
        paths.append(base)
        paths.extend(base.rglob("*"))
    for path in sorted(paths):
        contained(path, root)
        info = path.lstat()
        name = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode):
            entries[name] = {"kind": "directory", "mode": stat.S_IMODE(info.st_mode)}
        elif stat.S_ISREG(info.st_mode):
            entries[name] = {"kind": "file", "size": info.st_size,
                             "sha256": digest(path), "mode": stat.S_IMODE(info.st_mode)}
        else:
            raise ValueError("snapshot contains a link or special file")
    return entries


def create_backup(root, destination, image_id):
    root = managed(root)
    backups = contained(root / "backups", root)
    if not backups.is_dir():
        raise ValueError("missing backup directory")
    destination = contained(destination, backups)
    if destination.parent != backups or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("backup destination must be a new empty direct child")
    if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{12,64}", image_id):
        raise ValueError("invalid immutable image ID")
    os.chmod(destination, 0o700)
    entries = inventory(root)
    manifest = {"format": 1, "project": MARKER, "image_id": image_id,
                "created_at": datetime.now(timezone.utc).isoformat(), "entries": entries}
    archive = destination / "snapshot.tar.gz"
    with tarfile.open(archive, "x:gz", format=tarfile.PAX_FORMAT) as handle:
        for name in entries:
            # recursion is explicit so symlinks/special files cannot enter implicitly.
            handle.add(root / name, arcname=name, recursive=False)
    if inventory(root) != entries:
        raise ValueError("snapshot input changed while the application was stopped")
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    (destination / "SHA256SUMS").write_text(
        f"{digest(archive)}  snapshot.tar.gz\n{digest(destination / 'manifest.json')}  manifest.json\n", encoding="ascii")
    # Re-read every member; a successful tar write alone is not verification.
    verify_archive(destination)
    return {"files": sum(e["kind"] == "file" for e in entries.values()),
            "bytes": sum(e.get("size", 0) for e in entries.values())}


def load_manifest(backup):
    for name in ("manifest.json", "SHA256SUMS", "snapshot.tar.gz"):
        if not regular(backup / name):
            raise ValueError("backup file is missing, a link or a special file")
    checks = (backup / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    expected = {"snapshot.tar.gz", "manifest.json"}
    if len(checks) != 2:
        raise ValueError("invalid backup checksums")
    for row in checks:
        parts = row.split("  ")
        if len(parts) != 2 or not HASH.fullmatch(parts[0]) or parts[1] not in expected:
            raise ValueError("invalid backup checksums")
        expected.remove(parts[1])
        if digest(backup / parts[1]) != parts[0]:
            raise ValueError("backup checksum mismatch")
    if (backup / "manifest.json").stat().st_size > 64 * 1024 * 1024:
        raise ValueError("backup manifest is too large")
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("project") != MARKER or not isinstance(manifest.get("entries"), dict):
        raise ValueError("invalid backup manifest")
    entries = manifest["entries"]
    if not set(FILES + TREES).issubset(entries):
        raise ValueError("incomplete backup manifest")
    for name, entry in entries.items():
        path = PurePosixPath(name)
        if (not isinstance(name, str) or str(path) != name or path.is_absolute()
                or ".." in path.parts or not path.parts or "\\" in name
                or not (path.parts[0] in TREES or name in FILES) or not isinstance(entry, dict)):
            raise ValueError("unsafe backup member name")
        if entry.get("kind") not in {"file", "directory"}:
            raise ValueError("invalid backup member kind")
        mode = entry.get("mode")
        if type(mode) is not int or not 0 <= mode <= 0o777:
            raise ValueError("unsafe backup file mode")
        if entry["kind"] == "file" and (type(entry.get("size")) is not int
                or not 0 <= entry["size"] <= 100 * 1024 ** 3
                or not isinstance(entry.get("sha256"), str) or not HASH.fullmatch(entry["sha256"])):
            raise ValueError("invalid backup file metadata")
    return manifest


def verify_archive(backup, restore_to=None):
    manifest = load_manifest(backup)
    entries = manifest["entries"]
    seen = set()
    with tarfile.open(backup / "snapshot.tar.gz", "r:gz") as handle:
        for member in handle:
            name = member.name
            if name not in entries or name in seen:
                raise ValueError("unexpected or duplicate archive member")
            seen.add(name)
            expected = entries[name]
            is_file = expected["kind"] == "file"
            if (is_file and not member.isfile()) or (not is_file and not member.isdir()):
                raise ValueError("archive contains a link, special file or wrong member type")
            if member.size != expected.get("size", 0):
                raise ValueError("archive member size mismatch")
            destination = None if restore_to is None else restore_to.joinpath(*PurePosixPath(name).parts)
            if not is_file:
                if destination is not None:
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                continue
            value = sha256()
            source = handle.extractfile(member)
            if source is None:
                raise ValueError("unreadable archive member")
            output = None
            try:
                if destination is not None:
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    output = destination.open("xb")
                    os.chmod(destination, 0o600)
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    value.update(block)
                    if output is not None:
                        output.write(block)
            finally:
                source.close()
                if output is not None:
                    output.close()
            if value.hexdigest() != expected["sha256"]:
                raise ValueError("archive member checksum mismatch")
    if seen != set(entries):
        raise ValueError("archive is missing snapshot members")
    return manifest


def verify_restored_data(restored):
    data = restored / "data"
    if not regular(data / "knowledge.sqlite3") or not (data / "objects").is_dir():
        raise ValueError("restored application data is incomplete")
    databases = list(data.rglob("*.sqlite3"))
    checked = 0
    for path in databases:
        # Only the isolated copy can create WAL/SHM sidecars during read checks.
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("restored SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("restored SQLite foreign key check failed")
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "revisions" in tables:
                for key, metadata in connection.execute("SELECT raw_key, metadata_json FROM revisions"):
                    references = [key]
                    source_key = json.loads(metadata).get("source_object_key")
                    if source_key is not None:
                        references.append(source_key)
                    for reference in references:
                        if not isinstance(reference, str) or not HASH.fullmatch(reference):
                            raise ValueError("invalid restored object reference")
                        object_path = data / "objects" / reference[:2] / reference
                        if not regular(object_path) or digest(object_path) != reference:
                            raise ValueError("restored database references a missing or corrupt object")
        checked += 1
    objects = 0
    for path in (data / "objects").rglob("*"):
        if path.is_file():
            if (not regular(path) or not HASH.fullmatch(path.name) or path.parent.name != path.name[:2]
                    or digest(path) != path.name):
                raise ValueError("restored object hash mismatch")
            objects += 1
    return {"sqlite_databases": checked, "objects": objects}


def restore_check(root, backup):
    root = managed(root)
    backups = contained(root / "backups", root)
    backup = contained(backup, backups)
    if backup.parent != backups or not backup.is_dir() or backup.name.startswith(".partial-"):
        raise ValueError("restore check requires a completed backup in the managed backups directory")
    manifest = verify_archive(backup)
    required = sum(e.get("size", 0) for e in manifest["entries"].values())
    if shutil.disk_usage(backups).free < required + 100 * 1024 * 1024:
        raise ValueError("insufficient disk for isolated restore check")
    destination = Path(tempfile.mkdtemp(prefix=".restore-check-", dir=backups))
    os.chmod(destination, 0o700)
    try:
        verify_archive(backup, destination)
        managed(destination)
        result = verify_restored_data(destination)
    except Exception:
        # Private evidence stays under the project; production paths are untouched.
        (destination / "RESTORE-FAILED").touch(mode=0o600)
        raise
    result.update(restore_directory=str(destination), image_id=manifest["image_id"],
                  scope="isolated file, SQLite and object verification; no production restore or model calls")
    (destination / "restore-result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    create = subcommands.add_parser("create")
    create.add_argument("destination", type=Path)
    create.add_argument("--image-id", required=True)
    restore = subcommands.add_parser("restore-check")
    restore.add_argument("backup", type=Path)
    args = parser.parse_args()
    try:
        result = (create_backup(ROOT, args.destination, args.image_id) if args.command == "create"
                  else restore_check(ROOT, args.backup))
    except (OSError, ValueError, sqlite3.Error, tarfile.TarError):
        # No config values, source text, private file paths or SQL in public logs.
        parser.exit(1, "Snapshot validation failed; private partial evidence retained under managed backups.\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
