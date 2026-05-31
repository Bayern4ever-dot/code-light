"""Floating window implementation using tkinter."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import queue
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from typing import Callable, Optional

from PIL import Image, ImageDraw, ImageTk

from ..models import AgentStatus, AgentType, QuotaInfo, StatusLevel
from ..utils.logger import logger

BG_WINDOW = "#F3EBE4"
BG_SURFACE = "#FBF9F7"
BG_SURFACE_HOVER = "#FFFDFC"
BG_PILL = "#EEF4FE"
BG_PILL_HOVER = "#E8F0FC"
BORDER = "#D9D0C7"
BORDER_HOVER = "#C7BAAD"
SEPARATOR = "#DED6CE"
TEXT_PRIMARY = "#1F2933"
TEXT_SECONDARY = "#6F6761"
TEXT_MUTED = "#8A8179"
ACCENT = "#2F7BEA"
ACCENT_SOFT = "#DCE9FD"
ACCENT_BORDER = "#C8DBFA"
SHADOW_1 = "#DED3C8"
SHADOW_2 = "#E9E1DA"
CLOSE_BG = "#F1EAE3"
CLOSE_HOVER = "#E6DED5"
CLOSE_PRESSED = "#D9CFC5"
CONNECTED_GREEN = "#3BA55D"

WINDOW_RADIUS = 22
PANEL_RADIUS = 20
CARD_RADIUS = 16
PILL_RADIUS = 12
CARD_HEIGHT = 88
PILL_WIDTH = 88
PILL_HEIGHT = 24

STATUS_COLORS = {
    StatusLevel.IDLE: "#8E8680",
    StatusLevel.WORKING: "#3BA55D",
    StatusLevel.DONE: ACCENT,
    StatusLevel.WAITING: "#C6922A",
    StatusLevel.ERROR: "#D94052",
    StatusLevel.QUOTA_WARNING: "#D97B2A",
    StatusLevel.OFFLINE: "#B5AFA8",
}

PULSE_BRIGHT = "#3BA55D"
PULSE_DIM = "#7ABF8E"

STATUS_LABELS = {
    StatusLevel.IDLE: "Idle",
    StatusLevel.WORKING: "Working",
    StatusLevel.DONE: "Done",
    StatusLevel.WAITING: "Waiting",
    StatusLevel.ERROR: "Error",
    StatusLevel.QUOTA_WARNING: "Quota",
    StatusLevel.OFFLINE: "Offline",
}


def _remaining_percent(used_percent: object) -> float:
    try:
        used = float(used_percent or 0)
    except (TypeError, ValueError):
        used = 0.0
    return max(0.0, min(100.0, 100.0 - used))


def _quota_window_used_percent(quota: QuotaInfo, window_name: str) -> float | None:
    window = quota.extra_info.get(window_name)
    if not isinstance(window, dict):
        return None
    try:
        return float(window.get("used_percent", 0) or 0)
    except (TypeError, ValueError):
        return None

FONT_CANDIDATES = (
    "Inter",
    "SF Pro Text",
    "Segoe UI Variable",
    "Geist",
    "Manrope",
    "Segoe UI",
)


def _draw_rounded_rect(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int,
    *,
    fill: str,
    outline: str = "",
    width: int = 1,
    tags: tuple[str, ...] = (),
    image_key: str = "",
    matte: str | None = None,
    antialias: bool = True,
) -> int:
    """Draw an anti-aliased rounded rectangle without alpha-edge artifacts."""
    rect_width = max(1, x2 - x1)
    rect_height = max(1, y2 - y1)
    scale = 4 if antialias else 1
    scaled_size = (rect_width * scale, rect_height * scale)
    scaled_radius = max(0, radius * scale)
    scaled_width = max(1, width * scale)
    matte = matte or str(canvas.cget("bg"))

    img = Image.new("RGB", scaled_size, matte)
    draw = ImageDraw.Draw(img)
    box = [0, 0, scaled_size[0] - 1, scaled_size[1] - 1]
    draw.rounded_rectangle(
        box,
        radius=scaled_radius,
        fill=fill,
        outline=outline or None,
        width=scaled_width,
    )
    if antialias:
        img = img.resize((rect_width, rect_height), Image.Resampling.LANCZOS)

    photo = ImageTk.PhotoImage(img)

    refs = getattr(canvas, "_aa_image_refs", None)
    if refs is None:
        refs = {}
        setattr(canvas, "_aa_image_refs", refs)
    refs[image_key or ":".join(tags) or str(id(photo))] = photo

    return canvas.create_image(x1, y1, image=photo, anchor=tk.NW, tags=tags)


class FloatingWindow:
    """Desktop floating window showing agent status."""

    def __init__(
        self,
        on_focus_agent: Callable[[AgentType], None],
        on_open_dashboard: Callable[[], None],
        opacity: float = 1.0,
        width: int = 340,
        height: int = 400,
    ) -> None:
        """Initialize floating window."""
        self._on_focus_agent = on_focus_agent
        self._on_open_dashboard = on_open_dashboard
        self._opacity = max(0.0, min(opacity, 1.0))
        self._width = width
        self._height = height

        self._root: Optional[tk.Tk] = None
        self._agent_widgets: dict[AgentType, dict] = {}
        self._quotas: dict[AgentType, QuotaInfo] = {}
        self._visible = False
        self._pulse_state = False
        self._working_agents: set[AgentType] = set()
        self._font_family = "Segoe UI"
        self._ui_thread_id = threading.get_ident()

        self._pulse_after_id: Optional[str] = None
        self._queue_after_id: Optional[str] = None

        self._update_queue: queue.Queue = queue.Queue()
        self._control_queue: queue.Queue[str] = queue.Queue()

    def _is_ui_thread(self) -> bool:
        return threading.get_ident() == self._ui_thread_id

    def _queue_control(self, command: str) -> bool:
        """Queue a UI command when called from a non-tkinter thread."""
        if self._is_ui_thread():
            return False
        self._control_queue.put(command)
        return True

    def _resolve_font_family(self) -> str:
        """Choose the first available modern UI font."""
        try:
            available = set(tkfont.families())
        except tk.TclError:
            return "Segoe UI"
        for family in FONT_CANDIDATES:
            if family in available:
                return family
        return "Segoe UI"

    def _font(self, size: int, weight: str = "normal") -> tuple[str, int, str]:
        return (self._font_family, size, weight)

    def _apply_windows_shape(self) -> None:
        """Ask Windows to clip the borderless window to a rounded region."""
        if not self._root or sys.platform != "win32":
            return

        try:
            self._root.update_idletasks()
            hwnd = wintypes.HWND(self._root.winfo_id())
            width = max(1, self._root.winfo_width())
            height = max(1, self._root.winfo_height())
            diameter = max(1, WINDOW_RADIUS * 2)

            corner_pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                33,
                ctypes.byref(corner_pref),
                ctypes.sizeof(corner_pref),
            )

            create_region = ctypes.windll.gdi32.CreateRoundRectRgn
            create_region.restype = wintypes.HRGN

            set_window_region = ctypes.windll.user32.SetWindowRgn
            set_window_region.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
            set_window_region.restype = ctypes.c_int

            region = create_region(
                0,
                0,
                width + 1,
                height + 1,
                diameter,
                diameter,
            )
            if region and not set_window_region(hwnd, region, True):
                ctypes.windll.gdi32.DeleteObject(region)
        except Exception as exc:
            logger.debug(f"Rounded window shape unavailable: {exc}")

    def _bind_drag(self, widget: tk.Widget) -> None:
        """Bind widget to move the borderless floating window."""
        drag_data = {"x": 0, "y": 0}

        def start_drag(event: tk.Event) -> None:
            drag_data["x"] = event.x_root
            drag_data["y"] = event.y_root

        def drag(event: tk.Event) -> None:
            if not self._root:
                return
            dx = event.x_root - drag_data["x"]
            dy = event.y_root - drag_data["y"]
            x = self._root.winfo_x() + dx
            y = self._root.winfo_y() + dy
            self._root.geometry(f"+{x}+{y}")
            drag_data["x"] = event.x_root
            drag_data["y"] = event.y_root

        widget.bind("<Button-1>", start_drag)
        widget.bind("<B1-Motion>", drag)

    def _draw_shell(self, canvas: tk.Canvas) -> None:
        canvas.delete("shell")
        w = max(canvas.winfo_width(), self._width)
        h = max(canvas.winfo_height(), self._height)
        _draw_rounded_rect(
            canvas,
            0,
            0,
            w,
            h,
            WINDOW_RADIUS,
            fill=BG_WINDOW,
            outline=BORDER,
            width=1,
            tags=("shell",),
            image_key="shell_surface",
            matte=str(canvas.cget("bg")),
        )
        canvas.tag_lower("shell")

    def _create_logo(self, parent: tk.Widget) -> tk.Canvas:
        logo = tk.Canvas(parent, width=34, height=34, bg=BG_WINDOW, bd=0, highlightthickness=0)
        _draw_rounded_rect(
            logo,
            2,
            3,
            32,
            33,
            10,
            fill=SHADOW_2,
            outline="",
            tags=("logo",),
            image_key="logo_shadow",
        )
        _draw_rounded_rect(
            logo,
            1,
            1,
            31,
            31,
            10,
            fill=BG_SURFACE,
            outline=BORDER,
            width=1,
            tags=("logo",),
            image_key="logo_surface",
        )
        logo.create_text(
            16,
            16,
            text="CL",
            fill=ACCENT,
            font=self._font(10, "bold"),
            tags=("logo",),
        )
        logo.configure(cursor="hand2")
        logo.bind("<Double-Button-1>", lambda _e: self._on_open_dashboard())
        return logo

    def _create_close_button(self, parent: tk.Widget) -> tk.Canvas:
        button = tk.Canvas(parent, width=28, height=28, bg=BG_WINDOW, bd=0, highlightthickness=0)

        def draw(fill: str) -> None:
            button.delete("close")
            _draw_rounded_rect(
                button,
                2,
                2,
                26,
                26,
                9,
                fill=fill,
                outline=BORDER,
                width=1,
                tags=("close",),
                image_key="close_surface",
            )
            button.create_text(
                14,
                13,
                text="\u00d7",
                fill=TEXT_SECONDARY,
                font=self._font(13, "normal"),
                tags=("close",),
            )

        draw(CLOSE_BG)
        button.configure(cursor="hand2")
        button.bind("<ButtonRelease-1>", lambda _e: self.hide())
        button.bind("<Enter>", lambda _e: draw(CLOSE_HOVER))
        button.bind("<Leave>", lambda _e: draw(CLOSE_BG))
        button.bind("<ButtonPress-1>", lambda _e: draw(CLOSE_PRESSED))
        return button

    def _draw_pill(self, pill: tk.Canvas, fill: str) -> None:
        pill.delete("pill_shape")
        _draw_rounded_rect(
            pill,
            1,
            1,
            PILL_WIDTH - 1,
            PILL_HEIGHT - 1,
            PILL_RADIUS,
            fill=fill,
            outline=ACCENT_BORDER,
            width=1,
            tags=("pill_shape",),
            image_key="status_pill",
        )
        pill.tag_lower("pill_shape")

    def _draw_accent_bar(self, bar: tk.Canvas, color: str) -> None:
        bar.delete("accent")
        _draw_rounded_rect(
            bar,
            1,
            0,
            5,
            50,
            3,
            fill=color,
            outline="",
            tags=("accent",),
            image_key="accent_bar",
        )

    def _create_status_pill(self, parent: tk.Widget) -> tk.Canvas:
        pill = tk.Canvas(
            parent,
            width=PILL_WIDTH,
            height=PILL_HEIGHT,
            bg=BG_SURFACE,
            bd=0,
            highlightthickness=0,
        )
        self._draw_pill(pill, BG_PILL)
        pill.create_oval(
            12,
            9,
            18,
            15,
            fill=STATUS_COLORS[StatusLevel.OFFLINE],
            outline="",
            tags=("status_dot",),
        )
        pill.create_text(
            24,
            12,
            text="Offline",
            fill=TEXT_SECONDARY,
            font=self._font(9, "normal"),
            anchor=tk.W,
            tags=("status_text",),
        )
        return pill

    def _set_card_surface(self, widgets: dict, surface: str, border: str, pill_bg: str) -> None:
        widgets["card_canvas"].configure(bg=BG_WINDOW)
        widgets["content_frame"].configure(bg=surface)
        widgets["accent_bar"].configure(bg=surface)
        widgets["main_frame"].configure(bg=surface)
        widgets["top_row"].configure(bg=surface)
        widgets["info_label"].configure(bg=surface)
        widgets["detail_label"].configure(bg=surface)
        widgets["name_label"].configure(bg=surface)
        widgets["status_pill"].configure(bg=surface)
        widgets["border_color"] = border
        self._draw_pill(widgets["status_pill"], pill_bg)
        self._redraw_card(widgets)

    def _redraw_card(self, widgets: dict) -> None:
        canvas = widgets["card_canvas"]
        canvas.delete("card_shape")
        w = max(canvas.winfo_width(), self._width - 34)
        _draw_rounded_rect(
            canvas,
            6,
            8,
            w - 4,
            CARD_HEIGHT - 3,
            CARD_RADIUS,
            fill=widgets["shadow_color"],
            outline="",
            tags=("card_shape",),
            image_key=widgets["shadow_key"],
        )
        _draw_rounded_rect(
            canvas,
            2,
            2,
            w - 8,
            CARD_HEIGHT - 8,
            CARD_RADIUS,
            fill=widgets["surface_color"](),
            outline=widgets["border_color"],
            width=1,
            tags=("card_shape",),
            image_key=widgets["surface_key"],
        )
        canvas.coords(widgets["content_window"], 14, 10)
        canvas.itemconfigure(
            widgets["content_window"],
            width=max(1, w - 34),
            height=CARD_HEIGHT - 22,
        )
        canvas.tag_lower("card_shape")

    def _create_window(self) -> None:
        """Create the floating window."""
        self._root = tk.Tk()
        self._font_family = self._resolve_font_family()
        self._root.title("code-light")
        self._root.geometry(f"{self._width}x{self._height}")
        self._root.attributes("-topmost", True)
        self._root.attributes("-alpha", self._opacity)
        self._root.overrideredirect(True)
        self._root.configure(bg=BG_WINDOW)
        self._root.bind("<Escape>", lambda _e: self.hide())

        shell = tk.Canvas(self._root, bg=BG_WINDOW, bd=0, highlightthickness=0)
        shell.pack(fill=tk.BOTH, expand=True)
        self._bind_drag(shell)

        content = tk.Frame(shell, bg=BG_WINDOW)
        content_window = shell.create_window(16, 14, window=content, anchor=tk.NW)

        def resize_content(event: tk.Event) -> None:
            shell.itemconfigure(
                content_window,
                width=max(1, event.width - 32),
                height=max(1, event.height - 28),
            )

        def on_shell_configure(event: tk.Event) -> None:
            self._draw_shell(shell)
            resize_content(event)
            self._apply_windows_shape()

        shell.bind("<Configure>", on_shell_configure)

        title_frame = tk.Frame(content, bg=BG_WINDOW)
        title_frame.pack(fill=tk.X)
        self._bind_drag(title_frame)

        logo = self._create_logo(title_frame)
        logo.pack(side=tk.LEFT)

        title_label = tk.Label(
            title_frame,
            text="code-light",
            bg=BG_WINDOW,
            fg=TEXT_PRIMARY,
            font=self._font(14, "bold"),
        )
        title_label.pack(side=tk.LEFT, padx=(10, 0), pady=(2, 0))
        title_label.bind("<Double-Button-1>", lambda _e: self._on_open_dashboard())
        self._bind_drag(title_label)

        close_btn = self._create_close_button(title_frame)
        close_btn.pack(side=tk.RIGHT, pady=(3, 0))

        sep = tk.Frame(content, bg=SEPARATOR, height=1)
        sep.pack(fill=tk.X, pady=(12, 10))

        self._cards_container = tk.Frame(content, bg=BG_WINDOW)
        self._cards_container.pack(fill=tk.X)

        for agent_type in [AgentType.CLAUDE_CODE, AgentType.CODEX]:
            self._create_agent_card(agent_type)

        footer_sep = tk.Frame(content, bg=SEPARATOR, height=1)
        footer_sep.pack(fill=tk.X, pady=(4, 0))

        footer = tk.Frame(content, bg=BG_WINDOW)
        footer.pack(fill=tk.X, pady=(7, 0))

        dash_link = tk.Label(
            footer,
            text="Open Dashboard",
            bg=BG_WINDOW,
            fg=ACCENT,
            font=self._font(9, "normal"),
            cursor="hand2",
        )
        dash_link.pack(side=tk.LEFT)
        dash_link.bind("<Button-1>", lambda _e: self._on_open_dashboard())
        dash_link.bind("<Enter>", lambda _e: dash_link.configure(fg="#1F65C8"))
        dash_link.bind("<Leave>", lambda _e: dash_link.configure(fg=ACCENT))

        conn = tk.Frame(footer, bg=BG_WINDOW)
        conn.pack(side=tk.RIGHT)
        conn_dot = tk.Canvas(conn, width=10, height=10, bg=BG_WINDOW, bd=0, highlightthickness=0)
        conn_dot.create_oval(2, 2, 8, 8, fill=CONNECTED_GREEN, outline="")
        conn_dot.pack(side=tk.LEFT, padx=(0, 5), pady=(3, 0))
        conn_label = tk.Label(
            conn,
            text="Connected",
            bg=BG_WINDOW,
            fg=TEXT_SECONDARY,
            font=self._font(8, "normal"),
        )
        conn_label.pack(side=tk.LEFT)
        self._conn_label = conn_label

        self._process_queue()
        self._animate_pulse()
        self._apply_windows_shape()

    def _create_agent_card(self, agent_type: AgentType) -> None:
        """Create status card for an agent."""
        card_canvas = tk.Canvas(
            self._cards_container,
            height=CARD_HEIGHT,
            bg=BG_WINDOW,
            bd=0,
            highlightthickness=0,
        )
        card_canvas.pack(fill=tk.X, pady=(0, 8))

        content = tk.Frame(card_canvas, bg=BG_SURFACE)
        content_window = card_canvas.create_window(14, 10, window=content, anchor=tk.NW)

        accent_bar = tk.Canvas(content, width=5, height=50, bg=BG_SURFACE, bd=0, highlightthickness=0)
        accent_bar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12), pady=(2, 2))
        self._draw_accent_bar(accent_bar, STATUS_COLORS[StatusLevel.OFFLINE])

        main = tk.Frame(content, bg=BG_SURFACE)
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        top_row = tk.Frame(main, bg=BG_SURFACE)
        top_row.pack(fill=tk.X)

        agent_name = "Claude Code" if agent_type == AgentType.CLAUDE_CODE else "Codex"
        name_label = tk.Label(
            top_row,
            text=agent_name,
            bg=BG_SURFACE,
            fg=TEXT_PRIMARY,
            font=self._font(11, "bold"),
            anchor=tk.W,
        )
        name_label.pack(side=tk.LEFT)

        status_pill = self._create_status_pill(top_row)
        status_pill.pack(side=tk.RIGHT)

        info_label = tk.Label(
            main,
            text="No active session",
            bg=BG_SURFACE,
            fg=TEXT_SECONDARY,
            font=self._font(9, "normal"),
            anchor=tk.W,
        )
        info_label.pack(fill=tk.X, pady=(5, 0))

        detail_label = tk.Label(
            main,
            text="",
            bg=BG_SURFACE,
            fg=TEXT_MUTED,
            font=self._font(8, "normal"),
            anchor=tk.W,
        )
        detail_label.pack(fill=tk.X, pady=(1, 0))

        widgets = {
            "card_canvas": card_canvas,
            "content_frame": content,
            "content_window": content_window,
            "accent_bar": accent_bar,
            "main_frame": main,
            "top_row": top_row,
            "name_label": name_label,
            "status_pill": status_pill,
            "info_label": info_label,
            "detail_label": detail_label,
            "border_color": BORDER,
            "shadow_color": SHADOW_2,
            "shadow_key": f"{agent_type.value}_card_shadow",
            "surface_key": f"{agent_type.value}_card_surface",
            "hovered": False,
            "last_status": StatusLevel.OFFLINE,
        }
        widgets["surface_color"] = lambda w=widgets: BG_SURFACE_HOVER if w["hovered"] else BG_SURFACE

        card_canvas.bind("<Configure>", lambda _e, w=widgets: self._redraw_card(w))
        self._redraw_card(widgets)

        def set_hover(hovered: bool) -> None:
            widgets["hovered"] = hovered
            if hovered:
                self._set_card_surface(widgets, BG_SURFACE_HOVER, BORDER_HOVER, BG_PILL_HOVER)
            else:
                self._set_card_surface(widgets, BG_SURFACE, BORDER, BG_PILL)

        def on_click(_event: tk.Event) -> None:
            self._on_focus_agent(agent_type)

        bind_targets = [
            card_canvas,
            content,
            accent_bar,
            main,
            top_row,
            name_label,
            info_label,
            detail_label,
            status_pill,
        ]
        for widget in bind_targets:
            widget.bind("<Enter>", lambda _e, hover=True: set_hover(hover))
            widget.bind("<Leave>", lambda _e, hover=False: set_hover(hover))
            widget.bind("<Button-1>", on_click)
            widget.configure(cursor="hand2")

        self._agent_widgets[agent_type] = widgets

    def _animate_pulse(self) -> None:
        """Animate pulsing dot for working status."""
        if not self._root:
            return

        self._pulse_state = not self._pulse_state

        for agent_type in self._working_agents:
            if agent_type in self._agent_widgets:
                widgets = self._agent_widgets[agent_type]
                color = PULSE_BRIGHT if self._pulse_state else PULSE_DIM
                try:
                    widgets["status_pill"].itemconfigure("status_dot", fill=color)
                except tk.TclError:
                    pass

        try:
            self._pulse_after_id = self._root.after(600, self._animate_pulse)
        except tk.TclError:
            pass

    def _process_queue(self) -> None:
        """Process queued updates on the main thread."""
        try:
            while True:
                item = self._update_queue.get_nowait()
                if isinstance(item, tuple):
                    statuses, quotas = item
                    self._quotas.update(quotas)
                else:
                    statuses = item
                self._apply_update(statuses)
        except queue.Empty:
            pass

        try:
            self._queue_after_id = self._root.after(100, self._process_queue)
        except tk.TclError:
            pass

    def _apply_update(self, statuses: dict[AgentType, AgentStatus]) -> None:
        """Apply status update on the tkinter thread."""
        if not self._root:
            return

        for agent_type, status in statuses.items():
            if agent_type not in self._agent_widgets:
                continue

            widgets = self._agent_widgets[agent_type]
            color = STATUS_COLORS.get(status.status, STATUS_COLORS[StatusLevel.OFFLINE])
            label = STATUS_LABELS.get(status.status, status.status.value)

            if status.status == StatusLevel.WORKING:
                self._working_agents.add(agent_type)
            else:
                self._working_agents.discard(agent_type)

            try:
                self._draw_accent_bar(widgets["accent_bar"], color)
                widgets["status_pill"].itemconfigure("status_dot", fill=color)
                widgets["status_pill"].itemconfigure("status_text", text=label)
            except tk.TclError:
                pass

            detail_parts = []
            if status.project_path:
                project_name = status.project_path.split("\\")[-1].split("/")[-1]
                detail_parts.append(project_name)
            if status.last_activity:
                detail_parts.append(status.last_activity.strftime("%H:%M:%S"))

            tokens = status.tokens
            if agent_type == AgentType.CODEX and agent_type in self._quotas:
                quota = self._quotas[agent_type]
                primary_used = _quota_window_used_percent(quota, "primary_window")
                if primary_used is None:
                    primary_used = quota.used_percent
                secondary_used = _quota_window_used_percent(quota, "secondary_window")

                info_text = f"5H {_remaining_percent(primary_used):.1f}% left"
                if secondary_used is not None:
                    info_text += f" / 1W {_remaining_percent(secondary_used):.1f}% left"
            elif tokens.total_tokens > 0:
                model_str = status.model.split("/")[-1] if status.model else "Unknown"
                info_text = f"{model_str} / {tokens.total_tokens:,} tokens"
            elif status.model:
                info_text = status.model
            else:
                info_text = "No active session"

            try:
                widgets["info_label"].configure(text=info_text)
            except tk.TclError:
                pass

            detail_text = " / ".join(detail_parts) if detail_parts else ""
            try:
                widgets["detail_label"].configure(text=detail_text)
            except tk.TclError:
                pass

            widgets["last_status"] = status.status

    def update_status(
        self,
        statuses: dict[AgentType, AgentStatus],
        quotas: dict[AgentType, QuotaInfo] | None = None,
    ) -> None:
        """Update floating window with current statuses."""
        if quotas is None:
            self._update_queue.put(statuses)
        else:
            self._update_queue.put((statuses, quotas))

    def _show_now(self) -> None:
        """Show the floating window on the tkinter thread."""
        if not self._root:
            self._create_window()

        if not self._visible:
            self._root.deiconify()
            self._visible = True
            logger.info("Floating window shown")

    def _hide_now(self) -> None:
        """Hide the floating window on the tkinter thread."""
        if self._root and self._visible:
            self._root.withdraw()
            self._visible = False
            logger.info("Floating window hidden")

    def _toggle_now(self) -> None:
        """Toggle floating window visibility on the tkinter thread."""
        if self._visible:
            self._hide_now()
        else:
            self._show_now()

    def _process_control_queue(self) -> None:
        """Apply UI commands queued by non-tkinter threads."""
        while True:
            try:
                command = self._control_queue.get_nowait()
            except queue.Empty:
                return

            if command == "show":
                self._show_now()
            elif command == "hide":
                self._hide_now()
            elif command == "toggle":
                self._toggle_now()
            elif command == "destroy":
                self._destroy_now()

    def show(self) -> None:
        """Show the floating window."""
        if self._queue_control("show"):
            return
        self._show_now()

    def hide(self) -> None:
        """Hide the floating window."""
        if self._queue_control("hide"):
            return
        self._hide_now()

    def toggle(self) -> None:
        """Toggle floating window visibility."""
        if self._queue_control("toggle"):
            return
        self._toggle_now()

    def _destroy_now(self) -> None:
        """Destroy the floating window on the tkinter thread."""
        if self._root:
            for aid in (self._pulse_after_id, self._queue_after_id):
                if aid:
                    try:
                        self._root.after_cancel(aid)
                    except tk.TclError:
                        pass
            self._pulse_after_id = None
            self._queue_after_id = None

            self._root.destroy()
            self._root = None
            self._agent_widgets.clear()
            self._quotas.clear()
            self._working_agents.clear()
            self._visible = False
            logger.info("Floating window destroyed")

    def destroy(self) -> None:
        """Destroy the floating window."""
        if self._queue_control("destroy"):
            return
        self._destroy_now()

    def process_events(self) -> None:
        """Process tkinter events."""
        self._process_control_queue()
        if self._root:
            try:
                self._root.update_idletasks()
                self._root.update()
            except tk.TclError:
                pass
