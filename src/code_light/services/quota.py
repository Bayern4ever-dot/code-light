"""Quota tracking and warning service."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..config import Config
from ..models import QuotaInfo


class QuotaTracker:
    """Tracks quota usage and generates warnings."""

    def __init__(self, config: Config) -> None:
        """Initialize quota tracker.

        Args:
            config: Application configuration.
        """
        self._config = config
        self._last_warning: dict[str, datetime] = {}

    def check_warning(self, quota: QuotaInfo) -> Optional[str]:
        """Check if quota warning should be generated.

        Args:
            quota: Current quota info.

        Returns:
            Warning message or None.
        """
        agent_key = quota.agent_type.value

        # Check cooldown
        if agent_key in self._last_warning:
            cooldown = timedelta(seconds=self._config.notification_cooldown_seconds)
            if datetime.utcnow() - self._last_warning[agent_key] < cooldown:
                return None

        # Check thresholds
        if quota.used_percent >= self._config.quota_emergency_percent:
            msg = self._format_warning(quota, "EMERGENCY")
            self._last_warning[agent_key] = datetime.utcnow()
            return msg
        elif quota.used_percent >= self._config.quota_critical_percent:
            msg = self._format_warning(quota, "CRITICAL")
            self._last_warning[agent_key] = datetime.utcnow()
            return msg
        elif quota.used_percent >= self._config.quota_warn_percent:
            msg = self._format_warning(quota, "WARNING")
            self._last_warning[agent_key] = datetime.utcnow()
            return msg

        return None

    def _format_warning(self, quota: QuotaInfo, level: str) -> str:
        """Format warning message.

        Args:
            quota: Quota info.
            level: Warning level.

        Returns:
            Formatted warning message.
        """
        agent_name = quota.agent_type.value.replace("_", " ").title()
        plan = quota.plan_name or "Unknown"

        if quota.reset_at:
            remaining = quota.reset_at - datetime.utcnow()
            remaining_min = max(0, int(remaining.total_seconds() / 60))
            reset_info = f"Resets in {remaining_min} min"
        else:
            reset_info = ""

        return (
            f"[{level}] {agent_name} ({plan}): "
            f"{quota.used_percent:.1f}% used. {reset_info}"
        )

    def get_status_label(self, quota: Optional[QuotaInfo]) -> str:
        """Get human-readable status label for quota.

        Args:
            quota: Quota info or None.

        Returns:
            Status label string.
        """
        if not quota:
            return "N/A"

        if quota.used_percent >= self._config.quota_emergency_percent:
            return "EMERGENCY"
        elif quota.used_percent >= self._config.quota_critical_percent:
            return "CRITICAL"
        elif quota.used_percent >= self._config.quota_warn_percent:
            return "Warning"
        else:
            return "OK"

    def get_progress_color(self, quota: Optional[QuotaInfo]) -> str:
        """Get color for quota progress bar.

        Args:
            quota: Quota info or None.

        Returns:
            Hex color string.
        """
        if not quota:
            return "#6b7280"  # gray

        if quota.used_percent >= self._config.quota_emergency_percent:
            return "#ef4444"  # red
        elif quota.used_percent >= self._config.quota_critical_percent:
            return "#f97316"  # orange
        elif quota.used_percent >= self._config.quota_warn_percent:
            return "#eab308"  # yellow
        else:
            return "#22c55e"  # green

    def format_remaining_time(self, quota: Optional[QuotaInfo]) -> str:
        """Format remaining time until quota reset.

        Args:
            quota: Quota info or None.

        Returns:
            Formatted time string.
        """
        if not quota or not quota.reset_at:
            return ""

        remaining = quota.reset_at - datetime.utcnow()
        total_seconds = max(0, int(remaining.total_seconds()))

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        if hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"
