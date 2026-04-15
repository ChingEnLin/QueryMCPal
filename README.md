# QueryMCPal

An MCP server that lets Claude talk directly to your Azure Cosmos DB (MongoDB API) accounts — no service principals, no managed identities, no connection strings in config files. Just `az login` and go.

## Why this instead of the official toolkit?

The [official Azure Cosmos DB MCP Toolkit](https://github.com/AzureCosmosDB/MCPToolkit) requires you to supply a connection string up front. That means either hardcoding credentials in your Claude Desktop config, or setting up a service principal with a client secret — extra Azure resources to manage, extra things to rotate, extra things to leak.

QueryMCPal takes a different approach:

| | QueryMCPal | Official Toolkit |
|---|---|---|
| **Auth** | Your existing `az login` session | Connection string in config |
| **Account discovery** | Automatic — finds all your Cosmos accounts via ARM | Manual — one connection string per server |
| **Credentials in config** | None | Yes (connection string) |
| **Extra Azure setup** | None | Service principal or managed identity recommended |
| **Multi-account** | Switch between accounts in the same session | One instance per account |
| **Runs as** | Docker container (isolated) | Node.js process on host |

If you already use the Azure CLI day-to-day, QueryMCPal just works with what you have.

## Features

- **Auto-discovery** — lists every Cosmos DB account your Azure credential can see, across all subscriptions
- **Session context** — connect once, then query without repeating the account/database/collection on every message
- **Schema inference** — samples a collection and returns field names, types, and nullability
- **Full read toolkit** — `find`, `aggregate`, `count`, `distinct`, `get by ID`
- **BSON-safe output** — ObjectIds, datetimes, and other BSON types are serialised cleanly for Claude
- **Read-only** — no write operations exposed, safe to point at production

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) (`brew install azure-cli`)
- An Azure account with access to at least one Cosmos DB (MongoDB API) account

## Setup

**1. Log in to Azure**

```bash
az login
```

**2. Clone and build the image**

```bash
git clone https://github.com/ChingEnLin/QueryMCPal.git
cd QueryMCPal
docker build --platform linux/arm64 -t querymcpal:dev .
```

> For Intel Macs change `linux/arm64` to `linux/amd64` in the build command and the config below.

**3. Add to Claude Desktop**

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "querymcpal": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--platform", "linux/arm64",
        "-v", "/Users/YOUR_USERNAME/.azure:/home/mcpuser/.azure",
        "querymcpal:dev"
      ]
    }
  }
}
```

Replace `YOUR_USERNAME` with your macOS username (`echo $USER`). Use the full path — `${HOME}` does not expand in Claude Desktop's config.

**4. Restart Claude Desktop**

Quit and relaunch. QueryMCPal will appear as a connected MCP server.

## Usage

Once connected, talk to Claude naturally:

```
"What Cosmos DB accounts do I have access to?"
"Connect to my-cosmos-account"
"What databases are in there?"
"Show me the schema of the orders collection"
"Find all orders where status is 'pending', limit 10"
"Count documents in the users collection where createdAt is after 2024-01-01"
"What are the distinct values for the country field?"
```

Claude will chain the tools automatically — you don't need to know which tool does what.

## How authentication works

When the container starts, it mounts your local `~/.azure` directory (where `az login` stores its token cache). The Azure CLI inside the container reads those cached credentials — no tokens leave your machine, nothing is stored in config files, and your existing Azure RBAC permissions apply as-is.

Token refresh is handled automatically by the Azure identity library on each request.

## Available tools

| Tool | Description |
|------|-------------|
| `list_cosmos_accounts` | List all accessible Cosmos DB accounts across subscriptions |
| `connect_account` | Connect to an account and store it in session context |
| `list_databases` | List databases in the connected account |
| `list_collections` | List collections in a database with estimated counts |
| `describe_collection` | Infer schema by sampling documents |
| `get_document` | Fetch a single document by `_id` |
| `find_documents` | Run a `find()` with filter, projection, sort, skip, limit |
| `aggregate` | Run an aggregation pipeline |
| `count_documents` | Count documents matching a filter |
| `distinct_values` | Get distinct values for a field |
| `show_context` | Show the current session (account / database / collection) |
| `set_context` | Change the active database or collection |
| `clear_context` | Disconnect and reset session |

## Limitations

- macOS + Apple Silicon only in this configuration (ARM64 Docker image). Intel Mac and Linux support is straightforward — change the `--platform` flag.
- Read-only — no insert, update, or delete operations by design.
- Requires the MongoDB API variant of Cosmos DB. The Core (SQL) API is not supported.
