"""Windows-specific utilities."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
from typing import Optional

import psutil

# Win32 API constants
GWL_STYLE = -16
WS_MINIMIZE = 0x2000000
SW_RESTORE = 9
SW_SHOW = 5


def get_foreground_window_pid() -> Optional[int]:
    """Get the process ID of the current foreground window.

    Returns:
        Process ID or None if unable to determine.
    """
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value
    except Exception:
        return None


def is_window_minimized(hwnd: int) -> bool:
    """Check if a window is minimized.

    Args:
        hwnd: Window handle.

    Returns:
        True if window is minimized.
    """
    try:
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
        return bool(style & WS_MINIMIZE)
    except Exception:
        return False


def restore_window(hwnd: int) -> bool:
    """Restore and bring a minimized window to foreground.

    Args:
        hwnd: Window handle.

    Returns:
        True if successful.
    """
    try:
        if is_window_minimized(hwnd):
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def get_process_name(pid: int) -> Optional[str]:
    """Get process name by PID.

    Args:
        pid: Process ID.

    Returns:
        Process name or None.
    """
    try:
        proc = psutil.Process(pid)
        return proc.name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
