"""
context.py
Lightweight in-process session that remembers which Cosmos account / database /
collection the user is currently working with.  This lets Claude omit redundant
arguments after the first `connect` call.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Session:
    account_id: str = ""
    account_name: str = ""
    connection_string: str = ""
    database: str = ""
    collection: str = ""

    def is_connected(self) -> bool:
        return bool(self.connection_string)

    def context_summary(self) -> str:
        parts = []
        if self.account_name:
            parts.append(f"account={self.account_name}")
        if self.database:
            parts.append(f"db={self.database}")
        if self.collection:
            parts.append(f"collection={self.collection}")
        return ", ".join(parts) if parts else "no active context"

    def clear(self) -> None:
        self.account_id = ""
        self.account_name = ""
        self.connection_string = ""
        self.database = ""
        self.collection = ""


# Global singleton — one per server process
session = Session()
