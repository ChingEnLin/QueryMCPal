"""
server.py
QueryMCPal – Azure Cosmos DB (MongoDB API) MCP Server

Tools exposed to Claude:
  Discovery  : list_cosmos_accounts, connect_account, list_databases, list_collections
  Inspection : describe_collection, get_document
  Query      : find_documents, aggregate, count_documents, distinct_values
  Context    : show_context, clear_context
"""

from __future__ import annotations

import json
import logging
import sys

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from querymcpal import azure_service, mongo_service
from querymcpal.context import session

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("querymcpal")

# ---------------------------------------------------------------------------
# Server bootstrap
# ---------------------------------------------------------------------------

app = Server("querymcpal")

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

@app.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        # ── Discovery ──────────────────────────────────────────────────────
        types.Tool(
            name="list_cosmos_accounts",
            description=(
                "List all Azure Cosmos DB accounts accessible via the current "
                "Azure credential (az login / managed identity). Returns account "
                "names, IDs, subscriptions, and locations."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="connect_account",
            description=(
                "Connect to a specific Cosmos DB account by its ARM resource ID. "
                "Retrieves the connection string and stores it in session context. "
                "Optionally sets a default database and collection."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "string",
                        "description": "ARM resource ID of the Cosmos DB account (from list_cosmos_accounts).",
                    },
                    "account_name": {
                        "type": "string",
                        "description": "Human-readable account name (for display purposes).",
                    },
                    "database": {
                        "type": "string",
                        "description": "Optional: set a default database for subsequent calls.",
                    },
                    "collection": {
                        "type": "string",
                        "description": "Optional: set a default collection for subsequent calls.",
                    },
                },
                "required": ["account_id", "account_name"],
            },
        ),
        types.Tool(
            name="list_databases",
            description=(
                "List all databases in the currently connected Cosmos DB account, "
                "along with their collections and collection count."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="list_collections",
            description=(
                "List all collections in a database with their estimated document counts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "database": {
                        "type": "string",
                        "description": "Database name. Defaults to session database if set.",
                    }
                },
                "required": [],
            },
        ),
        # ── Inspection ─────────────────────────────────────────────────────
        types.Tool(
            name="describe_collection",
            description=(
                "Infer the schema of a collection by sampling up to 100 documents. "
                "Returns field names, observed types, and nullable flags. "
                "Great for understanding data structure before querying."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "database": {"type": "string", "description": "Database name. Defaults to session database."},
                    "collection": {"type": "string", "description": "Collection name. Defaults to session collection."},
                    "sample_size": {
                        "type": "integer",
                        "description": "Number of documents to sample (default 100, max 500).",
                        "default": 100,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_document",
            description="Fetch a single document by its _id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "document_id": {"type": "string", "description": "The document _id (string or ObjectId)."},
                    "database": {"type": "string"},
                    "collection": {"type": "string"},
                },
                "required": ["document_id"],
            },
        ),
        # ── Query ──────────────────────────────────────────────────────────
        types.Tool(
            name="find_documents",
            description=(
                "Execute a MongoDB find() query. "
                "Accepts a JSON filter, optional projection, sort, skip, and limit. "
                "Returns matching documents plus total match count. "
                "Example filter: '{\"status\": \"active\", \"age\": {\"$gt\": 30}}'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "JSON MongoDB filter. Default: {} (all documents).",
                        "default": "{}",
                    },
                    "projection": {
                        "type": "string",
                        "description": "JSON projection, e.g. '{\"name\": 1, \"email\": 1, \"_id\": 0}'.",
                    },
                    "sort": {
                        "type": "string",
                        "description": "JSON sort spec, e.g. '{\"createdAt\": -1}'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max documents to return (default 20, max 200).",
                        "default": 20,
                    },
                    "skip": {
                        "type": "integer",
                        "description": "Number of documents to skip (for pagination).",
                        "default": 0,
                    },
                    "database": {"type": "string"},
                    "collection": {"type": "string"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="aggregate",
            description=(
                "Execute a MongoDB aggregation pipeline. "
                "Accepts a JSON array of pipeline stages. "
                "A $limit stage is appended automatically if not present. "
                "Example: '[{\"$match\": {\"status\": \"active\"}}, {\"$group\": {\"_id\": \"$country\", \"count\": {\"$sum\": 1}}}]'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "pipeline": {
                        "type": "string",
                        "description": "JSON array of MongoDB aggregation pipeline stages.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50, max 200).",
                        "default": 50,
                    },
                    "database": {"type": "string"},
                    "collection": {"type": "string"},
                },
                "required": ["pipeline"],
            },
        ),
        types.Tool(
            name="count_documents",
            description="Count documents matching a filter. Fast and doesn't return document data.",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "JSON filter. Default: {} (count all).",
                        "default": "{}",
                    },
                    "database": {"type": "string"},
                    "collection": {"type": "string"},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="distinct_values",
            description=(
                "Get all distinct values for a field. Useful for understanding "
                "enums, categories, and cardinality."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "Field name (supports dot notation, e.g. 'address.country')."},
                    "filter": {"type": "string", "description": "Optional JSON filter to scope the distinct query."},
                    "database": {"type": "string"},
                    "collection": {"type": "string"},
                },
                "required": ["field"],
            },
        ),
        # ── Session context ────────────────────────────────────────────────
        types.Tool(
            name="show_context",
            description="Show the current session context: connected account, database, and collection.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        types.Tool(
            name="set_context",
            description="Update the active database and/or collection without reconnecting.",
            inputSchema={
                "type": "object",
                "properties": {
                    "database": {"type": "string", "description": "Set the active database."},
                    "collection": {"type": "string", "description": "Set the active collection."},
                },
                "required": [],
            },
        ),
        types.Tool(
            name="clear_context",
            description="Disconnect from the current account and clear all session state.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_connection() -> str | None:
    """Return error message if not connected, else None."""
    if not session.is_connected():
        return (
            "Not connected to any Cosmos DB account. "
            "Call list_cosmos_accounts first, then connect_account."
        )
    return None

def _resolve_db(args: dict) -> str | None:
    return args.get("database") or session.database or None

def _resolve_col(args: dict) -> str | None:
    return args.get("collection") or session.collection or None

def _require_db_col(args: dict) -> tuple[str, str] | tuple[None, str]:
    db = _resolve_db(args)
    col = _resolve_col(args)
    if not db:
        return None, "No database specified. Pass 'database' or call set_context first."
    if not col:
        return None, "No collection specified. Pass 'collection' or call set_context first."
    return (db, col), None  # type: ignore[return-value]

def _ok(data: dict | list | str) -> list[types.TextContent]:
    text = json.dumps(data, indent=2, default=str) if not isinstance(data, str) else data
    return [types.TextContent(type="text", text=text)]

def _err(msg: str) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=f"ERROR: {msg}")]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    try:
        return await _dispatch(name, arguments)
    except Exception as exc:
        logger.exception("Tool %s raised an exception", name)
        return _err(str(exc))


async def _dispatch(name: str, args: dict) -> list[types.TextContent]:

    # ── list_cosmos_accounts ────────────────────────────────────────────────
    if name == "list_cosmos_accounts":
        accounts = azure_service.list_cosmos_accounts()
        if not accounts:
            return _ok("No Cosmos DB accounts found. Ensure you are logged in (az login) and have access.")
        return _ok(accounts)

    # ── connect_account ─────────────────────────────────────────────────────
    if name == "connect_account":
        account_id = args["account_id"]
        account_name = args["account_name"]
        conn_str = azure_service.get_connection_string(account_id)
        session.account_id = account_id
        session.account_name = account_name
        session.connection_string = conn_str
        session.database = args.get("database", "")
        session.collection = args.get("collection", "")
        return _ok(
            {
                "status": "connected",
                "account": account_name,
                "database": session.database or "(not set)",
                "collection": session.collection or "(not set)",
                "hint": "Call list_databases to explore, or set_context to pick a database/collection.",
            }
        )

    # ── list_databases ──────────────────────────────────────────────────────
    if name == "list_databases":
        if err := _require_connection():
            return _err(err)
        dbs = mongo_service.list_databases(session.connection_string)
        return _ok(dbs)

    # ── list_collections ────────────────────────────────────────────────────
    if name == "list_collections":
        if err := _require_connection():
            return _err(err)
        db = _resolve_db(args)
        if not db:
            return _err("No database specified. Pass 'database' or call set_context first.")
        cols = mongo_service.list_collections(session.connection_string, db)
        return _ok(cols)

    # ── describe_collection ─────────────────────────────────────────────────
    if name == "describe_collection":
        if err := _require_connection():
            return _err(err)
        target, err = _require_db_col(args)
        if err:
            return _err(err)
        db, col = target
        sample = min(args.get("sample_size", 100), 500)
        schema = mongo_service.infer_schema(session.connection_string, db, col, sample)
        return _ok({"database": db, "collection": col, "sampled": sample, "schema": schema})

    # ── get_document ────────────────────────────────────────────────────────
    if name == "get_document":
        if err := _require_connection():
            return _err(err)
        target, err = _require_db_col(args)
        if err:
            return _err(err)
        db, col = target
        doc = mongo_service.get_document_by_id(session.connection_string, db, col, args["document_id"])
        if doc is None:
            return _err(f"Document '{args['document_id']}' not found in {db}.{col}")
        return _ok(doc)

    # ── find_documents ──────────────────────────────────────────────────────
    if name == "find_documents":
        if err := _require_connection():
            return _err(err)
        target, err = _require_db_col(args)
        if err:
            return _err(err)
        db, col = target
        limit = min(args.get("limit", 20), 200)
        result = mongo_service.query_collection(
            connection_string=session.connection_string,
            db_name=db,
            collection_name=col,
            filter_str=args.get("filter"),
            projection_str=args.get("projection"),
            sort_str=args.get("sort"),
            limit=limit,
            skip=args.get("skip", 0),
        )
        return _ok(result)

    # ── aggregate ───────────────────────────────────────────────────────────
    if name == "aggregate":
        if err := _require_connection():
            return _err(err)
        target, err = _require_db_col(args)
        if err:
            return _err(err)
        db, col = target
        limit = min(args.get("limit", 50), 200)
        result = mongo_service.aggregate_collection(
            connection_string=session.connection_string,
            db_name=db,
            collection_name=col,
            pipeline_str=args["pipeline"],
            limit=limit,
        )
        return _ok(result)

    # ── count_documents ─────────────────────────────────────────────────────
    if name == "count_documents":
        if err := _require_connection():
            return _err(err)
        target, err = _require_db_col(args)
        if err:
            return _err(err)
        db, col = target
        result = mongo_service.count_documents(
            session.connection_string, db, col, args.get("filter")
        )
        return _ok(result)

    # ── distinct_values ─────────────────────────────────────────────────────
    if name == "distinct_values":
        if err := _require_connection():
            return _err(err)
        target, err = _require_db_col(args)
        if err:
            return _err(err)
        db, col = target
        result = mongo_service.get_distinct_values(
            session.connection_string, db, col, args["field"], args.get("filter")
        )
        return _ok(result)

    # ── show_context ────────────────────────────────────────────────────────
    if name == "show_context":
        return _ok(
            {
                "connected": session.is_connected(),
                "account": session.account_name or "(none)",
                "account_id": session.account_id or "(none)",
                "database": session.database or "(not set)",
                "collection": session.collection or "(not set)",
            }
        )

    # ── set_context ─────────────────────────────────────────────────────────
    if name == "set_context":
        if "database" in args:
            session.database = args["database"]
        if "collection" in args:
            session.collection = args["collection"]
        return _ok({"status": "context updated", "context": session.context_summary()})

    # ── clear_context ───────────────────────────────────────────────────────
    if name == "clear_context":
        session.clear()
        return _ok({"status": "disconnected", "context": "cleared"})

    return _err(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    import asyncio
    asyncio.run(_run())


async def _run() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    main()
