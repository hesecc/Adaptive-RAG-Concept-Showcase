"""Explicit offline knowledge/trace migration; never imports accounts or objects."""

import argparse
import json
from pathlib import Path
import sys

from adaptive_rag.domain import RagError, ValidationError
from adaptive_rag.environment import read_environment
from adaptive_rag.postgres import migrate_sqlite_to_postgres


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True,
                        help="Offline knowledge.sqlite3 copy; does not modify the source")
    parser.add_argument("--env-file", type=Path, required=True,
                        help="Private environment containing explicit storage opt-in and target DSN")
    parser.add_argument("--schema", default="adaptive_rag")
    args = parser.parse_args(argv)
    try:
        values = read_environment(args.env_file)
        if values.get("RAG_ENABLE_REMOTE_STORAGE", "false").lower() != "true":
            raise ValidationError("migration requires RAG_ENABLE_REMOTE_STORAGE=true")
        dsn = values.get("RAG_POSTGRES_DSN", "")
        if not dsn:
            raise ValidationError("migration requires RAG_POSTGRES_DSN")
        counts = migrate_sqlite_to_postgres(args.source, dsn, schema=args.schema)
        print(json.dumps({"status": "migrated", "tables": counts}, ensure_ascii=False))
        return 0
    except RagError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception:
        # Drivers can include full DSNs in exceptions; do not echo them.
        print(json.dumps({"error": {"code": "migration_error", "message": "Migration failed; verify target availability and source compatibility."}}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
