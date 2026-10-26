"""Trusted backend-only orchestration. Run the installed, reviewed package.

No injected commands, model-selected tools, publication, or simulation CLI exists.
The controller owns per-run accounting; the provider owns cumulative accounting;
the verifier owns application, Docker execution, evidence and workspace cleanup.
Only the public projection below may be printed, never ExecutionReport itself.
"""

import argparse
import asyncio
import hashlib
from pathlib import Path
import sys
from typing import Literal

from .adapters import GeneratorAdapter
from .analysis import GitChangeAnalyzer
from .application import ProposalApplier
from .config import AgentConfig, ConfigurationError, parse_config
from .context import ContextCollector
from .contracts import (
    Classification, Contract, Count, CoverageMetrics, EstimatedUSD, PRContext,
    PRIdentity, RunOutcome, TestCounts, VerificationIdentity,
)
from .controller import ExecutionController
from .docker_runner import DockerSandbox
from .execution import BaselineIdentity, ExecutionLimits
from .git_changes import GitReader, validate_commit
from .lifecycle import ExecutionReport, RunInput, accounted_cost
from .openai_transport import openai_provider
from .verification import VerifierAdapter


class UsageSummary(Contract):
    input_tokens: Count | None
    output_tokens: Count | None
    estimated_reported_cost_usd: EstimatedUSD
    accounted_cost_usd: EstimatedUSD
    unknown_cost_calls: Count


class PublicReport(Contract):
    """No source, proposal content/rationale, raw diagnostics, or request IDs."""

    identity: PRIdentity | None = None
    classification: Classification | None = None
    outcome: RunOutcome
    reason: str
    generated_paths: tuple[str, ...] = ()
    verification_state: Literal["available", "missing", "unavailable"] = "unavailable"
    verification_outcome: RunOutcome | None = None
    verification_identity: VerificationIdentity | None = None
    verification_reason: str = "not_executed"
    tests: TestCounts | None = None
    coverage_state: Literal["available", "missing", "unavailable"] = "unavailable"
    coverage: CoverageMetrics | None = None
    model_calls: Count = 0
    repairs: Count = 0
    usage: UsageSummary | None = None


_VERIFICATION_REASONS = frozenset({
    "aggregate_passed", "aggregate_failed", "aggregate_incomplete", "aggregate_blocked",
    "identity_mismatch", "sandbox_unavailable", "unsafe_sandbox", "analysis_blocked",
    "application_failed", "application_identity_mismatch", "capture_identity_mismatch",
    "execution_not_authenticated", "execution_incomplete", "runner_timeout",
    "cleanup_failed", "capture_invalid", "runner_failure",
})


def public_report(identity: PRIdentity, classification: Classification,
                  execution: ExecutionReport) -> PublicReport:
    """Project controller data without publishing any untrusted narrative."""
    evidence = execution.result.verification
    usage = execution.result.usage
    calls = execution.model_call_accounting
    return PublicReport(
        identity=identity, classification=classification, outcome=execution.result.outcome,
        reason=execution.reason_code,
        generated_paths=tuple(change.path for change in execution.proposal.changes) if execution.proposal else (),
        verification_state=evidence.state if evidence else "unavailable",
        verification_outcome=evidence.outcome if evidence else None,
        verification_identity=VerificationIdentity(**{**evidence.identity.model_dump(), "artifact_refs": ()})
            if evidence and evidence.identity else None,
        verification_reason=(evidence.reason if evidence.reason in _VERIFICATION_REASONS else "diagnostic_unavailable")
            if evidence else "not_executed",
        tests=evidence.tests if evidence else None,
        coverage_state=evidence.coverage.state if evidence else "unavailable",
        coverage=evidence.coverage.metrics if evidence else None,
        model_calls=execution.model_calls, repairs=execution.repair_attempts,
        usage=UsageSummary(
            input_tokens=sum(item.input_tokens for item in usage) if len(usage) == len(calls)
                and all(item.input_tokens is not None for item in usage) else None,
            output_tokens=sum(item.output_tokens for item in usage) if len(usage) == len(calls)
                and all(item.output_tokens is not None for item in usage) else None,
            estimated_reported_cost_usd=float(sum(item.reported_cost_usd or 0 for item in calls)),
            accounted_cost_usd=float(accounted_cost(calls)),
            unknown_cost_calls=sum(item.reported_cost_usd is None for item in calls)),
    )


def validate_config(config: AgentConfig) -> AgentConfig:
    config = AgentConfig.model_validate(config)
    config.require_live_budget()
    if config.review_enabled or config.execution_policy.limits.max_model_calls > 2:
        raise ConfigurationError("Backend demonstration requires review disabled and at most two calls")
    return config  # Existing policy already caps repairs at one.


async def run(repository: Path, identity: PRIdentity, image: str, trusted_revision: str,
              config: AgentConfig, *, allow_paid: bool = False, patch_directory: Path | None = None) -> PublicReport:
    """Trusted Python entry point; opt-in is required even with live config.

    Preflight is a trusted runtime smoke probe, not a baseline test run or test
    evidence. Only VerifierAdapter may produce generated-proposal evidence.
    """
    classification = None
    execution = None
    phase = "invalid_input"
    try:
        identity = PRIdentity.model_validate(identity)
        config = validate_config(config)
        trusted_revision = validate_commit(trusted_revision)
        if trusted_revision != identity.base_sha:
            raise ValueError("Trusted image must match pinned base")
        runner = DockerSandbox(image, trusted_revision)
        if type(runner) is not DockerSandbox or runner.mode != "docker":
            raise ValueError("Real Docker execution required")
        analyzer = GitChangeAnalyzer(GitReader(repository), base_sha=identity.base_sha, head_sha=identity.head_sha)
        phase = "analysis_unavailable"
        analysis = await asyncio.to_thread(analyzer.inspect)
        classification = analysis.decision.classification
        if analysis.decision.action != "generate":
            return PublicReport(identity=identity, classification=classification,
                outcome="skipped" if analysis.decision.action == "skip" else "blocked",
                reason="analysis_skipped" if analysis.decision.action == "skip" else "analysis_blocked")
        if classification != "backend":
            return PublicReport(identity=identity, classification=classification,
                outcome="blocked", reason="backend_only")
        if not allow_paid:
            return PublicReport(identity=identity, classification=classification,
                outcome="blocked", reason="paid_execution_not_authorized")
        limits = ExecutionLimits(memory_bytes=1_073_741_824,
            wall_seconds=min(60, config.execution_policy.limits.command_timeout_seconds))
        preflight = BaselineIdentity(repository=identity.repository, head_sha=identity.head_sha,
            policy_digest=config.execution_policy.digest,
            baseline_digest=hashlib.sha256(b"taskly-runtime-probe-v1\0" + identity.head_sha.encode()).hexdigest())
        phase = "sandbox_unavailable"
        await runner.validate(limits, preflight)
        phase = "provider_session_unavailable"
        async with openai_provider(config) as provider:
            collector = ContextCollector(analyzer, provider.count_input_tokens)
            applier = ProposalApplier(analyzer, identity=identity,
                content_limits=config.execution_policy.content_limits)
            controller = ExecutionController(analyzer=analyzer,
                generator=GeneratorAdapter(collector, provider),
                verifier=VerifierAdapter(applier, runner, limits=limits))
            execution = await controller.run_configured(
                RunInput(pr=PRContext(identity=identity, changed_files=())), config)
        if patch_directory is not None and execution.result.outcome == "passed":
            from .patches import export_verified_patch
            from .verification import _drain
            phase = "patch_export_unavailable"
            exporting = asyncio.create_task(asyncio.to_thread(
                export_verified_patch, applier, execution, config.execution_policy, patch_directory))
            try:
                await asyncio.shield(exporting)
            except asyncio.CancelledError:
                await _drain(exporting)
                raise
        return public_report(identity, classification, execution)
    except asyncio.CancelledError:
        phase = "cancelled"
    except Exception:
        pass  # Never print exception messages, repository data or provider bodies.
    if execution is not None:
        # Closing a provider session may fail after independent verification.
        report = public_report(identity, classification, execution)
        return PublicReport(**{**report.model_dump(), "outcome": "incomplete", "reason": phase})
    return PublicReport(identity=identity if isinstance(identity, PRIdentity) else None,
        classification=classification, outcome="incomplete" if phase == "cancelled" else "blocked", reason=phase)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Trusted backend AI generation with independent Docker verification")
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--number", type=int, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--trusted-revision", required=True)
    parser.add_argument("--image", required=True, help="Local immutable sha256 image ID")
    parser.add_argument("--config", type=Path, required=True, help="Trusted AgentConfig JSON, no credentials")
    parser.add_argument("--allow-paid", action="store_true", help="Explicitly authorize bounded paid generation")
    args = parser.parse_args(argv)
    try:
        identity = PRIdentity(repository=args.name, number=args.number, base_sha=args.base, head_sha=args.head)
        with args.config.open("rb") as stream:
            data = stream.read(65_537)
        if len(data) > 65_536:
            raise ValueError
        config = parse_config(data.decode("utf-8"))
        report = asyncio.run(run(args.repository, identity, args.image, args.trusted_revision,
                                 config, allow_paid=args.allow_paid))
    except (Exception, KeyboardInterrupt):
        report = PublicReport(outcome="blocked", reason="invalid_input_or_interrupted")
    print(report.model_dump_json(indent=2))
    counts = report.tests
    counts_text = (f"{counts.passed} passed, {counts.failed} failed, {counts.skipped} skipped, {counts.errors} errors"
                   if counts else "unavailable")
    coverage_parts = []
    if report.coverage:
        for name in ("statements", "branches", "lines"):
            metric = getattr(report.coverage, name)
            coverage_parts.append(f"{name}={metric.covered}/{metric.total}" if metric else f"{name}=unavailable")
    print(f"Taskly: {report.outcome}; {report.reason}; head={report.identity.head_sha if report.identity else 'unavailable'}; "
          f"verification={report.verification_state}/{report.verification_outcome or 'not_run'}; "
          f"tests={counts_text}; "
          f"calls={report.model_calls}; repairs={report.repairs}; "
          f"accounted USD={report.usage.accounted_cost_usd if report.usage else 0}", file=sys.stderr)
    print("Generated paths: " + (", ".join(report.generated_paths) or "none") + "; coverage: " +
          (", ".join(coverage_parts) or report.coverage_state),
          file=sys.stderr)
    return 0 if report.outcome in {"passed", "skipped"} else 1


if __name__ == "__main__":
    sys.exit(main())
