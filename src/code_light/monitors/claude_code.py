"""Claude Code session monitor.

Watches ~/.claude/projects/ for JSONL session files and tracks:
- Token usage (input, output, cache read/write)
- Model information (DeepSeek, Mimo, etc.)
- Session activity and status (fine-grained via tail-event analysis)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..models import AgentStatus, AgentType, QuotaInfo, StatusLevel, TokenUsage
from ..utils.logger import logger
from .base import BaseMonitor

# Maximum number of tail events to keep for status analysis
_TAIL_EVENT_LIMIT = 20


def _parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse ISO timestamp string."""
    if not ts_str:
        return None
    try:
        # Handle both Z suffix and +00:00
        ts_str = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return None


def _normalize_model(model: str) -> str:
    """Normalize model name."""
    if not model:
        return "unknown"
    model = model.lower()
    if "deepseek" in model:
        if "reason" in model:
            return "deepseek-reasoner"
        if "coder" in model:
            return "deepseek-coder"
        return "deepseek-chat"
    if "mimo" in model:
        return "mimo"
    if "gpt-4o-mini" in model:
        return "gpt-4o-mini"
    if "gpt-4o" in model:
        return "gpt-4o"
    if "gpt-4-turbo" in model:
        return "gpt-4-turbo"
    if "gpt-4" in model:
        return "gpt-4"
    if "claude-3-opus" in model or "claude-opus" in model:
        return "claude-3-opus"
    if "claude-3-sonnet" in model or "claude-sonnet" in model:
        return "claude-3-sonnet"
    if "claude-3-haiku" in model or "claude-haiku" in model:
        return "claude-3-haiku"
    return model


def _usage_int(usage: dict, *keys: str) -> int:
    """Read an integer usage value from any known Claude Code key."""
    for key in keys:
        value = usage.get(key)
        if value is not None:
            return int(value or 0)
    return 0


def _parse_usage(usage: dict) -> TokenUsage:
    """Parse usage dict into TokenUsage.

    Args:
        usage: Usage dict from JSONL.

    Returns:
        TokenUsage instance.
    """
    input_tok = _usage_int(usage, "input_tokens", "input")
    output_tok = _usage_int(usage, "output_tokens", "output")
    cache_read = _usage_int(usage, "cache_read_input_tokens", "cacheRead")
    cache_write = _usage_int(
        usage,
        "cache_creation_input_tokens",
        "cacheWrite",
    )

    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        cache_write += _usage_int(
            cache_creation,
            "ephemeral_1h_input_tokens",
            "ephemeral_5m_input_tokens",
        )
    total = input_tok + output_tok + cache_read + cache_write

    return TokenUsage(
        input_tokens=input_tok,
        output_tokens=output_tok,
        cached_input_tokens=cache_read,
        reasoning_output_tokens=cache_write,
        total_tokens=total,
    )


@dataclass
class _SessionState:
    """Fine-grained state derived from tail-event analysis."""

    status: StatusLevel = StatusLevel.IDLE
    detail: str = ""           # human-readable: "Running Bash", "Tool error in Edit"
    last_tool_name: str = ""   # last tool invoked
    stop_reason: str = ""      # "end_turn" | "tool_use" | ""
    has_tool_error: bool = False
    is_waiting: bool = False   # waiting for tool result / permission


def _extract_tool_names(content_blocks: list) -> list[str]:
    """Extract tool names from assistant message content blocks.

    Args:
        content_blocks: List of content block dicts from message.content.

    Returns:
        List of tool names found.
    """
    names = []
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name", "")
            if name:
                names.append(name)
    return names


def _extract_tool_error_detail(obj: dict) -> str:
    """Extract error detail from a tool_result user event.

    Args:
        obj: The JSONL event dict (type=user with tool_result content).

    Returns:
        Error description string, or empty if no error.
    """
    message = obj.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    if not isinstance(content, list):
        return ""

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result" and block.get("is_error"):
            # Try to get error text from content
            inner = block.get("content")
            if isinstance(inner, list):
                texts = [
                    b.get("text", "")
                    for b in inner
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                if texts:
                    return texts[0][:200]
            elif isinstance(inner, str):
                return inner[:200]
            return "tool error"

    # Also check top-level toolUseResult
    tur = obj.get("toolUseResult")
    if isinstance(tur, str) and tur.startswith("Error"):
        return tur[:200]

    return ""


def _has_tool_result_after(events: list[dict], start_idx: int, tool_use_ids: set[str]) -> bool:
    """Check if any tool_use IDs have corresponding tool_result events after start_idx.

    Args:
        events: Full list of events.
        start_idx: Index to start searching from.
        tool_use_ids: Set of tool_use IDs to look for.

    Returns:
        True if at least one tool_result was found for the given IDs.
    """
    if not tool_use_ids:
        return False

    for i in range(start_idx, len(events)):
        obj = events[i]
        if obj.get("type") != "user":
            continue
        message = obj.get("message", {})
        if not isinstance(message, dict):
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if block.get("tool_use_id") in tool_use_ids:
                    return True
    return False


def _extract_tool_use_ids(content_blocks: list) -> set[str]:
    """Extract tool_use IDs from assistant content blocks.

    Args:
        content_blocks: List of content block dicts.

    Returns:
        Set of tool_use ID strings.
    """
    ids = set()
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            tid = block.get("id")
            if tid:
                ids.add(tid)
    return ids


def _analyze_tail_events(events: list[dict]) -> _SessionState:
    """Analyze the last few events to determine fine-grained status.

    Examines events in reverse chronological order to find the most
    recent state-significant event.

    Args:
        events: List of parsed JSONL dicts (chronological order).

    Returns:
        _SessionState with status and detail.
    """
    if not events:
        return _SessionState(status=StatusLevel.IDLE)

    # Walk backwards through events looking for state-significant ones
    for idx, obj in enumerate(reversed(events)):
        event_type = obj.get("type", "")

        # 1. assistant with stop_reason=tool_use → tool call issued
        if event_type == "assistant":
            message = obj.get("message", {})
            if not isinstance(message, dict):
                continue

            stop_reason = message.get("stop_reason", "")
            content = message.get("content", [])
            if not isinstance(content, list):
                content = []

            tool_names = _extract_tool_names(content)
            has_thinking = any(
                isinstance(b, dict) and b.get("type") == "thinking"
                for b in content
            )

            if stop_reason == "tool_use" and tool_names:
                # Check if the tool call has been resolved (has tool_result)
                # idx is the reversed index; convert to forward index
                forward_idx = len(events) - 1 - idx
                tool_use_ids = _extract_tool_use_ids(content)
                resolved = _has_tool_result_after(events, forward_idx + 1, tool_use_ids)

                if not resolved:
                    # Tool call not yet resolved → waiting for permission
                    detail = f"Waiting: {tool_names[-1]}"
                    if has_thinking:
                        detail = f"Thinking → Waiting: {tool_names[-1]}"
                    return _SessionState(
                        status=StatusLevel.WAITING,
                        detail=detail,
                        last_tool_name=tool_names[-1],
                        stop_reason=stop_reason,
                        is_waiting=True,
                    )

                # Tool call resolved → WORKING (tool is executing/completed)
                detail = f"Running {tool_names[-1]}"
                if has_thinking:
                    detail = f"Thinking → {tool_names[-1]}"
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail=detail,
                    last_tool_name=tool_names[-1],
                    stop_reason=stop_reason,
                )

            if stop_reason == "end_turn":
                # Turn complete — could be done or just finished one step
                detail = "Response complete"
                if tool_names:
                    detail = f"{tool_names[-1]} complete"
                return _SessionState(
                    status=StatusLevel.DONE,
                    detail=detail,
                    last_tool_name=tool_names[-1] if tool_names else "",
                    stop_reason=stop_reason,
                )

            # assistant with no stop_reason yet (streaming) → WORKING
            if not stop_reason:
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail="Generating response",
                )

        # 2. user event with tool_result → tool just completed
        if event_type == "user":
            message = obj.get("message", {})
            if not isinstance(message, dict):
                continue

            content = message.get("content", [])
            if isinstance(content, list):
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if has_tool_result:
                    error_detail = _extract_tool_error_detail(obj)
                    if error_detail:
                        return _SessionState(
                            status=StatusLevel.ERROR,
                            detail=error_detail,
                            has_tool_error=True,
                        )
                    return _SessionState(
                        status=StatusLevel.WORKING,
                        detail="Tool result received",
                    )

            # user event with promptId → new user prompt
            if obj.get("promptId"):
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail="Processing new prompt",
                )

        # 3. system event with stop_hook_summary → task complete
        if event_type == "system":
            subtype = obj.get("subtype", "")
            if subtype == "stop_hook_summary":
                return _SessionState(
                    status=StatusLevel.DONE,
                    detail="Task complete",
                    stop_reason="end_turn",
                )

        # 4. attachment events
        if event_type == "attachment":
            attachment = obj.get("attachment", {})
            if not isinstance(attachment, dict):
                continue

            hook_event = attachment.get("hookEvent", "")
            hook_name = attachment.get("hookName", "")

            # Non-blocking error from hook (check FIRST — errors take priority)
            if attachment.get("type") == "hook_non_blocking_error":
                return _SessionState(
                    status=StatusLevel.ERROR,
                    detail=f"Hook error: {hook_name}" if hook_name else "Hook error",
                    has_tool_error=True,
                )

            # Stop hook → task done
            if hook_event == "Stop":
                return _SessionState(
                    status=StatusLevel.DONE,
                    detail="Task complete",
                    stop_reason="end_turn",
                )

            # UserPromptSubmit hook → new prompt being processed
            if hook_event == "UserPromptSubmit":
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail="Processing new prompt",
                )

            # PreToolUse hook → tool about to execute
            if hook_event == "PreToolUse":
                tool = hook_name.replace("PreToolUse:", "").strip() if ":" in hook_name else hook_name
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail=f"Preparing {tool}" if tool else "Preparing tool",
                    last_tool_name=tool,
                )

        # 5. queue-operation with enqueue → prompt queued
        if event_type == "queue-operation":
            operation = obj.get("operation", "")
            if operation == "enqueue":
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail="Prompt queued",
                )

    # No state-significant event found — fall back to IDLE
    return _SessionState(status=StatusLevel.IDLE)


class _SessionFileHandler(FileSystemEventHandler):
    """Watchdog handler for Claude session file changes."""

    def __init__(self, callback: Callable[[], None]) -> None:
        super().__init__()
        self._callback = callback

    def on_modified(self, event) -> None:
        if event.is_directory:
            return
        if event.src_path.endswith(".jsonl"):
            self._callback()


class ClaudeCodeMonitor(BaseMonitor):
    """Monitor for Claude Code sessions."""

    def __init__(
        self,
        claude_home: Path,
        idle_threshold_seconds: int = 300,
        active_threshold_seconds: int = 15,
        max_sessions: int = 8,
    ) -> None:
        """Initialize Claude Code monitor.

        Args:
            claude_home: Path to ~/.claude directory.
            idle_threshold_seconds: Seconds of inactivity before considering idle.
            active_threshold_seconds: Seconds since last write to consider working.
            max_sessions: Maximum recent sessions to expose.
        """
        self._claude_home = claude_home
        self._projects_dir = claude_home / "projects"
        self._idle_threshold = timedelta(seconds=idle_threshold_seconds)
        self._active_threshold = timedelta(seconds=active_threshold_seconds)
        self._max_sessions = max_sessions
        self._observer: Optional[Observer] = None
        self._last_status: Optional[AgentStatus] = None

    def _find_session_files(self) -> list[Path]:
        """Find all JSONL session files.

        Returns:
            List of paths to session JSONL files, sorted by modification time.
        """
        if not self._projects_dir.exists():
            logger.debug(f"Claude projects directory not found: {self._projects_dir}")
            return []

        session_files = []
        for jsonl_file in self._projects_dir.rglob("*.jsonl"):
            if jsonl_file.is_file():
                session_files.append(jsonl_file)

        # Sort by modification time (newest first)
        session_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        if session_files:
            logger.debug(f"Found {len(session_files)} Claude session files")
        else:
            logger.debug(f"No Claude session files found in {self._projects_dir}")

        return session_files

    def _parse_session_file(
        self,
        latest_file: Path,
    ) -> tuple[TokenUsage, str, str, str, Optional[datetime], _SessionState]:
        """Parse one Claude Code JSONL session file.

        Returns:
            Tuple of (usage, model, project_path, session_id, last_activity, session_state).
        """
        total_input = 0
        total_output = 0
        total_cache_read = 0
        total_cache_write = 0
        model = "unknown"
        project_path = ""
        session_id = latest_file.stem
        last_activity = None
        conversation_count = 0
        tail_events: list[dict] = []

        try:
            with open(latest_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    usage = None
                    if "message" in obj and isinstance(obj["message"], dict):
                        usage = obj["message"].get("usage")
                        m = obj["message"].get("model")
                        if m:
                            model = _normalize_model(m)
                    elif "usage" in obj:
                        usage = obj["usage"]
                        m = obj.get("model")
                        if m:
                            model = _normalize_model(m)

                    if usage and isinstance(usage, dict):
                        parsed_usage = _parse_usage(usage)
                        total_input += parsed_usage.input_tokens
                        total_output += parsed_usage.output_tokens
                        total_cache_read += parsed_usage.cached_input_tokens
                        total_cache_write += parsed_usage.reasoning_output_tokens
                        conversation_count += 1

                    ts_str = obj.get("timestamp")
                    if ts_str:
                        ts = _parse_timestamp(ts_str)
                        if ts and (last_activity is None or ts > last_activity):
                            last_activity = ts

                    cwd = obj.get("cwd")
                    if cwd:
                        project_path = str(cwd)
                    sid = obj.get("sessionId")
                    if sid:
                        session_id = str(sid)

                    # Keep tail events for state analysis
                    tail_events.append(obj)
                    if len(tail_events) > _TAIL_EVENT_LIMIT:
                        tail_events.pop(0)

        except (OSError, IOError) as e:
            logger.warning(f"Failed to read session file {latest_file}: {e}")

        total_tokens = total_input + total_output + total_cache_read + total_cache_write
        usage = TokenUsage(
            input_tokens=total_input,
            output_tokens=total_output,
            cached_input_tokens=total_cache_read,
            reasoning_output_tokens=total_cache_write,
            total_tokens=total_tokens,
            conversation_count=conversation_count,
        )

        if not project_path:
            try:
                rel = latest_file.relative_to(self._projects_dir)
                parts = rel.parts
                if len(parts) >= 1:
                    project_path = parts[0]
            except (ValueError, IndexError):
                pass

        # Determine fine-grained state from tail events
        session_state = _analyze_tail_events(tail_events)

        # Apply time-based fallback: if event analysis says WORKING but
        # last activity is older than active_threshold, demote to DONE/IDLE
        if session_state.status == StatusLevel.WORKING and last_activity:
            now = datetime.utcnow()
            if last_activity.tzinfo is not None:
                last_activity_check = last_activity.replace(tzinfo=None)
            else:
                last_activity_check = last_activity
            time_since = now - last_activity_check
            if time_since > self._active_threshold:
                # Event says working but it's been a while — likely stale
                if time_since <= self._idle_threshold:
                    session_state = _SessionState(
                        status=StatusLevel.DONE,
                        detail=session_state.detail,
                        last_tool_name=session_state.last_tool_name,
                        stop_reason=session_state.stop_reason,
                    )
                else:
                    session_state = _SessionState(status=StatusLevel.IDLE)

        return usage, model, project_path, session_id, last_activity, session_state

    def _parse_latest_session(
        self,
    ) -> tuple[TokenUsage, str, str, str, Optional[datetime], _SessionState]:
        """Parse the most recent session file for token usage.

        Returns:
            Tuple of (total_usage, model, project_path, session_id, last_activity, session_state).
        """
        session_files = self._find_session_files()
        if not session_files:
            return TokenUsage(), "unknown", "", "", None, _SessionState()

        # Parse the most recent file
        latest_file = session_files[0]
        return self._parse_session_file(latest_file)

    def _determine_status(
        self,
        last_activity: Optional[datetime],
        has_usage: bool,
        session_state: Optional[_SessionState] = None,
    ) -> tuple[StatusLevel, str]:
        """Determine agent status based on activity and event analysis.

        Args:
            last_activity: Timestamp of last activity.
            has_usage: Whether any usage data exists.
            session_state: Fine-grained state from tail-event analysis.

        Returns:
            Tuple of (StatusLevel, detail string).
        """
        # Use event-aware state when available
        if session_state and session_state.status != StatusLevel.IDLE:
            return session_state.status, session_state.detail

        # Fallback to time-based logic
        if last_activity is None:
            return StatusLevel.IDLE, ""

        now = datetime.utcnow()
        # Make last_activity offset-naive for comparison if needed
        if last_activity.tzinfo is not None:
            last_activity = last_activity.replace(tzinfo=None)

        time_since = now - last_activity
        if time_since <= self._active_threshold:
            return StatusLevel.WORKING, ""

        if has_usage or time_since <= self._idle_threshold:
            return StatusLevel.DONE, ""

        return StatusLevel.IDLE, ""

    async def poll_status(self) -> AgentStatus:
        """Poll current Claude Code status.

        Returns:
            Current AgentStatus.
        """
        usage, model, project_path, session_id, last_activity, session_state = (
            self._parse_latest_session()
        )
        has_usage = usage.total_tokens > 0
        status, detail = self._determine_status(last_activity, has_usage, session_state)

        agent_status = AgentStatus(
            agent_type=AgentType.CLAUDE_CODE,
            status=status,
            model=model,
            project_path=project_path,
            session_id=session_id,
            last_activity=last_activity,
            tokens=usage,
            message=detail,
            detail=detail,
        )
        self._last_status = agent_status
        return agent_status

    async def poll_sessions(self) -> list[AgentStatus]:
        """Poll recent Claude Code sessions."""
        statuses: list[AgentStatus] = []
        for session_file in self._find_session_files()[: self._max_sessions]:
            usage, model, project_path, session_id, last_activity, session_state = (
                self._parse_session_file(session_file)
            )
            has_usage = usage.total_tokens > 0
            status, detail = self._determine_status(last_activity, has_usage, session_state)
            statuses.append(
                AgentStatus(
                    agent_type=AgentType.CLAUDE_CODE,
                    status=status,
                    model=model,
                    project_path=project_path,
                    session_id=session_id,
                    last_activity=last_activity,
                    tokens=usage,
                    message=detail,
                    detail=detail,
                )
            )
        return statuses

    async def poll_quota(self) -> Optional[QuotaInfo]:
        """Poll Claude Code quota.

        Claude Code with DeepSeek/Mimo uses API keys, not subscription quotas.
        Returns None for now - quota tracking is done via token counting.
        """
        return None

    async def start_watching(self, callback: Callable[[AgentStatus], None]) -> None:
        """Start watching for session file changes.

        Args:
            callback: Function to call when status changes.
        """
        if self._observer:
            return

        def on_change():
            """Handle file change event."""
            try:
                # poll_status is synchronous despite being marked async
                usage, model, project_path, session_id, last_activity, session_state = (
                    self._parse_latest_session()
                )
                has_usage = usage.total_tokens > 0
                status_level, detail = self._determine_status(
                    last_activity, has_usage, session_state
                )

                status = AgentStatus(
                    agent_type=AgentType.CLAUDE_CODE,
                    status=status_level,
                    model=model,
                    project_path=project_path,
                    session_id=session_id,
                    last_activity=last_activity,
                    tokens=usage,
                    message=detail,
                    detail=detail,
                )
                self._last_status = status
                callback(status)
            except Exception as e:
                logger.error(f"Error in Claude Code watcher callback: {e}")

        self._observer = Observer()
        handler = _SessionFileHandler(on_change)

        # Watch the projects directory
        if self._projects_dir.exists():
            self._observer.schedule(handler, str(self._projects_dir), recursive=True)
            self._observer.start()
            logger.info(f"Started watching {self._projects_dir} for Claude Code sessions")
        else:
            logger.warning(f"Claude projects directory not found: {self._projects_dir}")

    async def stop_watching(self) -> None:
        """Stop watching for session file changes."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            logger.info("Stopped Claude Code watcher")
