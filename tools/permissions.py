from __future__ import annotations

import logging

from storage.database import Database

logger = logging.getLogger(__name__)

ASK = "ask"
ALLOW = "allow"
POLICIES = (ASK, ALLOW)
POLICY_LABELS = {ASK: "Perguntar sempre", ALLOW: "Permitir sempre"}
_PREFIX = "tool_permission."


class PermissionStore:
    """Persists one authorization policy per tool.

    Missing, invalid or unreadable values always resolve to ASK. Values are read
    from the database on every call so that a change takes effect immediately, and
    environment variables are deliberately never consulted.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    def policy(self, tool_id: str) -> str:
        try:
            value = self.database.get_setting(_PREFIX + tool_id, ASK)
        except Exception:
            logger.warning("Política de %s ilegível; usando 'perguntar sempre'.", tool_id)
            return ASK
        return value if value in POLICIES else ASK

    def set_policy(self, tool_id: str, policy: str) -> None:
        if policy not in POLICIES:
            raise ValueError("Política de autorização inválida.")
        self.database.set_setting(_PREFIX + tool_id, policy)
        logger.info("Política da ferramenta %s alterada para %s", tool_id, policy)
