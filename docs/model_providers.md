# Configuring models

`models_config.json` is the only file you edit. Pick an entry key and pass it to
`--model`:

```bash
export GEMINI_API_KEY=...
export OPENAI_API_KEY=sk-...   # the supplier NPC defaults to gpt-4o-mini
python run.py --model gemini-3.5-flash --max-days 10 --max-turns 50   # smoke test
python run.py --model gemini-3.5-flash                                # full 365-day episode
```

The shipped entries are the 18 models of the paper's leaderboard, each at the
reasoning effort it was evaluated with. Anything else needs one entry of your
own.

## Providers

Each provider is reached at its own documented endpoint. Nothing is inferred from
a model name, so a newly released model needs an entry here and no code change.

| provider | key variable | endpoint | reference |
|---|---|---|---|
| `google` | `GEMINI_API_KEY` | generativelanguage.googleapis.com | [OpenAI compatibility](https://ai.google.dev/gemini-api/docs/openai) |
| `openai` | `OPENAI_API_KEY` | api.openai.com | [Chat Completions](https://platform.openai.com/docs/api-reference/chat) |
| `anthropic` | `ANTHROPIC_API_KEY` | api.anthropic.com, native Messages API | [Messages](https://docs.claude.com/en/api/messages) |
| `openrouter` | `OPENROUTER_API_KEY` | openrouter.ai | [docs](https://openrouter.ai/docs) |
| `openai-compatible` | the variable you name | your `base_url` | your server |

The shipped `openai-compatible` entries reach Qwen on DashScope
(`DASHSCOPE_API_KEY`), GLM on Zhipu (`ZHIPU_API_KEY`), Kimi on Moonshot
(`MOONSHOT_API_KEY`) and DeepSeek (`DEEPSEEK_API_KEY`) — each at that vendor's
own public endpoint.

## Adding a model

Two fields are enough when the provider's endpoint is already known:

```json
"my-gemini":  { "provider": "google",    "model_name": "gemini-3.5-flash" },
"my-gpt":     { "provider": "openai",    "model_name": "gpt-5-mini" },
"my-claude":  { "provider": "anthropic", "model_name": "claude-opus-4-5-20251101" }
```

Anything that speaks the OpenAI wire format (vLLM, SGLang, Ollama, LM Studio, a
company gateway) needs a `base_url`:

```json
"local-vllm": {
  "provider": "openai-compatible",
  "model_name": "your-served-model-name",
  "base_url": "http://localhost:8000/v1",
  "api_key": "${LOCAL_API_KEY}"
}
```

Optional fields:

- `api_key` — omit it and the provider's standard variable supplies it; write
  `"${MY_VAR}"` to name your own.
- `effort` — `low` | `medium` | `high` | `xhigh` | `max`. Sent as
  `reasoning_effort` on the chat-completions path, and converted to a thinking
  token budget on the native Anthropic path. Omit it to request no effort at all.
  A runtime `MODEL_EFFORT` overrides every entry.
- `extra_body` — vendor-documented request-body fields, merged into every
  request. This is how a family's thinking switch is turned on without the client
  knowing anything about that family:

  ```json
  "glm-5.2-max": {
    "provider": "openai-compatible",
    "model_name": "glm-5.2",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "api_key": "${ZHIPU_API_KEY}",
    "extra_body": { "thinking": { "type": "enabled" } },
    "effort": "max"
  }
  ```

- `thinking_env` — the environment variable that turns on extended reasoning,
  currently `CLAUDE_THINKING`. A list is allowed if a model needs more than one.
- `api_style` — `chat` (default) or `responses` for the OpenAI Responses API.

Pick exact model ids, not moving aliases such as `gemini-flash-latest`: an alias
silently changes which model produced a result, which makes a run
irreproducible. Note also that a model appearing in a provider's model listing is
not proof it is callable — `gemini-2.5-pro` lists but returns 404 for new keys.

## Gemini and thought signatures

Gemini 3.x returns a `thought_signature` with every function call and **rejects
the next request** unless it is sent back:

```
400 INVALID_ARGUMENT: Function call is missing a thought_signature in
functionCall parts
```

The client carries the field through and replays it automatically, so no
configuration is needed. There is no way to opt out of the requirement —
`reasoning_effort` of `none`, `low` and `minimal` all still demand it — so any
client running Gemini 3.x in a tool-calling loop must do this. Gemini 2.5 does
not use signatures. See Google's
[thought signatures](https://ai.google.dev/gemini-api/docs/thought-signatures).

## The supplier NPC

`npc_tools` is a second, separate model: the supplier's role-play voice. It only
renders dialogue into natural language. Every price, concession and accept or
reject decision comes from the deterministic kernel, which the renderer cannot
override, so a small cheap model is the right choice and does not affect the
economics. It takes the same fields as any other entry.

## A private registry

For a local setup you do not want to commit, use `models_config.local.json`,
which is gitignored and takes precedence, or point at any path:

```bash
ECBENCH_MODELS_CONFIG=/path/to/my_models.json python run.py --model my-model
```

## Failure modes

- Unset key, wrong model id, or a key without access fails immediately rather
  than retrying, and names the entry to fix.
- Rate limits and transient 5xx retry 8 times with exponential backoff. For a
  long unattended run: `API_MAX_RETRIES=100 bash run.sh`.
