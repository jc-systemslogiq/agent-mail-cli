"""Direct SQLite database access for fast read-only queries."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Default database location (bind-mounted from Docker)
DEFAULT_DB_PATH = os.path.expanduser("~/.mcp_agent_mail_git_mailbox_repo/storage.sqlite3")


@dataclass
class AgentMailDB:
    """Direct SQLite access for read-only queries."""

    db_path: str = DEFAULT_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        """Get a read-only connection."""
        if not Path(self.db_path).exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _get_project_id(self, conn: sqlite3.Connection, project_key: str) -> int | None:
        """Get project ID from human_key or slug."""
        cur = conn.execute(
            "SELECT id FROM projects WHERE human_key = ? OR slug = ? LIMIT 1",
            (project_key, project_key),
        )
        row = cur.fetchone()
        return row["id"] if row else None

    def _get_agent_id(self, conn: sqlite3.Connection, project_id: int, agent_name: str) -> int | None:
        """Get agent ID by name within project."""
        cur = conn.execute(
            "SELECT id FROM agents WHERE project_id = ? AND name = ? LIMIT 1",
            (project_id, agent_name),
        )
        row = cur.fetchone()
        return row["id"] if row else None

    # --- Projects ---

    def list_projects(self, limit: int = 100) -> list[dict[str, Any]]:
        """List all projects."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT id, slug, human_key, created_at FROM projects ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    # --- File Reservations ---

    def file_reservations_active(self, project_key: str, limit: int = 100) -> list[dict[str, Any]]:
        """List active (non-released) file reservations with agent names."""
        with self._connect() as conn:
            project_id = self._get_project_id(conn, project_key)
            if not project_id:
                return []
            cur = conn.execute(
                """
                SELECT fr.id, a.name as agent, fr.path_pattern, fr.exclusive,
                       fr.expires_ts, fr.reason, fr.created_ts
                FROM file_reservations fr
                JOIN agents a ON fr.agent_id = a.id
                WHERE fr.project_id = ? AND fr.released_ts IS NULL
                ORDER BY fr.expires_ts ASC
                LIMIT ?
                """,
                (project_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def file_reservations_soon(self, project_key: str, minutes: int = 30) -> list[dict[str, Any]]:
        """List reservations expiring within N minutes."""
        with self._connect() as conn:
            project_id = self._get_project_id(conn, project_key)
            if not project_id:
                return []
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                """
                SELECT fr.id, a.name as agent, fr.path_pattern, fr.exclusive,
                       fr.expires_ts, fr.reason
                FROM file_reservations fr
                JOIN agents a ON fr.agent_id = a.id
                WHERE fr.project_id = ?
                  AND fr.released_ts IS NULL
                  AND fr.expires_ts <= datetime(?, '+' || ? || ' minutes')
                  AND fr.expires_ts > ?
                ORDER BY fr.expires_ts ASC
                """,
                (project_id, now, minutes, now),
            )
            return [dict(row) for row in cur.fetchall()]

    def file_reservations_list(self, project_key: str, active_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        """List file reservations (optionally including released)."""
        with self._connect() as conn:
            project_id = self._get_project_id(conn, project_key)
            if not project_id:
                return []
            query = """
                SELECT fr.id, a.name as agent, fr.path_pattern, fr.exclusive,
                       fr.expires_ts, fr.released_ts, fr.reason, fr.created_ts
                FROM file_reservations fr
                JOIN agents a ON fr.agent_id = a.id
                WHERE fr.project_id = ?
            """
            params: list[Any] = [project_id]
            if active_only:
                query += " AND fr.released_ts IS NULL"
            query += " ORDER BY fr.expires_ts ASC LIMIT ?"
            params.append(limit)
            cur = conn.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    # --- Acknowledgements ---

    def acks_pending(self, project_key: str, agent_name: str, limit: int = 20) -> list[dict[str, Any]]:
        """List messages requiring ack where this agent hasn't acknowledged."""
        with self._connect() as conn:
            project_id = self._get_project_id(conn, project_key)
            if not project_id:
                return []
            agent_id = self._get_agent_id(conn, project_id, agent_name)
            if not agent_id:
                raise ValueError(f"Agent '{agent_name}' not registered for project '{project_key}'")
            cur = conn.execute(
                """
                SELECT m.id, m.subject, m.importance, m.created_ts, m.thread_id,
                       sender.name as sender
                FROM messages m
                JOIN message_recipients mr ON m.id = mr.message_id
                JOIN agents sender ON m.sender_id = sender.id
                WHERE m.project_id = ?
                  AND mr.agent_id = ?
                  AND m.ack_required = 1
                  AND mr.ack_ts IS NULL
                ORDER BY m.created_ts DESC
                LIMIT ?
                """,
                (project_id, agent_id, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def acks_overdue(self, project_key: str, agent_name: str, hours: int = 24, limit: int = 20) -> list[dict[str, Any]]:
        """List ack-required messages older than threshold without ack."""
        with self._connect() as conn:
            project_id = self._get_project_id(conn, project_key)
            if not project_id:
                return []
            agent_id = self._get_agent_id(conn, project_id, agent_name)
            if not agent_id:
                raise ValueError(f"Agent '{agent_name}' not registered for project '{project_key}'")
            threshold = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                """
                SELECT m.id, m.subject, m.importance, m.created_ts, m.thread_id,
                       sender.name as sender
                FROM messages m
                JOIN message_recipients mr ON m.id = mr.message_id
                JOIN agents sender ON m.sender_id = sender.id
                WHERE m.project_id = ?
                  AND mr.agent_id = ?
                  AND m.ack_required = 1
                  AND mr.ack_ts IS NULL
                  AND m.created_ts <= datetime(?, '-' || ? || ' hours')
                ORDER BY m.created_ts ASC
                LIMIT ?
                """,
                (project_id, agent_id, threshold, hours, limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def list_acks(self, project_key: str, agent_name: str, limit: int = 10) -> list[dict[str, Any]]:
        """List messages requiring ack for an agent (alias for acks_pending)."""
        return self.acks_pending(project_key, agent_name, limit)

    # --- Agents ---

    def list_agents(self, project_key: str) -> list[dict[str, Any]]:
        """List agents in a project."""
        with self._connect() as conn:
            project_id = self._get_project_id(conn, project_key)
            if not project_id:
                return []
            cur = conn.execute(
                """
                SELECT id, name, program, model, task_description, last_active_ts
                FROM agents
                WHERE project_id = ?
                ORDER BY last_active_ts DESC
                """,
                (project_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_agent(self, project_key: str, agent_name: str) -> dict[str, Any] | None:
        """Get agent details."""
        with self._connect() as conn:
            project_id = self._get_project_id(conn, project_key)
            if not project_id:
                return None
            cur = conn.execute(
                """
                SELECT id, name, program, model, task_description,
                       inception_ts, last_active_ts, contact_policy
                FROM agents
                WHERE project_id = ? AND name = ?
                """,
                (project_id, agent_name),
            )
            row = cur.fetchone()
            return dict(row) if row else None
