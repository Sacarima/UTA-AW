# Isolated GitHub verification

The `Taskly isolated verification` PR workflow uses a fresh **ubuntu-24.04
GitHub-hosted VM**, `contents: read`, and no other token permissions or API
secrets. Checkout/setup actions and both Docker base images use immutable
commit/content identifiers. Credentials are not persisted. No reports are
uploaded, evaluated as commands, or passed to a publishing workflow.

## Trust and execution

`trusted/` is the PR event's **base SHA**, including agent code, bundled skills,
Dockerfile, Python requirements and npm lockfile. `candidate/` is the exact
head SHA and supplies Git objects only. PR dependency changes never control
installation: dependencies are provisioned from the reviewed base manifests
before any PR code executes. Dependency upgrades need a reviewed base update;
all three candidate dependency manifests must match the provisioned copies
byte-for-byte before any PR code executes. Dependency changes (including
format-only edits) block verification rather than testing the wrong graph.
The Dockerfile-specific context allowlist excludes Git metadata, virtual
environments, application code and files beyond these provisioning inputs.

The image is built separately from reviewed inputs and then addressed by its
local `sha256:` image ID. Its labels bind it to the trusted base and profile.
Base images are Python 3.12.10 and Node 22.14.0 (compatible with Node >=22.12).
The image is Linux/amd64 only. Python packages use the fully pinned backend
requirements; npm uses lockfile integrity and disables lifecycle scripts.
Upstream registry/build trust remains a supply-chain assumption; these are
not reproducible-build or signed-image attestations.

`python -I -m taskly_agent.ci` invokes the agent logic, not PR scripts. It uses
`ProposalApplier.baseline()` to materialize raw Git blobs. Baseline identities
have a separate contract and **no proposal digest**. Both complete suites run
even for docs/test-only PRs. No fake test or no-op proposal is generated.

For future proposal callers, existing `VerifierAdapter` and
`ExecutionController` use `ProposalApplier.apply()`, the same fixed plans,
and the reviewed `DockerSandbox` implementation. Merely claiming `mode=docker`
on another runner does not enable real evidence. Simulators remain unavailable.

The transport requires Linux, cgroup v2 and builtin seccomp. Each container has:

- Non-root UID/GID 10001, read-only image, all capabilities dropped, no privilege
  escalation, private PID/IPC/cgroup namespaces, and `network=none`.
- One CPU quota, inherited per-process CPU-time limit, memory+swap cap, PID and
  descriptor caps, bounded tmpfs storage, and host wall deadline.
- Only tmpfs `/workspace`, `/reports` and `/tmp`; no bind mounts, Docker socket,
  host repository/home, credentials, host environment or network installation.
- Disabled Docker log storage. Child output is bounded/discarded; only bounded
  structured reports are transferred. CLI output is independently bounded.

Before PR code runs, a trusted probe checks its actual UID, capabilities,
seccomp/no-new-privileges, cgroup memory/CPU/PID limits, CPU rlimit, network
interfaces, writable scratch, immutable image and absence of credential env
variables/socket. Docker inspect independently validates the created settings.
Failure means no available evidence. A running container is force-removed and
absence checked on success, exception, timeout and cancellation before the
host workspace scope ends. Daemon failure or lost cleanup confirmation blocks
verification; VM teardown is the last resort, not a successful cleanup claim.

Workspace transfer is a bounded stdin archive with explicit regular-file-only
extraction, not a host mount or unrestricted tar extraction. Reports live in a
separate tmpfs, never inside the transferred source. No-follow, regular-file,
single-link and byte limits apply to report capture. Backend invokes isolated
Python and an explicitly loaded pytest-cov plugin; frontend invokes the pinned
Vitest entrypoint directly with one worker. Project coverage/test configs apply.

## Evidence limits

Fresh single-use host receipts bind capture bytes to the expected job, pinned
head/proposal/policy identity and validated image invocation, after termination
and cleanup. Stale/altered/foreign receipts and inconsistent reports cannot pass.
Parsers check testcase counts, runner exits, coverage file totals and required
mixed suites. Missing metrics are unavailable, never 100%.

**Receipts authenticate capture provenance, not the truth of adversarial test
code.** Arbitrary Python/JavaScript inside a sandbox can manipulate its reporter,
exit behavior or coverage configuration. Containers share a kernel; kernel or
daemon exploits are not prevented by this implementation. The approved runtime
assumes a patched trusted Docker daemon on a disposable hosted VM. These results
are CI observations, not cryptographic proof of behavioral coverage, increased
coverage, or a security attestation. There is no privileged artifact consumer.
The existing execution-policy digest binds the controller policy; image/profile
identity is checked separately by the transport, not included in that digest.

GitHub can run a PR-modified workflow definition. This file's safeguards cannot
enforce repository-wide Actions policy against a malicious replacement workflow.
Review workflow changes and use repository/organization Actions restrictions.
Do not reference secrets in this job or grant fork runs write tokens. No
self-hosted runners or `pull_request_target` is used. The separate M5
[PR agent follower](PR_AUTOMATION.md) uses trusted `workflow_run` orchestration
and GitHub metadata; it never consumes artifacts/code produced by this PR job.

## First GitHub-hosted validation

1. Review and land this implementation in the trusted target branch yourself.
   A PR whose base lacks these modules intentionally cannot bootstrap trusted
   orchestration from its own head. Nothing has been committed/pushed here.
2. Open a harmless docs-only PR against that branch, then push a small update to
   exercise `opened` and `synchronize`. The baseline still runs both suites.
3. Inspect the job: trusted and candidate SHAs must equal the event base/head;
   provisioning uses only `trusted/`; the built image ID must match the summary.
   The sandbox probe must succeed before backend/frontend execution.
4. Confirm the summary has genuine per-suite counts and integer statement,
   branch and line coverage. Missing reports, dependency resolution failure,
   unsupported daemon/cgroup settings or failed tests must make the job red.
5. In a disposable PR, add a failing backend assertion and a failing frontend
   assertion. Each must produce a red check with genuine failing counts. Also
   try a test exceeding the wall deadline; confirm the check fails and containers
   do not remain. Never test host escapes or load credentials into the job.

No live Docker or GitHub-hosted run was available during local M3A development.
The first hosted run must validate image build, shared-library availability,
dependency availability, actual resource probe, both real suites and cleanup.
Local deterministic tests do not substitute for that run. No host-test fallback
exists. Useful control-plane diagnostics (no PR report text):

```
docker info --format '{{json .SecurityOptions}} {{.CgroupVersion}}'
docker image inspect sha256:THE_BUILT_IMAGE_ID
```

Official references: [GitHub secure use](https://docs.github.com/en/actions/reference/security/secure-use),
[GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
[Docker security](https://docs.docker.com/engine/security/),
[Docker runtime limits](https://docs.docker.com/engine/containers/run/).
