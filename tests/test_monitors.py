import json
import asyncio
from pathlib import Path
from uuid import uuid4

from code_light.models import StatusLevel
from code_light.monitors.claude_code import (
    ClaudeCodeMonitor,
    _parse_usage,
    _analyze_tail_events,
    _SessionState,
)
from code_light.monitors.codex import CodexMonitor, _analyze_codex_tail_events


def _test_root(name: str) -> Path:
    root = Path(__file__).parent / ".tmp_monitor_tests" / f"{name}_{uuid4().hex}"
    root.mkdir(parents=True)
    return root


def test_claude_usage_accepts_vscode_field_names():
    usage = _parse_usage(
        {
            "input_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 30,
            "output_tokens": 40,
        }
    )

    assert usage.input_tokens == 10
    assert usage.reasoning_output_tokens == 20
    assert usage.cached_input_tokens == 30
    assert usage.output_tokens == 40
    assert usage.total_tokens == 100


def test_claude_monitor_reads_vscode_session_metadata():
    root = _test_root("claude")
    session_dir = root / ".claude" / "projects" / "D--repo"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "session-1.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "timestamp": "2026-05-30T09:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "model": "mimo-v2.5-pro",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 30,
                        "output_tokens": 40,
                    },
                },
                "cwd": "D:\\repo",
                "sessionId": "session-1",
                "entrypoint": "claude-vscode",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monitor = ClaudeCodeMonitor(root / ".claude")
    usage, model, project_path, session_id, last_activity, session_state = (
        monitor._parse_latest_session()
    )

    assert usage.total_tokens == 80
    assert model == "mimo"
    assert project_path == "D:\\repo"
    assert session_id == "session-1"
    assert last_activity is not None


def test_claude_monitor_exposes_multiple_recent_sessions():
    root = _test_root("claude_multi")
    session_dir = root / ".claude" / "projects" / "D--repo"
    session_dir.mkdir(parents=True)
    for idx in range(2):
        (session_dir / f"session-{idx}.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": f"2999-05-30T09:00:0{idx}.000Z",
                    "message": {
                        "role": "assistant",
                        "model": "mimo-v2.5-pro",
                        "usage": {"input_tokens": 10 + idx, "output_tokens": 20},
                    },
                    "cwd": f"D:\\repo-{idx}",
                    "sessionId": f"session-{idx}",
                    "entrypoint": "claude-vscode",
                }
            )
            + "\n",
            encoding="utf-8",
        )

    monitor = ClaudeCodeMonitor(root / ".claude")
    sessions = asyncio.run(monitor.poll_sessions())

    assert len(sessions) == 2
    assert {s.session_id for s in sessions} == {"session-0", "session-1"}
    assert all(s.status == StatusLevel.WORKING for s in sessions)


def test_codex_monitor_marks_recent_vscode_session_working():
    root = _test_root("codex")
    session_dir = root / ".codex" / "sessions" / "2026" / "05" / "30"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "rollout-2026-05-30T17-13-42-test.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "timestamp": "2999-05-30T09:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "codex-session",
                    "cwd": "D:\\repo",
                    "originator": "codex_vscode",
                    "source": "vscode",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monitor = CodexMonitor(root / ".codex")
    status = asyncio.run(monitor.poll_status())

    assert status.status == StatusLevel.WORKING
    assert status.session_id == "codex-session"
    assert status.project_path == "D:\\repo"


def test_codex_monitor_exposes_multiple_recent_sessions():
    root = _test_root("codex_multi")
    session_dir = root / ".codex" / "sessions" / "2026" / "05" / "30"
    session_dir.mkdir(parents=True)
    for idx in range(2):
        (session_dir / f"rollout-2026-05-30T17-13-4{idx}-test.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": f"2999-05-30T09:00:0{idx}.000Z",
                    "type": "session_meta",
                    "payload": {
                        "id": f"codex-session-{idx}",
                        "cwd": f"D:\\repo-{idx}",
                        "originator": "codex_vscode",
                        "source": "vscode",
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

    monitor = CodexMonitor(root / ".codex")
    sessions = asyncio.run(monitor.poll_sessions())

    assert len(sessions) == 2
    assert {s.session_id for s in sessions} == {"codex-session-0", "codex-session-1"}
    assert all(s.status == StatusLevel.WORKING for s in sessions)


# ──────────────────────────────────────────────────────────
# Fine-grained status detection tests
# ──────────────────────────────────────────────────────────


def test_status_waiting_on_unresolved_tool_use():
    """Assistant with stop_reason=tool_use and no tool_result should be WAITING."""
    events = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "mimo-v2.5-pro",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "text", "text": "Let me read that file."},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    },
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.WAITING
    assert "Read" in state.detail
    assert state.last_tool_name == "Read"
    assert state.is_waiting is True


def test_status_working_on_resolved_tool_use():
    """Assistant with tool_use followed by tool_result should be WORKING."""
    events = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "mimo-v2.5-pro",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/test.py"},
                    },
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": []}
                ],
            },
            "timestamp": "2026-05-31T12:00:01.000Z",
        },
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.WORKING
    assert "Tool result" in state.detail


def test_status_waiting_on_thinking_then_unresolved_tool_use():
    """Thinking + unresolved tool_use should be WAITING with thinking detail."""
    events = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "mimo-v2.5-pro",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "thinking", "thinking": "I should check..."},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.WAITING
    assert "Thinking" in state.detail
    assert "Waiting" in state.detail
    assert "Bash" in state.detail


def test_status_done_on_end_turn():
    """Assistant with stop_reason=end_turn should be DONE."""
    events = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "mimo-v2.5-pro",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done!"}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.DONE
    assert state.detail == "Response complete"


def test_status_done_on_stop_hook():
    """System event with stop_hook_summary should be DONE."""
    events = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "mimo-v2.5-pro",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "Done!"}],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        },
        {
            "type": "system",
            "subtype": "stop_hook_summary",
            "hookCount": 1,
            "timestamp": "2026-05-31T12:00:01.000Z",
        },
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.DONE
    assert state.detail == "Task complete"


def test_status_error_on_tool_error():
    """User event with is_error=true in tool_result should be ERROR."""
    events = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": "mimo-v2.5-pro",
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "Bash",
                        "input": {"command": "ls /nonexistent"},
                    },
                ],
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        },
        {
            "type": "user",
            "promptId": "prompt-1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_1",
                        "content": [
                            {"type": "text", "text": "ls: cannot access '/nonexistent': No such file or directory"}
                        ],
                        "is_error": True,
                    }
                ],
            },
            "toolUseResult": "Error: ls failed",
            "timestamp": "2026-05-31T12:00:01.000Z",
        },
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.ERROR
    assert state.has_tool_error is True
    assert "No such file" in state.detail


def test_status_working_on_user_prompt():
    """User event with promptId (new prompt) should be WORKING."""
    events = [
        {
            "type": "user",
            "promptId": "prompt-1",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Help me fix this bug"}],
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.WORKING
    assert "prompt" in state.detail.lower()


def test_status_working_on_queue_enqueue():
    """Queue-operation with enqueue should be WORKING."""
    events = [
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.WORKING


def test_status_done_on_stop_attachment():
    """Attachment with hookEvent=Stop should be DONE."""
    events = [
        {
            "type": "attachment",
            "attachment": {
                "type": "hook_success",
                "hookName": "Stop",
                "hookEvent": "Stop",
                "exitCode": 0,
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.DONE


def test_status_working_on_pretooluse_attachment():
    """Attachment with hookEvent=PreToolUse should be WORKING."""
    events = [
        {
            "type": "attachment",
            "attachment": {
                "type": "hook_success",
                "hookName": "PreToolUse:Edit",
                "hookEvent": "PreToolUse",
                "exitCode": 0,
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.WORKING
    assert "Edit" in state.detail


def test_status_error_on_hook_non_blocking_error():
    """Attachment with hook_non_blocking_error should be ERROR."""
    events = [
        {
            "type": "attachment",
            "attachment": {
                "type": "hook_non_blocking_error",
                "hookName": "security-guard",
                "hookEvent": "PreToolUse",
                "stderr": "Permission denied",
                "exitCode": 1,
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.ERROR
    assert "security-guard" in state.detail


def test_status_idle_on_empty_events():
    """Empty events should be IDLE."""
    state = _analyze_tail_events([])
    assert state.status == StatusLevel.IDLE


def test_status_working_then_tool_result_then_end_turn():
    """Full sequence: tool_use → tool_result → end_turn should reflect last event."""
    events = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "c1", "name": "Read", "input": {}},
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": []}
                ],
            },
            "timestamp": "2026-05-31T12:00:01.000Z",
        },
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "All done!"}],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
            "timestamp": "2026-05-31T12:00:02.000Z",
        },
    ]
    state = _analyze_tail_events(events)
    assert state.status == StatusLevel.DONE
    assert state.detail == "Response complete"


# ──────────────────────────────────────────────────────────
# Codex fine-grained status detection tests
# ──────────────────────────────────────────────────────────


def test_codex_status_done_on_task_complete():
    """event_msg with task_complete should be DONE."""
    events = [
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t1", "duration_ms": 5000},
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.DONE
    assert "complete" in state.detail.lower()


def test_codex_status_error_on_turn_aborted():
    """event_msg with turn_aborted should be ERROR."""
    events = [
        {
            "type": "event_msg",
            "payload": {"type": "turn_aborted", "reason": "interrupted"},
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.ERROR
    assert "interrupted" in state.detail


def test_codex_status_working_on_reasoning():
    """response_item with type=reaction should be WORKING (thinking)."""
    events = [
        {
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": [], "content": None},
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.WORKING
    assert "thinking" in state.detail.lower()


def test_codex_status_working_on_function_call():
    """Unresolved function_call should be WORKING."""
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call_1",
                "arguments": json.dumps({"command": "ls -la", "workdir": "/tmp"}),
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.WORKING
    assert "ls -la" in state.detail


def test_codex_status_working_on_function_call_output_success():
    """function_call_output with exit code 0 should be WORKING."""
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call_1",
                "arguments": json.dumps({"command": "echo hi"}),
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nhi\n",
            },
            "timestamp": "2026-05-31T12:00:01.000Z",
        },
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.WORKING
    assert "completed" in state.detail.lower()


def test_codex_status_error_on_function_call_output_failure():
    """function_call_output with non-zero exit code should be ERROR."""
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call_1",
                "arguments": json.dumps({"command": "ls /nonexistent"}),
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Exit code: 1\nWall time: 0.05 seconds\nOutput:\nls: cannot access '/nonexistent': No such file or directory\n",
            },
            "timestamp": "2026-05-31T12:00:01.000Z",
        },
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.ERROR
    assert state.has_tool_error is True


def test_codex_status_waiting_on_escalated_command():
    """Unresolved function_call with require_escalated should be WAITING."""
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call_1",
                "arguments": json.dumps({
                    "command": "rm -rf /tmp/dangerous",
                    "workdir": "/tmp",
                    "sandbox_permissions": "require_escalated",
                    "justification": "Need to clean up temp files",
                }),
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.WAITING
    assert "Waiting" in state.detail


def test_codex_status_working_on_custom_tool_call():
    """Unresolved custom_tool_call (apply_patch) should be WORKING."""
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_2",
                "name": "apply_patch",
                "input": "*** Begin Patch\n*** Update File: test.py\n@@\n-old\n+new\n*** End Patch\n",
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.WORKING
    assert "apply_patch" in state.last_tool_name


def test_codex_status_done_on_final_answer_message():
    """response_item message with phase=final_answer should be DONE."""
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "All done!"}],
                "phase": "final_answer",
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.DONE


def test_codex_status_working_on_commentary_message():
    """response_item message with phase=commentary should be WORKING."""
    events = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Let me check..."}],
                "phase": "commentary",
            },
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.WORKING


def test_codex_status_working_on_task_started():
    """event_msg with task_started should be WORKING."""
    events = [
        {
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "t1"},
            "timestamp": "2026-05-31T12:00:00.000Z",
        }
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.WORKING


def test_codex_status_idle_on_empty_events():
    """Empty events should be IDLE."""
    state = _analyze_codex_tail_events([])
    assert state.status == StatusLevel.IDLE


def test_codex_status_full_sequence():
    """Full sequence: reasoning → function_call → output → task_complete."""
    events = [
        {
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": [], "content": None},
            "timestamp": "2026-05-31T12:00:00.000Z",
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell_command",
                "call_id": "c1",
                "arguments": json.dumps({"command": "pytest"}),
            },
            "timestamp": "2026-05-31T12:00:01.000Z",
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "Exit code: 0\nOutput:\nAll tests passed\n",
            },
            "timestamp": "2026-05-31T12:00:05.000Z",
        },
        {
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "t1", "duration_ms": 5000},
            "timestamp": "2026-05-31T12:00:06.000Z",
        },
    ]
    state = _analyze_codex_tail_events(events)
    assert state.status == StatusLevel.DONE
