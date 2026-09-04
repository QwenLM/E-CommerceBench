import json
import logging
import os
import uuid
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .llm_client import MultiProviderClient
from . import providers
from .ecommerce_tool_manager import EcommerceToolManager
from .prompts import CONTEXT_WINDOW_PROMPT
from .job import build_ecommerce_job, normalize_termination_reason

logger = logging.getLogger(__name__)

_CONFIG_CACHE: Optional[Dict] = None


def load_models_config() -> Dict:
    """Load the model registry.

    Resolution order, first hit wins:
      $ECBENCH_MODELS_CONFIG   explicit path
      models_config.local.json a private registry, gitignored
      models_config.json       the shipped template
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    root = Path(__file__).parent.parent
    candidates = [Path(p) for p in [os.environ.get("ECBENCH_MODELS_CONFIG")] if p]
    candidates += [root / "models_config.local.json", root / "models_config.json"]
    config_path = next((p for p in candidates if p.exists()), None)
    if config_path is None:
        raise FileNotFoundError(
            f"No model registry found. Expected one of: "
            f'{", ".join(str(p) for p in candidates)}'
        )
    with open(config_path, "r", encoding="utf-8") as f:
        _CONFIG_CACHE = json.load(f)
    return _CONFIG_CACHE


def resolve_model_config(model: str) -> Dict[str, Any]:
    """Resolve model config directly from models_config.json."""
    config = load_models_config()
    models = config.get("models", {})

    if model not in models:
        raise ValueError(
            f'Model "{model}" not found in the model registry. '
            f'Available: {", ".join(k for k in models if not k.startswith("_"))}'
        )

    return providers.resolve(model, models[model])


def setup_model_env(model_cfg: Dict[str, Any]) -> None:
    """Set environment variables required by tools based on model config."""
    # "thinking_env" names the environment variable(s) that switch this model's
    # reasoning surface on. A single name or a list of them, so an entry can ask
    # for more than one flag (e.g. CLAUDE_THINKING plus
    # CLAUDE_ADAPTIVE_THINKING for a release that wants the adaptive shape).
    thinking_env = model_cfg.get("thinking_env")
    if isinstance(thinking_env, str):
        thinking_env = [thinking_env]
    for env_name in thinking_env or []:
        os.environ[env_name] = "1"
        logger.info(f"Set {env_name}=1")

    # Optional per-model reasoning-effort default. Every effort-aware family
    # (Claude opus, GLM-5.2) reads the SAME env var, MODEL_EFFORT, so config
    # only carries the value — no per-family env-name mapping needed. A runtime
    # env var always wins, so `MODEL_EFFORT=max bash run.sh` overrides the
    # config default.
    effort_value = model_cfg.get("effort")
    if effort_value and not os.environ.get("MODEL_EFFORT"):
        os.environ["MODEL_EFFORT"] = str(effort_value)
        logger.info(f"Set MODEL_EFFORT={effort_value} (config default)")
    setup_npc_env()


def setup_npc_env() -> None:
    """Configure the benchmark-owned supplier dialogue model."""
    npc_config = dict(load_models_config().get("npc_tools", {}) or {})
    # the NPC entry names its model under "model"; resolve() expects "model_name"
    npc_config.setdefault("model_name", npc_config.get("model", ""))
    npc_resolved = providers.resolve("npc_tools", npc_config)
    npc_api_key = npc_resolved.get("api_key", "")
    npc_base_url = npc_resolved.get("base_url", "")
    npc_model = npc_resolved.get("model_name", "")
    if npc_api_key:
        os.environ["GPT_API_KEY"] = npc_api_key
    if npc_base_url:
        os.environ["GPT_BASE_URL"] = npc_base_url
    if npc_model:
        os.environ["NPC_MODEL"] = npc_model


class EcommerceBenchAgent:
    """Standalone agent for E-Commerce Bench.

    Runs the tool-calling loop: LLM call -> parse tool_calls -> execute -> repeat.
    Integrates context management for long-horizon episodes.
    """

    def __init__(
        self,
        model: str,
        max_tokens: int = 16384,
        temperature: Optional[float] = None,
        max_turns: int = 4000,
        initial_balance: float = 100000.0,
        daily_fee: float = 50.0,
        max_day: int = 365,
        max_token_capacity: int = 128000,
        tokenizer_path: Optional[str] = None,
        log_dir: Optional[str] = None,
        run_index: int = 0,
    ):
        model_cfg = resolve_model_config(model)
        setup_model_env(model_cfg)

        actual_model = model_cfg["model_name"]
        api_key = model_cfg["api_key"]
        base_url = model_cfg["base_url"]

        self.model = actual_model
        self.max_turns = max_turns
        self.max_token_capacity = max_token_capacity
        # Context-editing config — kept as attributes so the prompt the agent
        # sees and the context manager that actually enforces it share ONE
        # source of truth (previously the prompt hardcoded "50% removed at
        # max_token_capacity" while the real trigger was 64000).
        # Overridable from the environment so a short run can exercise context
        # editing: the trigger is otherwise only reachable in a long episode.
        # Defaults are unchanged, and because the prompt is rendered from these
        # same attributes, an override keeps prompt and enforcement in sync.
        self.context_trigger = int(os.getenv("ECBENCH_CONTEXT_TRIGGER", 120000))
        self.context_clear_at_least = int(
            os.getenv("ECBENCH_CONTEXT_CLEAR_AT_LEAST", 60000)
        )
        self.context_keep_tool_use = int(os.getenv("ECBENCH_CONTEXT_KEEP_TOOL_USE", 2))
        self.run_index = run_index
        _default_tokenizer = str(
            Path(__file__).parent.parent / "context_manager" / "tokenizer"
        )
        self.tokenizer_path = tokenizer_path or os.getenv(
            "TOKENIZER_PATH", _default_tokenizer
        )

        if log_dir:
            os.environ["ECOMMERCE_BENCH_LOG_DIR"] = log_dir
        os.environ["MODEL_ID"] = actual_model

        if temperature is None and os.getenv("LLM_CLIENT_TEMPERATURE"):
            temperature = float(os.getenv("LLM_CLIENT_TEMPERATURE"))

        # Unique per-run session id so gateway cache affinity pins every turn
        # of this run to the same cache backend (required for Claude
        # prompt-cache hits when a gateway load-balances across cache nodes).
        session_id = f"ecom-{actual_model[:24].replace('/', '_')}-r{run_index}-{uuid.uuid4().hex[:8]}"

        self.llm_client = MultiProviderClient(
            api_key=api_key,
            model=actual_model,
            max_tokens=max_tokens,
            temperature=temperature,
            base_url=base_url,
            # Must be passed: the client selects the wire protocol from it, so
            # leaving it out silently sent every provider down the OpenAI
            # chat-completions path — including "anthropic", whose native
            # Messages API was therefore unreachable.
            provider=model_cfg.get("provider"),
            api_style=model_cfg.get("api_style"),
            extra_body=model_cfg.get("extra_body"),
            effort=model_cfg.get("effort"),
            session_id=session_id,
        )

        from tools import ECOMMERCE_TOOL_SCHEMAS

        self.tool_schemas = ECOMMERCE_TOOL_SCHEMAS

        self.initial_balance = initial_balance
        self.daily_fee = daily_fee
        self.max_day = max_day

        logger.info(f"Agent initialized: model={actual_model}, base_url={base_url}")

    def run(self, job: Optional[Dict] = None) -> Dict[str, Any]:
        """Run a full episode and return the job dict with results."""
        if job is None:
            job = self._build_default_job()

        job.setdefault("agent_info", {})
        job["agent_info"]["run_index"] = self.run_index

        tool_manager = EcommerceToolManager.init(job)

        try:
            context_manager = self._create_context_manager()
        except Exception as e:
            logger.warning(
                f"Context manager init failed ({e}), running without context editing."
            )
            context_manager = None

        if context_manager:
            for msg in job["messages"]:
                context_manager.add_message(msg)

        messages = list(job["messages"])
        traj = list(job.get("traj", []))
        tool_schemas = job.get("tool_schemas", self.tool_schemas)
        max_turns = job.get("agent_info", {}).get("max_turn", self.max_turns)

        turn = 0
        consecutive_no_tool_calls = 0
        max_no_tool_calls = 3
        while turn < max_turns:
            turn += 1

            if context_manager:
                context_manager.apply_context_editing()
                current_messages = context_manager.get_messages()
                # Surface the freed-tokens notice in the SAME turn the clear
                # took effect, BEFORE the model generates against the trimmed
                # context — so it is aware its history was truncated this turn,
                # rather than learning about the loss one turn too late on a tool result.
                freed_now = context_manager.consume_tokens_freed()
                if freed_now > 0:
                    used, cap = context_manager.get_token_usage()
                    # Count the truncation as a real event here -- this branch is
                    # entered once per pass that actually cleared content, so it is
                    # the authoritative in-memory source for "how many times was
                    # context truncated".
                    ai = job.setdefault("agent_info", {})
                    ai["context_clear_count"] = ai.get("context_clear_count", 0) + 1
                    ai["context_tokens_freed_total"] = (
                        ai.get("context_tokens_freed_total", 0) + freed_now
                    )
                    # Log a durable meta-event into the message log so the plotter
                    # can count truncations straight from run_*_messages.jsonl,
                    # without a separate context_stats.json. It is NOT a chat turn
                    # (no 'role'), so it never reaches the model and the analysis
                    # turn/tool counters ignore it.
                    try:
                        tool_manager._append_message_log(
                            {
                                "_event": "context_truncation",
                                "turn": turn,
                                "tokens_freed": freed_now,
                            }
                        )
                    except Exception as e:
                        logger.warning(f"Failed to log context truncation event: {e}")
                    # Surface the freed-tokens notice in the SAME turn
                    # the clear took effect, but MERGE it into the trailing message
                    # instead of appending a standalone {'role':'user'} turn. The
                    # tail of current_messages is a tool/user message (a tool_result
                    # under the Anthropic protocol), so a separate user turn produced
                    # two consecutive user roles, which Bedrock/aws Claude channels
                    # reject ("roles must alternate"). Appending the marker to the
                    # last message's textual content keeps the role sequence valid
                    # while still informing the model.
                    notice = (
                        f"<system_warning>{freed_now} oldest tokens were just removed "
                        f"from your context to stay within the window. Token usage: "
                        f"{used}/{cap} tokens ({used*100//max(1,cap)}%); {cap-used} remaining."
                        f"</system_warning>"
                    )
                    current_messages = self._append_notice_to_last_message(
                        current_messages, notice
                    )
            else:
                current_messages = messages

            start_time = time.time()
            try:
                response = self.llm_client.generate_with_tools(
                    messages=current_messages,
                    tool_schemas=tool_schemas,
                )
            except Exception as e:
                logger.error(f"LLM call failed at turn {turn}: {e}")
                job["termination_reason"] = f"llm_error: {e}"
                break
            inference_time = time.time() - start_time

            reasoning_content = response.get("reasoning_content", "")
            content = response.get("content", "")
            tool_calls = response.get("tool_calls", [])
            signature = response.get("signature", None)
            reasoning_items = response.get("reasoning_items") or []
            extra = response.get("extra", {})

            assistant_msg = {
                "role": "assistant",
                "content": content,
            }
            if reasoning_content:
                assistant_msg["reasoning_content"] = reasoning_content
            if signature:
                assistant_msg["signature"] = signature
            # Raw reasoning items (with encrypted_content) used to replay the
            # model's reasoning chain on the next turn (GPT-5.x Responses API).
            # Opaque to everything except llm_client; not counted toward the
            # context-window budget and redacted from the message log.
            if reasoning_items:
                assistant_msg["reasoning_items"] = reasoning_items
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            assistant_msg["extra"] = {
                "inference_time": inference_time,
                **(extra or {}),
            }

            messages.append(assistant_msg)
            traj.append(assistant_msg)
            if context_manager:
                context_manager.add_message(assistant_msg)
            tool_manager._append_message_log(assistant_msg)

            if not tool_calls:
                consecutive_no_tool_calls += 1
                if consecutive_no_tool_calls >= max_no_tool_calls:
                    logger.warning(
                        f"Agent returned no tool calls {consecutive_no_tool_calls} times in a row; terminating."
                    )
                    job["termination_reason"] = (
                        "agent_idle: no tool calls for "
                        f"{consecutive_no_tool_calls} consecutive turns"
                    )
                    break
                nudge_msg = {
                    "role": "user",
                    "content": (
                        "You did not call any tool. You must call a tool to operate the "
                        "business and advance time. Please call a tool now. "
                        f"(warning {consecutive_no_tool_calls}/{max_no_tool_calls}: after "
                        f"{max_no_tool_calls} consecutive turns without a tool call the "
                        "episode will be terminated as a failure.)"
                    ),
                }
                messages.append(nudge_msg)
                traj.append(nudge_msg)
                if context_manager:
                    context_manager.add_message(nudge_msg)
                tool_manager._append_message_log(nudge_msg)
                continue

            consecutive_no_tool_calls = 0

            tool_call_infos = []
            for tc in tool_calls:
                func = tc.get("function", {})
                t_name = func.get("name", "")
                t_args_raw = func.get("arguments", "{}")
                if isinstance(t_args_raw, str):
                    try:
                        t_args = json.loads(t_args_raw)
                    except json.JSONDecodeError:
                        t_args = {}
                else:
                    t_args = t_args_raw
                tool_call_infos.append(
                    {
                        "tool_name": t_name,
                        "tool_args": t_args,
                        "tool_call_id": tc.get("id", str(uuid.uuid4())),
                    }
                )

            tool_responses = tool_manager.ask_code_exec(job, tool_call_infos)

            max_chars = job.get("agent_info", {}).get(
                "max_tool_response_chars", 64 * 1024
            )
            # The freed-tokens notice is now surfaced at the TOP of the turn,
            # before generation, so here we only emit the running
            # token-usage gauge appended to each tool response.
            for i, (info, resp) in enumerate(zip(tool_call_infos, tool_responses)):
                if len(resp) > max_chars:
                    tool_responses[i] = resp[:max_chars] + "\n... [truncated]"

                if context_manager:
                    used, cap = context_manager.get_token_usage()
                    warning = f"\n<system_warning>Token usage: {used}/{cap} tokens ({used*100//max(1,cap)}%); {cap-used} remaining</system_warning>"
                else:
                    warning = ""

                tool_msg = {
                    "role": "tool",
                    "content": tool_responses[i] + warning,
                    "tool_call_id": info["tool_call_id"],
                }
                messages.append(tool_msg)
                traj.append(tool_msg)
                if context_manager:
                    context_manager.add_message(tool_msg)
                tool_manager._append_message_log(tool_msg)

            if job.get("termination_reason"):
                logger.info(f'Episode terminated: {job["termination_reason"]}')
                break

            if turn >= max_turns:
                job["termination_reason"] = "max_turns_reached"
                break

        job["traj"] = traj
        tool_manager.snapshot_final_state(job)
        if not job.get("termination_reason"):
            job["termination_reason"] = "max_turns_reached"
        self._normalize_termination_reason(job)
        tool_manager.cleanup(job)
        tool_manager.close()

        return job

    @staticmethod
    def _append_notice_to_last_message(messages: List[Dict], notice: str) -> List[Dict]:
        """Append a system notice to the textual content of the LAST message
        rather than as a new standalone turn.

        The freed-tokens truncation notice used to be added as its own
        ``{'role': 'user'}`` message. The tail of the request is a tool/user
        message (a tool_result under the Anthropic protocol), so that produced
        two consecutive user roles, which Bedrock/aws Claude channels reject
        ("roles must alternate"). Merging the notice into the last message's
        string content keeps the role sequence valid. The notice text is left
        intact so it stays greppable in the message log (the plotter counts
        these markers to report context truncations).

        ``get_messages`` only ever yields messages whose ``content`` is a
        string in this codebase, so the common path simply concatenates. If a
        non-string content is ever encountered (or the list is empty), we fall
        back to a standalone user turn — only safe when the tail is not already
        a user message.
        """
        if not messages:
            return [{"role": "user", "content": notice}]
        last = messages[-1]
        content = last.get("content")
        if isinstance(content, str):
            sep = "\n\n" if content else ""
            merged = dict(last)
            merged["content"] = f"{content}{sep}{notice}"
            return messages[:-1] + [merged]
        # Non-string content (already in provider block form). Only append a
        # separate user turn if doing so does not create consecutive user roles.
        if last.get("role") != "user":
            return messages + [{"role": "user", "content": notice}]
        return messages

    @staticmethod
    def _normalize_termination_reason(job: Dict[str, Any]) -> None:
        normalize_termination_reason(job)

    def _build_default_job(self) -> Dict[str, Any]:
        context_prompt = CONTEXT_WINDOW_PROMPT.format(
            max_token_capacity=self.max_token_capacity,
            context_trigger=self.context_trigger,
            context_clear_at_least=self.context_clear_at_least,
            context_keep=self.context_keep_tool_use,
        )
        return build_ecommerce_job(
            max_turns=self.max_turns,
            max_day=self.max_day,
            initial_balance=self.initial_balance,
            daily_fee=self.daily_fee,
            tool_schemas=self.tool_schemas,
            system_prompt_suffix=context_prompt,
            run_index=self.run_index,
        )

    def _create_context_manager(self):
        from context_manager.base import ContextManager

        return ContextManager(
            tokenizer_path=self.tokenizer_path,
            max_token_capacity=self.max_token_capacity,
            config={
                "trigger": self.context_trigger,
                "clear_at_least": self.context_clear_at_least,
                "keep_tool_use": self.context_keep_tool_use,
            },
        )
