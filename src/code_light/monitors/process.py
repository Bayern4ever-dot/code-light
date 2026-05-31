"""Process detector for VS Code and AI agent processes."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
from typing import Optional

import psutil

from ..models import AgentType, VSCodeWindow
from ..utils.logger import logger

# VS Code window class name
VSCODE_WINDOW_CLASS = "Chrome_WidgetWin_1"
# VS Code process names
VSCODE_PROCESS_NAMES = {"Code.exe", "code.exe"}


def _enum_windows() -> list[tuple[int, str]]:
    """Enumerate all visible windows.

    Returns:
        List of (hwnd, title) tuples.
    """
    windows = []

    def _callback(hwnd, _lparam):
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True

        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True

        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        if title:
            windows.append((hwnd, title))
        return True

    # Define the callback type
    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(_callback), 0)
    return windows


def _get_window_class(hwnd: int) -> str:
    """Get window class name.

    Args:
        hwnd: Window handle.

    Returns:
        Window class name.
    """
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _get_window_pid(hwnd: int) -> int:
    """Get process ID for a window.

    Args:
        hwnd: Window handle.

    Returns:
        Process ID.
    """
    pid = ctypes.wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _extract_project_name(title: str) -> str:
    """Extract project name from VS Code window title.

    VS Code title format: "filename - project - Visual Studio Code"
    or "project - Visual Studio Code"

    Args:
        title: Window title.

    Returns:
        Project name or empty string.
    """
    if " - Visual Studio Code" not in title:
        return ""

    # Remove " - Visual Studio Code" suffix
    title = title.replace(" - Visual Studio Code", "").strip()

    # Split by " - " and take the last part as project name
    parts = title.split(" - ")
    if len(parts) >= 2:
        return parts[-1].strip()
    return parts[0].strip() if parts else ""


def _detect_agent_type(title: str) -> Optional[AgentType]:
    """Detect which AI agent is associated with a VS Code window.

    Since Claude Code and Codex are VS Code extensions, the window title
    doesn't contain the agent name. We detect by checking for running
    agent processes or return None for manual association.

    Args:
        title: Window title.

    Returns:
        AgentType or None.
    """
    # Check for explicit agent markers in title
    title_lower = title.lower()
    if "claude" in title_lower:
        return AgentType.CLAUDE_CODE
    if "codex" in title_lower:
        return AgentType.CODEX

    # No explicit marker - agent type unknown
    return None


class ProcessDetector:
    """Detects VS Code windows and AI agent processes."""

    def __init__(self) -> None:
        """Initialize process detector."""
        self._vscode_pids: set[int] = set()
        self._update_vscode_pids()

    def _update_vscode_pids(self) -> None:
        """Update set of VS Code process IDs."""
        self._vscode_pids.clear()
        try:
            for proc in psutil.process_iter(["name", "pid"]):
                try:
                    proc_name = proc.info["name"]
                    if proc_name and proc_name in VSCODE_PROCESS_NAMES:
                        self._vscode_pids.add(proc.info["pid"])
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as e:
            logger.warning(f"Error enumerating processes: {e}")

    def get_vscode_windows(self) -> list[VSCodeWindow]:
        """Get all VS Code windows.

        Returns:
            List of VSCodeWindow instances.
        """
        self._update_vscode_pids()
        windows = []

        for hwnd, title in _enum_windows():
            # Check if this is a VS Code window by class name
            class_name = _get_window_class(hwnd)
            if class_name != VSCODE_WINDOW_CLASS:
                continue

            pid = _get_window_pid(hwnd)

            # Also check if window title contains "Visual Studio Code"
            if "Visual Studio Code" not in title:
                continue

            project_name = _extract_project_name(title)
            agent_type = _detect_agent_type(title)

            windows.append(
                VSCodeWindow(
                    hwnd=hwnd,
                    pid=pid,
                    title=title,
                    project_name=project_name,
                    agent_type=agent_type,
                )
            )

        return windows

    def find_window_for_agent(self, agent_type: AgentType) -> Optional[VSCodeWindow]:
        """Find VS Code window associated with an agent.

        Args:
            agent_type: The agent type to find.

        Returns:
            VSCodeWindow or None.
        """
        windows = self.get_vscode_windows()
        for window in windows:
            if window.agent_type == agent_type:
                return window
        return None

    def focus_window(self, hwnd: int) -> bool:
        """Bring a window to foreground.

        Args:
            hwnd: Window handle.

        Returns:
            True if successful.
        """
        try:
            # Check if window is minimized
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -16)
            if style & 0x2000000:  # WS_MINIMIZE
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE

            ctypes.windll.user32.SetForegroundWindow(hwnd)
            return True
        except Exception as e:
            logger.warning(f"Failed to focus window {hwnd}: {e}")
            return False

    def get_active_window_info(self) -> Optional[VSCodeWindow]:
        """Get info about the currently active VS Code window.

        Returns:
            VSCodeWindow of active window or None.
        """
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return None

            class_name = _get_window_class(hwnd)
            if class_name != VSCODE_WINDOW_CLASS:
                return None

            buf = ctypes.create_unicode_buffer(512)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
            title = buf.value

            if "Visual Studio Code" not in title:
                return None

            pid = _get_window_pid(hwnd)

            return VSCodeWindow(
                hwnd=hwnd,
                pid=pid,
                title=title,
                project_name=_extract_project_name(title),
                is_active=True,
                agent_type=_detect_agent_type(title),
            )
        except Exception as e:
            logger.warning(f"Error getting active window: {e}")
            return None
