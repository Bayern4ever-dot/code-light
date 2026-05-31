"""State manager with SQLite persistence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Generator, Optional

from .models import AgentStatus, AgentType, QuotaInfo, StatusLevel, TaskRecord, TokenUsage
from .utils.logger import logger


class StateManager:
    """SQLite-backed state persistence."""

    def __init__(self, db_path: Path) -> None:
        """Initialize state manager.

        Args:
            db_path: Path to SQLite database file.
        """
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Get a database connection with context manager."""
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_status (
                    agent_type TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'offline',
                    model TEXT DEFAULT '',
                    project_path TEXT DEFAULT '',
                    session_id TEXT DEFAULT '',
                    last_activity TEXT,
                    tokens_json TEXT DEFAULT '{}',
                    message TEXT DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    model TEXT DEFAULT '',
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    cached_input_tokens INTEGER DEFAULT 0,
                    reasoning_output_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    cost_usd REAL DEFAULT 0.0,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_sessions (
                    agent_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'offline',
                    model TEXT DEFAULT '',
                    project_path TEXT DEFAULT '',
                    last_activity TEXT,
                    tokens_json TEXT DEFAULT '{}',
                    message TEXT DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (agent_type, session_id)
                );

                CREATE TABLE IF NOT EXISTS task_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_type TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    project_path TEXT DEFAULT '',
                    started_at TEXT,
                    ended_at TEXT,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    total_tokens INTEGER DEFAULT 0,
                    cost_usd REAL DEFAULT 0.0,
                    status TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS quota_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_type TEXT NOT NULL,
                    plan_name TEXT DEFAULT '',
                    used_percent REAL DEFAULT 0.0,
                    limit_window_seconds INTEGER DEFAULT 0,
                    reset_at TEXT,
                    credits_balance REAL DEFAULT 0.0,
                    extra_json TEXT DEFAULT '{}',
                    recorded_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_token_usage_agent
                    ON token_usage(agent_type, recorded_at);
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent
                    ON agent_sessions(agent_type, last_activity);
                CREATE INDEX IF NOT EXISTS idx_task_history_agent
                    ON task_history(agent_type, started_at);
                CREATE INDEX IF NOT EXISTS idx_quota_snapshots_agent
                    ON quota_snapshots(agent_type, recorded_at);
            """)
        logger.info(f"Database initialized at {self._db_path}")

    # --- Agent Status ---

    def get_status(self, agent_type: AgentType) -> Optional[AgentStatus]:
        """Get current status for an agent.

        Args:
            agent_type: The agent type to query.

        Returns:
            Current AgentStatus or None.
        """
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM agent_status WHERE agent_type = ?",
                (agent_type.value,),
            ).fetchone()
            if not row:
                return None

            tokens_data = json.loads(row["tokens_json"]) if row["tokens_json"] else {}
            return AgentStatus(
                agent_type=AgentType(row["agent_type"]),
                status=StatusLevel(row["status"]),
                model=row["model"] or "",
                project_path=row["project_path"] or "",
                session_id=row["session_id"] or "",
                last_activity=(
                    datetime.fromisoformat(row["last_activity"])
                    if row["last_activity"]
                    else None
                ),
                tokens=TokenUsage(**tokens_data) if tokens_data else TokenUsage(),
                message=row["message"] or "",
            )

    def get_all_statuses(self) -> list[AgentStatus]:
        """Get current status for all agents.

        Returns:
            List of AgentStatus for all tracked agents.
        """
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM agent_status").fetchall()

        statuses = []
        for row in rows:
            tokens_data = json.loads(row["tokens_json"]) if row["tokens_json"] else {}
            statuses.append(
                AgentStatus(
                    agent_type=AgentType(row["agent_type"]),
                    status=StatusLevel(row["status"]),
                    model=row["model"] or "",
                    project_path=row["project_path"] or "",
                    session_id=row["session_id"] or "",
                    last_activity=(
                        datetime.fromisoformat(row["last_activity"])
                        if row["last_activity"]
                        else None
                    ),
                    tokens=TokenUsage(**tokens_data) if tokens_data else TokenUsage(),
                    message=row["message"] or "",
                )
            )
        return statuses

    def get_sessions(self, agent_type: AgentType | None = None) -> list[AgentStatus]:
        """Get current tracked sessions."""
        query = "SELECT * FROM agent_sessions"
        params: tuple[str, ...] = ()
        if agent_type:
            query += " WHERE agent_type = ?"
            params = (agent_type.value,)
        query += " ORDER BY last_activity DESC"

        with self._get_conn() as conn:
            rows = conn.execute(query, params).fetchall()

        sessions = []
        for row in rows:
            tokens_data = json.loads(row["tokens_json"]) if row["tokens_json"] else {}
            sessions.append(
                AgentStatus(
                    agent_type=AgentType(row["agent_type"]),
                    status=StatusLevel(row["status"]),
                    model=row["model"] or "",
                    project_path=row["project_path"] or "",
                    session_id=row["session_id"] or "",
                    last_activity=(
                        datetime.fromisoformat(row["last_activity"])
                        if row["last_activity"]
                        else None
                    ),
                    tokens=TokenUsage(**tokens_data) if tokens_data else TokenUsage(),
                    message=row["message"] or "",
                )
            )
        return sessions

    def update_status(self, status: AgentStatus) -> None:
        """Update or insert agent status.

        Args:
            status: New AgentStatus to persist.
        """
        now = datetime.utcnow().isoformat()
        tokens_json = json.dumps({
            "input_tokens": status.tokens.input_tokens,
            "output_tokens": status.tokens.output_tokens,
            "cached_input_tokens": status.tokens.cached_input_tokens,
            "reasoning_output_tokens": status.tokens.reasoning_output_tokens,
            "total_tokens": status.tokens.total_tokens,
            "conversation_count": status.tokens.conversation_count,
        })
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO agent_status
                    (agent_type, status, model, project_path, session_id,
                     last_activity, tokens_json, message, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_type) DO UPDATE SET
                    status = excluded.status,
                    model = excluded.model,
                    project_path = excluded.project_path,
                    session_id = excluded.session_id,
                    last_activity = excluded.last_activity,
                    tokens_json = excluded.tokens_json,
                    message = excluded.message,
                    updated_at = excluded.updated_at
                """,
                (
                    status.agent_type.value,
                    status.status.value,
                    status.model,
                    status.project_path,
                    status.session_id,
                    status.last_activity.isoformat() if status.last_activity else None,
                    tokens_json,
                    status.message,
                    now,
                ),
            )

    def update_sessions(self, agent_type: AgentType, sessions: list[AgentStatus]) -> None:
        """Replace the tracked session set for one agent."""
        now = datetime.utcnow().isoformat()
        unique_sessions: list[AgentStatus] = []
        seen: set[str] = set()
        for index, status in enumerate(sessions):
            session_id = status.session_id or f"session-{index}"
            key = f"{status.agent_type.value}:{session_id}"
            if key in seen:
                continue
            seen.add(key)
            if status.session_id:
                unique_sessions.append(status)
            else:
                unique_sessions.append(
                    AgentStatus(
                        agent_type=status.agent_type,
                        status=status.status,
                        model=status.model,
                        project_path=status.project_path,
                        session_id=session_id,
                        last_activity=status.last_activity,
                        tokens=status.tokens,
                        message=status.message,
                    )
                )

        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM agent_sessions WHERE agent_type = ?",
                (agent_type.value,),
            )
            for status in unique_sessions:
                tokens_json = json.dumps({
                    "input_tokens": status.tokens.input_tokens,
                    "output_tokens": status.tokens.output_tokens,
                    "cached_input_tokens": status.tokens.cached_input_tokens,
                    "reasoning_output_tokens": status.tokens.reasoning_output_tokens,
                    "total_tokens": status.tokens.total_tokens,
                    "conversation_count": status.tokens.conversation_count,
                })
                conn.execute(
                    """
                    INSERT INTO agent_sessions
                        (agent_type, session_id, status, model, project_path,
                         last_activity, tokens_json, message, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        status.agent_type.value,
                        status.session_id,
                        status.status.value,
                        status.model,
                        status.project_path,
                        status.last_activity.isoformat() if status.last_activity else None,
                        tokens_json,
                        status.message,
                        now,
                    ),
                )

    # --- Token Usage ---

    def record_token_usage(
        self,
        agent_type: AgentType,
        session_id: str,
        model: str,
        tokens: TokenUsage,
        cost_usd: float = 0.0,
    ) -> None:
        """Record a token usage snapshot.

        Args:
            agent_type: The agent type.
            session_id: Session identifier.
            model: Model name.
            tokens: TokenUsage data.
            cost_usd: Computed cost in USD.
        """
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO token_usage
                    (agent_type, session_id, model, input_tokens, output_tokens,
                     cached_input_tokens, reasoning_output_tokens, total_tokens,
                     cost_usd, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_type.value,
                    session_id,
                    model,
                    tokens.input_tokens,
                    tokens.output_tokens,
                    tokens.cached_input_tokens,
                    tokens.reasoning_output_tokens,
                    tokens.total_tokens,
                    cost_usd,
                    now,
                ),
            )

    def get_usage_summary(
        self,
        agent_type: AgentType,
        days: int = 7,
    ) -> dict:
        """Get token usage summary for the last N days.

        Args:
            agent_type: The agent type.
            days: Number of days to look back.

        Returns:
            Summary dict with total tokens, cost, and per-model breakdown.
        """
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT model,
                       SUM(input_tokens) as total_input,
                       SUM(output_tokens) as total_output,
                       SUM(total_tokens) as total_tokens,
                       SUM(cost_usd) as total_cost,
                       COUNT(*) as record_count
                FROM token_usage
                WHERE agent_type = ? AND recorded_at >= ?
                GROUP BY model
                ORDER BY total_cost DESC
                """,
                (agent_type.value, since),
            ).fetchall()

        summary = {
            "total_input": 0,
            "total_output": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
            "by_model": {},
        }
        for row in rows:
            summary["total_input"] += row["total_input"] or 0
            summary["total_output"] += row["total_output"] or 0
            summary["total_tokens"] += row["total_tokens"] or 0
            summary["total_cost"] += row["total_cost"] or 0.0
            summary["by_model"][row["model"] or "unknown"] = {
                "input": row["total_input"] or 0,
                "output": row["total_output"] or 0,
                "total": row["total_tokens"] or 0,
                "cost": row["total_cost"] or 0.0,
            }
        return summary

    # --- Task History ---

    def add_task(self, record: TaskRecord) -> int:
        """Add a task history record.

        Args:
            record: TaskRecord to add.

        Returns:
            Inserted record ID.
        """
        with self._get_conn() as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_history
                    (agent_type, session_id, project_path, started_at, ended_at,
                     input_tokens, output_tokens, total_tokens, cost_usd, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.agent_type.value,
                    record.session_id,
                    record.project_path,
                    record.started_at.isoformat() if record.started_at else None,
                    record.ended_at.isoformat() if record.ended_at else None,
                    record.input_tokens,
                    record.output_tokens,
                    record.total_tokens,
                    record.cost_usd,
                    record.status,
                ),
            )
            return cursor.lastrowid or 0

    def get_history(
        self,
        agent_type: AgentType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskRecord]:
        """Get task history records.

        Args:
            agent_type: Optional filter by agent type.
            limit: Max records to return.
            offset: Offset for pagination.

        Returns:
            List of TaskRecord.
        """
        with self._get_conn() as conn:
            if agent_type:
                rows = conn.execute(
                    """
                    SELECT * FROM task_history
                    WHERE agent_type = ?
                    ORDER BY started_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (agent_type.value, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM task_history
                    ORDER BY started_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()

        return [
            TaskRecord(
                id=row["id"],
                agent_type=AgentType(row["agent_type"]),
                session_id=row["session_id"],
                project_path=row["project_path"] or "",
                started_at=(
                    datetime.fromisoformat(row["started_at"])
                    if row["started_at"]
                    else None
                ),
                ended_at=(
                    datetime.fromisoformat(row["ended_at"])
                    if row["ended_at"]
                    else None
                ),
                input_tokens=row["input_tokens"] or 0,
                output_tokens=row["output_tokens"] or 0,
                total_tokens=row["total_tokens"] or 0,
                cost_usd=row["cost_usd"] or 0.0,
                status=row["status"] or "",
            )
            for row in rows
        ]

    # --- Quota Snapshots ---

    def record_quota(self, quota: QuotaInfo) -> None:
        """Record a quota snapshot.

        Args:
            quota: QuotaInfo to record.
        """
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO quota_snapshots
                    (agent_type, plan_name, used_percent, limit_window_seconds,
                     reset_at, credits_balance, extra_json, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    quota.agent_type.value,
                    quota.plan_name,
                    quota.used_percent,
                    quota.limit_window_seconds,
                    quota.reset_at.isoformat() if quota.reset_at else None,
                    quota.credits_balance,
                    json.dumps(quota.extra_info),
                    now,
                ),
            )

    def get_latest_quota(self, agent_type: AgentType) -> Optional[QuotaInfo]:
        """Get the most recent quota snapshot.

        Args:
            agent_type: The agent type.

        Returns:
            Latest QuotaInfo or None.
        """
        with self._get_conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM quota_snapshots
                WHERE agent_type = ?
                ORDER BY recorded_at DESC
                LIMIT 1
                """,
                (agent_type.value,),
            ).fetchone()

        if not row:
            return None

        return QuotaInfo(
            agent_type=AgentType(row["agent_type"]),
            plan_name=row["plan_name"] or "",
            used_percent=row["used_percent"] or 0.0,
            limit_window_seconds=row["limit_window_seconds"] or 0,
            reset_at=(
                datetime.fromisoformat(row["reset_at"])
                if row["reset_at"]
                else None
            ),
            credits_balance=row["credits_balance"] or 0.0,
            extra_info=json.loads(row["extra_json"]) if row["extra_json"] else {},
        )

    # --- Cleanup ---

    def cleanup_old_records(self, retention_days: int = 90) -> int:
        """Delete records older than retention period.

        Args:
            retention_days: Number of days to retain.

        Returns:
            Number of deleted records.
        """
        cutoff = (datetime.utcnow() - timedelta(days=retention_days)).isoformat()
        with self._get_conn() as conn:
            cursor = conn.execute(
                "DELETE FROM token_usage WHERE recorded_at < ?", (cutoff,)
            )
            token_deleted = cursor.rowcount
            cursor = conn.execute(
                "DELETE FROM task_history WHERE started_at < ?", (cutoff,)
            )
            task_deleted = cursor.rowcount
            cursor = conn.execute(
                "DELETE FROM quota_snapshots WHERE recorded_at < ?", (cutoff,)
            )
            quota_deleted = cursor.rowcount

        total = token_deleted + task_deleted + quota_deleted
        if total > 0:
            logger.info(f"Cleaned up {total} old records")
        return total
