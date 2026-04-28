"""
azure_service.py
Handles Azure DefaultCredential auth, ARM-based Cosmos DB account discovery,
and connection string retrieval.
"""

from __future__ import annotations

import base64
import json as _json
import logging
import os
from functools import lru_cache
from typing import Any

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError, DefaultAzureCredential
from cachetools import TTLCache, cached

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Emulator shortcut — set QUERYMCPAL_EMULATOR=true to skip ARM entirely
# ---------------------------------------------------------------------------
_EMULATOR = os.getenv("QUERYMCPAL_EMULATOR", "").lower() == "true"
# Default emulator connection string (Cosmos DB emulator default key)
_EMULATOR_CONN_STR = (
    "mongodb://localhost:C2y6yDjf5/R+ob0N8A7Cgv30VRDJIWEHLM+4QDU5DE2nQ9nDuVTqobD4"
    "b8mGGyPMbIZnqyMcsG9CXoGXkOKDiNZ/YSj5rtXM96whEh27Y3BKg=@localhost:10255/admin?ssl=true"
)

# ---------------------------------------------------------------------------
# TTL caches — avoids hammering ARM on every tool call
# ---------------------------------------------------------------------------
_subscriptions_cache: TTLCache[Any, Any] = TTLCache(maxsize=1, ttl=3600)
_accounts_cache: TTLCache[Any, Any] = TTLCache(maxsize=10, ttl=3600)
_connstr_cache: TTLCache[Any, Any] = TTLCache(maxsize=20, ttl=3600)


@lru_cache(maxsize=1)
def _credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


def _arm_token() -> str:
    """Acquire a bearer token scoped to Azure ARM."""
    # In Docker the az CLI isn't available; accept a pre-fetched token from the host.
    env_token = os.environ.get("AZURE_ACCESS_TOKEN")
    if env_token:
        return env_token
    try:
        token = _credential().get_token("https://management.azure.com/.default")
        return token.token
    except ClientAuthenticationError:
        raise RuntimeError(
            "Azure credential not found or expired. "
            "Please run `az login` in your terminal and try again."
        )


def _arm_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_arm_token()}"}


def _upn_from_token(token_str: str) -> str:
    """Extract UPN/email from JWT payload without verifying signature."""
    try:
        payload_b64 = token_str.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload: dict[str, Any] = _json.loads(base64.b64decode(payload_b64))
        upn = payload.get("upn") or payload.get("unique_name") or payload.get("email") or ""
        return str(upn) if upn else "authenticated"
    except Exception:  # noqa: BLE001
        return "authenticated"


# ---------------------------------------------------------------------------
# Auth health check (Item 3)
# ---------------------------------------------------------------------------

def check_auth() -> dict[str, Any]:
    """Return auth status without side-effects — safe to call any time."""
    if _EMULATOR:
        return {"ok": True, "account": "emulator", "mode": "emulator"}
    try:
        cred = DefaultAzureCredential()
        token = cred.get_token("https://management.azure.com/.default")
        return {"ok": True, "account": _upn_from_token(token.token)}
    except (ClientAuthenticationError, CredentialUnavailableError):
        return {
            "ok": False,
            "reason": "Run `az login` in a terminal and restart Claude Desktop.",
        }


# ---------------------------------------------------------------------------
# Subscription & account discovery
# ---------------------------------------------------------------------------

@cached(_subscriptions_cache)
def list_subscriptions() -> list[dict[str, Any]]:
    url = "https://management.azure.com/subscriptions?api-version=2020-01-01"
    resp = requests.get(url, headers=_arm_headers(), timeout=15)
    resp.raise_for_status()
    return list(resp.json().get("value", []))


@cached(_accounts_cache)
def list_cosmos_accounts() -> list[dict[str, Any]]:
    """Return all Cosmos DB accounts visible to the current credential."""
    if _EMULATOR:
        return [
            {
                "name": "local-emulator",
                "id": "emulator",
                "subscription": "local",
                "location": "localhost",
                "kind": "GlobalDocumentDB",
            }
        ]

    accounts: list[dict[str, Any]] = []
    for sub in list_subscriptions():
        sub_id = sub["subscriptionId"]
        sub_name = sub.get("displayName", sub_id)
        url = (
            f"https://management.azure.com/subscriptions/{sub_id}/resources"
            "?api-version=2021-04-01"
            "&$filter=resourceType eq 'Microsoft.DocumentDB/databaseAccounts'"
        )
        resp = requests.get(url, headers=_arm_headers(), timeout=15)
        if resp.status_code != 200:
            logger.warning("Could not list resources in subscription %s: %s", sub_id, resp.text)
            continue
        for acct in resp.json().get("value", []):
            accounts.append(
                {
                    "name": acct["name"],
                    "id": acct["id"],
                    "subscription": sub_name,
                    "location": acct.get("location", "unknown"),
                    "kind": acct.get("kind", "GlobalDocumentDB"),
                }
            )
    return accounts


@cached(_connstr_cache)
def get_connection_string(account_id: str) -> str:
    """Retrieve the primary connection string for a Cosmos DB account via ARM."""
    if _EMULATOR:
        return _EMULATOR_CONN_STR

    url = (
        f"https://management.azure.com/{account_id}"
        "/listConnectionStrings?api-version=2023-03-15"
    )
    resp = requests.post(url, headers=_arm_headers(), timeout=15)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to retrieve connection string for {account_id}: "
            f"{resp.status_code} – {resp.text}"
        )
    conn_strings: list[dict[str, Any]] = resp.json().get("connectionStrings", [])
    if not conn_strings:
        raise RuntimeError(f"No connection strings returned for account {account_id}")
    # Prefer the primary MongoDB connection string
    for cs in conn_strings:
        desc = cs.get("description", "").lower()
        if "primary" in desc and "mongo" in desc:
            return str(cs["connectionString"])
    return str(conn_strings[0]["connectionString"])
