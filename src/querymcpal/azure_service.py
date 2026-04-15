"""
azure_service.py
Handles Azure DefaultCredential auth, ARM-based Cosmos DB account discovery,
and connection string retrieval.
"""

from __future__ import annotations

import logging
from functools import lru_cache

import os

import requests
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import DefaultAzureCredential
from cachetools import TTLCache, cached

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TTL caches — avoids hammering ARM on every tool call
# ---------------------------------------------------------------------------
_subscriptions_cache: TTLCache = TTLCache(maxsize=1, ttl=3600)
_accounts_cache: TTLCache = TTLCache(maxsize=10, ttl=3600)
_connstr_cache: TTLCache = TTLCache(maxsize=20, ttl=3600)


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


# ---------------------------------------------------------------------------
# Subscription & account discovery
# ---------------------------------------------------------------------------

@cached(_subscriptions_cache)
def list_subscriptions() -> list[dict]:
    url = "https://management.azure.com/subscriptions?api-version=2020-01-01"
    resp = requests.get(url, headers=_arm_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json().get("value", [])


@cached(_accounts_cache)
def list_cosmos_accounts() -> list[dict]:
    """Return all Cosmos DB accounts visible to the current credential."""
    accounts: list[dict] = []
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
    conn_strings = resp.json().get("connectionStrings", [])
    if not conn_strings:
        raise RuntimeError(f"No connection strings returned for account {account_id}")
    # Prefer the primary MongoDB connection string
    for cs in conn_strings:
        desc = cs.get("description", "").lower()
        if "primary" in desc and "mongo" in desc:
            return cs["connectionString"]
    return conn_strings[0]["connectionString"]
