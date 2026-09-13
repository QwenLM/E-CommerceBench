"""Runtime-neutral interface for driving E-Commerce Bench with an external agent."""

from __future__ import annotations

from copy import deepcopy

import json
import os
import threading
import uuid
from typing import Any, Dict, List, Optional

from tools import ECOMMERCE_TOOL_SCHEMAS

from .ecommerce_agent import setup_npc_env
from .ecommerce_tool_manager import EcommerceToolManager
from .job import build_ecommerce_job, normalize_termination_reason


class ExternalRuntimeError(ValueError):
    """An invalid external-runtime request."""


class SessionStateError(ExternalRuntimeError):
    """An operation that conflicts with the episode lifecycle."""


class ExternalAgentSession:
    """One benchmark episode controlled by externally supplied tool calls.

    The session deliberately delegates all action execution, time advancement,
    notifications, logging, and scoring to ``EcommerceToolManager``. External
    runtimes own only policy: they receive the canonical task and tools, then
    submit the tool calls they choose.
    """

    def __init__(
        self,
        *,
        max_turns: int = 4000,
        max_day: int = 365,
        initial_balance: float = 100000.0,
        daily_fee: float = 50.0,
        log_dir: Optional[str] = None,
        run_index: int = 0,
    ) -> None:
        if max_turns <= 0 or max_day <= 0 or initial_balance <= 0:
            raise ExternalRuntimeError(
                "max_turns, max_day, and initial_balance must be positive"
            )
        if daily_fee < 0:
            raise ExternalRuntimeError("daily_fee must be non-negative")

        previous_log_dir = os.environ.get("ECOMMERCE_BENCH_LOG_DIR")
        if log_dir:
            os.environ["ECOMMERCE_BENCH_LOG_DIR"] = log_dir
        try:
            # Supplier NPC configuration is benchmark-owned even when the policy
            # agent runs out of process.
            setup_npc_env()
            self.job = build_ecommerce_job(
                max_turns=max_turns,
                max_day=max_day,
                initial_balance=initial_balance,
                daily_fee=daily_fee,
                tool_schemas=ECOMMERCE_TOOL_SCHEMAS,
                run_index=run_index,
            )
            self.tool_manager = EcommerceToolManager.init(self.job)
        finally:
            if log_dir:
                if previous_log_dir is None:
                    os.environ.pop("ECOMMERCE_BENCH_LOG_DIR", None)
                else:
                    os.environ["ECOMMERCE_BENCH_LOG_DIR"] = previous_log_dir
        self.max_turns = max_turns
        self.turn = 0
        self._finished = False
        self._lock = threading.Lock()

    def descriptor(self) -> Dict[str, Any]:
        """Return the exact initial messages and tool schemas for the policy."""
        agent_info = self.job["agent_info"]
        return {
            "messages": deepcopy(self.job["messages"]),
            "tools": deepcopy(self.job["tool_schemas"]),
            "allow_parallel_tool_calls": agent_info["allow_parallel_tool_calls"],
            "max_turns": self.max_turns,
            "config": {
                key: agent_info[key]
                for key in (
                    "max_day",
                    "initial_balance",
                    "daily_fee",
                    "run_index",
                    "max_tool_response_chars",
                )
            },
        }

    def act(
        self,
        tool_calls: List[Dict[str, Any]],
        *,
        content: str = "",
        reasoning_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute one external-agent turn through the canonical tool manager."""
        with self._lock:
            if self._finished:
                raise SessionStateError("episode has already terminated")
            if not isinstance(tool_calls, list) or not tool_calls:
                raise ExternalRuntimeError("tool_calls must be a non-empty list")
            if self.turn >= self.max_turns:
                raise SessionStateError("maximum agent turns reached")
            if not isinstance(content, str):
                raise ExternalRuntimeError("content must be a string")
            if reasoning_content is not None and not isinstance(
                reasoning_content, str
            ):
                raise ExternalRuntimeError("reasoning_content must be a string")

            normalized = self._normalize_tool_calls(tool_calls)
            assistant_message = {
                "role": "assistant",
                "content": content,
                "tool_calls": [
                    {
                        "id": call["tool_call_id"],
                        "type": "function",
                        "function": {
                            "name": call["tool_name"],
                            "arguments": json.dumps(
                                call["tool_args"], ensure_ascii=False
                            ),
                        },
                    }
                    for call in normalized
                ],
            }
            if reasoning_content is not None:
                assistant_message["reasoning_content"] = reasoning_content
            self.job["traj"].append(assistant_message)
            self.tool_manager._append_message_log(assistant_message)

            responses = self.tool_manager.ask_code_exec(self.job, normalized)
            max_chars = self.job["agent_info"]["max_tool_response_chars"]
            response_items = []
            for call, content in zip(normalized, responses):
                if len(content) > max_chars:
                    content = content[:max_chars] + "\n... [truncated]"
                tool_message = {
                    "role": "tool",
                    "content": content,
                    "tool_call_id": call["tool_call_id"],
                }
                self.job["traj"].append(tool_message)
                self.tool_manager._append_message_log(tool_message)
                response_items.append(
                    {
                        "tool_call_id": call["tool_call_id"],
                        "content": content,
                    }
                )

            self.turn += 1
            if not self.job.get("termination_reason") and self.turn >= self.max_turns:
                self.job["termination_reason"] = "max_turns_reached"
            if self.job.get("termination_reason"):
                self._finalize()

            return {
                "tool_responses": response_items,
                "turn": self.turn,
                "done": self._finished,
                "termination_reason": self.job.get("termination_reason"),
                "termination_detail": self.job.get("termination_detail"),
            }

    def result(self) -> Dict[str, Any]:
        """Return benchmark results after termination."""
        with self._lock:
            if not self._finished:
                raise SessionStateError("episode is still running")
            return {
                "termination_reason": self.job.get("termination_reason"),
                "termination_detail": self.job.get("termination_detail"),
                "reward_meta": self.job.get("reward_meta", {}),
                "final_state": self.job.get("final_state"),
                "turns": self.turn,
            }

    def close(self) -> None:
        """Finalize an unfinished episode as externally terminated."""
        with self._lock:
            if self._finished:
                return
            self.job.setdefault("termination_reason", "external_runtime_closed")
            self._finalize()

    @staticmethod
    def _normalize_tool_calls(
        tool_calls: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        normalized = []
        ids = set()
        for call in tool_calls:
            if not isinstance(call, dict):
                raise ExternalRuntimeError("each tool call must be an object")
            name = call.get("name")
            if not isinstance(name, str) or not name:
                raise ExternalRuntimeError("each tool call requires a non-empty name")
            call_id = call.get("id") or str(uuid.uuid4())
            if not isinstance(call_id, str) or call_id in ids:
                raise ExternalRuntimeError("tool call ids must be unique strings")
            ids.add(call_id)
            arguments = call.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError as exc:
                    raise ExternalRuntimeError(
                        "tool call arguments must contain valid JSON"
                    ) from exc
            if not isinstance(arguments, dict):
                raise ExternalRuntimeError("tool call arguments must be an object")
            normalized.append(
                {
                    "tool_name": name,
                    "tool_args": arguments,
                    "tool_call_id": call_id,
                }
            )
        return normalized

    def _finalize(self) -> None:
        self.tool_manager.snapshot_final_state(self.job)
        normalize_termination_reason(self.job)
        self.tool_manager.cleanup(self.job)
        self.tool_manager.close()
        self._finished = True

