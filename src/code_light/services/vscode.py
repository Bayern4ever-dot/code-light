"""VS Code window management service."""

from __future__ import annotations

from ..models import AgentType, VSCodeWindow
from ..monitors.process import ProcessDetector
from ..utils.logger import logger


class VSCodeService:
    """Service for VS Code window management."""

    def __init__(self) -> None:
        """Initialize VS Code service."""
        self._detector = ProcessDetector()

    def get_windows(self) -> list[VSCodeWindow]:
        """Get all VS Code windows.

        Returns:
            List of VSCodeWindow instances.
        """
        return self._detector.get_vscode_windows()

    def get_windows_for_agent(self, agent_type: AgentType) -> list[VSCodeWindow]:
        """Get VS Code windows associated with an agent.

        Args:
            agent_type: The agent type.

        Returns:
            List of matching VSCodeWindow instances.
        """
        windows = self._detector.get_vscode_windows()
        return [w for w in windows if w.agent_type == agent_type]

    def focus_window(self, hwnd: int) -> bool:
        """Bring a VS Code window to foreground.

        Args:
            hwnd: Window handle.

        Returns:
            True if successful.
        """
        return self._detector.focus_window(hwnd)

    def focus_agent_window(self, agent_type: AgentType) -> bool:
        """Bring the VS Code window for an agent to foreground.

        Since Claude Code and Codex are VS Code extensions, we can't reliably
        detect which window has which agent. Instead, we:
        1. Try to find a window with matching agent marker in title
        2. Fall back to the first available VS Code window

        Args:
            agent_type: The agent type.

        Returns:
            True if window found and focused.
        """
        windows = self._detector.get_vscode_windows()
        if not windows:
            logger.warning("No VS Code windows found")
            return False

        # Try to find window with agent marker in title
        for window in windows:
            if window.agent_type == agent_type:
                return self.focus_window(window.hwnd)

        # Fall back to first VS Code window
        logger.info(f"No window with {agent_type.value} marker, focusing first VS Code window")
        return self.focus_window(windows[0].hwnd)

    def get_active_window(self) -> VSCodeWindow | None:
        """Get the currently active VS Code window.

        Returns:
            VSCodeWindow or None.
        """
        return self._detector.get_active_window_info()

    def has_agent_window(self, agent_type: AgentType) -> bool:
        """Check if a VS Code window exists for an agent.

        Args:
            agent_type: The agent type.

        Returns:
            True if window exists.
        """
        window = self._detector.find_window_for_agent(agent_type)
        return window is not None
