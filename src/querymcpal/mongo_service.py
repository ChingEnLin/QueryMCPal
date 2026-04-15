"""
mongo_service.py
Read-only MongoDB operations against Azure Cosmos DB (MongoDB API).
Connection objects are cached per connection string to avoid reconnects.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any

from bson import ObjectId
from cachetools import TTLCache, cached
from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import ServerSelectionTimeoutError

logger = logging.getLogger(__name__)

# Cache clients (connection pool reuse)
_client_cache: dict[str, MongoClient] = {}

# Schema inference cache — TTL 10 min
_schema_cache: TTLCache = TTLCache(maxsize=200, ttl=600)


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------

def _get_client(connection_string: str) -> MongoClient:
    if connection_string not in _client_cache:
        try:
            client = MongoClient(
                connection_string,
                serverSelectionTimeoutMS=8000,
                connectTimeoutMS=8000,
            )
            client.admin.command("ping")  # fail fast on connectivity issues
            _client_cache[connection_string] = client
        except ServerSelectionTimeoutError:
            raise RuntimeError(
                "Could not connect to Cosmos DB. "
                "Check your network connection and that the account is accessible."
            )
    return _client_cache[connection_string]


def _get_collection(connection_string: str, db_name: str, collection_name: str) -> Collection:
    client = _get_client(connection_string)
    return client[db_name][collection_name]


# ---------------------------------------------------------------------------
# Database / collection discovery
# ---------------------------------------------------------------------------

def list_databases(connection_string: str) -> list[dict]:
    client = _get_client(connection_string)
    db_names = [n for n in client.list_database_names() if n not in ("admin", "local", "config")]
    result = []
    for name in db_names:
        db: Database = client[name]
        collections = db.list_collection_names()
        result.append({"database": name, "collections": collections, "collection_count": len(collections)})
    return result


def list_collections(connection_string: str, db_name: str) -> list[dict]:
    client = _get_client(connection_string)
    db: Database = client[db_name]
    collections = []
    for name in db.list_collection_names():
        col = db[name]
        try:
            count = col.estimated_document_count()
        except Exception:
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


def _merge_schema(base: dict, incoming: dict, path: str = "") -> dict:
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


def infer_schema(connection_string: str, db_name: str, collection_name: str, sample_size: int = 100) -> dict:
    cache_key = f"{connection_string}|{db_name}|{collection_name}"
    if cache_key in _schema_cache:
        return _schema_cache[cache_key]

    col = _get_collection(connection_string, db_name, collection_name)
    docs = list(col.find({}, limit=sample_size))
    schema: dict = {}
    for doc in docs:
        _merge_schema(schema, doc)

    # Serialise sets → sorted lists for JSON output
    result = {
        k: {"types": sorted(v["types"]), "nullable": "null" in v["types"]}
        for k, v in schema.items()
    }
    _schema_cache[cache_key] = result
    return result


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------

_BSON_SAFE = re.compile(r"ObjectId\(['\"](.+?)['\"]\)")


def _parse_filter(filter_str: str | None) -> dict:
    """
    Parse a JSON filter string safely.
    Converts ObjectId("…") shorthand to the actual ObjectId.
    """
    if not filter_str:
        return {}
    # Replace ObjectId("…") with a sentinel so json.loads works
    placeholders: dict[str, ObjectId] = {}
    def _replace(m: re.Match) -> str:
        ph = f'"__oid_{len(placeholders)}__"'
        placeholders[ph.strip('"')] = ObjectId(m.group(1))
        return ph
    cleaned = _BSON_SAFE.sub(_replace, filter_str)
    parsed = json.loads(cleaned)
    # Re-inject ObjectId values
    def _restore(obj: Any) -> Any:
        if isinstance(obj, str) and obj in placeholders:
            return placeholders[obj]
        if isinstance(obj, dict):
            return {k: _restore(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_restore(v) for v in obj]
        return obj
    return _restore(parsed)


def _bson_safe(obj: Any) -> Any:
    """Recursively make a MongoDB document JSON-serialisable."""
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _bson_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bson_safe(v) for v in obj]
    # datetime etc.
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
) -> dict:
    col = _get_collection(connection_string, db_name, collection_name)
    filter_doc = _parse_filter(filter_str)
    projection = json.loads(projection_str) if projection_str else None
    sort = list(json.loads(sort_str).items()) if sort_str else None

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
) -> dict:
    col = _get_collection(connection_string, db_name, collection_name)
    pipeline: list = json.loads(pipeline_str)
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
) -> dict:
    col = _get_collection(connection_string, db_name, collection_name)
    filter_doc = _parse_filter(filter_str)
    count = col.count_documents(filter_doc)
    return {"collection": collection_name, "filter": filter_doc, "count": count}


def get_document_by_id(
    connection_string: str,
    db_name: str,
    collection_name: str,
    document_id: str,
) -> dict | None:
    col = _get_collection(connection_string, db_name, collection_name)
    try:
        doc = col.find_one({"_id": ObjectId(document_id)})
    except Exception:
        doc = col.find_one({"_id": document_id})
    return _bson_safe(doc) if doc else None


def get_distinct_values(
    connection_string: str,
    db_name: str,
    collection_name: str,
    field: str,
    filter_str: str | None = None,
) -> dict:
    col = _get_collection(connection_string, db_name, collection_name)
    filter_doc = _parse_filter(filter_str)
    values = col.distinct(field, filter_doc)
    return {
        "field": field,
        "distinct_count": len(values),
        "values": [_bson_safe(v) for v in values[:100]],  # cap at 100
    }
