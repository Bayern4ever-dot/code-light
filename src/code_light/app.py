"""Main application orchestrator."""

from __future__ import annotations

import asyncio
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Optional

from .config import Config
from .models import AgentStatus, AgentType, QuotaInfo, TaskRecord
from .monitors.claude_code import ClaudeCodeMonitor
from .monitors.codex import CodexMonitor
from .services.quota import QuotaTracker
from .services.token_counter import TokenCounter
from .services.vscode import VSCodeService
from .state import StateManager
from .ui.dashboard import Dashboard
from .ui.floating import FloatingWindow
from .ui.tray import SystemTray
from .utils.logger import logger


class App:
    """Main application orchestrator."""

    def __init__(self, config: Optional[Config] = None) -> None:
        """Initialize application.

        Args:
            config: Optional configuration override.
        """
        self._config = config or Config()
        self._config.ensure_dirs()

        # Core components
        self._state = StateManager(self._config.db_path)
        self._token_counter = TokenCounter(self._config)
        self._quota_tracker = QuotaTracker(self._config)
        self._vscode = VSCodeService()

        # Monitors
        self._claude_monitor = ClaudeCodeMonitor(
            self._config.claude_home,
            self._config.idle_threshold_seconds,
            self._config.active_threshold_seconds,
            self._config.max_sessions_per_agent,
        )
        self._codex_monitor = CodexMonitor(
            self._config.codex_home,
            self._config.idle_threshold_seconds,
            self._config.active_threshold_seconds,
            self._config.max_sessions_per_agent,
        )

        # UI components
        self._tray: Optional[SystemTray] = None
        self._floating: Optional[FloatingWindow] = None
        self._dashboard: Optional[Dashboard] = None

        # State
        self._running = False
        self._poll_thread: Optional[threading.Thread] = None
        self._last_statuses: dict[AgentType, AgentStatus] = {}
        self._last_quotas: dict[AgentType, QuotaInfo] = {}

    def _on_open_dashboard(self) -> None:
        """Handle open dashboard request."""
        if self._dashboard:
            import webbrowser
            webbrowser.open(self._dashboard.url)

    def _on_toggle_floating(self) -> None:
        """Handle toggle floating window request."""
        if self._floating:
            self._floating.toggle()

    def _on_focus_agent(self, agent_type: AgentType) -> bool:
        """Handle focus agent request.

        Args:
            agent_type: Agent to focus.

        Returns:
            True if successful.
        """
        return self._vscode.focus_agent_window(agent_type)

    def _on_quit(self) -> None:
        """Handle quit request."""
        logger.info("Quit requested")
        self.stop()

    def _update_tray(self) -> None:
        """Update tray icon with current statuses."""
        if not self._tray:
            return

        statuses = {
            at: s.status for at, s in self._last_statuses.items()
        }
        self._tray.update_status(statuses)

    def _update_floating(self) -> None:
        """Update floating window with current statuses."""
        if not self._floating:
            return

        if AgentType.CODEX not in self._last_quotas:
            latest_codex_quota = self._state.get_latest_quota(AgentType.CODEX)
            if latest_codex_quota:
                self._last_quotas[AgentType.CODEX] = latest_codex_quota

        self._floating.update_status(self._last_statuses, self._last_quotas)

    def _poll_once(self) -> None:
        """Perform one polling cycle."""
        try:
            # Poll Claude Code
            import asyncio
            loop = asyncio.new_event_loop()
            claude_status = loop.run_until_complete(self._claude_monitor.poll_status())
            claude_sessions = loop.run_until_complete(self._claude_monitor.poll_sessions())
            loop.close()

            self._last_statuses[AgentType.CLAUDE_CODE] = claude_status
            self._state.update_status(claude_status)
            self._state.update_sessions(AgentType.CLAUDE_CODE, claude_sessions)

            # Record token usage if changed
            if claude_status.tokens.total_tokens > 0:
                cost = self._token_counter.compute_cost(
                    claude_status.model, claude_status.tokens
                )
                self._state.record_token_usage(
                    AgentType.CLAUDE_CODE,
                    claude_status.session_id,
                    claude_status.model,
                    claude_status.tokens,
                    cost,
                )

            # Poll Codex
            loop = asyncio.new_event_loop()
            codex_status = loop.run_until_complete(self._codex_monitor.poll_status())
            codex_sessions = loop.run_until_complete(self._codex_monitor.poll_sessions())
            loop.close()

            self._last_statuses[AgentType.CODEX] = codex_status
            self._state.update_status(codex_status)
            self._state.update_sessions(AgentType.CODEX, codex_sessions)

            # Poll Codex quota (less frequently)
            loop = asyncio.new_event_loop()
            codex_quota = loop.run_until_complete(self._codex_monitor.poll_quota())
            loop.close()

            if codex_quota:
                self._last_quotas[AgentType.CODEX] = codex_quota
                self._state.record_quota(codex_quota)
                warning = self._quota_tracker.check_warning(codex_quota)
                if warning:
                    logger.warning(warning)

            # Update UI (thread-safe - uses queue)
            self._update_tray()
            self._update_floating()

        except Exception as e:
            logger.error(f"Polling error: {e}")

    def _poll_loop(self) -> None:
        """Background polling loop."""
        while self._running:
            try:
                self._poll_once()
            except Exception as e:
                logger.error(f"Poll loop error: {e}")

            # Sleep until next poll
            for _ in range(self._config.poll_interval_seconds):
                if not self._running:
                    break
                time.sleep(1)

    def _start_polling(self) -> None:
        """Start background polling thread."""
        if self._poll_thread and self._poll_thread.is_alive():
            return

        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        logger.info("Polling started")

    def _stop_polling(self) -> None:
        """Stop background polling."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None
        logger.info("Polling stopped")

    def start(self) -> None:
        """Start the application."""
        if self._running:
            logger.debug("code-light is already running, skipping start()")
            return

        logger.info("Starting code-light...")

        # Initialize UI
        self._tray = SystemTray(
            on_open_dashboard=self._on_open_dashboard,
            on_toggle_floating=self._on_toggle_floating,
            on_focus_agent=self._on_focus_agent,
            on_quit=self._on_quit,
        )

        self._floating = FloatingWindow(
            on_focus_agent=self._on_focus_agent,
            on_open_dashboard=self._on_open_dashboard,
            opacity=self._config.floating_window_opacity,
            width=self._config.floating_window_width,
            height=self._config.floating_window_height,
        )

        self._dashboard = Dashboard(
            config=self._config,
            state=self._state,
            on_focus_agent=self._on_focus_agent,
        )

        # Start components
        self._tray.start()
        self._dashboard.start()

        # Start file watchers
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                self._claude_monitor.start_watching(
                    lambda status: self._on_status_update(AgentType.CLAUDE_CODE, status)
                )
            )
            loop.close()
        except Exception as e:
            logger.warning(f"Failed to start Claude Code watcher: {e}")

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(
                self._codex_monitor.start_watching(
                    lambda status: self._on_status_update(AgentType.CODEX, status)
                )
            )
            loop.close()
        except Exception as e:
            logger.warning(f"Failed to start Codex watcher: {e}")

        # Start polling
        self._start_polling()

        # Show floating window by default
        self._floating.show()

        logger.info("code-light started successfully")
        logger.info(f"Dashboard: {self._dashboard.url}")

    def _on_status_update(self, agent_type: AgentType, status: AgentStatus) -> None:
        """Handle status update from monitor.

        Args:
            agent_type: Agent type.
            status: New status.
        """
        self._last_statuses[agent_type] = status
        self._state.update_status(status)
        self._update_tray()
        self._update_floating()

    def stop(self) -> None:
        """Stop the application."""
        if not self._running:
            return

        logger.info("Stopping code-light...")

        self._stop_polling()

        if self._floating:
            self._floating.destroy()
            self._floating = None

        if self._tray:
            self._tray.stop()
            self._tray = None

        # Record final task history
        for agent_type, status in self._last_statuses.items():
            if status.tokens.total_tokens > 0:
                cost = self._token_counter.compute_cost(
                    status.model, status.tokens
                )
                record = TaskRecord(
                    agent_type=agent_type,
                    session_id=status.session_id,
                    project_path=status.project_path,
                    started_at=status.last_activity,
                    ended_at=datetime.utcnow(),
                    input_tokens=status.tokens.input_tokens,
                    output_tokens=status.tokens.output_tokens,
                    total_tokens=status.tokens.total_tokens,
                    cost_usd=cost,
                    status=status.status.value,
                )
                self._state.add_task(record)

        logger.info("code-light stopped")

    def run(self) -> None:
        """Run the application (blocking)."""
        self.start()

        # Handle Ctrl+C
        def signal_handler(sig, frame):
            logger.info("Interrupt received")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Keep main thread alive for tkinter
        try:
            while self._running:
                if self._floating:
                    self._floating.process_events()
                time.sleep(0.05)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
