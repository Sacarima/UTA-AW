# Backend/frontend end-to-end execution

The trusted entry point is `python -I -m taskly_agent.end_to_end`. Install it
from reviewed orchestration code, independently of the candidate checkout.
The CLI reads candidate Git objects only; it never imports candidate Python.
It accepts identity/configuration/image parameters, with no commands, test-path
arguments, tools, simulation mode, publisher, or workflow changes.

Execution uses the existing analyzer, context collector and bundled skills,
GeneratorAdapter, budgeted `openai_provider` session, ExecutionController,
ProposalApplier, DockerSandbox and VerifierAdapter. Review is deliberately
disabled for bounded single-stack generation. Frontend-only and backend-only
generation are supported; mixed generation is blocked. Irrelevant changes skip
without opening a provider session. Running
without `--allow-paid` performs identity/analysis checks only and cannot spend.
The CLI rejects images whose trusted revision differs from the pinned base.

After explicit opt-in, a trusted Docker smoke probe runs before the provider
opens. This probe is not a test baseline or verification evidence. The controller
allows at most two generation calls, with the second only after available,
independent failed verification. Missing evidence, unavailable execution, invalid
proposals and provider errors do not trigger repair. The verifier applies each
proposal to a fresh pinned snapshot, executes the fixed pytest or Vitest suite in Docker,
checks identities/one-use receipts, and disposes the workspace. No generated
file is copied to the candidate or retained after verification.

Docker controls and image provisioning remain those documented in
[CI_EXECUTION.md](CI_EXECUTION.md). Linux, cgroup v2, builtin seccomp, a trusted
local Docker daemon and the immutable reviewed image are mandatory. There is no
host test fallback. The Docker child receives a fixed environment with no API
key or GitHub credentials, no network, no host mounts and no model tools.
Provisioning the trusted image may use network access; candidate execution cannot.
Candidate dependency manifests must match those provisioned in the image; the
trusted launcher checks this before executing tests and fails closed otherwise.

## Reports and cost semantics

Stdout is a single typed JSON report; stderr is a concise summary. Both exclude
prompts, source, proposal contents/rationale, model findings, request IDs, raw
runner output and exception messages. Reports include pinned PR identity,
classification, final generated paths, independent evidence identity, test
counts, integer coverage fractions, controller model-call reservations, repair
count, reported token totals, estimated reported cost, conservative accounted
cost and unknown-cost count. Unavailable counts/coverage remain null.
Paths mean validated proposals, not proof of application: application may fail.
The evidence proposal digest identifies the verified proposal. A later failed
repair can leave evidence for the earlier proposal; overall success is then
impossible. Provider-session cleanup failure preserves existing evidence and
costs while marking the workflow incomplete. Fixed reason codes avoid publishing
untrusted diagnostics; detailed test failure output is intentionally unavailable.

The controller reserves a model call before the adapter collects context, so its
call count/cost can conservatively include a rejected pre-provider attempt.
Per-run and cumulative ledgers measure the same calls at separate boundaries;
their totals must not be added. Reliable usage replaces each reservation once;
unknown usage retains the reservation and blocks further provider spending.
The cumulative ledger is process-local and does not survive restarts. This is
an estimated software spending guard, not an account-level billing cap. Consult
[OPENAI_TRANSPORT.md](OPENAI_TRANSPORT.md) for transport limits and price-table
assumptions; review the existing model availability/pricing before live use.

No baseline coverage run is performed by this CLI. Its coverage is the resulting
suite's observation only; **no coverage improvement is claimed**. A comparable,
independently measured baseline would be required for that claim. Sandbox
receipts authenticate invocation/capture correlation, not the honesty of
adversarial test/reporting code. Containers share the host kernel. Review source
eligibility before sending it to a provider: credential detection is heuristic,
and prompt separation does not guarantee resistance to prompt injection.

## One live demonstration, only after explicit authorization

These steps are a procedure for a future trusted disposable Linux machine;
they were not executed as a live model/Docker demonstration during implementation.

1. Obtain approval for **one CLI run, at most two paid requests**, with a USD
   0.10 run/process ceiling. Review the PR source for secrets and select a small
   backend-only behavior change with a deterministic unit-test target. An
   agent-only or documentation-only PR will intentionally skip generation.
   Pin full base/head SHAs from the trusted PR event; have both objects already
   present locally. Do not fetch refs derived from untrusted input.
2. Use a reviewed base checkout for image provisioning and a separately reviewed
   agent installation. The candidate repository is data only. The base checkout
   must include `agent/sandbox/{Dockerfile,Dockerfile.dockerignore,launch.py}`,
   `agent/src`, `backend/requirements.txt`, and both frontend package manifests.
   Check that all three dependency manifests are unchanged between base/head.
   Set the following trusted values (replace every placeholder):

   ```bash
   TRUSTED=/absolute/path/to/reviewed-base-checkout
   ORCHESTRATION=/absolute/path/to/reviewed-M4A-checkout
   CANDIDATE=/absolute/path/to/candidate-git-repository
   BASE=FULL_PINNED_BASE_SHA
   HEAD=FULL_PINNED_HEAD_SHA
   PR_NUMBER=123
   DEMO_DIR=$(mktemp -d)
   python3.12 -m venv "$DEMO_DIR/venv"
   "$DEMO_DIR/venv/bin/python" -m pip install "$ORCHESTRATION/agent[openai]"
   test "$(git -C "$TRUSTED" rev-parse HEAD)" = "$BASE"
   # Review that TRUSTED is clean before provisioning; do not build from CANDIDATE.
   git -C "$TRUSTED" status --short
   /usr/bin/docker build --platform linux/amd64 \
     --build-arg "TRUSTED_REVISION=$BASE" --iidfile "$DEMO_DIR/image.id" \
     --file "$TRUSTED/agent/sandbox/Dockerfile" "$TRUSTED"
   "$DEMO_DIR/venv/bin/python" -I -c \
     'from taskly_agent.openai_transport import demo_config; print(demo_config(budget_usd=0.10).model_dump_json(indent=2))' \
     > "$DEMO_DIR/config.json"
   ```

3. With credentials still unset, run the CLI command below **without**
   `--allow-paid`. Confirm the pinned identity and backend classification, with
   `paid_execution_not_authorized` and no model calls. This intentionally exits
   nonzero; it does not probe Docker or contact OpenAI.
4. After the explicit paid-run approval, read the key with hidden input. Do not
   put it in a command argument, config file, repository, Docker build argument
   or shell history. Run once from outside the candidate tree with a minimal
   environment (no GitHub credentials or ambient SDK overrides):

   ```bash
   read -r -s -p 'OpenAI API key: ' OPENAI_API_KEY
   printf '\n'
   cd "$DEMO_DIR"
   env -i PATH=/usr/bin:/bin HOME="$DEMO_DIR" OPENAI_API_KEY="$OPENAI_API_KEY" \
     "$DEMO_DIR/venv/bin/python" -I -m taskly_agent.end_to_end \
     --repository "$CANDIDATE" --name Sacarima/UTA-AW --number "$PR_NUMBER" \
     --base "$BASE" \
     --head "$HEAD" \
     --trusted-revision "$BASE" \
     --image "$(cat "$DEMO_DIR/image.id")" --config "$DEMO_DIR/config.json" \
     --allow-paid > "$DEMO_DIR/report.json" 2> "$DEMO_DIR/summary.txt"
   RUN_STATUS=$?
   unset OPENAI_API_KEY
   printf 'CLI exit status: %s\n' "$RUN_STATUS"
   cat "$DEMO_DIR/summary.txt"
   ```

5. Review `"$DEMO_DIR/report.json"` (stdout) and `"$DEMO_DIR/summary.txt"`
   (stderr): available independent evidence must match the pinned
   head and policy/proposal digests, counts/coverage must be present for success,
   calls must be at most two, repairs at most one, and estimated accounted cost
   within the approved ceiling. Check the original repository status against
   its pre-run status. A nonzero exit or unavailable evidence is not a successful
   demonstration. Retain the safe report for review; do not automatically retry,
   publish tests, commit, push, or infer a coverage increase.

Automated integration tests mock both HTTP and the Docker daemon. Their passing
counts validate wiring and failure boundaries, not live model quality, account
access, or real Taskly execution. Those remain for the authorized demonstration.
