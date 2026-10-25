# Local Responses transport (M3C)

No live API request was made during implementation. Docker verification and
GitHub workflows are unchanged; **never add this key to the test job**.

From the repository root, install the agent's optional transport dependencies:

```sh
agent/.venv/bin/python -m pip install './agent[openai,test]'
agent/.venv/bin/python -m taskly_agent.openai_smoke
```

The second command is a credential-free dry run. It validates the complete
structured request, approved skill instructions, schema, model and token/cost
ceilings. It neither initializes a live client nor collects repository files.
The test suite uses the official SDK over `httpx2.MockTransport`; no tests use
real credentials or contact OpenAI.

## Integration and configuration

Use `demo_config(model=..., budget_usd=...)` and `async with
openai_provider(config) as boundary:`. Pass that boundary to the existing
generator/reviewer adapters. Use its `count_input_tokens` callable when assembling
ContextCollector (the existing boundary callback, not another tokenizer/API).
ExecutionController still owns per-run call limits, review and the single
bounded repair. Each transport invocation makes one SDK request and never
repairs/retries model or service errors itself.

The default is `gpt-5.4-mini-2026-03-17`; a separate priced nano snapshot is
allowlisted as an explicit alternative. Unknown models fail before invocation.
Nano is marked deprecated in the current catalog; check availability before
opting into it. Model access/account support has not been tested live.

Model settings, schemas, pricing and spending caps are trusted configuration,
never merged from PR metadata. Defaults: 30-second total provider deadline,
2,000 output tokens, two model calls, at most one repair, review disabled,
$0.05 reservation per invocation and $3 run/process spending ceilings. Token
checks use conservative UTF-8 byte counts plus framing allowance (including the
final strict schema/messages); they intentionally overestimate normal text and
can reject context that would fit a tokenizer. No paid counting endpoint is used.

The official SDK is pinned to 3.37.0, using
`AsyncOpenAI.responses.with_streaming_response.create` to read bounded JSON
without buffering arbitrary bodies. API SSE streaming is off. HTTPX2 is pinned
to 2.16.0. Request and HTTP retries are zero, redirects/proxies are disabled,
the endpoint is fixed, service tier is standard, and there are no tools,
background jobs, previous response IDs or conversations. `store=false` opts out
of Responses storage; it does not promise provider-wide zero data retention.

Transport DTOs have required nullable review fields and only portable strict
schema features. Original TestProposal/ReviewResult validation remains decisive.
Trusted task/skill bytes are checked against local code/catalog before becoming
developer-role instructions. Repository content and previous model/verifier
output remain user-role reference data. Role separation is not a guarantee of
model resistance to injection; path authorization and independent verification
remain necessary. No generated proposal is written or executed by the transport.

## Costs and failure behavior

Pricing checked 2026-10-25, standard USD per million tokens: mini $0.75 input /
$4.50 output; nano $0.20 / $1.25. Review current official prices before paid use.
Cached input is conservatively estimated at the full input rate. Actual API
input/output counts are recorded; reported reasoning tokens are a subset of
output tokens, not an extra charge. These are **estimated costs**, not invoices.

One shared InMemoryBudgetStore covers all factory sessions in this process.
The process ceiling may be lowered at initialization (maximum $3) but cannot
change/reset between sessions. A process gate rejects parallel calls before
cumulative reservation/network access. Known usage replaces its reservation exactly once,
even for refused, incomplete or unusable output. Missing/invalid usage retains
the full reservation and blocks subsequent spending. Closing/recreating a
session does not reset the ledger. There is no public reset method.

This is a conservative software guard, **not an OpenAI account billing hard
limit**. Restarted/other processes, direct SDK use and other applications are
outside it. Provider pricing/framing changes can invalidate estimates; reported
overages remain charged and block later calls. Set provider-side limits
separately. Only use the budgeted factory; raw OpenAITransport is an internal
ProviderBoundary implementation, not a standalone metered API.

## One paid smoke test, only after explicit authorization

Set `OPENAI_API_KEY` locally using a secret manager, or use hidden input in a
Bash shell (no key literal enters command history):

```bash
read -r -s -p 'OpenAI API key: ' OPENAI_API_KEY
export OPENAI_API_KEY
```

Only that variable supplies credentials. Do not use dotenv/config files or CLI
key arguments. Unset ambient SDK admin/org/project/webhook/custom-header/logging
overrides; they fail closed. Ambient base URL/proxy settings are ignored. The
transport never logs raw responses/errors and suppresses SDK request diagnostics
during invocation. Do not enable HTTP wire tracing around credentialed traffic.

After authorization, this exact command makes **one** synthetic review request,
with a 512-output-token cap and $0.05 request/run reservation, and prints only
numeric usage/cost. It does not read PR files, apply tests, invoke Docker, or
claim verification. It has no retry or repair loop:

```sh
agent/.venv/bin/python -m taskly_agent.openai_smoke --allow-paid
```

Without `--allow-paid` it remains a dry run. A live result/model quality and
account access remain unvalidated until that explicitly authorized smoke test.

Official references: [Python SDK](https://developers.openai.com/api/reference/python),
[strict Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses),
[mini model/pricing](https://developers.openai.com/api/docs/models/gpt-5.4-mini),
[nano model/pricing](https://developers.openai.com/api/docs/models/gpt-5.4-nano).
