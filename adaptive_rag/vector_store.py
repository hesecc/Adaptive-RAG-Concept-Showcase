"""Derived text-vector indexes. Profiles isolate model and processing versions.

Indexes never grant access to assets: callers hydrate and authorize matches at
the authoritative asset store. Upsert is repeatable for the same record ID.
"""

from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
from typing import Protocol

from .domain import ConflictError, ValidationError, identifier


def validate_vector(value: Sequence[float], dimension: int | None = None) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not 1 <= len(value) <= 16384:
        raise ValidationError("vector must have 1-16384 finite numeric dimensions")
    try:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
            raise ValueError
        vector = tuple(float(v) for v in value)
    except (ValueError, OverflowError):
        raise ValidationError("vector must contain finite numeric values") from None
    norm = math.hypot(*vector)
    if norm == 0 or not math.isfinite(norm):
        raise ValidationError("vector must have a finite nonzero norm")
    if dimension is not None and len(vector) != dimension:
        raise ConflictError("vector dimension differs from this index profile")
    return vector


def validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValidationError("vector limit must be an integer between 1 and 1000")


@dataclass(frozen=True)
class VectorRecord:
    record_id: str
    tenant_id: str
    asset_id: str
    revision_id: str
    profile: str
    vector: tuple[float, ...]

    def __post_init__(self):
        for field in ("record_id", "tenant_id", "asset_id", "revision_id", "profile"):
            identifier(getattr(self, field), field)
        object.__setattr__(self, "vector", validate_vector(self.vector))


@dataclass(frozen=True)
class VectorMatch:
    record_id: str
    tenant_id: str
    asset_id: str
    revision_id: str
    profile: str
    score: float

    def __post_init__(self):
        for field in ("record_id", "tenant_id", "asset_id", "revision_id", "profile"):
            identifier(getattr(self, field), field)
        try:
            if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) or not math.isfinite(self.score):
                raise ValueError
        except (ValueError, OverflowError):
            raise ValidationError("vector score must be finite") from None


class VectorIndex(Protocol):
    def upsert(self, records: Sequence[VectorRecord]) -> None: ...
    def search(self, tenant_id: str, profile: str, vector: Sequence[float], limit: int) -> tuple[VectorMatch, ...]: ...
    def delete(self, tenant_id: str, asset_id: str) -> None: ...


class SQLiteVectorIndex:
    """Persistent exact cosine adapter for local development, not an ANN index."""

    def __init__(self, data_dir: Path):
        root = Path(data_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "asset-vectors.sqlite3"
        if self.path.is_symlink():
            raise ValidationError("vector database must not be a symlink")
        with closing(self._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS vector_profiles (
                    profile TEXT PRIMARY KEY, dimension INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_vectors (
                    tenant_id TEXT NOT NULL, profile TEXT NOT NULL,
                    record_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                    revision_id TEXT NOT NULL, vector_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, profile, record_id)
                );
                CREATE INDEX IF NOT EXISTS asset_vector_lookup
                    ON asset_vectors (tenant_id, asset_id);
            """)

    def _connect(self):
        if self.path.is_symlink():
            raise ValidationError("vector database must not be a symlink")
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence) or len(records) > 10000:
            raise ValidationError("vector batch must contain at most 10000 records")
        if any(not isinstance(record, VectorRecord) for record in records):
            raise ValidationError("vector batch must contain VectorRecord values")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            for record in records:
                dimension = len(record.vector)
                connection.execute("INSERT OR IGNORE INTO vector_profiles VALUES (?,?)", (record.profile, dimension))
                stored = connection.execute("SELECT dimension FROM vector_profiles WHERE profile=?", (record.profile,)).fetchone()
                validate_vector(record.vector, stored[0])
                previous = connection.execute(
                    "SELECT asset_id,revision_id FROM asset_vectors WHERE tenant_id=? AND profile=? AND record_id=?",
                    (record.tenant_id, record.profile, record.record_id),
                ).fetchone()
                if previous is not None and previous != (record.asset_id, record.revision_id):
                    raise ConflictError("vector record identity cannot change")
                connection.execute("""INSERT INTO asset_vectors VALUES (?,?,?,?,?,?)
                    ON CONFLICT (tenant_id,profile,record_id)
                    DO UPDATE SET vector_json=excluded.vector_json""",
                    (record.tenant_id, record.profile, record.record_id, record.asset_id,
                     record.revision_id, json.dumps(record.vector, allow_nan=False, separators=(",", ":"))))

    def search(self, tenant_id: str, profile: str, vector: Sequence[float], limit: int) -> tuple[VectorMatch, ...]:
        identifier(tenant_id, "tenant_id")
        identifier(profile, "profile")
        validate_limit(limit)
        query = validate_vector(vector)
        with closing(self._connect()) as connection:
            dimension = connection.execute("SELECT dimension FROM vector_profiles WHERE profile=?", (profile,)).fetchone()
            if dimension is None:
                return ()
            validate_vector(query, dimension[0])
            rows = connection.execute("SELECT record_id,asset_id,revision_id,vector_json FROM asset_vectors WHERE tenant_id=? AND profile=?",
                                      (tenant_id, profile)).fetchall()
        qnorm = math.hypot(*query)
        query = tuple(v / qnorm for v in query)
        matches = []
        for record_id, asset_id, revision_id, serialized in rows:
            try:
                stored = validate_vector(json.loads(serialized), len(query))
            except (ValueError, TypeError, ValidationError, ConflictError):
                raise ConflictError("stored asset vector is invalid; rebuild this derived index") from None
            norm = math.hypot(*stored)
            score = max(-1.0, min(1.0, math.fsum(a * (b / norm) for a, b in zip(query, stored))))
            matches.append(VectorMatch(record_id, tenant_id, asset_id, revision_id, profile, score))
        return tuple(sorted(matches, key=lambda match: (-match.score, match.record_id))[:limit])

    def delete(self, tenant_id: str, asset_id: str) -> None:
        identifier(tenant_id, "tenant_id")
        identifier(asset_id, "asset_id")
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM asset_vectors WHERE tenant_id=? AND asset_id=?", (tenant_id, asset_id))
