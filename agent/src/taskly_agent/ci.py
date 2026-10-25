"""Trusted PR CI baseline entrypoint; no model, generated proposal or publisher.

Run from the reviewed base checkout's installed package. The candidate is read
only through pinned Git objects. Baseline correlation has a distinct identity
contract so it cannot be confused with a generated proposal's verification.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys

from .analysis import GitChangeAnalyzer
from .application import ProposalApplier
from .contracts import PRIdentity
from .docker_runner import DockerSandbox
from .execution import BaselineIdentity, ExecutionLimits, execution_job
from .git_changes import GitReader, validate_commit
from .lifecycle import ExecutionPolicy
from .verification_reports import aggregate_reports, parse_capture


async def baseline(repository: Path, identity: PRIdentity, image: str, trusted_revision: str):
    reader = GitReader(repository)
    analyzer = GitChangeAnalyzer(reader, base_sha=identity.base_sha, head_sha=identity.head_sha)
    applier = ProposalApplier(analyzer, identity=identity)
    policy = ExecutionPolicy()
    limits = ExecutionLimits(memory_bytes=1_073_741_824)
    correlation = BaselineIdentity(repository=identity.repository, head_sha=identity.head_sha,
        policy_digest=policy.digest, baseline_digest=hashlib.sha256(b"taskly-baseline-v1\0" + identity.head_sha.encode()).hexdigest())
    runner = DockerSandbox(image, trusted_revision)
    await runner.validate(limits, correlation)
    # Both complete suites run for the CI baseline, even for docs/test-only PRs.
    # Proposal verification uses VerifierAdapter + ExecutionController instead.
    with applier.baseline(identity) as workspace:
        reports = []
        for suite in ("backend", "frontend"):
            job = execution_job(suite, correlation)
            capture = await runner.execute(workspace, job, limits)
            if not runner.consume_receipt(capture, job):
                raise ValueError("execution receipt invalid")
            reports.append(parse_capture(capture, limits))
    return tuple(reports), aggregate_reports(tuple(reports), ("backend", "frontend"))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Trusted sandboxed Taskly baseline")
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--number", type=int, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--trusted-revision", required=True)
    parser.add_argument("--image-file", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    arguments = parser.parse_args(argv)
    text = "Taskly verification unavailable: sandbox, dependency, identity, or report checks failed.\n"
    code = 1
    try:
        identity = PRIdentity(repository=arguments.name, number=arguments.number,
            base_sha=validate_commit(arguments.base), head_sha=validate_commit(arguments.head))
        trusted = validate_commit(arguments.trusted_revision)
        image = arguments.image_file.read_bytes()
        if len(image) > 128:
            raise ValueError
        reports, summary = asyncio.run(baseline(arguments.repository, identity, image.decode().strip(), trusted))
        lines = ["Taskly sandboxed baseline (no model or generated proposal)", "",
                 "Pinned head: " + identity.head_sha, "Trusted code: " + trusted, "Image: " + image.decode().strip(), ""]
        for report in reports:
            counts = report.tests
            lines.append(report.suite + ": " + report.outcome + ("; counts unavailable" if counts is None else
                f"; passed={counts.passed}, failed={counts.failed}, skipped={counts.skipped}, errors={counts.errors}"))
            if report.coverage.metrics is not None:
                for metric in ("statements", "branches", "lines"):
                    value = getattr(report.coverage.metrics, metric)
                    lines.append(metric + ": unavailable" if value is None else f"{metric}: {value.covered}/{value.total}")
            else:
                lines.append("coverage: " + report.coverage.state)
        text = "\n".join(lines) + "\n"
        code = 0 if summary.outcome == "passed" else 1
    except Exception:
        pass  # Never echo PR-controlled paths, report text, stderr or exceptions.
    arguments.summary.write_text(text[:8192], encoding="utf-8")
    print("Taskly baseline passed" if code == 0 else "Taskly baseline failed or unavailable")
    return code


if __name__ == "__main__":
    sys.exit(main())
