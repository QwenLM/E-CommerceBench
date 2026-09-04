# External agent runtimes

E-Commerce Bench can expose one episode over a small HTTP protocol so an agent
framework can own planning, memory, and model calls without changing benchmark
tools, environment dynamics, logging, or scoring.

The benchmark owns the environment; the external runtime owns the policy.
`ExternalAgentSession` delegates every submitted action to the same
`EcommerceToolManager.ask_code_exec` path used by the built-in agent.

## Start a session

Set the supplier NPC key as for a normal run, then start the adapter:

```bash
export OPENAI_API_KEY=sk-...
python external_agent_server.py \
  --token local-development-token \
  --log-dir log/external-agent
```

The server binds to `127.0.0.1:8765` by default and prints one JSON readiness
line. Omit `--token` to generate a random bearer token. Use `--host` and `--port`
to change the listener; do not expose it on an untrusted network without a
transport security layer.

## Protocol

Every endpoint except `/health` requires:

```text
Authorization: Bearer <token>
```

JSON requests must include `Content-Length`; chunked request bodies are not
supported by the standard-library HTTP adapter.

### Read the benchmark task

```http
GET /v1/session
```

The response contains the benchmark task messages, complete tool schemas,
parallel-call capability, turn limit, and episode configuration. Pass the
messages and tools to the external agent unchanged. Runtime-specific context
management instructions used by the built-in model loop are intentionally
excluded because the external runtime owns its own context management.

### Submit one agent turn

```http
POST /v1/actions
Content-Type: application/json

{
  "content": "I will inspect the beauty market first.",
  "reasoning_content": "Compare categories before opening a store.",
  "tool_calls": [
    {
      "id": "call_1",
      "name": "market_search",
      "arguments": {"store_type": "beauty"}
    }
  ]
}
```

`arguments` may be a JSON object or a JSON-encoded string. `content` and
`reasoning_content` are optional strings retained in benchmark logs. A request
represents one agent turn and may contain multiple tool calls. Calls execute
sequentially through the canonical tool manager, preserving its batching and
day-advance semantics.

The response maps each result back to its call id:

```json
{
  "tool_responses": [
    {"tool_call_id": "call_1", "content": "{...}"}
  ],
  "turn": 1,
  "done": false,
  "termination_reason": null,
  "termination_detail": null
}
```

`content` is the exact tool-response string produced for the built-in agent.
Append each result to the external agent conversation as a tool message. Continue
until `done` is true.

### Finish early

```http
POST /v1/finish
```

This finalizes an unfinished episode as `env_terminated`, preserves
`external_runtime_closed` as the termination detail, and returns the same
payload as `GET /v1/result`. The server remains available for result retrieval.

### Read final metrics

```http
GET /v1/result
```

This endpoint returns `409 Conflict` while the episode is running. After
termination it returns the canonical termination fields, reward metadata, final
state, and external turn count.

## Python embedding

Framework adapters that run in the same process can skip HTTP:

```python
from agent.external_runtime import ExternalAgentSession

session = ExternalAgentSession(log_dir="log/my-runtime")
descriptor = session.descriptor()
result = session.act([
    {"id": "call_1", "name": "check_balance", "arguments": {}}
])
session.close()
```

Call `close()` in a `finally` block. Closing an unfinished episode records an
`env_terminated` result with `external_runtime_closed` as its detail.

## Isolation

The protocol prevents an external runtime from receiving Python environment
objects through the adapter. Fair execution still requires deploying the agent
where it cannot read this repository's hidden data or saved state. A container,
VM, or dedicated OS account is the enforcement boundary; a prompt telling an
agent not to inspect files is not one.
