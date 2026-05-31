"""Abstract base monitor."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..models import AgentStatus, QuotaInfo


class BaseMonitor(ABC):
    """Abstract base class for agent monitors."""

    @abstractmethod
    async def poll_status(self) -> AgentStatus:
        """Poll current agent status.

        Returns:
            Current AgentStatus.
        """
        ...

    async def poll_sessions(self) -> list[AgentStatus]:
        """Poll recent sessions for this monitor.

        Monitors that cannot expose multiple sessions can rely on the aggregate
        status as a single-session fallback.
        """
        return [await self.poll_status()]

    @abstractmethod
    async def poll_quota(self) -> Optional[QuotaInfo]:
        """Poll quota/rate limit information.

        Returns:
            QuotaInfo or None if not available.
        """
        ...

    @abstractmethod
    async def start_watching(self, callback: Callable[[AgentStatus], None]) -> None:
        """Start watching for status changes.

        Args:
            callback: Function to call when status changes.
        """
        ...

    @abstractmethod
    async def stop_watching(self) -> None:
        """Stop watching for status changes."""
        ...
