"""LLM client for the agent loop.

One client speaks to every provider the benchmark supports, dispatching on the
provider resolved from models_config.json (see agent/providers.py). Only the
providers' own documented endpoints are supported — there is no gateway or
proxy-specific behaviour in this file, and nothing is inferred from a model name.

Two wire formats cover everything:

- OpenAI Chat Completions, used for OpenAI, OpenRouter, Google (through
  Google's official OpenAI-compatible endpoint) and any self-hosted or
  third-party server that speaks it (vLLM, SGLang, Ollama, LM Studio,
  DashScope, DeepSeek). Optionally the OpenAI Responses API, via
  api_style="responses".
- The Anthropic Messages API, natively, including budgeted extended thinking.

Exactly one provider-specific quirk is handled, because the protocol requires
it rather than because an endpoint prefers it: Gemini 3.x returns a
thought_signature with every function call and rejects the next request unless
it is sent back. The field is carried through verbatim; see the comment in
generate_with_tools.
"""

import json
import logging
import time
import uuid
import os
from typing import Optional, List, Dict, Any
import functools
from copy import deepcopy
import traceback
import requests

logger = logging.getLogger(__name__)


# Errors that never succeed on retry, matched by exception class name so this
# module needs no provider imports. A mistyped key used to burn every retry with
# backoff before surfacing, which is minutes of silence on a first run.
FATAL_EXC_NAMES = (
    "AuthenticationError",  # bad or revoked key
    "PermissionDeniedError",  # key lacks access to the model
    "NotFoundError",  # model id does not exist for this provider
    "UnprocessableEntityError",
)


def retry_with_exponential_backoff(func):
    # 8 is enough to ride out rate limits and transient 5xx. Long unattended
    # runs against a flaky gateway can raise it: API_MAX_RETRIES=100 bash run.sh
    max_retries: int = (
        8 if os.getenv("API_MAX_RETRIES") is None else int(os.getenv("API_MAX_RETRIES"))
    )
    base_delay: float = 1.0
    backoff_factor: float = 2.0
    max_delay: float = 30.0

    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        attempt = 0
        while True:
            try:
                return func(self, *args, **kwargs)
            except Exception as e:
                last_traceback = traceback.format_exc()
                attempt += 1
                if attempt > max_retries:
                    print(
                        f"[retry] Max retries exceeded for {func.__name__} "
                        f"after {max_retries} attempts. Last error: {e}, traceback: {last_traceback}"
                    )
                    raise
                sleep_seconds = min(
                    base_delay * (backoff_factor ** (attempt - 1)), max_delay
                )
                # Bail out on known-fatal errors BEFORE sleeping. These never
                # succeed on retry, so the whole point of the bail-out list is to
                # fail fast — sleeping the full backoff first (up to max_delay)
                # defeats that and just adds dead latency to a doomed request.
                for bad_request in [
                    "Input is too long for requested model",
                    "Range of input length should be",
                    "maximum context length",
                    "context_length_exceeded",
                    "parameter of the code model must be in JSON format",
                    "Invalid schema for function",
                    "Input data may contain inappropriate content",
                    "input_schema does not support oneOf, allOf, or anyOf at the top level",
                    "custom.input_schema.type: Field required",
                    "functionDeclaration parameters schema should be of type OBJECT",
                    "Repetitive tool calls",
                ]:
                    if bad_request in str(e):
                        print(
                            f"No more retry because of known exception, quitting: {str(e)}"
                        )
                        raise
                for fatal_request in [
                    "Unsupported parameter",
                    "Missing `reasoning_content` field",
                    "all messages must have non-empty content except for the optional final assistant message",
                ]:
                    if fatal_request in str(e):
                        raise
                if type(e).__name__ in FATAL_EXC_NAMES:
                    print(
                        f"[retry] {type(e).__name__} is not retryable, quitting. "
                        f"Check the api_key, base_url and model_name for this entry "
                        f"in models_config.json. Error: {e}"
                    )
                    raise
                print(
                    f"[retry] {func.__name__} failed on attempt {attempt}/{max_retries}: {e}, traceback: {last_traceback}\n"
                    f"Retrying in {sleep_seconds:.1f}s..."
                )
                time.sleep(sleep_seconds)

    return wrapper


class MultiProviderClient:
    """One chat client for every provider the benchmark supports.

    Dispatches on the resolved provider and model name: the native Anthropic
    Messages API, the OpenAI Chat Completions and Responses APIs, the Gemini
    generateContent protocol, and any OpenAI-compatible endpoint. Supports tool
    calling, streaming, and reasoning content.

    Requires: pip install openai (plus anthropic for provider "anthropic").
    """

    def __init__(
        self,
        api_key: str = None,
        model: str = None,
        max_tokens: int = 16384,
        temperature: float = None,
        base_url: str = None,
        api_style: str = None,
        session_id: str = None,
        provider: str = None,
        extra_body: Dict[str, Any] = None,
        effort: str = None,
    ):
        """
        Args:
            api_key: API key (from models_config.json)
            model: Model name (qwen-plus, qwen-turbo, qwen-max, etc.)
            max_tokens: Max tokens to generate
            temperature: Sampling temperature
            base_url: API base URL. None uses the SDK default, which is what
                provider "openai" and "anthropic" want.
            provider: openai | anthropic | openrouter | openai-compatible, as
                resolved from models_config.json by agent/providers.py. Selects
                the wire protocol; "anthropic" talks the native Messages API,
                everything else the OpenAI chat-completions format.
            api_style: Optional request shape override from config. "responses"
                routes to the OpenAI Responses API path; None/"chat" uses the
                default chat-completions path.
            extra_body: Vendor-documented request-body fields from the model's
                config entry, merged into every chat-completions request. This is
                how a family's thinking switch is turned on without the client
                knowing anything about that family, e.g.
                {"enable_thinking": true} for Qwen on DashScope, or
                {"thinking": {"type": "enabled"}} for GLM and DeepSeek.
            effort: Requested reasoning effort from the config entry ("low" |
                "medium" | "high" | "xhigh" | "max"). Sent as reasoning_effort on
                the chat-completions path and converted to a thinking token
                budget on the native Anthropic path. A runtime MODEL_EFFORT wins.
            session_id: Stable session identifier sent as a "session-id" header.
                Gateways that pin a cache node per session need it for prompt
                caching to survive load-balancing; harmless elsewhere. Each run
                should pass a distinct id; falls back to SESSION_ID or a random
                per-instance uuid.
        """
        if not api_key:
            raise ValueError(
                "API key required. Set it in models_config.json, or export the "
                "provider's key variable (OPENAI_API_KEY, ANTHROPIC_API_KEY, "
                "OPENROUTER_API_KEY)."
            )

        # Stable session id so gateways with cache-node affinity pin every turn
        # of this client to the same backend. When multiple runs share a process
        # (ThreadPoolExecutor in run.py) each agent must pass its own id, or the
        # runs pollute each other's prompt cache.
        self._session_id = (
            session_id or os.getenv("SESSION_ID") or f"ecom-{uuid.uuid4().hex[:12]}"
        )
        self.provider = (provider or "").strip().lower()

        headers = {"session-id": self._session_id}
        if self.provider == "openrouter":
            # OpenRouter attributes traffic with these; both are optional and
            # only affect its dashboard, never the completion.
            referer = os.getenv("OPENROUTER_REFERER")
            title = os.getenv("OPENROUTER_TITLE", "E-Commerce Bench")
            if referer:
                headers["HTTP-Referer"] = referer
            headers["X-Title"] = title

        if self.provider == "anthropic":
            try:
                from anthropic import Anthropic
            except ImportError:
                raise ImportError(
                    'provider "anthropic" needs the SDK: pip install anthropic. '
                    "Alternatively reach Claude through provider "
                    '"openrouter", which speaks the OpenAI format."'
                )
            kw = dict(api_key=api_key, default_headers=headers)
            if base_url:
                kw["base_url"] = base_url
            self.anthropic = Anthropic(**kw)
            self.client = None
        else:
            try:
                from openai import OpenAI
            except ImportError:
                raise ImportError("Please install openai: pip install openai")
            kw = dict(api_key=api_key, default_headers=headers)
            if base_url:
                kw["base_url"] = base_url
            self.client = OpenAI(**kw)
            self.anthropic = None

        self.inference_kwargs = {}
        self.config_extra_body = dict(extra_body or {})
        self.effort = (effort or "").strip().lower() or None
        self.model = model
        self.api_style = (api_style or "").strip().lower()
        self.max_tokens = max_tokens
        self.temperature = temperature

    def _generate_gpt_responses(self, messages, tools, max_tokens):
        """Generate using OpenAI Responses API for gpt models.

        Some gateways proxy the Responses API shape via /chat/completions.
        """
        input_items = []
        system_content = None
        # Track tool calls we skip because of an empty name (see parser note),
        # so their matching function_call_output is skipped too — an orphaned
        # output (output with no preceding call) is itself an API error.
        dropped_call_ids = set()

        for msg in messages:
            role = msg.get("role")
            if role == "system":
                system_content = msg["content"]
            elif role == "user":
                input_items.append(
                    {"type": "message", "role": "user", "content": msg["content"]}
                )
            elif role == "assistant":
                # Replay this turn's reasoning items (carrying encrypted_content)
                # BEFORE the assistant's text/tool calls, matching the order the
                # API originally emitted them. This preserves the model's
                # reasoning chain across turns for reasoning models (GPT-5.x).
                # Items are only stored when the API returned encrypted_content
                # (see the response parser below), so replaying them is safe in
                # stateless (store=false) mode.
                for r_item in msg.get("reasoning_items") or []:
                    input_items.append(r_item)
                content = msg.get("content", "") or ""
                if content:
                    input_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": content}],
                        }
                    )
                for tc in msg.get("tool_calls", []):
                    func = tc.get("function", {})
                    name = func.get("name") or ""
                    call_id = tc.get("id", str(uuid.uuid4()))
                    # Defensive: never send a function_call with an empty name —
                    # the API rejects it. The parser drops these at the source for
                    # fresh runs; this also covers any malformed call already in
                    # history.
                    if not name:
                        dropped_call_ids.add(call_id)
                        continue
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call_id,
                            "name": name,
                            "arguments": func.get("arguments", "{}"),
                        }
                    )
            elif role == "tool":
                tc_id = msg.get("tool_call_id") or msg.get("tool_use_id", "")
                if tc_id in dropped_call_ids:
                    continue  # orphan: its function_call was dropped above
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": tc_id,
                        "output": str(msg.get("content", "")),
                    }
                )

        # Reasoning effort via the shared MODEL_EFFORT env var (injected from the
        # model's "effort" field in models_config.json by setup_model_env, or set
        # at runtime, which wins). Defaults to "high" — note GPT-5.4's own API
        # default is "none", so we keep thinking on unless told otherwise.
        # GPT-5.x accepts none|low|medium|high|xhigh.
        gpt_effort = (
            (os.getenv("MODEL_EFFORT") or self.effort or "high").strip().lower()
        )
        # Reasoning configuration.
        #   - summary="auto" returns a human-readable reasoning summary, surfaced
        #     as reasoning_content (otherwise the summary array is empty and we
        #     capture nothing). ALWAYS ON — cheap and works on every endpoint.
        #   - Cross-turn reasoning round-trip (store=false +
        #     include=["reasoning.encrypted_content"], then replaying the
        #     encrypted reasoning items on later turns) is OPT-IN via
        #     GPT_REASONING_ROUNDTRIP=1 and DEFAULTS OFF. It is the documented way
        #     to preserve a reasoning model's chain across multi-turn function
        #     calling, BUT it only works on a backend-affine endpoint: a routing
        #     gateway load-balances across upstream backends, and reasoning
        #     encrypted_content produced by one backend fails to decrypt when the
        #     replay lands on another ("encrypted content could not be verified").
        #     When enabled, a decrypt failure is handled gracefully below (resend
        #     once without the reasoning items) so it degrades instead of crashing.
        roundtrip = os.getenv("GPT_REASONING_ROUNDTRIP", "0") == "1"
        payload = {
            "model": self.model,
            "input": input_items,
            "max_output_tokens": max_tokens,
            "reasoning": {"effort": gpt_effort, "summary": "auto"},
        }
        if roundtrip:
            payload["store"] = False
            payload["include"] = ["reasoning.encrypted_content"]
        print(
            "Model Name:",
            self.model,
            "using GPT reasoning effort:",
            gpt_effort,
            "| reasoning roundtrip:",
            roundtrip,
        )
        if system_content is not None:
            payload["instructions"] = system_content
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "parameters": t["function"].get("parameters", {}),
                }
                for t in tools
            ]
        if os.getenv("LLM_CLIENT_TEMPERATURE"):
            payload["temperature"] = float(os.getenv("LLM_CLIENT_TEMPERATURE"))

        api_key = self.client.api_key
        base_url = str(self.client.base_url).rstrip("/")
        url = base_url + "/responses"

        def _post(p):
            return requests.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                json=p,
                timeout=120,
            )

        resp = _post(payload)
        if resp.status_code != 200:
            txt = resp.text or ""
            # Graceful fallback: the model-router routed this replay to a backend
            # that cannot decrypt the reasoning items we sent. Strip them and
            # resend once — we lose the reasoning chain for this hop but the call
            # succeeds. Other 4xx/5xx (e.g. transient upstream saturation) fall
            # through to the outer retry_with_exponential_backoff.
            decrypt_failed = (
                "could not be verified" in txt
                or "could not be decrypted" in txt
                or "encrypted content" in txt.lower()
            )
            had_reasoning = any(it.get("type") == "reasoning" for it in input_items)
            if decrypt_failed and had_reasoning:
                payload_fb = dict(payload)
                payload_fb["input"] = [
                    it for it in input_items if it.get("type") != "reasoning"
                ]
                print(
                    "Reasoning replay rejected by backend (encrypted content unverifiable); "
                    "resending without reasoning items."
                )
                resp = _post(payload_fb)
            if resp.status_code != 200:
                raise Exception(
                    f"Responses API error {resp.status_code}: {resp.text[:4000]}"
                )

        data = resp.json()
        reasoning_parts = []
        reasoning_items = []
        content_parts = []
        tool_calls = []
        dropped_malformed_fc = 0

        for item in data.get("output", []):
            item_type = item.get("type")
            if item_type == "reasoning":
                for summary in item.get("summary", []):
                    if summary.get("type") == "summary_text":
                        reasoning_parts.append(summary.get("text", ""))
                # Preserve the reasoning item for cross-turn replay, but ONLY when
                # the API returned encrypted_content. Without it the item cannot be
                # resolved in stateless (store=false) mode and would be rejected on
                # the next turn — so if the proxy omits encrypted_content we still
                # keep the summary above but do not replay an unusable item.
                enc = item.get("encrypted_content")
                if enc:
                    reasoning_items.append(
                        {
                            "type": "reasoning",
                            "id": item.get("id"),
                            "summary": item.get("summary", []),
                            "encrypted_content": enc,
                        }
                    )
            elif item_type == "message":
                for c in item.get("content", []):
                    if c.get("type") == "output_text":
                        content_parts.append(c.get("text", ""))
            elif item_type == "function_call":
                # Some endpoints intermittently return malformed function_call items
                # with a null/empty name (often with null arguments too) despite
                # status="completed". They are useless and toxic: executing one
                # yields "Unknown tool: " AND, once stored on the assistant
                # message, the API rejects the ENTIRE next request
                # ("Invalid 'input[N].name': empty string") when the turn is
                # replayed. Drop them at the source so they never enter history.
                name = item.get("name") or ""
                if not name:
                    dropped_malformed_fc += 1
                    continue
                args = item.get("arguments")
                if args is None:
                    args = "{}"
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                tool_calls.append(
                    {
                        "id": item.get("call_id", str(uuid.uuid4())),
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": args,
                        },
                    }
                )

        answer_content = "".join(content_parts)
        # If the model TRIED to call tools but every function_call came back
        # malformed (empty name) and there is no text either, don't waste the
        # turn (which would trip the agent's 3-strikes idle terminator) — raise
        # so retry_with_exponential_backoff re-issues the call. Only retry when
        # the proxy mangled a real attempt; a clean no-tool, no-text reply is
        # left to the normal "." path below.
        if not tool_calls and not answer_content and dropped_malformed_fc:
            raise Exception(
                f"proxy returned {dropped_malformed_fc} malformed tool_call(s) "
                f"with empty name and no content; retrying"
            )
        if not tool_calls and not answer_content:
            answer_content = "."

        return {
            "reasoning_content": "\n\n".join(reasoning_parts),
            "reasoning_items": reasoning_items,
            "content": answer_content,
            "tool_calls": tool_calls,
            "extra": {
                "finish_reason": "stop",
            },
        }

    # ---------------------------------------------------------------- anthropic
    EFFORT_BUDGET = {"low": 4096, "medium": 8192, "high": 16384, "max": 32768}

    @staticmethod
    def _to_anthropic(messages, tools):
        """OpenAI chat messages and tools to the native Messages API shape.

        Returns (system_blocks, messages, tools). The system prompt leaves the
        message list and becomes a top-level parameter, tool_calls become
        tool_use blocks, and role="tool" turns become user tool_result blocks.
        """
        system_text = []
        out = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                if m.get("content"):
                    system_text.append(m["content"])
                continue
            if role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id") or "",
                    "content": m.get("content") or "",
                }
                # consecutive tool results belong in one user turn
                if (
                    out
                    and out[-1]["role"] == "user"
                    and isinstance(out[-1]["content"], list)
                    and out[-1]["content"]
                    and out[-1]["content"][0].get("type") == "tool_result"
                ):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue
            if role == "assistant" and m.get("tool_calls"):
                blocks = []
                if m.get("content"):
                    blocks.append({"type": "text", "text": m["content"]})
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {}) or {}
                    raw = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw) if isinstance(raw, str) else raw
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc.get("id") or str(uuid.uuid4()),
                            "name": fn.get("name") or "",
                            "input": args,
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
                continue
            if m.get("content"):
                out.append({"role": role, "content": m["content"]})

        system_blocks = None
        if system_text:
            system_blocks = [{"type": "text", "text": "\n\n".join(system_text)}]
            if os.getenv("CLAUDE_PROMPT_CACHE", "1") != "0":
                # the system prompt is the largest stable prefix, so it is the
                # one worth caching; Anthropic bills cache writes once
                system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

        atools = None
        if tools:
            atools = []
            for t in tools:
                fn = t.get("function", t) or {}
                atools.append(
                    {
                        "name": fn.get("name"),
                        "description": fn.get("description") or "",
                        "input_schema": fn.get("parameters")
                        or {"type": "object", "properties": {}},
                    }
                )
        return system_blocks, out, atools

    @retry_with_exponential_backoff
    def _generate_anthropic(self, messages, tools, max_tokens):
        """One turn against the native Anthropic Messages API."""
        system_blocks, amsgs, atools = self._to_anthropic(messages, tools)

        request = dict(model=self.model, messages=amsgs, max_tokens=max_tokens)
        if system_blocks:
            request["system"] = system_blocks
        if atools:
            request["tools"] = atools
        if self.temperature is not None:
            request["temperature"] = self.temperature

        if os.getenv("CLAUDE_THINKING") == "1":
            effort = (
                (os.getenv("MODEL_EFFORT") or self.effort or "high").strip().lower()
            )
            budget = self.EFFORT_BUDGET.get(effort, self.EFFORT_BUDGET["high"])
            # the cap has to leave room for the answer on top of the thinking
            request["max_tokens"] = max(max_tokens, budget + 4096)
            request["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # extended thinking rejects a temperature
            request.pop("temperature", None)

        response = self.anthropic.messages.create(**request)

        reasoning, answer, tool_calls = "", "", []
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "thinking":
                reasoning += getattr(block, "thinking", "") or ""
            elif btype == "redacted_thinking":
                reasoning += "[redacted]"
            elif btype == "text":
                answer += getattr(block, "text", "") or ""
            elif btype == "tool_use":
                tool_calls.append(
                    {
                        "id": getattr(block, "id", None) or str(uuid.uuid4()),
                        "type": "function",
                        "function": {
                            "name": getattr(block, "name", ""),
                            "arguments": json.dumps(
                                getattr(block, "input", {}) or {}, ensure_ascii=False
                            ),
                        },
                    }
                )
        return {
            "reasoning_content": reasoning,
            "content": answer,
            "tool_calls": tool_calls,
        }

    @retry_with_exponential_backoff
    def generate_with_tools(
        self,
        messages: List[Dict[str, str]],
        tool_schemas: List[Dict[str, Any]],
        timeout: int = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Generate with tool calling support (advanced interface)

        Args:
            messages: Conversation messages
            tool_schemas: List of tool schemas (OpenAI function format)
            timeout: Request timeout in seconds
            **kwargs: Additional parameters

        Returns:
            Dict with 'reasoning_content', 'content', and 'tool_calls'
        """
        max_tokens = kwargs.get("max_tokens", self.max_tokens)

        if self.provider == "anthropic":
            tools_a = (
                [{"type": "function", "function": t} for t in tool_schemas]
                if tool_schemas
                else None
            )
            return self._generate_anthropic(
                messages=deepcopy(messages), tools=tools_a, max_tokens=max_tokens
            )

        # Convert tool schemas to OpenAI format
        tools = (
            [
                {"type": "function", "function": tool_schema}
                for tool_schema in tool_schemas
            ]
            if tool_schemas
            else None
        )
        messages = deepcopy(messages)
        # Start from whatever the config entry asked for, then add the requested
        # reasoning effort. Both travel in the request body, so a family that
        # names its switch differently needs a config entry, not a code change.
        extra_body = dict(self.config_extra_body)
        effort = (os.getenv("MODEL_EFFORT") or self.effort or "").strip().lower()
        if effort:
            extra_body.setdefault("reasoning_effort", effort)
        if self.api_style == "responses":
            return self._generate_gpt_responses(
                messages=messages, tools=tools, max_tokens=max_tokens
            )

        create_kwargs = dict(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto" if tools else None,
            stream=False,
            max_tokens=max_tokens,
            extra_body=extra_body,
            **(
                {"temperature": float(os.getenv("LLM_CLIENT_TEMPERATURE"))}
                if os.getenv("LLM_CLIENT_TEMPERATURE")
                else {}
            ),
            **self.inference_kwargs,
        )
        completion = self.client.chat.completions.create(**create_kwargs)
        # Prompt-cache telemetry: Anthropic and OpenAI-compatible gateways report cache_read /
        # cache_creation on the OpenAI-shape usage object. Log per-turn so
        # we can confirm hit rate. Healthy loops show a big cache_read
        # from turn 2 onwards.
        try:
            _u = getattr(completion, "usage", None)
            if _u is not None:
                cache_read = int(getattr(_u, "cache_read_input_tokens", 0) or 0)
                cache_write = int(getattr(_u, "cache_creation_input_tokens", 0) or 0)
                if cache_read or cache_write:
                    fresh_in = int(
                        getattr(_u, "input_tokens", 0)
                        or getattr(_u, "prompt_tokens", 0)
                        or 0
                    )
                    out_tok = int(
                        getattr(_u, "output_tokens", 0)
                        or getattr(_u, "completion_tokens", 0)
                        or 0
                    )
                    total_in = fresh_in + cache_read + cache_write
                    hit = (cache_read / total_in * 100.0) if total_in else 0.0
                    print(
                        f"[cache] read={cache_read} write={cache_write} fresh={fresh_in} out={out_tok} hit={hit:.0f}% sid={self._session_id}"
                    )
        except Exception:
            pass
        message = completion.choices[0].message
        finish_reason = (
            completion.choices[0].finish_reason
            if hasattr(completion.choices[0], "finish_reason")
            else None
        )
        if finish_reason == "tool_calls":
            finish_reason = "stop"
        # completion_tokens = completion.usage.completion_tokens if hasattr(completion, 'usage') and hasattr(completion.usage, 'completion_tokens') else None

        # Extract reasoning content
        reasoning_content = ""
        if hasattr(message, "reasoning_content") and message.reasoning_content:
            reasoning_content = message.reasoning_content or ""

        # Extract regular content
        answer_content = message.content or ""

        # Extract tool calls. Keep the provider's own tool_call id when it
        # sends one — that is what the OpenAI format specifies, and some
        # providers (Kimi/Moonshot) reject a tool result that does not echo it
        # back. Only synthesise an id when the provider omits one.
        tool_calls = []
        if message.tool_calls is not None:
            for tool_call in message.tool_calls:
                tc_id = tool_call.id or str(uuid.uuid4())
                tc = {
                    "id": tc_id,
                    "type": "function",
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                # Gemini 3.x attaches a thought_signature to every function
                # call and REQUIRES it back on the following turn; without it
                # the next request fails with
                #   400 INVALID_ARGUMENT: Function call is missing a
                #   thought_signature in functionCall parts
                # It rides in the OpenAI-compatible response as
                # tool_calls[].extra_content.google.thought_signature, so
                # carry the field through verbatim and replay it unchanged.
                # Verified against generativelanguage.googleapis.com: no
                # reasoning_effort setting (none/low/minimal) avoids the
                # requirement. Providers that never send the field are
                # unaffected, so this needs no per-model branching.
                extra_content = getattr(tool_call, "extra_content", None)
                if extra_content is None:
                    extra_content = (getattr(tool_call, "model_extra", None) or {}).get(
                        "extra_content"
                    )
                if extra_content is not None:
                    if hasattr(extra_content, "model_dump"):
                        extra_content = extra_content.model_dump()
                    tc["extra_content"] = extra_content
                tool_calls.append(tc)

        # Some providers reject an assistant turn that carries neither text nor
        # tool calls once it is replayed in history, so give such a turn a single
        # character. This replaces four separate branches that keyed off model
        # names and vendor thinking flags (gpt-5, one Claude id, DeepSeek, Qwen).
        if not tool_calls and not answer_content:
            answer_content = "."
        return {
            "reasoning_content": reasoning_content,
            "content": answer_content,
            "tool_calls": tool_calls,
            "extra": {
                "finish_reason": finish_reason,
            },
        }
