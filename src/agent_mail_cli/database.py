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

    def _connect_rw(self) -> sqlite3.Connection:
        """Get a read-write connection."""
        if not Path(self.db_path).exists():
            raise FileNotFoundError(f"Database not found: {self.db_path}")
        conn = sqlite3.connect(self.db_path)
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

    def agent_dependencies(self, project_key: str, agent_name: str) -> dict[str, Any]:
        """Check for agent dependencies that would prevent deletion."""
        with self._connect() as conn:
            project_id = self._get_project_id(conn, project_key)
            if not project_id:
                raise ValueError(f"Project not found: {project_key}")
            agent_id = self._get_agent_id(conn, project_id, agent_name)
            if not agent_id:
                raise ValueError(f"Agent '{agent_name}' not found in project")

            # Check for unread messages where agent is recipient
            cur = conn.execute(
                """
                SELECT COUNT(*) as count FROM message_recipients mr
                JOIN messages m ON mr.message_id = m.id
                WHERE mr.agent_id = ? AND mr.read_ts IS NULL
                """,
                (agent_id,),
            )
            unread_messages = cur.fetchone()["count"]

            # Check for active file reservations
            cur = conn.execute(
                """
                SELECT COUNT(*) as count FROM file_reservations
                WHERE agent_id = ? AND released_ts IS NULL
                  AND (expires_ts IS NULL OR expires_ts > datetime('now'))
                """,
                (agent_id,),
            )
            active_reservations = cur.fetchone()["count"]

            # Check for messages sent by agent (informational)
            cur = conn.execute(
                "SELECT COUNT(*) as count FROM messages WHERE sender_id = ?",
                (agent_id,),
            )
            sent_messages = cur.fetchone()["count"]

            return {
                "agent_id": agent_id,
                "unread_messages": unread_messages,
                "active_reservations": active_reservations,
                "sent_messages": sent_messages,
                "can_delete": unread_messages == 0 and active_reservations == 0,
            }

    def delete_agent(self, project_key: str, agent_name: str, force: bool = False) -> dict[str, Any]:
        """Delete an agent from the project.

        Args:
            project_key: Project path or slug
            agent_name: Agent name to delete
            force: If True, delete even with unread messages/active reservations

        Returns:
            Dict with deletion status and cleaned up records
        """
        deps = self.agent_dependencies(project_key, agent_name)

        if not deps["can_delete"] and not force:
            raise ValueError(
                f"Cannot delete agent '{agent_name}': "
                f"{deps['unread_messages']} unread messages, "
                f"{deps['active_reservations']} active reservations. "
                "Use --force to delete anyway."
            )

        with self._connect_rw() as conn:
            project_id = self._get_project_id(conn, project_key)
            agent_id = deps["agent_id"]

            # Release any active file reservations
            cur = conn.execute(
                """
                UPDATE file_reservations
                SET released_ts = datetime('now')
                WHERE agent_id = ? AND released_ts IS NULL
                """,
                (agent_id,),
            )
            released_reservations = cur.rowcount

            # Delete message_recipients entries (keeps messages but removes agent as recipient)
            cur = conn.execute(
                "DELETE FROM message_recipients WHERE agent_id = ?",
                (agent_id,),
            )
            removed_recipient_entries = cur.rowcount

            # Delete contact links
            cur = conn.execute(
                "DELETE FROM agent_links WHERE a_agent_id = ? OR b_agent_id = ?",
                (agent_id, agent_id),
            )
            removed_links = cur.rowcount

            # Delete the agent
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            conn.commit()

            return {
                "deleted": True,
                "agent_name": agent_name,
                "released_reservations": released_reservations,
                "removed_recipient_entries": removed_recipient_entries,
                "removed_links": removed_links,
                "orphaned_sent_messages": deps["sent_messages"],
            }
