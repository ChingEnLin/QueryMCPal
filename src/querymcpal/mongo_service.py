"""
mongo_service.py
Read-only MongoDB operations against Azure Cosmos DB (MongoDB API).
Connection objects are cached per connection string to avoid reconnects.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from bson import ObjectId
from cachetools import TTLCache
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ServerSelectionTimeoutError

logger = logging.getLogger(__name__)

# Cache clients (connection pool reuse)
_client_cache: dict[str, MongoClient[dict[str, Any]]] = {}

# Schema inference cache — TTL 10 min
_schema_cache: TTLCache[Any, Any] = TTLCache(maxsize=200, ttl=600)

# Pattern used to scrub connection strings from exception messages before logging
_CONN_STR_RE = re.compile(
    r"(mongodb://[^\s\"']+|AccountEndpoint=[^\s\"';]+)", re.IGNORECASE
)


def _scrub(text: str) -> str:
    """Replace credential substrings with [REDACTED]."""
    return _CONN_STR_RE.sub("[REDACTED]", text)


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------

def _get_client(connection_string: str) -> MongoClient[dict[str, Any]]:
    if connection_string not in _client_cache:
        try:
            client: MongoClient[dict[str, Any]] = MongoClient(
                connection_string,
                serverSelectionTimeoutMS=8000,
                connectTimeoutMS=8000,
            )
            client.admin.command("ping")  # fail fast on connectivity issues
            _client_cache[connection_string] = client
        except ServerSelectionTimeoutError as exc:
            raise RuntimeError(
                "Could not connect to Cosmos DB. "
                "Check your network connection and that the account is accessible."
            ) from exc
        except Exception as exc:
            raise RuntimeError(_scrub(str(exc))) from exc
    return _client_cache[connection_string]


def _get_collection(
    connection_string: str, db_name: str, collection_name: str
) -> Collection[dict[str, Any]]:
    client = _get_client(connection_string)
    db: Database[dict[str, Any]] = client[db_name]
    return db[collection_name]


# ---------------------------------------------------------------------------
# Database / collection discovery
# ---------------------------------------------------------------------------

def list_databases(connection_string: str) -> list[dict[str, Any]]:
    client = _get_client(connection_string)
    db_names = [n for n in client.list_database_names() if n not in ("admin", "local", "config")]
    result: list[dict[str, Any]] = []
    for name in db_names:
        db: Database[dict[str, Any]] = client[name]
        collections = db.list_collection_names()
        result.append({"database": name, "collections": collections, "collection_count": len(collections)})
    return result


def list_collections(connection_string: str, db_name: str) -> list[dict[str, Any]]:
    client = _get_client(connection_string)
    db: Database[dict[str, Any]] = client[db_name]
    collections: list[dict[str, Any]] = []
    for name in db.list_collection_names():
        col = db[name]
        try:
            count = col.estimated_document_count()
        except Exception:  # noqa: BLE001
            count = -1
        collections.append({"collection": name, "estimated_count": count})
    return collections


# ---------------------------------------------------------------------------
# Schema inference
# ---------------------------------------------------------------------------

def _infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        inner = {_infer_type(v) for v in value[:5]} if value else {"unknown"}
        return f"array<{' | '.join(sorted(inner))}>"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, ObjectId):
        return "ObjectId"
    return type(value).__name__


def _merge_schema(base: dict[str, Any], incoming: dict[str, Any], path: str = "") -> dict[str, Any]:
    for key, value in incoming.items():
        full_key = f"{path}.{key}" if path else key
        inferred = _infer_type(value)
        if full_key not in base:
            base[full_key] = {"types": {inferred}, "nullable": False}
        else:
            base[full_key]["types"].add(inferred)
        # Recurse into nested objects (one level deep only for perf)
        if isinstance(value, dict) and not path:
            _merge_schema(base, value, path=key)
    return base


def infer_schema(
    connection_string: str, db_name: str, collection_name: str, sample_size: int = 100
) -> dict[str, Any]:
    cache_key = f"{connection_string}|{db_name}|{collection_name}"
    if cache_key in _schema_cache:
        return _schema_cache[cache_key]  # type: ignore[no-any-return]

    col = _get_collection(connection_string, db_name, collection_name)
    docs = list(col.find({}, limit=sample_size))
    schema: dict[str, Any] = {}
    for doc in docs:
        _merge_schema(schema, doc)

    # Serialise sets → sorted lists for JSON output
    result: dict[str, Any] = {
        k: {"types": sorted(v["types"]), "nullable": "null" in v["types"]}
        for k, v in schema.items()
    }
    _schema_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

_BSON_SAFE_RE = re.compile(r"ObjectId\(['\"](.+?)['\"]\)")


def _parse_filter(filter_str: str | None) -> dict[str, Any]:
    """Parse a JSON filter string, converting ObjectId("…") shorthand."""
    if not filter_str:
        return {}
    placeholders: dict[str, ObjectId] = {}

    def _replace(m: re.Match[str]) -> str:
        ph = f'"__oid_{len(placeholders)}__"'
        placeholders[ph.strip('"')] = ObjectId(m.group(1))
        return ph

    cleaned = _BSON_SAFE_RE.sub(_replace, filter_str)
    parsed: Any = json.loads(cleaned)

    def _restore(obj: Any) -> Any:
        if isinstance(obj, str) and obj in placeholders:
            return placeholders[obj]
        if isinstance(obj, dict):
            return {k: _restore(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_restore(v) for v in obj]
        return obj

    result: dict[str, Any] = _restore(parsed)
    return result


def _bson_safe(obj: Any) -> Any:
    """Recursively make a MongoDB document JSON-serialisable."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _bson_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bson_safe(v) for v in obj]
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return obj


def query_collection(
    connection_string: str,
    db_name: str,
    collection_name: str,
    filter_str: str | None = None,
    projection_str: str | None = None,
    sort_str: str | None = None,
    limit: int = 20,
    skip: int = 0,
) -> dict[str, Any]:
    col = _get_collection(connection_string, db_name, collection_name)
    filter_doc = _parse_filter(filter_str)
    projection: dict[str, Any] | None = json.loads(projection_str) if projection_str else None
    sort: list[Any] | None = list(json.loads(sort_str).items()) if sort_str else None

    cursor = col.find(filter_doc, projection)
    if sort:
        cursor = cursor.sort(sort)
    cursor = cursor.skip(skip).limit(limit)

    docs = [_bson_safe(d) for d in cursor]
    total = col.count_documents(filter_doc)

    return {
        "total_matching": total,
        "returned": len(docs),
        "skip": skip,
        "limit": limit,
        "documents": docs,
    }


def aggregate_collection(
    connection_string: str,
    db_name: str,
    collection_name: str,
    pipeline_str: str,
    limit: int = 50,
) -> dict[str, Any]:
    col = _get_collection(connection_string, db_name, collection_name)
    pipeline: list[Any] = json.loads(pipeline_str)
    # Safety: inject a hard limit at the end if not present
    if not any("$limit" in str(s) for s in pipeline[-2:]):
        pipeline.append({"$limit": min(limit, 200)})

    results = [_bson_safe(d) for d in col.aggregate(pipeline)]
    return {"pipeline_stages": len(pipeline), "returned": len(results), "results": results}


def count_documents(
    connection_string: str,
    db_name: str,
    collection_name: str,
    filter_str: str | None = None,
) -> dict[str, Any]:
    col = _get_collection(connection_string, db_name, collection_name)
    filter_doc = _parse_filter(filter_str)
    count = col.count_documents(filter_doc)
    return {"collection": collection_name, "filter": filter_doc, "count": count}


def get_document_by_id(
    connection_string: str,
    db_name: str,
    collection_name: str,
    document_id: str,
) -> dict[str, Any] | None:
    col = _get_collection(connection_string, db_name, collection_name)
    try:
        doc = col.find_one({"_id": ObjectId(document_id)})
    except Exception:  # noqa: BLE001
        doc = col.find_one({"_id": document_id})
    return _bson_safe(doc) if doc else None


def get_distinct_values(
    connection_string: str,
    db_name: str,
    collection_name: str,
    field: str,
    filter_str: str | None = None,
) -> dict[str, Any]:
    col = _get_collection(connection_string, db_name, collection_name)
    filter_doc = _parse_filter(filter_str)
    values = col.distinct(field, filter_doc)
    return {
        "field": field,
        "distinct_count": len(values),
        "values": [_bson_safe(v) for v in values[:100]],
    }
