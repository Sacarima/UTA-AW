"""Trusted workflow_run integration; candidate code executes only in Docker.

No source-workflow artifacts are consumed. Analysis/generation jobs use read
tokens. Only publish needs PR write access and never executes candidate code,
models, Docker, patches, or commands from downloaded artifacts.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Literal, get_args

from pydantic import model_validator

from . import end_to_end
from .analysis import GitChangeAnalyzer
from .contracts import AgentDecision, Contract, CoverageEvidence, VerificationEvidence
from .end_to_end import PublicReport, _VERIFICATION_REASONS
from .github_pr import GitHub, GitHubError, PullRequestRun, resolve_run
from .git_changes import GitReader, validate_commit
from .lifecycle import ReasonCode
from .live_demo import read_bounded
from .openai_transport import demo_config
from .policy import validate_test_path


Reason = Literal["ready", "analysis_skipped", "analysis_blocked", "git_unavailable", "fork", "untrusted_author",
    "draft", "baseline_failed", "repeat_attempt", "source_unbound", "unsupported_stack", "ai_disabled",
    "approval_mismatch", "missing_credential", "generation_unavailable", "generation_report_unavailable", "completed"]
_SAFE_REASONS = frozenset(get_args(ReasonCode)) | {"patch_export_unavailable", "sandbox_unavailable", "provider_session_unavailable"}


class PRReport(Contract):
    context: PullRequestRun
    decision: AgentDecision | None = None
    reason: Reason
    result: PublicReport
    patch_available: bool = False

    @model_validator(mode="after")
    def consistent(self):
        result, identity = self.result, self.context.identity
        if result.identity != identity or result.model_calls > 2 or result.repairs > 1:
            raise ValueError("report identity/limits mismatch")
        if self.decision is not None and result.classification != self.decision.classification:
            raise ValueError("analysis classification mismatch")
        for path in result.generated_paths:
            root = {"backend": "backend/tests/", "frontend": "frontend/src/"}.get(result.classification)
            if root is None or not validate_test_path(path).startswith(root):
                raise ValueError("generated test path must match the supported stack")
        if result.usage and (result.usage.accounted_cost_usd > 0.10 or result.usage.estimated_reported_cost_usd > 0.10):
            raise ValueError("spending report exceeds policy")
        evidence_identity = result.verification_identity
        if evidence_identity is not None and (evidence_identity.repository != identity.repository
            or evidence_identity.head_sha != identity.head_sha or evidence_identity.artifact_refs
            or evidence_identity.policy_digest != demo_config(budget_usd=0.10).execution_policy.digest):
            raise ValueError("verification identity mismatch")
        coverage = CoverageEvidence(state=result.coverage_state, metrics=result.coverage,
            reason=None if result.coverage_state == "available" else "coverage_unavailable")
        if result.verification_outcome is not None:
            VerificationEvidence(state=result.verification_state, outcome=result.verification_outcome, identity=evidence_identity,
                tests=result.tests, coverage=coverage, reason="trusted_workflow_observation")
        elif result.verification_state == "available" or result.tests is not None or coverage.state == "available":
            raise ValueError("inconsistent evidence")
        if result.outcome == "passed" and (result.verification_state != "available" or result.verification_outcome != "passed"
            or not self.patch_available or not result.generated_paths or result.model_calls not in {1, 2}
            or result.usage is None or result.usage.unknown_cost_calls or not result.usage.input_tokens or not result.usage.output_tokens
            or self.decision is None or self.decision.action != "generate" or self.decision.classification not in {"backend", "frontend"}):
            raise ValueError("success requires actual generation, verification and a reviewable patch")
        if self.patch_available and result.outcome != "passed":
            raise ValueError("failed/unavailable proposals cannot publish patches")
        return self


def generation_gate(context: PullRequestRun, decision: AgentDecision | None) -> Reason:
    if decision is None:
        return "git_unavailable"
    if decision.action != "generate":
        return "analysis_skipped" if decision.action == "skip" else "analysis_blocked"
    if context.fork:
        return "fork"
    if not context.trusted_author:
        return "untrusted_author"
    if context.draft:
        return "draft"
    if decision.classification not in {"backend", "frontend"}:
        return "unsupported_stack"
    if not context.baseline_passed:
        return "baseline_failed"
    if not context.first_attempt:
        return "repeat_attempt"
    if not context.source_bound:
        return "source_unbound"
    return "ready"


def unavailable(context, reason: Reason, decision=None) -> PRReport:
    return PRReport(context=context, decision=decision, reason=reason,
        result=PublicReport(identity=context.identity, classification=decision.classification if decision else None,
            outcome="skipped" if reason == "analysis_skipped" else "blocked", reason=reason))


def analyze(repository: Path, context: PullRequestRun) -> PRReport:
    identity = context.identity
    plan = GitChangeAnalyzer(GitReader(repository), base_sha=identity.base_sha, head_sha=identity.head_sha).inspect().decision
    # Never publish path-bearing analysis rationale or any repository prose.
    decision = AgentDecision(classification=plan.classification, action=plan.action,
        rationale={"generate": "Supported production changes detected.", "skip": "No surviving supported generation target.",
                   "block": "Deterministic analysis could not authorize generation."}[plan.action], selected_skills=plan.selected_skills)
    return unavailable(context, generation_gate(context, decision), decision)


def authorize(context, decision, *, enabled, approved_base, workflow_sha, credential_present) -> Reason:
    reason = generation_gate(context, decision)
    if reason != "ready":
        return reason
    if not enabled:
        return "ai_disabled"
    if approved_base != context.identity.base_sha or approved_base != validate_commit(workflow_sha):
        return "approval_mismatch"
    if not credential_present:
        return "missing_credential"
    return "ready"


async def generate(repository, context, *, image, workflow_sha, approved_base, enabled,
                   credential_present, patch_directory) -> PRReport:
    report = analyze(repository, context)
    reason = authorize(context, report.decision, enabled=enabled, approved_base=approved_base,
        workflow_sha=workflow_sha, credential_present=credential_present)
    if reason != "ready":
        return unavailable(context, reason, report.decision)
    result = await end_to_end.run(repository, context.identity, image, context.identity.base_sha,
        demo_config(budget_usd=0.10), allow_paid=True, patch_directory=patch_directory)
    if result.classification is None:
        # Runtime/input preflight can stop before the runner's own analysis.
        # Retain this trusted Git analysis while keeping its failure/evidence.
        result = PublicReport(**{**result.model_dump(), "classification": report.decision.classification})
    # The existing CLI owns the provider session/controller/verifier. A directory
    # is exported only after its authenticated verification and cleanup succeed.
    patch = result.outcome == "passed" and (patch_directory / "tests.patch").is_file() and (patch_directory / "manifest.json").is_file()
    if result.outcome == "passed" and not patch:
        result = PublicReport(**{**result.model_dump(), "outcome": "incomplete", "reason": "patch_export_unavailable"})
    return PRReport(context=context, decision=report.decision, reason="completed" if patch else "generation_unavailable",
        result=result, patch_available=patch)


def render(report: PRReport, workflow_run_id: int) -> str:
    """Allowlisted fields only: no PR titles, branch names, rationale or diagnostics."""
    if type(workflow_run_id) is not int or workflow_run_id <= 0:
        raise ValueError("invalid workflow identity")
    identity, result = report.context.identity, report.result
    marker = f"<!-- taskly-agent:{identity.head_sha} -->"
    lines = [marker, "Taskly unit-test agent", "", f"Pinned head: `{identity.head_sha}`", f"Pinned base: `{identity.base_sha}`",
        "Decision: " + (report.decision.action if report.decision else "unavailable"),
        "Classification: " + (report.decision.classification if report.decision else "unavailable"),
        "Generation status: " + report.reason, "Workflow outcome: " + result.outcome,
        "Execution reason: " + (result.reason if result.reason in _SAFE_REASONS else "diagnostic_unavailable"),
        "Independent verification: " + result.verification_state + "/" + (result.verification_outcome or "not_run"),
        "Generated paths: " + (", ".join("`" + path + "`" for path in result.generated_paths) or "none")]
    if report.decision and report.decision.action == "generate":
        lines.append("Test impact: review/update existing tests without duplicating them."
            if "update-existing-tests" in report.decision.selected_skills else
            "Test impact: add focused tests where the changed behavior needs coverage.")
    counts = result.tests
    lines.append(f"Tests: {counts.passed} passed, {counts.failed} failed, {counts.skipped} skipped, {counts.errors} errors"
        if counts else "Tests: unavailable")
    if result.coverage:
        for name in ("statements", "branches", "lines"):
            metric = getattr(result.coverage, name)
            lines.append(f"Coverage {name}: {metric.covered}/{metric.total}" if metric else f"Coverage {name}: unavailable")
    else:
        lines.append("Coverage: unavailable")
    lines.append(f"Model-call reservations: {result.model_calls}; repairs: {result.repairs}; accounted estimated USD: "
                 + str(result.usage.accounted_cost_usd) if result.usage else
                 "Model usage/cost: unavailable; interrupted calls may have consumed credits.")
    if result.usage:
        lines.append(f"Tokens: input={result.usage.input_tokens if result.usage.input_tokens is not None else 'unavailable'}, "
                     f"output={result.usage.output_tokens if result.usage.output_tokens is not None else 'unavailable'}")
    if report.patch_available:
        lines.append("Verified test-only patch available as a review artifact; it is untrusted code, never auto-applied.")
    lines.extend([f"[Workflow report/artifacts](https://github.com/{identity.repository}/actions/runs/{workflow_run_id})", "",
        "Limits: backend-only or frontend-only generation; mixed changes unsupported; no coverage improvement claim; passing counts do not prove assertion quality. "
        "Skipped/blocked generation is not AI success. Spending guards are process-local, not account billing caps."])
    return "\n".join(lines)


def publish(client, report, source_run_id, workflow_run_id):
    fresh = resolve_run(client, source_run_id)
    if fresh.identity != report.context.identity or source_run_id != report.context.source_run_id:
        raise GitHubError("stale_commit")
    if report.result.model_calls and (fresh.fork or not fresh.trusted_author or fresh.draft):
        raise GitHubError("authorization_changed")
    body = render(report, workflow_run_id)
    if already_reported(client, fresh.identity):
        return "already_reported"
    # Recheck after reading comments, immediately before the sole non-retried
    # write. GitHub has no atomic compare-and-comment API; the body stays pinned.
    if resolve_run(client, source_run_id).identity != fresh.identity:
        raise GitHubError("stale_commit")
    client.comment(fresh.identity.number, body)
    return "published"


def already_reported(client, identity):
    comments = client.comments(identity.number)
    if not isinstance(comments, list) or len(comments) >= 100:
        raise GitHubError("comment_history_limit")
    marker = f"<!-- taskly-agent:{identity.head_sha} -->"
    return any(comment.get("user", {}).get("login") == "github-actions[bot]" and comment.get("user", {}).get("type") == "Bot"
        and comment.get("body", "").startswith(marker) for comment in comments)


def _write(directory, report):
    # Keep even otherwise valid narratives out of machine-readable artifacts.
    result = report.result
    safe = PublicReport(**{**result.model_dump(), "reason": result.reason if result.reason in _SAFE_REASONS else "diagnostic_unavailable",
        "verification_reason": result.verification_reason if result.verification_reason in _VERIFICATION_REASONS else "diagnostic_unavailable"})
    report = PRReport(**{**report.model_dump(), "result": safe})
    (directory / "report.json").write_text(report.model_dump_json(indent=2) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Trusted PR analysis, bounded single-stack generation and safe reporting")
    parser.add_argument("action", choices=("prepare", "analyze", "admit", "generate", "publish"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--source-run", type=int, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--expected-head")
    parser.add_argument("--workflow-sha")
    parser.add_argument("--workflow-run", type=int)
    parser.add_argument("--image-file", type=Path)
    parser.add_argument("--generated-report", type=Path)
    parser.add_argument("--classification", choices=("backend", "frontend"), default="backend",
        help="Trusted analysis classification for admission; generation independently reanalyzes Git")
    parser.add_argument("--generation-state", choices=("skipped", "success", "failure", "cancelled"), default="skipped")
    args = parser.parse_args(argv)
    try:
        client = GitHub(args.name, os.environ.get("GH_TOKEN", ""))
        context = resolve_run(client, args.source_run)
        if args.expected_head is not None and context.identity.head_sha != validate_commit(args.expected_head):
            raise GitHubError("stale_commit")
        directory = args.directory
        if args.action in {"prepare", "admit"}:
            report = unavailable(context, "git_unavailable")
            eligible = False
            if args.action == "admit":
                decision = AgentDecision(classification=args.classification, action="generate", rationale="Trusted analysis requested single-stack generation.")
                reason = authorize(context, decision, enabled=os.environ.get("TASKLY_PR_AI_ENABLED") == "true",
                    approved_base=os.environ.get("APPROVED_BASE", ""), workflow_sha=args.workflow_sha,
                    credential_present=bool(os.environ.get("OPENAI_API_KEY")))
                if reason == "ready" and already_reported(client, context.identity):
                    reason = "repeat_attempt"
                eligible = reason == "ready"
                report = unavailable(context, reason, decision)
            _write(directory, report)
            _outputs(context, eligible)
            if args.action == "admit" and report.reason not in {"ready", "repeat_attempt"}:
                return 1  # Requested generation was blocked; retain its report.
        elif args.action == "analyze":
            report = analyze(args.repository, context)
            _write(directory, report)
            _outputs(context, report.reason == "ready", classification=report.decision.classification)
        elif args.action == "generate":
            image = read_bounded(args.image_file, 128).decode().strip()
            if already_reported(client, context.identity):
                report = unavailable(context, "repeat_attempt", analyze(args.repository, context).decision)
            else:
                report = asyncio.run(generate(args.repository, context, image=image, workflow_sha=args.workflow_sha,
                    approved_base=os.environ.get("APPROVED_BASE", ""), enabled=os.environ.get("TASKLY_PR_AI_ENABLED") == "true",
                    credential_present=bool(os.environ.get("OPENAI_API_KEY")), patch_directory=directory / "patch"))
            _write(directory, report)
            _outputs(context, report.patch_available)
            return 0 if report.result.outcome == "passed" else 1
        else:
            report = PRReport.model_validate_json(read_bounded(directory / "report.json", 65_536))
            if report.context.source_run_id != args.source_run or report.context.identity != context.identity:
                raise GitHubError("stale_commit")
            if args.generated_report is not None and args.generated_report.is_file():
                generated = PRReport.model_validate_json(read_bounded(args.generated_report, 65_536))
                if generated.context.identity != report.context.identity or generated.context.source_run_id != args.source_run:
                    raise GitHubError("stale_commit")
                if generated.reason == "ready":
                    report = unavailable(context, "generation_report_unavailable", report.decision)
                elif generated.result.outcome == "passed" and args.generation_state != "success":
                    # An artifact upload/publication failure is a workflow failure,
                    # never a reason to overwrite independent passing evidence.
                    result = PublicReport(**{**generated.result.model_dump(), "outcome": "incomplete"})
                    report = PRReport(context=generated.context, decision=generated.decision,
                        reason="generation_report_unavailable", result=result, patch_available=False)
                else:
                    report = generated
            elif report.reason == "ready":
                report = unavailable(context, "ai_disabled" if args.generation_state == "skipped" else "generation_report_unavailable", report.decision)
            print("Taskly PR result: " + publish(client, report, args.source_run, args.workflow_run))
        return 0
    except GitHubError as exc:
        print("Taskly PR operation stopped: " + exc.code)
    except Exception:
        print("Taskly PR operation stopped: invalid or unavailable inputs")
    return 1


def _outputs(context, eligible, *, classification=None):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as stream:
            stream.write(f"base={context.identity.base_sha}\nhead={context.identity.head_sha}\nnumber={context.identity.number}\n"
                         f"eligible={'true' if eligible else 'false'}\nready=true\n")
            if classification in {"backend", "frontend", "mixed", "irrelevant"}:
                stream.write(f"classification={classification}\n")


if __name__ == "__main__":
    raise SystemExit(main())
