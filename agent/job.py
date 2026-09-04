"""Canonical benchmark job construction shared by all agent runtimes."""

from typing import Any, Dict, List, Optional

from .prompts import SYSTEM_PROMPT, USER_PROMPT


def build_ecommerce_job(
    *,
    max_turns: int = 4000,
    max_day: int = 365,
    initial_balance: float = 100000.0,
    daily_fee: float = 50.0,
    tool_schemas: List[Dict[str, Any]],
    system_prompt_suffix: str = "",
    run_index: int = 0,
    extra_agent_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the job consumed by :class:`EcommerceToolManager`.

    Agent runtimes may append runtime-specific instructions to the system prompt,
    but the benchmark task, tools, and environment configuration stay canonical.
    """
    user_prompt = (
        USER_PROMPT.replace("{daily_rent}", str(daily_fee))
        .replace("{max_days}", str(max_day))
        .replace("{max_token_capacity}", "runtime-managed")
        .replace("{initial_balance}", str(int(initial_balance)))
    )
    agent_info = {
        "task": "ecommerce_bench",
        "allow_parallel_tool_calls": True,
        "max_tool_response_chars": 64 * 1024,
        "max_turn": max_turns,
        "max_day": max_day,
        "initial_balance": initial_balance,
        "daily_fee": daily_fee,
        "run_index": run_index,
    }
    if extra_agent_info:
        agent_info.update(extra_agent_info)

    return {
        "task": "agent_multiturn/long_horizon/ecommerce_bench",
        "idx": 0,
        "agent_info": agent_info,
        "tool_schemas": tool_schemas,
        "messages": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT + system_prompt_suffix + user_prompt,
            },
            {"role": "user", "content": "You are running an e-commerce business."},
        ],
        "traj": [],
        "database": {},
        "data_source": "ecommerce_bench",
    }


def normalize_termination_reason(job: Dict[str, Any]) -> None:
    """Normalize detailed environment exits to the benchmark's terminal states."""
    detail = job.get("termination_reason") or "unknown"
    job["termination_detail"] = detail
    job["termination_reason"] = (
        "env_completed"
        if detail in ("env_completed", "max_days_reached")
        else "env_terminated"
    )
