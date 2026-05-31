"""Configuration for code-light (immutable)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """Application configuration.

    All settings are immutable. Override via settings file or CLI args.
    """

    # Paths
    claude_home: Path = field(default_factory=lambda: Path.home() / ".claude")
    codex_home: Path = field(default_factory=lambda: Path.home() / ".codex")
    data_dir: Path = field(default_factory=lambda: Path.home() / ".code-light")
    db_path: Path = field(default_factory=lambda: Path.home() / ".code-light" / "data.db")
    settings_path: Path = field(default_factory=lambda: Path.home() / ".code-light" / "settings.json")

    # Polling
    poll_interval_seconds: int = 30
    api_poll_interval_seconds: int = 300  # 5 minutes for API calls
    active_threshold_seconds: int = 15  # recent writes mean the agent is working
    idle_threshold_seconds: int = 300  # 5 minutes to consider idle
    max_sessions_per_agent: int = 8

    # UI
    floating_window_opacity: float = 1.0
    floating_window_width: int = 340
    floating_window_height: int = 300
    dashboard_port: int = 7681
    dashboard_host: str = "127.0.0.1"

    # Quota warnings
    quota_warn_percent: float = 70.0
    quota_critical_percent: float = 85.0
    quota_emergency_percent: float = 95.0

    # Notifications
    enable_notifications: bool = True
    notification_cooldown_seconds: int = 300

    # Data retention
    history_retention_days: int = 90

    # Model pricing (per 1M tokens)
    model_pricing: dict = field(default_factory=lambda: {
        "deepseek-chat": {"input": 0.14, "output": 0.28},
        "deepseek-coder": {"input": 0.14, "output": 0.28},
        "deepseek-reasoner": {"input": 0.55, "output": 2.19},
        "mimo": {"input": 0.14, "output": 0.28},
        "gpt-4": {"input": 30.0, "output": 60.0},
        "gpt-4-turbo": {"input": 10.0, "output": 30.0},
        "gpt-4o": {"input": 2.5, "output": 10.0},
        "gpt-4o-mini": {"input": 0.15, "output": 0.60},
        "claude-3-opus": {"input": 15.0, "output": 75.0},
        "claude-3-sonnet": {"input": 3.0, "output": 15.0},
        "claude-3-haiku": {"input": 0.25, "output": 1.25},
    })

    def ensure_dirs(self) -> None:
        """Create necessary directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
