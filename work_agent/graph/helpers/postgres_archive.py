"""Immutable PostgreSQL context archive, recreated from a full task UUID.

The content hash is independent of traversal order and item labels. An archive
write is not an execution receipt: only a completed graph checkpoint advances
the tool cursor. Reads always include the task scope.
"""

from __future__ import annotations

import hashlib
import re
from uuid import UUID

from psycopg.rows import dict_row

from work_agent.core.db import get_pool
from work_agent.graph.helpers.context_archive import ArchiveRef
from work_agent.graph.helpers.context_selector import ContextItem

_ARTIFACT = re.compile(r"sha256_[0-9a-f]{64}\Z")


class PostgresContextArchive:
    def __init__(self, *, run_id: str, pool=None) -> None:
        self.task_id = str(UUID(run_id))
        self._pool = pool

    @property
    def pool(self):
        return self._pool if self._pool is not None else get_pool()

    def _ref(self, artifact_id: str, chars: int) -> ArchiveRef:
        return ArchiveRef(
            artifact_id=artifact_id,
            path=f"postgres://diagnosis_archive/{self.task_id}/{artifact_id}",
            chars=chars,
        )

    @property
    def refs(self) -> list[ArchiveRef]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT artifact_id, chars FROM diagnosis_archive "
                "WHERE task_id=%s ORDER BY artifact_id", (self.task_id,)
            ).fetchall()
        return [self._ref(row["artifact_id"], row["chars"]) for row in rows]

    def store(self, item: ContextItem) -> ArchiveRef:
        artifact_id = "sha256_" + hashlib.sha256(item.text.encode("utf-8")).hexdigest()
        # Explicit transaction also commits before returning when the shared
        # checkpointer pool uses autocommit connections.
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO diagnosis_archive (task_id, artifact_id, content, chars) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (task_id, artifact_id) DO NOTHING",
                (self.task_id, artifact_id, item.text, len(item.text)),
            )
        return self._ref(artifact_id, len(item.text))

    def store_all(self, items: list[ContextItem]) -> list[ArchiveRef]:
        return [self.store(item) for item in items]

    def read(self, artifact_id: str) -> str:
        if not _ARTIFACT.fullmatch(artifact_id):
            raise FileNotFoundError(f"Unknown diagnosis artifact {artifact_id!r}")
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT content FROM diagnosis_archive WHERE task_id=%s AND artifact_id=%s",
                (self.task_id, artifact_id),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"Unknown diagnosis artifact {artifact_id!r}")
        return row["content"]
