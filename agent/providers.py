"""Provider presets for models_config.json.

The config file stays the only thing a user edits. This module fills in what a
provider implies, so an entry can be as short as

    "gpt-5": {"provider": "openai", "model_name": "gpt-5"}

and still know its endpoint, which environment variable holds the key, and
which request shape the client should use.

Five providers ship:

    openai              api.openai.com, OPENAI_API_KEY
    anthropic           api.anthropic.com native Messages API, ANTHROPIC_API_KEY
    google              generativelanguage.googleapis.com, GEMINI_API_KEY, via
                        Google's own OpenAI-compatible endpoint
    openrouter          openrouter.ai, OPENROUTER_API_KEY, reaches every family
                        through one OpenAI-compatible endpoint
    openai-compatible   anything else that speaks the OpenAI wire format:
                        vLLM, SGLang, Ollama, LM Studio, DashScope, a company
                        gateway. Requires an explicit base_url.

Keys are never written in the config. Either leave api_key out and let the
provider's standard variable supply it, or write "${MY_VAR}" to name your own.
An api_key given literally still works, which is why the loader warns about it
rather than refusing.
"""

import logging
import os
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)

# base_url: None means the SDK's own default is correct and we pass nothing.
PROVIDERS: Dict[str, Dict[str, Any]] = {
    "openai": {
        "base_url": None,
        "key_env": "OPENAI_API_KEY",
        "api_style": "chat",
    },
    "anthropic": {
        "base_url": None,
        "key_env": "ANTHROPIC_API_KEY",
        "api_style": "anthropic",
    },
    # Google's official OpenAI-compatible surface. Chosen over the native
    # generateContent protocol because it needs no separate request/response
    # translation, and Gemini's one hard requirement (replaying
    # thought_signature on tool calls) is honoured on this path too.
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "api_style": "chat",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "api_style": "chat",
    },
    "openai-compatible": {
        "base_url": None,  # must be supplied by the entry
        "key_env": "OPENAI_API_KEY",
        "api_style": "chat",
    },
}

DEFAULT_PROVIDER = "openai-compatible"

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
# a literal key looks like a secret rather than a placeholder
_SECRET_SHAPED = re.compile(r"^(sk-|sk-ant-|sk-or-|xai-|gsk_)[A-Za-z0-9_\-]{12,}$")


def expand(value: str, *, field: str = "", entry: str = "") -> str:
    """Resolve "${VAR}" against the environment; pass anything else through."""
    if not isinstance(value, str):
        return value
    m = _ENV_REF.match(value.strip())
    if not m:
        if _SECRET_SHAPED.match(value.strip()):
            logger.warning(
                "models_config.json: %s.%s holds a literal key. Prefer leaving it "
                "out, or writing ${YOUR_VAR}, so the file stays safe to commit.",
                entry or "<entry>",
                field or "api_key",
            )
        return value
    name = m.group(1)
    resolved = os.environ.get(name)
    if not resolved:
        raise ValueError(
            f'models_config.json entry "{entry}" wants {field or "api_key"} from '
            f"${{{name}}}, but that environment variable is unset. Export it, or "
            f"put the value in the config."
        )
    return resolved


def infer_provider(entry: Dict[str, Any]) -> str:
    """Guess the provider for an entry written before providers existed."""
    if entry.get("provider"):
        return str(entry["provider"]).strip().lower()
    base = (entry.get("base_url") or "").lower()
    if "openrouter.ai" in base:
        return "openrouter"
    if "api.anthropic.com" in base:
        return "anthropic"
    if "api.openai.com" in base:
        return "openai"
    if "generativelanguage.googleapis.com" in base:
        return "google"
    if base:
        return "openai-compatible"
    # no base_url at all: the model name is the only hint
    model = (entry.get("model_name") or "").lower()
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith(("gpt-", "o1", "o3", "o4")):
        return "openai"
    if model.startswith("gemini"):
        return "google"
    return DEFAULT_PROVIDER


def resolve(name: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Fill a config entry out into everything the client needs.

    Returns a copy carrying provider, model_name, base_url, api_key and
    api_style. Raises if the entry cannot reach an endpoint.
    """
    entry = dict(entry or {})
    provider = infer_provider(entry)
    if provider not in PROVIDERS:
        raise ValueError(
            f'models_config.json entry "{name}" names unknown provider '
            f'"{provider}". Known: {", ".join(sorted(PROVIDERS))}.'
        )
    preset = PROVIDERS[provider]

    base_url = entry.get("base_url") or preset["base_url"]
    if base_url:
        base_url = expand(base_url, field="base_url", entry=name)
    if provider == "openai-compatible" and not base_url:
        raise ValueError(
            f'models_config.json entry "{name}" uses provider '
            f'"openai-compatible", which needs an explicit base_url (your '
            f"server's /v1 endpoint)."
        )

    api_key = entry.get("api_key")
    if api_key:
        api_key = expand(api_key, field="api_key", entry=name)
    else:
        api_key = os.environ.get(preset["key_env"], "")
        if not api_key:
            raise ValueError(
                f'models_config.json entry "{name}" has no api_key, so it falls '
                f"back to ${preset['key_env']}, which is unset. Export that "
                f"variable or add api_key to the entry."
            )

    model_name = entry.get("model_name") or name
    return dict(
        entry,
        provider=provider,
        model_name=model_name,
        base_url=base_url,
        api_key=api_key,
        api_style=(entry.get("api_style") or preset["api_style"]),
    )
