"""Data models for code-light."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class AgentType(str, Enum):
    """Supported AI coding agents."""

    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


class StatusLevel(str, Enum):
    """Agent status levels."""

    IDLE = "idle"
    WORKING = "working"
    DONE = "done"
    WAITING = "waiting"
    ERROR = "error"
    QUOTA_WARNING = "quota_warning"
    OFFLINE = "offline"


@dataclass(frozen=True)
class TokenUsage:
    """Token usage record."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    conversation_count: int = 0

    @property
    def cost_usd(self) -> float:
        """Compute cost from token counts (to be overridden per model)."""
        return 0.0


@dataclass(frozen=True)
class AgentStatus:
    """Current status of an AI coding agent."""

    agent_type: AgentType
    status: StatusLevel = StatusLevel.OFFLINE
    model: str = ""
    project_path: str = ""
    session_id: str = ""
    last_activity: Optional[datetime] = None
    tokens: TokenUsage = field(default_factory=TokenUsage)
    message: str = ""
    detail: str = ""


@dataclass(frozen=True)
class QuotaInfo:
    """Quota/rate limit information."""

    agent_type: AgentType
    plan_name: str = ""
    used_percent: float = 0.0
    limit_window_seconds: int = 0
    reset_at: Optional[datetime] = None
    remaining_seconds: int = 0
    credits_balance: float = 0.0
    extra_info: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TaskRecord:
    """Historical task record."""

    id: int = 0
    agent_type: AgentType = AgentType.CLAUDE_CODE
    session_id: str = ""
    project_path: str = ""
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    status: str = ""


@dataclass(frozen=True)
class VSCodeWindow:
    """VS Code window information."""

    hwnd: int = 0
    pid: int = 0
    title: str = ""
    project_name: str = ""
    is_active: bool = False
    agent_type: Optional[AgentType] = None
