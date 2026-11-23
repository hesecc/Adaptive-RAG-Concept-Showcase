"""Return pre-file-center SQLite catalogs to the positional schema written by release 1f2df52.

Both --check and apply refuse the whole directory before exporting or changing anything if
document or image lifecycle schema is present, even if its tables are empty. Do not drop views,
ownership, events or tombstones to bypass this guard: back up the current directory and restore
a verified pre-file-center cold backup instead. Use only closed, checkpointed SQLite databases;
never delete journal or WAL files to make a database appear checkpointed.

Why this exists: the previous release inserts with positional `INSERT ... VALUES (...)`. After the
data-domain release added columns, that release fails every write (new account, document
ingestion) with "table X has N columns but N-1 values were supplied", while reads, logins and
/health stay green. Starting the previous image alone is therefore not a rollback.

Removes exactly these three columns and nothing else:
    knowledge.sqlite3  documents.data_domains, revisions.created_at
    accounts.sqlite3   accounts.data_domains

What is lost: document classifications and account domain grants. They are printed as JSON on
stdout BEFORE any change, so they can be re-applied after a later upgrade (the new release treats
a document without a classification as admin-only and an account as ['public']).
Documents, accounts, traces, snapshots, the image databases and image objects are kept; the
previous release ignores the image files.

Run only with the application stopped and under /opt/adaptive-rag/.maintenance.lock. Run it inside a
container as the application user (UID 10001) so SQLite sidecar files keep the owner the
application needs:

    docker run --rm -i --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \\
        --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 \\
        -v <data directory>:/data <new image> python -I - /data [--check] < downgrade_schema.py > export.json

--check changes nothing. Progress goes to stderr; stdout is the JSON export only. A second run is a
no-op. The cold backup taken before the release remains the authoritative recovery point.
"""
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

PLAN = {
    "knowledge.sqlite3": (("documents", "data_domains"), ("revisions", "created_at")),
    "accounts.sqlite3": (("accounts", "data_domains"),),
}


def say(message):
    print(message, file=sys.stderr, flush=True)


def columns(connection, table):
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def cold_reader(path):
    # Safe only with writers stopped and journals checked. Creates no WAL/SHM sidecars.
    return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)


def preflight(data):
    markers = {
        "knowledge.sqlite3": {"active_documents", "document_trash", "document_events", "retired_document_ids"},
        "assets.sqlite3": {"image_trash", "image_events"},
        "accounts.sqlite3": set(),
    }
    for name, names in markers.items():
        path = data / name
        for suffix in ("", "-shm", "-wal", "-journal"):
            entry = data / (name + suffix)
            if entry.is_symlink() or (entry.exists() and not entry.is_file()):
                raise SystemExit(f"{entry.name}: cannot safely inspect linked or non-file catalog")
            if suffix in ("-wal", "-journal") and entry.exists() and entry.stat().st_size:
                raise SystemExit(f"{entry.name}: requires a closed, checkpointed cold backup; do not delete the journal")
        if not path.is_file():
            continue
        with closing(cold_reader(path)) as connection:
            objects = {row[0].lower() for row in connection.execute("SELECT name FROM sqlite_schema")}
            found = objects & (names | {"retention_object_candidates_v1"})
            if name == "knowledge.sqlite3" and "owner_id" in columns(connection, "documents"):
                found.add("documents.owner_id")
            if found:
                raise SystemExit(f"{name}: file-center schema detected ({', '.join(sorted(found))}); "
                                 "back up current data and restore a verified pre-file-center cold backup")


def export(connection, name):
    if name == "knowledge.sqlite3" and "data_domains" in columns(connection, "documents"):
        return [{"tenant_id": tenant, "document_id": document, "data_domains": json.loads(domains)}
                for tenant, document, domains in connection.execute(
                    "SELECT tenant_id,document_id,data_domains FROM documents ORDER BY 1,2")]
    if name == "accounts.sqlite3" and "data_domains" in columns(connection, "accounts"):
        return [{"username": username, "admin": bool(admin), "data_domains": json.loads(domains)}
                for username, admin, domains in connection.execute(
                    "SELECT username,admin,data_domains FROM accounts ORDER BY 1")]
    return []


def downgrade(data, check_only=False):
    """Return {'documents': [...], 'accounts': [...]} exported before the change."""
    if sqlite3.sqlite_version_info < (3, 35, 0):
        raise SystemExit(f"SQLite {sqlite3.sqlite_version} cannot DROP COLUMN (needs 3.35+)")
    data = Path(data).resolve()
    preflight(data)
    exported = {"documents": [], "accounts": []}
    for name, drops in PLAN.items():
        path = data / name
        if not path.is_file() or path.is_symlink():
            say(f"{name}: not present, skipped")
            continue
        reader = cold_reader(path) if check_only else sqlite3.connect(path, isolation_level=None, timeout=10)
        with closing(reader) as connection:
            present = [(table, column) for table, column in drops if column in columns(connection, table)]
            say(f"{name}: columns to drop = {present or 'none (already previous shape)'}")
            exported["documents" if name == "knowledge.sqlite3" else "accounts"] = export(connection, name)
            if check_only or not present:
                continue
            connection.execute("BEGIN IMMEDIATE")
            try:
                for table, column in present:
                    connection.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise SystemExit(f"{name}: integrity_check failed after downgrade")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise SystemExit(f"{name}: foreign_key_check failed after downgrade")
            for table, column in present:
                if column in columns(connection, table):
                    raise SystemExit(f"{name}: {table}.{column} still present")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            say(f"{name}: downgraded; integrity and foreign keys verified")
    return exported


def main(argv):
    check_only = "--check" in argv
    args = [argument for argument in argv if not argument.startswith("--")]
    if len(args) != 1 or any(argument.startswith("--") and argument != "--check" for argument in argv):
        raise SystemExit("usage: downgrade_schema.py <data-dir> [--check]")
    print(json.dumps(downgrade(args[0], check_only), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
