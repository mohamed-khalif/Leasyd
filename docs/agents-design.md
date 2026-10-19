# AI agents: sessions, transcripts and silent failures

Status: **approved 2026-10-19; building.** Steps 1–2 of the agent-observability gap (step 3, AI scoring, and step 4, prompt replay, come later). Built on what Leasyd already has: OpenTelemetry ingest, columnar Parquet on S3, serverless query workers, PromQL alerts.

## What a customer gets

- **AI agents page** (`#/agents`), under Applications:
  - **Sessions:** every agent conversation in the time range, newest first: agent, turns, tool calls, tokens, cost, errors, duration. Search by session ID; filter to sessions with problems.
  - **Session drawer:** the conversation as a transcript (user, assistant, tool call with arguments and result, errors) with links to each trace, plus tokens and cost per model.
  - **Issues:** silent failures found from the traces with no setup and no AI cost:
    - a tool failed (by tool), and whether the agent still answered as if it worked;
    - an LLM call failed (by model);
    - empty replies (0 output tokens);
    - loops (the same tool called 5+ times in one trace).

    Each issue shows how many sessions it hit and example sessions.
- **Alert templates:** "agent tool errors" and "LLM call errors", using the existing alert rules (Slack, email, webhook).
- **Instrumentation:** anything that follows OpenTelemetry's GenAI conventions (`gen_ai.*`): the OpenTelemetry SDKs for OpenAI / Anthropic / Bedrock / Vertex, OpenLIT, Traceloop/OpenLLMetry, Vercel AI SDK, Pydantic AI, LangChain via OpenLLMetry. Session IDs from `gen_ai.conversation.id`, `session.id` or `langfuse.session.id`.

## Architecture (the efficiency decisions)

**1. Agent fields become their own Parquet columns, at compaction.** Agent spans carry whole prompts and replies in their attributes, which are one map column. Reading tokens out of that map would read every prompt. So compaction (and the fast lane, same SQL) lifts eight small fields into columns:

| Column | From (first present) | Type |
|---|---|---|
| `session_id` | `gen_ai.conversation.id`, `session.id`, `langfuse.session.id` | VARCHAR |
| `gen_ai_operation` | `gen_ai.operation.name` (chat, execute_tool, invoke_agent, …) | VARCHAR |
| `gen_ai_model` | `gen_ai.response.model`, `gen_ai.request.model` | VARCHAR |
| `gen_ai_agent` | `gen_ai.agent.name` | VARCHAR |
| `gen_ai_tool` | `gen_ai.tool.name` | VARCHAR |
| `gen_ai_input_tokens` | `gen_ai.usage.input_tokens` (older: `prompt_tokens`) | BIGINT |
| `gen_ai_output_tokens` | `gen_ai.usage.output_tokens` (older: `completion_tokens`) | BIGINT |
| `gen_ai_cost` | `gen_ai.usage.cost` (OpenLIT and others report it) | DOUBLE |

- **Storage cost:** NULL on every non-agent span, which Parquet stores in almost nothing.
- **Old files:** they lack the columns. Readers already use `union_by_name`, so these read as NULL; no migration.

**2. Every list and issue is an aggregate query reading only those columns.**
- Query workers read Parquet in place, fetching just the needed columns over S3 range requests. So the Agents page reads a few bytes per span, never the prompts.
- No new store, pipeline or endpoint: it uses the existing `/v1/app/query`.
- **Four queries per page load:** sessions, tool and LLM errors, empty replies, loops. Each is grouped by trace (and session), so a trace whose child spans lack the session ID still joins its session: the app merges a trace's groups.

**3. A transcript reads full spans for one session only.**
- `session_id` gets a per-file bloom filter, like `trace_id`, and the query API's `match` accepts it.
- So opening a session reads only the files that can contain it, then its spans in full (attributes and events hold the messages).

**4. Cost.**
- A reported `gen_ai.usage.cost` is used as is.
- Otherwise the app estimates it from tokens × a list-price table per model (`lib/llmPrices.ts`), shown with "≈".
- An unknown model shows tokens without a cost. Prices are not stored, so a price fix applies to history.

**5. Issues need no AI and no stored state.** They're computed from the columns at view time, for the range on screen. Alerting reuses PromQL over spans, with attribute labels:

```
sum by ("gen_ai.tool.name") (increase(leasyd.spans{"gen_ai.operation.name"="execute_tool", status_code="ERROR"}[5m])) > 0
```

## Transcript rules (per span, oldest first)

- **Chat / LLM spans:**
  - **Where the messages come from**, first present:
    - `gen_ai.input.messages` / `gen_ai.output.messages` (current convention, JSON);
    - events `gen_ai.user.message`, `gen_ai.assistant.message`, `gen_ai.tool.message`, `gen_ai.choice`;
    - `gen_ai.prompt.N.*` / `gen_ai.completion.N.*` (OpenLLMetry).
  - **No repeats:** each request repeats the history, so a message already shown is not shown again.
- **Tool spans:** the tool, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, status and duration.
- **Errors:** a span with status ERROR is marked, with its message.
- **Size:** long texts are cut at 4,000 characters with "show all".

## Build plan

| Step | What |
|---|---|
| A1 | Engine: promoted columns in compaction and the fast lane; `session_id` bloom and `match`; API columns; tests |
| A2 | Demo agent traffic (`services/demo/agents.py`: a support agent with tools, sessions, failures); the engine mock answers JSON queries for agent pages |
| A3 | Web: Agents page (sessions, stats, filters) and the session drawer (transcript) |
| A4 | Web: Issues, alert templates, Get started, docs |

## Not in this step

- Sentiment, classifiers and LLM judges (step 3).
- Prompt versions and replay (step 4).
- "Fix in Claude Code/Cursor" links, Langfuse SDK endpoint.
- Agent fields in PromQL as columns (alerts read the attribute map; fine at alert windows of minutes).
