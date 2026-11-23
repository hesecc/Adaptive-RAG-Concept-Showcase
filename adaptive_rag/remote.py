"""Explicit S3 and Milvus REST adapters; no credential discovery on import.

Milvus uses the documented v2 REST API. A collection is dedicated to this
schema and has one vector dimension; model profiles remain scalar namespaces.
"""

from collections.abc import Sequence
from hashlib import sha256
import json
import math
import re
from threading import Lock
from urllib.parse import urlsplit

import httpx

from .domain import CapabilityError, ConflictError, NotFoundError, ValidationError, identifier, stable_id
from .storage import _hash
from .vector_store import VectorMatch, VectorRecord, validate_limit, validate_vector


def _endpoint(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("storage endpoint must be an HTTP(S) origin")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or any(c.isspace() for c in value)):
            raise ValueError
        parsed.port
    except ValueError:
        raise ValidationError("storage endpoint must be an HTTP(S) origin without credentials or a path") from None
    return value.rstrip("/")


class S3ObjectStore:
    """SHA256 content-addressed bytes in a provisioned S3-compatible bucket."""

    def __init__(self, endpoint_url: str, access_key: str, secret_key: str, bucket: str,
                 prefix: str = "objects", region: str = "us-east-1", client=None,
                 max_object_bytes: int = 64 * 1024 * 1024):
        endpoint_url = _endpoint(endpoint_url)
        if (not isinstance(bucket, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
                or ".." in bucket):
            raise ValidationError("S3 bucket must be a valid 3-63 character bucket name")
        if (not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{0,200}", prefix)
                or any(segment in {".", ".."} for segment in prefix.split("/"))):
            raise ValidationError("S3 prefix contains an invalid path segment")
        if type(max_object_bytes) is not int or not 1 <= max_object_bytes <= 256 * 1024 * 1024:
            raise ValidationError("object size limit must be between 1 and 256 MiB")
        if not all(isinstance(v, str) and v and "\x00" not in v for v in (access_key, secret_key, region)):
            raise ValidationError("S3 credentials and region must be explicitly configured")
        self.bucket, self.prefix = bucket, prefix.strip("/")
        self.max_object_bytes = max_object_bytes
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError:
                raise CapabilityError("S3 adapter requires the optional storage dependency") from None
            try:
                client = boto3.client("s3", endpoint_url=endpoint_url,
                    aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                    region_name=region, config=Config(connect_timeout=5, read_timeout=30,
                    retries={"total_max_attempts": 1}, s3={"addressing_style": "path"}))
            except Exception:
                raise CapabilityError("cannot initialize S3 object storage") from None
        self.client = client

    def object_key(self, key: str) -> str:
        _hash(key, "object key")
        return "/".join(part for part in (self.prefix, key[:2], key) if part)

    def put(self, content: bytes) -> str:
        if not isinstance(content, bytes) or len(content) > self.max_object_bytes:
            raise ValidationError("object content must be bytes within the configured size limit")
        key = sha256(content).hexdigest()
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.object_key(key), Body=content,
                ContentType="application/octet-stream", Metadata={"sha256": key})
        except Exception:
            raise CapabilityError("S3 object upload failed") from None
        return key

    def get(self, key: str) -> bytes:
        object_key = self.object_key(key)
        body = None
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=object_key)
            if result.get("ContentLength", 0) > self.max_object_bytes:
                raise ConflictError("stored object exceeds the configured size limit")
            body = result["Body"]
            content = body.read(self.max_object_bytes + 1)
            if not isinstance(content, bytes) or len(content) > self.max_object_bytes:
                raise ConflictError("stored object exceeds the configured size limit")
        except ConflictError:
            raise
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if str(code) in {"NoSuchKey", "NotFound", "404"}:
                raise NotFoundError("object not found") from None
            raise CapabilityError("S3 object download failed") from None
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception:
                    pass
        if sha256(content).hexdigest() != key:
            raise ConflictError("stored object failed SHA-256 integrity verification")
        return content


class MilvusVectorIndex:
    """Idempotent v2 REST vector index with explicit, validated collection schema."""

    _metadata = ("record_id", "tenant_id", "asset_id", "revision_id", "profile")

    def __init__(self, endpoint_url: str, collection: str, token: str = "",
                 dimension: int | None = None, client=None, timeout_seconds: float = 30):
        self.endpoint_url = _endpoint(endpoint_url)
        if not isinstance(collection, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", collection):
            raise ValidationError("Milvus collection name must contain ASCII letters, digits and underscores")
        if dimension is not None and (type(dimension) is not int or not 1 <= dimension <= 16384):
            raise ValidationError("Milvus dimension must be between 1 and 16384")
        if not isinstance(token, str) or any(c in token for c in ("\r", "\n", "\x00")):
            raise ValidationError("Milvus token contains invalid characters")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 120:
            raise ValidationError("Milvus timeout must be between 0.1 and 120 seconds")
        self.collection, self.dimension = collection, dimension
        self._token, self.client, self.timeout_seconds = token, client, timeout_seconds
        self._lock, self._ready = Lock(), False

    def _post(self, operation: str, payload: dict):
        headers = {"Content-Type": "application/json", "Request-Timeout": str(math.ceil(self.timeout_seconds))}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        try:
            if self.client is None:
                with httpx.Client(trust_env=False, follow_redirects=False) as client:
                    response = client.post(self.endpoint_url + "/v2/vectordb/" + operation,
                        json={"collectionName": self.collection, **payload}, headers=headers,
                        timeout=self.timeout_seconds)
            else:
                response = self.client.post(self.endpoint_url + "/v2/vectordb/" + operation,
                    json={"collectionName": self.collection, **payload}, headers=headers,
                    timeout=self.timeout_seconds, follow_redirects=False)
            if response.status_code != 200:
                raise ValueError
            result = response.json()
            if not isinstance(result, dict) or type(result.get("code")) is not int or result["code"] != 0 or "data" not in result:
                raise ValueError
            return result["data"]
        except Exception:
            raise CapabilityError("Milvus request failed; verify availability and collection configuration") from None

    def _create_schema(self, dimension: int):
        fields = [{"fieldName": "pk", "dataType": "VarChar", "isPrimary": True,
                   "elementTypeParams": {"max_length": "64"}}]
        fields.extend({"fieldName": field, "dataType": "VarChar", "elementTypeParams": {"max_length": "512"}}
                      for field in self._metadata)
        fields.append({"fieldName": "vector", "dataType": "FloatVector", "elementTypeParams": {"dim": str(dimension)}})
        return {"schema": {"autoID": False, "enableDynamicField": False, "fields": fields},
                "indexParams": [{"fieldName": "vector", "indexName": "vector_cosine",
                    "metricType": "COSINE", "params": {"index_type": "AUTOINDEX"}}],
                "params": {"consistencyLevel": "Strong"}}

    def _validate_schema(self, data, dimension: int):
        def parameters(field):
            value = field.get("params", {})
            if isinstance(value, list):
                return {entry["key"]: entry["value"] for entry in value}
            return value

        try:
            fields = {field["name"]: field for field in data["fields"]}
            if (data.get("autoID", data.get("autoId")) is not False or data.get("enableDynamicField") is not False
                    or set(fields) != {"pk", "vector", *self._metadata}
                    or fields["pk"].get("primaryKey") is not True
                    or fields["pk"].get("type") != "VarChar"
                    or int(parameters(fields["pk"])["max_length"]) < 64
                    or any(fields[name].get("type") != "VarChar" for name in self._metadata)
                    or any(int(parameters(fields[name])["max_length"]) < 512 for name in self._metadata)
                    or fields["vector"].get("type") != "FloatVector"
                    or int(parameters(fields["vector"])["dim"]) != dimension
                    or not any(index.get("fieldName") == "vector" and index.get("metricType") == "COSINE" for index in data["indexes"])):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            raise ConflictError("Milvus collection schema or dimension differs; select a matching dedicated collection") from None

    def _ensure(self, dimension: int, *, create: bool) -> bool:
        with self._lock:
            if self.dimension is not None and self.dimension != dimension:
                raise ConflictError("vector dimension differs from the configured Milvus collection")
            if self._ready:
                return True
            exists = self._post("collections/has", {})
            if not isinstance(exists, dict) or type(exists.get("has")) is not bool:
                raise CapabilityError("Milvus collection response is invalid")
            if not exists["has"]:
                if not create:
                    return False
                self._post("collections/create", self._create_schema(dimension))
            self._validate_schema(self._post("collections/describe", {}), dimension)
            self._post("collections/load", {})
            self.dimension, self._ready = dimension, True
            return True

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        if isinstance(records, (str, bytes)) or not isinstance(records, Sequence) or len(records) > 10000 or any(not isinstance(row, VectorRecord) for row in records):
            raise ValidationError("vector batch must contain at most 10000 VectorRecord values")
        if not records:
            return
        dimension = len(records[0].vector)
        for record in records:
            validate_vector(record.vector, dimension)
        self._ensure(dimension, create=True)
        rows = [{"pk": stable_id(record.tenant_id, record.profile, record.record_id),
                 **{field: getattr(record, field) for field in self._metadata}, "vector": list(record.vector)}
                for record in records]
        self._post("entities/upsert", {"data": rows})

    def search(self, tenant_id: str, profile: str, vector: Sequence[float], limit: int) -> tuple[VectorMatch, ...]:
        identifier(tenant_id, "tenant_id")
        identifier(profile, "profile")
        validate_limit(limit)
        query = validate_vector(vector)
        if not self._ensure(len(query), create=False):
            return ()
        expression = "tenant_id == " + json.dumps(tenant_id, ensure_ascii=True) + " and profile == " + json.dumps(profile, ensure_ascii=True)
        data = self._post("entities/search", {"data": [list(query)], "annsField": "vector",
            "filter": expression, "limit": limit, "outputFields": list(self._metadata),
            "consistencyLevel": "Strong", "searchParams": {"metricType": "COSINE", "params": {}}})
        if not isinstance(data, list) or len(data) > limit:
            raise CapabilityError("Milvus search response is invalid")
        matches = []
        try:
            for row in data:
                match = VectorMatch(*(row[field] for field in self._metadata), score=row["distance"])
                if match.tenant_id != tenant_id or match.profile != profile:
                    raise ValueError
                if row.get("pk") != stable_id(match.tenant_id, match.profile, match.record_id):
                    raise ValueError
                matches.append(match)
        except (KeyError, TypeError, ValueError, ValidationError):
            raise CapabilityError("Milvus returned mismatched or invalid vector metadata") from None
        if len({match.record_id for match in matches}) != len(matches):
            raise CapabilityError("Milvus returned duplicate vector records")
        return tuple(matches)

    def delete(self, tenant_id: str, asset_id: str) -> None:
        identifier(tenant_id, "tenant_id")
        identifier(asset_id, "asset_id")
        exists = self._post("collections/has", {})
        if not isinstance(exists, dict) or type(exists.get("has")) is not bool:
            raise CapabilityError("Milvus collection response is invalid")
        if exists["has"]:
            data = self._post("collections/describe", {})
            try:
                field = next(field for field in data["fields"] if field["name"] == "vector")
                params = field["params"]
                if isinstance(params, list):
                    params = {entry["key"]: entry["value"] for entry in params}
                dimension = int(params["dim"])
            except (AttributeError, KeyError, TypeError, ValueError, OverflowError, StopIteration):
                raise ConflictError("Milvus collection schema differs; refusing index deletion") from None
            self._validate_schema(data, self.dimension if self.dimension is not None else dimension)
            expression = "tenant_id == " + json.dumps(tenant_id, ensure_ascii=True) + " and asset_id == " + json.dumps(asset_id, ensure_ascii=True)
            self._post("entities/delete", {"filter": expression})
