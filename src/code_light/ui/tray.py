"""System tray icon implementation."""

from __future__ import annotations

import threading
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw

from ..models import AgentType, StatusLevel
from ..utils.logger import logger

STATUS_COLORS = {
    StatusLevel.IDLE: "#8E8680",
    StatusLevel.WORKING: "#3BA55D",
    StatusLevel.DONE: "#2F7BEA",
    StatusLevel.WAITING: "#C6922A",
    StatusLevel.ERROR: "#D94052",
    StatusLevel.QUOTA_WARNING: "#D97B2A",
    StatusLevel.OFFLINE: "#B5AFA8",
}


def _create_composite_icon(
    statuses: dict[AgentType, StatusLevel],
    size: int = 64,
) -> Image.Image:
    """Create a compact status icon for the tray."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if not statuses:
        draw.ellipse([2, 2, size - 2, size - 2], fill=STATUS_COLORS[StatusLevel.OFFLINE])
        return img

    priority = [
        StatusLevel.ERROR,
        StatusLevel.QUOTA_WARNING,
        StatusLevel.WAITING,
        StatusLevel.WORKING,
        StatusLevel.DONE,
        StatusLevel.IDLE,
        StatusLevel.OFFLINE,
    ]

    main_status = next(
        (status for status in priority if status in statuses.values()),
        StatusLevel.OFFLINE,
    )
    color = STATUS_COLORS.get(main_status, STATUS_COLORS[StatusLevel.OFFLINE])

    draw.ellipse([2, 2, size - 2, size - 2], fill=color)
    draw.ellipse([10, 8, size - 14, size - 18], fill="#ffffff33")

    if len(statuses) > 1:
        dot_size = 13
        x_offset = size - dot_size - 3
        for status in statuses.values():
            dot_color = STATUS_COLORS.get(status, STATUS_COLORS[StatusLevel.OFFLINE])
            draw.ellipse(
                [x_offset, 3, x_offset + dot_size, 3 + dot_size],
                fill=dot_color,
                outline="#111827",
                width=1,
            )
            x_offset -= dot_size + 3

    return img


class SystemTray:
    """System tray icon manager."""

    def __init__(
        self,
        on_open_dashboard: Callable[[], None],
        on_toggle_floating: Callable[[], None],
        on_focus_agent: Callable[[AgentType], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._on_open_dashboard = on_open_dashboard
        self._on_toggle_floating = on_toggle_floating
        self._on_focus_agent = on_focus_agent
        self._on_quit = on_quit

        self._icon: Optional[pystray.Icon] = None
        self._thread: Optional[threading.Thread] = None
        self._current_statuses: dict[AgentType, StatusLevel] = {}

    def _build_menu(self) -> pystray.Menu:
        """Build the tray context menu."""
        menu_items = [
            pystray.MenuItem(
                "Open Dashboard",
                lambda: self._on_open_dashboard(),
                default=True,
            ),
            pystray.MenuItem(
                "Toggle Floating Window",
                lambda: self._on_toggle_floating(),
            ),
            pystray.Menu.SEPARATOR,
        ]

        status_tags = {
            StatusLevel.IDLE: "[idle]",
            StatusLevel.WORKING: "[work]",
            StatusLevel.DONE: "[done]",
            StatusLevel.WAITING: "[wait]",
            StatusLevel.ERROR: "[error]",
            StatusLevel.QUOTA_WARNING: "[quota]",
            StatusLevel.OFFLINE: "[off]",
        }

        for agent_type, status in self._current_statuses.items():
            agent_name = agent_type.value.replace("_", " ").title()
            status_tag = status_tags.get(status, "[?]")
            menu_items.append(
                pystray.MenuItem(
                    f"{status_tag} {agent_name}",
                    lambda at=agent_type: self._on_focus_agent(at),
                )
            )

        menu_items.extend(
            [
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit", lambda: self._on_quit()),
            ]
        )

        return pystray.Menu(*menu_items)

    def update_status(self, statuses: dict[AgentType, StatusLevel]) -> None:
        """Update tray icon based on agent statuses."""
        self._current_statuses = statuses

        if self._icon:
            self._icon.icon = _create_composite_icon(statuses)
            self._icon.menu = self._build_menu()
            tooltip_parts = []
            for agent_type, status in statuses.items():
                agent_name = agent_type.value.replace("_", " ").title()
                tooltip_parts.append(f"{agent_name}: {status.value}")
            self._icon.title = "code-light\n" + "\n".join(tooltip_parts)

    def start(self) -> None:
        """Start the system tray icon in a background thread."""
        if self._icon:
            return

        self._icon = pystray.Icon(
            "code-light",
            _create_composite_icon({}),
            "code-light - AI Coding Monitor",
            self._build_menu(),
        )

        def _run():
            try:
                self._icon.run()
            except Exception as e:
                logger.error(f"Tray icon error: {e}")

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        logger.info("System tray started")

    def stop(self) -> None:
        """Stop the system tray icon."""
        if self._icon:
            self._icon.stop()
            self._icon = None
            logger.info("System tray stopped")
