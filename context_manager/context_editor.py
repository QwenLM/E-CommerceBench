"""Standalone context editing for long-horizon agent tasks.

Ports the core clearing logic from verl's ContextEditorHook.preprocess_traj()
into a dependency-free function that operates on plain dicts.
"""

from typing import Dict, List, Tuple


def _has_tool_calls(msg: Dict) -> bool:
    """Check if an assistant message contains tool calls."""
    if msg.get("role") != "assistant":
        return False
    # Explicit tool_calls list (OpenAI chat format)
    if msg.get("tool_calls"):
        return True
    # tool_calls stored inside extra dict
    extra = msg.get("extra")
    if isinstance(extra, dict) and extra.get("tool_calls"):
        return True
    # Text-based tool call markers
    content = msg.get("content") or ""
    if "<tool_call>" in content:
        return True
    return False


def _is_tool_response(msg: Dict) -> bool:
    """Check if a message is a tool response."""
    # OpenAI chat format: role="tool" with tool_call_id
    if msg.get("role") == "tool":
        return True
    # Text-based format: role="user" wrapping a <tool_response> block
    if msg.get("role") == "user":
        content = msg.get("content") or ""
        if "<tool_response>" in content:
            return True
        if msg.get("tool_call_id"):
            return True
    return False


def _find_clearable_groups(
    messages: List[Dict],
) -> List[Tuple[int, List[int]]]:
    """Identify groups of (assistant_tool_call_idx, [tool_response_indices]).

    Rules:
      - Index 0 (system message) and index 1 (first user message) are never
        included in any clearable group.
      - A group starts with an assistant message that has tool_calls and
        consists of all consecutive tool-response messages that follow it.
    """
    groups: List[Tuple[int, List[int]]] = []
    i = 2  # protect system (0) and first user (1)
    while i < len(messages):
        msg = messages[i]
        if _has_tool_calls(msg):
            response_indices: List[int] = []
            j = i + 1
            while j < len(messages) and _is_tool_response(messages[j]):
                response_indices.append(j)
                j += 1
            if response_indices:
                groups.append((i, response_indices))
                i = j
            else:
                # Assistant tool_call without a matching response; skip it
                i += 1
        else:
            i += 1
    return groups


def apply_context_editing(
    messages: List[Dict],
    token_counts: List[int],
    max_capacity: int,
    config: dict,
) -> Tuple[List[Dict], str, int]:
    """Apply context editing to keep token usage within budget.

    When the active token count exceeds *trigger*, old tool-call / tool-response
    groups are marked as cleared (``_cleared=True``) until at least
    *clear_at_least* tokens have been freed.  The most recent *keep_tool_use*
    groups are always preserved.

    Args:
        messages:  List of message dicts.  **Modified in-place** -- cleared
                   messages receive ``_cleared=True`` and ``_original_content``.
        token_counts:  Parallel list giving the token count of each message.
                       This list is **not** mutated.
        max_capacity:  Hard ceiling used only for the percentage display.
        config:  Configuration dict.  Recognised keys:

                 * ``trigger``  (int, default 90000) -- token threshold that
                   activates clearing.
                 * ``clear_at_least`` (int, default 34000) -- minimum number
                   of tokens to free when clearing is triggered.
                 * ``keep_tool_use`` (int, default 2) -- number of most-recent
                   tool-call/response groups to keep untouched.

    Returns:
        ``(messages, system_warning)`` where *messages* is the same list
        (mutated) and *system_warning* is an XML-tagged status string.
    """
    trigger = config.get("trigger", 90000)
    clear_at_least = config.get("clear_at_least", 34000)
    keep_tool_use = config.get("keep_tool_use", 2)

    # ---- current token usage (non-cleared messages only) --------------------
    current_usage = 0
    for i, msg in enumerate(messages):
        if not msg.get("_cleared", False):
            current_usage += token_counts[i]

    # ---- under threshold: return status only --------------------------------
    if current_usage < trigger:
        pct = current_usage / max_capacity if max_capacity > 0 else 0
        remaining = max_capacity - current_usage
        warning = (
            f"<system_warning>Token usage: {current_usage}/{max_capacity} "
            f"tokens ({pct:.0%}); {remaining} remaining</system_warning>"
        )
        return messages, warning, 0

    # ---- over threshold: clear old groups -----------------------------------
    all_groups = _find_clearable_groups(messages)

    # Keep only groups that have not already been cleared
    active_groups = [
        (asst_idx, tool_indices)
        for asst_idx, tool_indices in all_groups
        if not messages[asst_idx].get("_cleared", False)
    ]

    # Protect the most recent *keep_tool_use* active groups
    if keep_tool_use > 0 and len(active_groups) > keep_tool_use:
        groups_to_clear = active_groups[:-keep_tool_use]
    elif keep_tool_use > 0:
        # Not enough groups to spare -- nothing we can safely clear
        groups_to_clear = []
    else:
        groups_to_clear = list(active_groups)

    # Clear groups oldest-first until budget is met
    tokens_freed = 0
    target_to_free = max(clear_at_least, current_usage - trigger)

    for asst_idx, tool_indices in groups_to_clear:
        if tokens_freed >= target_to_free:
            break

        # Mark the assistant (tool-call) message as cleared
        asst_msg = messages[asst_idx]
        if not asst_msg.get("_cleared", False):
            asst_msg["_cleared"] = True
            asst_msg["_original_content"] = asst_msg.get("content", "")
            tokens_freed += token_counts[asst_idx]

        # Mark every tool-response message in the group as cleared
        for t_idx in tool_indices:
            t_msg = messages[t_idx]
            if not t_msg.get("_cleared", False):
                t_msg["_cleared"] = True
                t_msg["_original_content"] = t_msg.get("content", "")
                tokens_freed += token_counts[t_idx]

    # ---- build warning string -----------------------------------------------
    current_usage -= tokens_freed
    parts: List[str] = []
    if tokens_freed > 0:
        parts.append(
            f"<system_warning>{tokens_freed} oldest tokens cleared.</system_warning>"
        )
    pct = current_usage / max_capacity if max_capacity > 0 else 0
    remaining = max_capacity - current_usage
    parts.append(
        f"<system_warning>Token usage: {current_usage}/{max_capacity} "
        f"tokens ({pct:.0%}); {remaining} remaining</system_warning>"
    )
    warning = "\n".join(parts)

    return messages, warning, tokens_freed
