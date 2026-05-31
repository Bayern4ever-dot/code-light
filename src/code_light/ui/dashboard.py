"""Web dashboard implementation using Flask."""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
import sys
from typing import Optional

from flask import Flask, jsonify, render_template, request

from ..config import Config
from ..models import AgentType
from ..models import AgentStatus
from ..state import StateManager
from ..utils.logger import logger


def _dashboard_asset_root() -> Path:
    """Return the dashboard asset root in source or bundled builds."""
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        return Path(bundled_root) / "dashboard"
    return Path(__file__).parent.parent.parent.parent / "dashboard"


def _status_to_json(status: AgentStatus) -> dict:
    """Serialize an AgentStatus for the dashboard API."""
    return {
        "agent_type": status.agent_type.value,
        "status": status.status.value,
        "model": status.model,
        "project_path": status.project_path,
        "session_id": status.session_id,
        "last_activity": status.last_activity.isoformat()
        if status.last_activity
        else None,
        "tokens": {
            "input": status.tokens.input_tokens,
            "output": status.tokens.output_tokens,
            "cached": status.tokens.cached_input_tokens,
            "reasoning": status.tokens.reasoning_output_tokens,
            "total": status.tokens.total_tokens,
            "conversation_count": status.tokens.conversation_count,
        },
        "message": status.message,
    }


class Dashboard:
    """Web dashboard for code-light."""

    def __init__(
        self,
        config: Config,
        state: StateManager,
        on_focus_agent: Optional[callable] = None,
    ) -> None:
        """Initialize dashboard.

        Args:
            config: Application configuration.
            state: State manager for data access.
            on_focus_agent: Callback to focus agent window.
        """
        self._config = config
        self._state = state
        self._on_focus_agent = on_focus_agent

        dashboard_root = _dashboard_asset_root()
        self._app = Flask(
            __name__,
            template_folder=str(dashboard_root / "templates"),
            static_folder=str(dashboard_root / "static"),
        )
        self._setup_routes()
        self._thread: Optional[threading.Thread] = None

    def _setup_routes(self) -> None:
        """Set up Flask routes."""

        @self._app.route("/")
        def index():
            """Main dashboard page."""
            statuses = self._state.get_all_statuses()
            sessions_by_agent = {
                at.value: self._state.get_sessions(at)
                for at in AgentType
            }
            return render_template(
                "index.html",
                statuses=statuses,
                sessions_by_agent=sessions_by_agent,
                now=datetime.utcnow(),
            )

        @self._app.route("/api/status")
        def api_status():
            """Get current status of all agents."""
            statuses = self._state.get_all_statuses()
            return jsonify([_status_to_json(s) for s in statuses])

        @self._app.route("/api/sessions")
        def api_sessions():
            """Get current tracked sessions."""
            agent_type = request.args.get("agent")
            at = None
            if agent_type:
                try:
                    at = AgentType(agent_type)
                except ValueError:
                    return jsonify({"error": "Invalid agent type"}), 400
            sessions = self._state.get_sessions(at)
            return jsonify([_status_to_json(s) for s in sessions])

        @self._app.route("/api/quota")
        def api_quota():
            """Get latest quota snapshots."""
            data = {}
            for at in AgentType:
                quota = self._state.get_latest_quota(at)
                if not quota:
                    data[at.value] = None
                    continue
                data[at.value] = {
                    "plan_name": quota.plan_name,
                    "used_percent": quota.used_percent,
                    "limit_window_seconds": quota.limit_window_seconds,
                    "reset_at": quota.reset_at.isoformat() if quota.reset_at else None,
                    "remaining_seconds": quota.remaining_seconds,
                    "credits_balance": quota.credits_balance,
                    "extra_info": quota.extra_info,
                }
            return jsonify(data)

        @self._app.route("/api/usage")
        def api_usage():
            """Get token usage summary."""
            agent_type = request.args.get("agent")
            days = int(request.args.get("days", 7))

            if agent_type:
                try:
                    at = AgentType(agent_type)
                    summary = self._state.get_usage_summary(at, days)
                except ValueError:
                    summary = {"error": "Invalid agent type"}
            else:
                summary = {}
                for at in AgentType:
                    summary[at.value] = self._state.get_usage_summary(at, days)

            return jsonify(summary)

        @self._app.route("/api/history")
        def api_history():
            """Get task history."""
            agent_type = request.args.get("agent")
            limit = int(request.args.get("limit", 50))
            offset = int(request.args.get("offset", 0))

            at = None
            if agent_type:
                try:
                    at = AgentType(agent_type)
                except ValueError:
                    pass

            history = self._state.get_history(at, limit, offset)
            return jsonify([
                {
                    "id": r.id,
                    "agent_type": r.agent_type.value,
                    "session_id": r.session_id,
                    "project_path": r.project_path,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                    "input_tokens": r.input_tokens,
                    "output_tokens": r.output_tokens,
                    "total_tokens": r.total_tokens,
                    "cost_usd": r.cost_usd,
                    "status": r.status,
                }
                for r in history
            ])

        @self._app.route("/api/focus", methods=["POST"])
        def api_focus():
            """Focus an agent's VS Code window."""
            data = request.get_json()
            agent_type = data.get("agent_type")
            if agent_type and self._on_focus_agent:
                try:
                    at = AgentType(agent_type)
                    success = self._on_focus_agent(at)
                    return jsonify({"success": success})
                except ValueError:
                    return jsonify({"error": "Invalid agent type"}), 400
            return jsonify({"error": "Missing agent_type"}), 400

    def start(self) -> None:
        """Start the dashboard server in a background thread."""
        if self._thread and self._thread.is_alive():
            return

        def _run():
            try:
                self._app.run(
                    host=self._config.dashboard_host,
                    port=self._config.dashboard_port,
                    debug=False,
                    use_reloader=False,
                )
            except Exception as e:
                logger.error(f"Dashboard server error: {e}")

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        logger.info(
            f"Dashboard started at http://{self._config.dashboard_host}:{self._config.dashboard_port}"
        )

    def stop(self) -> None:
        """Stop the dashboard server."""
        # Flask doesn't have a clean shutdown method in threaded mode
        # The daemon thread will stop when the main process exits
        logger.info("Dashboard stopped")

    @property
    def url(self) -> str:
        """Get dashboard URL."""
        return f"http://{self._config.dashboard_host}:{self._config.dashboard_port}"
