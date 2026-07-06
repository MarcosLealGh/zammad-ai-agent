# Zammad AI Agent

![CI](https://github.com/MarcosLealGh/zammad-ai-agent/actions/workflows/ci.yml/badge.svg)

A conversational AI agent that acts as the resident expert on your [Zammad](https://zammad.org/) helpdesk — it knows your infrastructure (from a knowledge file you write) and checks the live system through the Zammad REST API before answering.

Built with the [Anthropic API](https://docs.claude.com) (tool use + streaming + prompt caching) and battle-tested against a real production Zammad deployment (~150 users).

## The problem

Operational knowledge about a ticketing system lives in two places that never talk to each other:

1. **Documentation** — topology, restart runbooks, group/field conventions, known issues
2. **Live state** — what the tickets, queues and server actually look like *right now*

When something happens ("the server was off this morning, we powered it on, looks fine now… right?"), a human has to mentally join both. This agent does that join.

## How it works

```
You:  "llegamos y el server estaba apagado, lo prendimos y está ok, igual analiza"

Agent:
  [→ consultando verificar_servidor...]     ← live API check
  [→ consultando obtener_resumen...]        ← live ticket stats
  "El servidor responde (latencia 12ms). Tras un reinicio, Elasticsearch
   tarda 1-2 min en indexar — si la búsqueda va lenta, es normal. Hay 3
   tickets nuevos sin asignar que llegaron mientras estaba caído: ..."
```

**Architecture:**

- **System prompt as knowledge base** — `system_prompt.md` holds your internal documentation (topology, runbooks, groups, custom fields, known issues). It's gitignored; a genericized `system_prompt.example.md` template ships with the repo. Served with **prompt caching** so the large context is billed once per session, not per message.
- **Four read-only tools over the Zammad REST API** — server health check, ticket listing with filters, single-ticket deep dive (with first-response / resolution time metrics), and aggregate statistics. All access goes through a `ZammadClient` that never writes or deletes.
- **Agentic loop with streaming** — the model decides when to call tools, results feed back into the conversation, responses stream token-by-token to the terminal.

## Install

```bash
pip install -e .        # or: pip install -e ".[dev]" for tests + linter
```

## Usage

```bash
cp .env.example .env    # then fill in, or export the variables directly
export ANTHROPIC_API_KEY="your_api_key"
export ZAMMAD_URL="https://your-zammad-server"
export ZAMMAD_TOKEN="your_api_token"     # Zammad: Profile → Token Access

cp system_prompt.example.md system_prompt.md
# edit system_prompt.md with your instance's real documentation

zammad-agent
```

## Design notes

- **No secrets or internal topology in the repo** — credentials come from environment variables; the instance-specific knowledge base is a local, gitignored file.
- **Secure by default** — TLS verification is **on**; internal self-signed deployments opt out with `ZAMMAD_VERIFY_SSL=false` (logged as a warning).
- **Read-only by design** — the agent can query but never mutate production tickets; the tool surface has no write/delete path.
- **Untrusted ticket content** — ticket bodies come from end users; the system prompt instructs the model to treat them as data to analyze, not instructions to follow (basic indirect-prompt-injection hygiene).
- **Prompt caching** (`cache_control: ephemeral`) on the system prompt — the knowledge base can be tens of KB; caching cuts input cost by ~90% on every turn after the first.
- **Defensive tool results** — API failures (server down, expired token, timeout) return structured diagnoses with suggested actions instead of raw exceptions, so the model can reason about the failure and guide the operator.
- **Testable client** — the `ZammadClient` methods are unit-tested against a mocked API, no live server needed.
- Code and agent responses are in Spanish (built for a Spanish-speaking operations team).

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest
```

## Related

- [`zammad-reporting-toolkit`](../zammad-reporting-toolkit) — the batch counterpart: extracts tickets to CSV and computes service metrics for monthly reports.

## License

MIT — see [LICENSE](LICENSE).
