"""Lightweight context manager for standalone long-horizon benchmark tasks.

Manages a conversation context window using plain Python dicts (no verl or
qwen_agent dependencies).  Token counting is done via a HuggingFace tokenizer.
"""

import copy
import json
from typing import Dict, List, Tuple

from transformers import AutoTokenizer

from .context_editor import apply_context_editing as _apply_context_editing


class ContextManager:
    """Tracks messages and enforces a token budget for long-horizon agents.

    Parameters
    ----------
    tokenizer_path : str
        Model name or local path passed to ``AutoTokenizer.from_pretrained``.
    max_token_capacity : int
        Hard ceiling on the context window (used for usage-percentage display).
    config : dict, optional
        Forwarded to :func:`context_editor.apply_context_editing`.  Recognised
        keys: ``trigger``, ``clear_at_least``, ``keep_tool_use``.
    """

    def __init__(
        self,
        tokenizer_path: str,
        max_token_capacity: int = 128000,
        config: dict = None,
    ):
        self.tokenizer = AutoTokenizer.from_pretrained(
            tokenizer_path, trust_remote_code=True
        )
        self.max_token_capacity = max_token_capacity
        self.config: dict = config if config is not None else {}
        self._messages: List[Dict] = []
        self._message_token_counts: List[int] = []
        self._last_tokens_freed: int = 0

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    def count_tokens(self, text: str) -> int:
        """Return the number of tokens produced by the tokenizer for *text*."""
        if not text:
            return 0
        return len(self.tokenizer.encode(text))

    def _count_message_tokens(self, message: Dict) -> int:
        """Estimate the token footprint of a single message dict.

        Accounts for ``content``, ``tool_calls`` (serialised to JSON), and
        ``reasoning_content``.
        """
        total = 0
        content = message.get("content") or ""
        total += self.count_tokens(content)

        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += self.count_tokens(json.dumps(tool_calls, ensure_ascii=False))

        reasoning = message.get("reasoning_content")
        if reasoning:
            total += self.count_tokens(reasoning)

        return total

    # ------------------------------------------------------------------
    # Message management
    # ------------------------------------------------------------------

    def add_message(self, message: Dict) -> None:
        """Append *message* and record its token count."""
        token_count = self._count_message_tokens(message)
        self._messages.append(message)
        self._message_token_counts.append(token_count)

    def get_messages(self) -> List[Dict]:
        """Return the conversation with cleared turns fully removed.

        Messages marked ``_cleared=True`` are **dropped entirely** -- they leave
        no placeholder behind, so the tokens they held (content, ``tool_calls``
        JSON, and ``reasoning_content``) are genuinely released from the request
        sent to the model.

        This is safe because the context editor clears in whole
        tool-call/tool-response *groups* (an assistant message that issues
        ``tool_calls`` together with every tool response that follows it), so
        removing all cleared messages never orphans a tool response from its
        originating tool call. The protected system message (index 0) and first
        user message (index 1) are never marked cleared, so the conversation
        still opens validly.

        Non-cleared messages are deep-copied so the caller cannot mutate
        internal state.
        """
        result: List[Dict] = []
        for msg in self._messages:
            if msg.get("_cleared", False):
                continue
            result.append(copy.deepcopy(msg))
        return result

    def get_full_trajectory(self) -> List[Dict]:
        """Return **all** messages including cleared ones with original content.

        This is useful for logging or post-hoc analysis.  Internal metadata
        keys (``_cleared``, ``_original_content``) are preserved.
        """
        return copy.deepcopy(self._messages)

    # ------------------------------------------------------------------
    # Context editing
    # ------------------------------------------------------------------

    def apply_context_editing(self) -> str:
        """Run the context editor and return the system-warning string.

        Old tool-call / tool-response groups are marked ``_cleared=True``
        when the active token count exceeds the configured trigger.
        """
        _, warning, tokens_freed = _apply_context_editing(
            self._messages,
            self._message_token_counts,
            self.max_token_capacity,
            self.config,
        )
        self._last_tokens_freed = tokens_freed
        return warning

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_token_usage(self) -> Tuple[int, int]:
        """Return ``(used_tokens, max_capacity)``.

        ``used_tokens`` counts only messages that have **not** been cleared.
        """
        used = 0
        for i, msg in enumerate(self._messages):
            if not msg.get("_cleared", False):
                used += self._message_token_counts[i]
        return used, self.max_token_capacity

    def consume_tokens_freed(self) -> int:
        """Return tokens freed by the last context editing pass and reset to 0."""
        freed = self._last_tokens_freed
        self._last_tokens_freed = 0
        return freed
