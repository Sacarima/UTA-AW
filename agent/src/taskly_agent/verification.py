"""Independent verifier adapter, fail closed until a real sandbox is validated.

Simulation exercises application, capture validation and parsing, but NEVER
returns available/passing VerificationEvidence. Counts in simulated reports
are parser test data, not executed Taskly results. There is no live mode, host
subprocess runner or structural 'secure=True' flag that enables real execution.
Future live provenance and sandbox validation require a separately reviewed
implementation; an enum/config change alone is insufficient.
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

from pydantic import ValidationError

from .application import ProposalApplier, WorkspaceCleanupError
from .contracts import CoverageEvidence, TestProposal, VerificationEvidence, VerificationIdentity
from .execution import ExecutionCapture, ExecutionLimits, SandboxRunner, UnavailableSandbox, execution_job
from .lifecycle import AdapterError, ExecutionPolicy, RunInput, proposal_digest
from .verification_reports import aggregate_reports, parse_capture


async def _drain(task):
    """Finish bounded preparation/cleanup even if cancellation is repeated."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


@asynccontextmanager
async def _workspace(applier, identity, proposal):
    cancellation = Event()
    manager = applier.apply(identity, proposal, cancellation=cancellation)
    entering = asyncio.create_task(asyncio.to_thread(manager.__enter__))
    try:
        result = await asyncio.shield(entering)
    except asyncio.CancelledError:
        # A cancelled to_thread await does not stop its worker. Reap preparation
        # and close its scope before allowing controller cancellation to finish.
        cancellation.set()
        try:
            await _drain(entering)
            await _drain(asyncio.create_task(asyncio.to_thread(manager.__exit__, None, None, None)))
        finally:
            raise asyncio.CancelledError() from None
    try:
        yield result
    finally:
        _, cancelled = await _drain(asyncio.create_task(asyncio.to_thread(manager.__exit__, None, None, None)))
        if cancelled:
            raise asyncio.CancelledError() from None


class VerifierAdapter:
    def __init__(self, applier: ProposalApplier, runner: SandboxRunner | None = None,
                 *, limits: ExecutionLimits | None = None):
        self.applier = applier
        self.runner = UnavailableSandbox() if runner is None else runner
        self.limits = ExecutionLimits.model_validate(ExecutionLimits() if limits is None else limits)

    async def verify(self, request: RunInput, proposal: TestProposal,
                     policy: ExecutionPolicy) -> VerificationEvidence:
        request = RunInput.model_validate(request)
        policy = ExecutionPolicy.model_validate(policy)
        proposal = TestProposal.model_validate(proposal, context={"content_limits": policy.content_limits})
        identity = VerificationIdentity(repository=request.pr.identity.repository, head_sha=request.pr.identity.head_sha,
            proposal_digest=proposal_digest(proposal), policy_digest=policy.digest)

        def unavailable(reason, outcome="incomplete"):
            return VerificationEvidence(state="unavailable", outcome=outcome, identity=identity,
                coverage=CoverageEvidence(state="unavailable", reason=reason), reason=reason)

        if request.pr.identity != self.applier.identity:
            return unavailable("identity_mismatch", "blocked")
        if policy.content_limits != self.applier.content_limits:
            raise AdapterError("invalid_input")
        mode = getattr(self.runner, "mode", "unavailable")
        if mode == "unavailable":
            return unavailable("sandbox_unavailable", "blocked")
        if mode != "simulation":
            return unavailable("unsafe_sandbox", "blocked")
        # Include every impacted stack, not just the language of model-selected
        # output files. Untrusted request.changed_files never selects commands.
        analysis = await asyncio.to_thread(self.applier.analyzer.inspect)
        required = {"backend": ("backend",), "frontend": ("frontend",),
                    "mixed": ("backend", "frontend")}.get(analysis.decision.classification, ())
        if analysis.decision.action != "generate" or not required:
            return unavailable("analysis_blocked", "blocked")
        limits = ExecutionLimits(**{**self.limits.model_dump(),
            "wall_seconds": min(self.limits.wall_seconds, policy.limits.command_timeout_seconds)})
        try:
            async with _workspace(self.applier, request.pr.identity, proposal) as applied:
                if applied.state != "applied":
                    return unavailable("application_failed", "blocked")
                if applied.identity != request.pr.identity or applied.proposal_digest != identity.proposal_digest:
                    return unavailable("application_identity_mismatch", "blocked")
                reports = []
                for suite in required:
                    job = execution_job(suite, identity)
                    capture = ExecutionCapture.model_validate(await asyncio.wait_for(
                        self.runner.execute(Path(applied.workspace), job, limits), limits.wall_seconds))
                    if capture.identity != identity or capture.suite != suite:
                        return unavailable("capture_identity_mismatch", "blocked")
                    reports.append(parse_capture(capture, limits))
                summary = aggregate_reports(tuple(reports), required)
            # Exiting the scope (and successful cleanup) is part of completion.
            return unavailable("simulation_only:" + summary.reason)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return unavailable("runner_timeout")
        except WorkspaceCleanupError:
            return unavailable("cleanup_failed", "blocked")
        except ValidationError:
            return unavailable("capture_invalid")
        except Exception:
            return unavailable("runner_failure")
