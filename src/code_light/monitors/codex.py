"""Codex session monitor.

Monitors OpenAI Codex by:
- Reading auth from ~/.codex/auth.json
- Calling usage API for rate limit data
- Tracking session rollout files with fine-grained tail-event analysis
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import httpx
import jwt

from ..models import AgentStatus, AgentType, QuotaInfo, StatusLevel, TokenUsage
from ..utils.logger import logger
from .base import BaseMonitor

# Maximum number of tail events to keep for status analysis
_TAIL_EVENT_LIMIT = 30

# Codex usage API endpoint
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_REFRESH_BUFFER_SECONDS = 300

# Plan label mapping
PLAN_LABELS = {
    "prolite": "Pro Lite",
    "pro": "Pro",
    "plus": "Plus",
    "business": "Business",
    "self_serve_business_usage_based": "Business Usage Based",
    "enterprise": "Enterprise",
}


def _decode_jwt(token: str) -> dict:
    """Decode JWT without verification (for reading claims).

    Args:
        token: JWT token string.

    Returns:
        Decoded payload dict.
    """
    try:
        # Decode without verification - we just need the claims
        payload = jwt.decode(token, options={"verify_signature": False})
        return payload
    except jwt.InvalidTokenError as e:
        logger.warning(f"Failed to decode JWT: {e}")
        return {}


def _extract_plan_type(id_token: str) -> str:
    """Extract plan type from JWT id_token.

    Args:
        id_token: JWT id_token from Codex auth.

    Returns:
        Plan type string.
    """
    if not id_token:
        return "unknown"
    payload = _decode_jwt(id_token)
    auth_claims = payload.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        auth_claims = {}
    plan = payload.get("chatgpt_plan_type") or auth_claims.get(
        "chatgpt_plan_type",
        "unknown",
    )
    return PLAN_LABELS.get(plan, plan)


def _parse_timestamp(ts_str: str) -> Optional[datetime]:
    """Parse a Codex JSONL timestamp."""
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        return ts
    except (ValueError, TypeError):
        return None


def _parse_reset_at(value) -> Optional[datetime]:
    """Parse Codex quota reset values from ISO strings or unix seconds."""
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.utcfromtimestamp(value)
        if isinstance(value, str) and value.isdigit():
            return datetime.utcfromtimestamp(int(value))
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        return ts
    except (OSError, ValueError, TypeError):
        return None


def _extract_usage(payload: dict) -> dict:
    """Return a token usage dict from common Codex rollout payload shapes."""
    candidates = [
        payload.get("usage"),
        payload.get("token_usage"),
    ]
    response = payload.get("response")
    if isinstance(response, dict):
        candidates.append(response.get("usage"))

    for candidate in candidates:
        if isinstance(candidate, dict):
            return candidate
    return {}


def _extract_token_count_usage(payload: dict) -> dict:
    """Return the cumulative token usage from Codex token_count events."""
    if payload.get("type") != "token_count":
        return {}

    info = payload.get("info")
    if not isinstance(info, dict):
        return {}

    usage = info.get("total_token_usage")
    return usage if isinstance(usage, dict) else {}


def _usage_int(usage: dict, key: str) -> int:
    """Read an integer token field from a usage payload."""
    try:
        return int(usage.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _to_float(value, default: float = 0.0) -> float:
    """Convert API number-ish values to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _read_auth(auth_path: Path) -> Optional[dict]:
    """Read Codex auth file.

    Args:
        auth_path: Path to ~/.codex/auth.json.

    Returns:
        Auth dict or None.
    """
    if not auth_path.exists():
        return None
    try:
        with open(auth_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read Codex auth: {e}")
        return None


@dataclass
class _SessionState:
    """Fine-grained state derived from tail-event analysis."""

    status: StatusLevel = StatusLevel.IDLE
    detail: str = ""           # human-readable: "Running shell command", "Editing file"
    last_tool_name: str = ""   # last tool invoked
    has_tool_error: bool = False


def _has_call_output_after(
    events: list[dict], start_idx: int, call_ids: set[str]
) -> bool:
    """Check if any call_id has a corresponding output event after start_idx.

    Args:
        events: Full list of events.
        start_idx: Index to start searching from.
        call_ids: Set of call_id strings to look for.

    Returns:
        True if at least one output was found for the given IDs.
    """
    if not call_ids:
        return False

    for i in range(start_idx, len(events)):
        obj = events[i]
        otype = obj.get("type", "")
        if otype != "response_item":
            continue
        payload = obj.get("payload", {})
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type", "")
        if ptype in ("function_call_output", "custom_tool_call_output"):
            if payload.get("call_id") in call_ids:
                return True
    return False


def _is_output_error(output_str: str) -> bool:
    """Check if a function_call_output indicates an error.

    Args:
        output_str: The output string from function_call_output.

    Returns:
        True if the output indicates an error.
    """
    if not output_str:
        return False
    # Check for non-zero exit code
    if output_str.startswith("Exit code:"):
        try:
            first_line = output_str.split("\n", 1)[0]
            code = int(first_line.split(":", 1)[1].strip())
            if code != 0:
                return True
        except (ValueError, IndexError):
            pass
    # Check for common error indicators
    error_patterns = ("Traceback", "Error:", "Exception:", "FAILED", "fatal:")
    for pattern in error_patterns:
        if pattern in output_str[:500]:
            return True
    return False


def _extract_command_from_call(payload: dict) -> str:
    """Extract shell command from a function_call payload.

    Args:
        payload: The function_call payload dict.

    Returns:
        Command string, or empty if not parseable.
    """
    args_str = payload.get("arguments", "")
    if not args_str:
        return ""
    try:
        args = json.loads(args_str)
        cmd = args.get("command", "")
        if cmd:
            # Truncate long commands for display
            return cmd[:80] if len(cmd) > 80 else cmd
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


def _analyze_codex_tail_events(events: list[dict]) -> _SessionState:
    """Analyze the last few Codex events to determine fine-grained status.

    Codex has explicit lifecycle events (task_started, task_complete,
    turn_aborted) which make status detection more reliable than Claude Code.

    Args:
        events: List of parsed JSONL dicts (chronological order).

    Returns:
        _SessionState with status and detail.
    """
    if not events:
        return _SessionState(status=StatusLevel.IDLE)

    # Walk backwards through events
    for idx, obj in enumerate(reversed(events)):
        otype = obj.get("type", "")
        payload = obj.get("payload", {})

        # ── event_msg: high-level lifecycle events ──────────────
        if otype == "event_msg" and isinstance(payload, dict):
            ptype = payload.get("type", "")

            if ptype == "task_complete":
                return _SessionState(
                    status=StatusLevel.DONE,
                    detail="Task complete",
                )

            if ptype == "turn_aborted":
                reason = payload.get("reason", "interrupted")
                return _SessionState(
                    status=StatusLevel.ERROR,
                    detail=f"Aborted: {reason}",
                    has_tool_error=True,
                )

            if ptype == "task_started":
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail="Task started",
                )

            if ptype == "agent_message":
                phase = payload.get("phase", "")
                if phase == "final_answer":
                    return _SessionState(
                        status=StatusLevel.DONE,
                        detail="Final answer",
                    )
                if phase == "commentary":
                    return _SessionState(
                        status=StatusLevel.WORKING,
                        detail="Generating",
                    )

            if ptype == "user_message":
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail="Processing new prompt",
                )

            # token_count, thread_rolled_back, patch_apply_end are not
            # state-significant on their own; continue walking backwards

        # ── response_item: model output events ──────────────────
        if otype == "response_item" and isinstance(payload, dict):
            ptype = payload.get("type", "")

            if ptype == "reasoning":
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail="Thinking",
                )

            if ptype == "function_call":
                cmd = _extract_command_from_call(payload)
                # Check if this call has been resolved
                forward_idx = len(events) - 1 - idx
                call_id = payload.get("call_id", "")
                resolved = _has_call_output_after(
                    events, forward_idx + 1, {call_id} if call_id else set()
                )
                if not resolved:
                    # Check if command requires approval (elevated permissions)
                    needs_approval = False
                    try:
                        args = json.loads(payload.get("arguments", "{}"))
                        if args.get("sandbox_permissions") == "require_escalated":
                            needs_approval = True
                    except (json.JSONDecodeError, TypeError):
                        pass

                    if needs_approval:
                        detail = f"Waiting: {cmd}" if cmd else "Waiting for approval"
                        return _SessionState(
                            status=StatusLevel.WAITING,
                            detail=detail,
                            last_tool_name="shell_command",
                        )

                    detail = f"Running: {cmd}" if cmd else "Running command"
                    return _SessionState(
                        status=StatusLevel.WORKING,
                        detail=detail,
                        last_tool_name="shell_command",
                    )
                # Resolved — check if it errored
                # Don't return here; continue walking to find a more
                # recent state-significant event

            if ptype == "function_call_output":
                output = payload.get("output", "")
                if _is_output_error(output):
                    return _SessionState(
                        status=StatusLevel.ERROR,
                        detail="Command failed",
                        has_tool_error=True,
                        last_tool_name="shell_command",
                    )
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail="Command completed",
                    last_tool_name="shell_command",
                )

            if ptype == "custom_tool_call":
                tool_name = payload.get("name", "apply_patch")
                forward_idx = len(events) - 1 - idx
                call_id = payload.get("call_id", "")
                resolved = _has_call_output_after(
                    events, forward_idx + 1, {call_id} if call_id else set()
                )
                if not resolved:
                    return _SessionState(
                        status=StatusLevel.WORKING,
                        detail=f"Editing file ({tool_name})",
                        last_tool_name=tool_name,
                    )

            if ptype == "custom_tool_call_output":
                output = payload.get("output", "")
                if _is_output_error(output):
                    return _SessionState(
                        status=StatusLevel.ERROR,
                        detail="Patch failed",
                        has_tool_error=True,
                        last_tool_name="apply_patch",
                    )
                return _SessionState(
                    status=StatusLevel.WORKING,
                    detail="Patch applied",
                    last_tool_name="apply_patch",
                )

            # assistant message with final_answer
            if ptype == "message":
                role = payload.get("role", "")
                phase = payload.get("phase", "")
                if role == "assistant" and phase == "final_answer":
                    return _SessionState(
                        status=StatusLevel.DONE,
                        detail="Final answer",
                    )
                if role == "assistant" and phase == "commentary":
                    return _SessionState(
                        status=StatusLevel.WORKING,
                        detail="Generating",
                    )

    # No state-significant event found
    return _SessionState(status=StatusLevel.IDLE)


class CodexMonitor(BaseMonitor):
    """Monitor for OpenAI Codex sessions."""

    def __init__(
        self,
        codex_home: Path,
        idle_threshold_seconds: int = 300,
        active_threshold_seconds: int = 15,
        max_sessions: int = 8,
    ) -> None:
        """Initialize Codex monitor.

        Args:
            codex_home: Path to ~/.codex directory.
            idle_threshold_seconds: Seconds of inactivity before considering idle.
            active_threshold_seconds: Seconds since last write to consider working.
            max_sessions: Maximum recent sessions to expose.
        """
        self._codex_home = codex_home
        self._auth_path = codex_home / "auth.json"
        self._sessions_dir = codex_home / "sessions"
        self._idle_threshold = timedelta(seconds=idle_threshold_seconds)
        self._active_threshold = timedelta(seconds=active_threshold_seconds)
        self._max_sessions = max_sessions
        self._last_quota: Optional[QuotaInfo] = None
        self._last_status: Optional[AgentStatus] = None
        self._observer = None
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._id_token: Optional[str] = None
        self._account_id: Optional[str] = None
        self._auth: Optional[dict] = None
        self._plan_type: str = "unknown"

    def _load_auth(self) -> bool:
        """Load authentication from Codex auth file.

        Returns:
            True if auth loaded successfully.
        """
        auth = _read_auth(self._auth_path)
        if not auth:
            logger.debug(f"Codex auth file not found: {self._auth_path}")
            return False

        tokens = auth.get("tokens")
        if not isinstance(tokens, dict):
            tokens = auth

        self._auth = auth
        self._access_token = tokens.get("access_token")
        self._refresh_token = tokens.get("refresh_token")
        self._id_token = tokens.get("id_token", "")
        self._account_id = tokens.get("account_id")
        self._plan_type = _extract_plan_type(self._id_token)

        logger.debug(f"Codex auth loaded: plan={self._plan_type}")
        return bool(self._access_token)

    def _access_token_expires_soon(self) -> bool:
        """Return True when the current access token should be refreshed."""
        if not self._access_token:
            return True
        payload = _decode_jwt(self._access_token)
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return exp < datetime.utcnow().timestamp() + TOKEN_REFRESH_BUFFER_SECONDS

    def _persist_tokens(self, token_data: dict) -> None:
        """Persist refreshed Codex tokens back to auth.json."""
        if not self._auth:
            return

        tokens = self._auth.setdefault("tokens", {})
        tokens["access_token"] = token_data.get("access_token", self._access_token)
        tokens["refresh_token"] = token_data.get("refresh_token", self._refresh_token)
        tokens["id_token"] = token_data.get("id_token", self._id_token)
        self._auth["last_refresh"] = datetime.utcnow().isoformat() + "Z"

        with open(self._auth_path, "w", encoding="utf-8") as f:
            json.dump(self._auth, f, indent=2)

        self._access_token = tokens.get("access_token")
        self._refresh_token = tokens.get("refresh_token")
        self._id_token = tokens.get("id_token")
        self._plan_type = _extract_plan_type(self._id_token or "")

    async def _ensure_access_token(self) -> bool:
        """Load and refresh Codex auth when needed."""
        if not self._access_token and not self._load_auth():
            return False

        if not self._access_token:
            return False

        if not self._access_token_expires_soon():
            return True

        if not self._refresh_token:
            logger.warning("Codex refresh token missing")
            return True

        body = {
            "client_id": CODEX_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "scope": "openid profile email",
        }

        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                response = await client.post(
                    CODEX_TOKEN_URL,
                    headers={"Content-Type": "application/json"},
                    json=body,
                )
            if response.status_code != 200:
                logger.warning(f"Codex token refresh returned {response.status_code}")
                return True

            data = response.json()
            if data.get("access_token"):
                self._persist_tokens(data)
            return bool(self._access_token)
        except (httpx.HTTPError, OSError, json.JSONDecodeError) as e:
            logger.warning(f"Failed to refresh Codex token: {e}")
            return True

    def _find_rollout_files(self) -> list[Path]:
        """Find Codex rollout JSONL files.

        Returns:
            List of rollout file paths, sorted by modification time.
        """
        if not self._sessions_dir.exists():
            logger.debug(f"Codex sessions directory not found: {self._sessions_dir}")
            return []

        rollout_files = []
        for jsonl_file in self._sessions_dir.rglob("rollout-*.jsonl"):
            if jsonl_file.is_file():
                rollout_files.append(jsonl_file)

        rollout_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        return rollout_files

    def _parse_session_file(
        self,
        latest_file: Path,
    ) -> tuple[TokenUsage, Optional[datetime], str, str, _SessionState]:
        """Parse one Codex rollout file.

        Returns:
            Tuple of (usage, last_activity, session_id, project_path, session_state).
        """
        total_input = 0
        total_output = 0
        total_cached = 0
        total_reasoning = 0
        total_tokens = 0
        latest_token_count_usage: dict | None = None
        last_activity = None
        session_id = latest_file.stem.removeprefix("rollout-")
        project_path = ""
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

                    # Extract usage from various formats
                    payload = obj.get("payload")
                    if not isinstance(payload, dict):
                        payload = {}

                    token_count_usage = _extract_token_count_usage(payload)
                    if token_count_usage:
                        latest_token_count_usage = token_count_usage
                        conversation_count += 1
                    else:
                        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
                        if not usage:
                            usage = _extract_usage(payload)
                        if isinstance(usage, dict) and usage:
                            usage_input = _usage_int(usage, "input_tokens")
                            usage_output = _usage_int(usage, "output_tokens")
                            usage_cached = _usage_int(usage, "cached_input_tokens")
                            usage_reasoning = _usage_int(usage, "reasoning_output_tokens")

                            total_input += usage_input
                            total_output += usage_output
                            total_cached += usage_cached
                            total_reasoning += usage_reasoning
                            total_tokens += _usage_int(usage, "total_tokens") or (
                                usage_input + usage_output + usage_cached + usage_reasoning
                            )
                            conversation_count += 1

                    if obj.get("type") == "session_meta":
                        sid = payload.get("id")
                        if sid:
                            session_id = str(sid)
                        cwd = payload.get("cwd")
                        if cwd:
                            project_path = str(cwd)

                    # Track timestamp
                    ts_str = obj.get("timestamp") or obj.get("created_at")
                    if ts_str:
                        ts = _parse_timestamp(ts_str)
                        if ts and (last_activity is None or ts > last_activity):
                            last_activity = ts

                    # Keep tail events for state analysis
                    tail_events.append(obj)
                    if len(tail_events) > _TAIL_EVENT_LIMIT:
                        tail_events.pop(0)

        except (OSError, IOError) as e:
            logger.warning(f"Failed to read rollout file {latest_file}: {e}")

        if latest_token_count_usage:
            total_input = _usage_int(latest_token_count_usage, "input_tokens")
            total_output = _usage_int(latest_token_count_usage, "output_tokens")
            total_cached = _usage_int(latest_token_count_usage, "cached_input_tokens")
            total_reasoning = _usage_int(
                latest_token_count_usage,
                "reasoning_output_tokens",
            )
            total_tokens = _usage_int(latest_token_count_usage, "total_tokens") or (
                total_input + total_output + total_cached + total_reasoning
            )

        usage = TokenUsage(
            input_tokens=total_input,
            output_tokens=total_output,
            cached_input_tokens=total_cached,
            reasoning_output_tokens=total_reasoning,
            total_tokens=total_tokens,
            conversation_count=conversation_count,
        )

        # Determine fine-grained state from tail events
        session_state = _analyze_codex_tail_events(tail_events)

        # Apply time-based adjustments:
        # 1. If events say WORKING but activity is stale → demote to DONE/IDLE
        # 2. If events say IDLE but file was recently modified → promote to WORKING
        if last_activity:
            now = datetime.utcnow()
            if last_activity.tzinfo is not None:
                last_activity_check = last_activity.replace(tzinfo=None)
            else:
                last_activity_check = last_activity
            time_since = now - last_activity_check

            if session_state.status == StatusLevel.WORKING:
                if time_since > self._active_threshold:
                    if time_since <= self._idle_threshold:
                        session_state = _SessionState(
                            status=StatusLevel.DONE,
                            detail=session_state.detail,
                            last_tool_name=session_state.last_tool_name,
                        )
                    else:
                        session_state = _SessionState(status=StatusLevel.IDLE)
            elif session_state.status == StatusLevel.IDLE:
                if time_since <= self._active_threshold:
                    session_state = _SessionState(
                        status=StatusLevel.WORKING,
                        detail="Active",
                    )
                elif time_since <= self._idle_threshold:
                    session_state = _SessionState(
                        status=StatusLevel.DONE,
                        detail="",
                    )

        return usage, last_activity, session_id, project_path, session_state

    def _parse_latest_session(
        self,
    ) -> tuple[TokenUsage, Optional[datetime], str, str, _SessionState]:
        """Parse the most recent rollout file.

        Returns:
            Tuple of (total_usage, last_activity, session_id, project_path, session_state).
        """
        rollout_files = self._find_rollout_files()
        if not rollout_files:
            return TokenUsage(), None, "", "", _SessionState()

        return self._parse_session_file(rollout_files[0])

    async def _fetch_usage_api(self) -> Optional[QuotaInfo]:
        """Fetch quota data from Codex usage API.

        Returns:
            QuotaInfo or None if request fails.
        """
        if not await self._ensure_access_token():
            return None

        try:
            headers = {
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type": "application/json",
            }
            if self._account_id:
                headers["chatgpt-account-id"] = self._account_id

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    CODEX_USAGE_URL,
                    headers=headers,
                )

                if response.status_code != 200:
                    logger.warning(f"Codex API returned {response.status_code}")
                    return None

                data = response.json()
                return self._parse_usage_response(data)

        except httpx.HTTPError as e:
            logger.warning(f"Failed to fetch Codex usage: {e}")
            return None

    def _parse_usage_response(self, data: dict) -> QuotaInfo:
        """Parse Codex usage API response.

        Args:
            data: API response dict.

        Returns:
            QuotaInfo instance.
        """
        rate_limit = data.get("rate_limit") or {}
        primary = rate_limit.get("primary_window") or {}
        secondary = rate_limit.get("secondary_window") or {}
        review_limit = data.get("code_review_rate_limit") or {}
        review_primary = review_limit.get("primary_window") or {}
        additional = data.get("additional_rate_limits") or []

        # Primary window (main rate limit)
        used_percent = _to_float(primary.get("used_percent"))
        limit_seconds = primary.get("limit_window_seconds", 0)
        reset_at_str = primary.get("reset_at")

        reset_at = _parse_reset_at(reset_at_str)

        remaining_seconds = 0
        if reset_at:
            delta = reset_at - datetime.utcnow()
            remaining_seconds = max(0, int(delta.total_seconds()))

        # Credits
        credits_data = data.get("credits", {})
        credits_balance = _to_float(credits_data.get("balance"))

        return QuotaInfo(
            agent_type=AgentType.CODEX,
            plan_name=PLAN_LABELS.get(data.get("plan_type"), self._plan_type),
            used_percent=used_percent,
            limit_window_seconds=limit_seconds,
            reset_at=reset_at,
            remaining_seconds=remaining_seconds,
            credits_balance=credits_balance,
            extra_info={
                "primary_window": primary,
                "secondary_window": secondary,
                "code_review_primary_window": review_primary,
                "additional_rate_limits": additional,
                "credits": credits_data,
            },
        )

    async def poll_status(self) -> AgentStatus:
        """Poll current Codex status.

        Returns:
            Current AgentStatus.
        """
        usage, last_activity, session_id, project_path, session_state = (
            self._parse_latest_session()
        )

        # Use event-aware status, but quota warning overrides
        status = session_state.status
        detail = session_state.detail

        # Check quota status
        if self._last_quota and self._last_quota.used_percent >= 95:
            status = StatusLevel.QUOTA_WARNING

        agent_status = AgentStatus(
            agent_type=AgentType.CODEX,
            status=status,
            model="codex",
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
        """Poll recent Codex sessions."""
        statuses: list[AgentStatus] = []
        for session_file in self._find_rollout_files()[: self._max_sessions]:
            usage, last_activity, session_id, project_path, session_state = (
                self._parse_session_file(session_file)
            )
            statuses.append(
                AgentStatus(
                    agent_type=AgentType.CODEX,
                    status=session_state.status,
                    model="codex",
                    project_path=project_path,
                    session_id=session_id,
                    last_activity=last_activity,
                    tokens=usage,
                    message=session_state.detail,
                    detail=session_state.detail,
                )
            )
        return statuses

    async def poll_quota(self) -> Optional[QuotaInfo]:
        """Poll Codex quota from API.

        Returns:
            QuotaInfo or None.
        """
        quota = await self._fetch_usage_api()
        if quota:
            self._last_quota = quota
        return quota

    async def start_watching(self, callback: Callable[[AgentStatus], None]) -> None:
        """Start watching for Codex session file changes.

        Args:
            callback: Function to call when status changes.
        """
        if self._observer:
            return

        def on_change() -> None:
            """Handle file change event."""
            try:
                usage, last_activity, session_id, project_path, session_state = (
                    self._parse_latest_session()
                )
                status = session_state.status
                detail = session_state.detail

                # Quota warning overrides
                if self._last_quota and self._last_quota.used_percent >= 95:
                    status = StatusLevel.QUOTA_WARNING

                agent_status = AgentStatus(
                    agent_type=AgentType.CODEX,
                    status=status,
                    model="codex",
                    project_path=project_path,
                    session_id=session_id,
                    last_activity=last_activity,
                    tokens=usage,
                    message=detail,
                    detail=detail,
                )
                self._last_status = agent_status
                callback(agent_status)
            except Exception as e:
                logger.error(f"Error in Codex watcher callback: {e}")

        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        class _RolloutFileHandler(FileSystemEventHandler):
            """Watchdog handler for Codex rollout file changes."""

            def __init__(self, cb: Callable[[], None]) -> None:
                super().__init__()
                self._cb = cb

            def on_modified(self, event) -> None:
                if event.is_directory:
                    return
                if "rollout-" in event.src_path and event.src_path.endswith(".jsonl"):
                    self._cb()

            def on_created(self, event) -> None:
                if event.is_directory:
                    return
                if "rollout-" in event.src_path and event.src_path.endswith(".jsonl"):
                    self._cb()

        self._observer = Observer()
        handler = _RolloutFileHandler(on_change)

        if self._sessions_dir.exists():
            self._observer.schedule(handler, str(self._sessions_dir), recursive=True)
            self._observer.start()
            logger.info(f"Started watching {self._sessions_dir} for Codex sessions")
        else:
            logger.warning(f"Codex sessions directory not found: {self._sessions_dir}")

    async def stop_watching(self) -> None:
        """Stop watching for Codex session file changes."""
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            logger.info("Stopped Codex watcher")
