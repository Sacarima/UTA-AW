"""Narrow hosted-demo admission and artifact checks; no pipeline or API client.

Only the existing end_to_end CLI performs generation/application/verification.
All paths here are trusted workflow locations, never model-selected paths.
"""

import argparse
import json
from pathlib import Path
import re
import sys
from typing import get_args

from .analysis import GitChangeAnalyzer
from .contracts import CoverageEvidence, PRIdentity, VerificationEvidence
from .end_to_end import PublicReport, _VERIFICATION_REASONS
from .git_changes import GitReader
from .lifecycle import ReasonCode
from .openai_transport import demo_config


SOURCE = "backend/app/features/todos/validators.py"
TEST = "backend/tests/features/todos/test_validators.py"
ORIGINAL = b'    for field in model.model_fields_set - {"due_date"}:\n'
REPLACEMENT = b'    for field in sorted(model.model_fields_set - {"due_date"}):\n'


def read_bounded(path: Path, limit=1_048_576) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("demo data limit")
    return data


def validate_pr(identity: PRIdentity, metadata: dict) -> None:
    """Metadata is fetched by the trusted workflow from a fixed GitHub endpoint.

    Structural checks alone do not authenticate arbitrary supplied JSON.
    This first demonstration deliberately supports same-repository open PRs only.
    """
    identity = PRIdentity.model_validate(identity)
    if (identity.base_sha == identity.head_sha or metadata["number"] != identity.number
        or type(metadata["number"]) is not int or metadata["state"] != "open"
        or metadata["base"]["sha"] != identity.base_sha
        or metadata["head"]["sha"] != identity.head_sha
        or metadata["base"]["ref"] != "main"
        or any(metadata[side]["repo"]["full_name"] != identity.repository for side in ("base", "head"))):
        raise ValueError("PR identity rejected")


def validate_candidate(repository: Path, identity: PRIdentity) -> None:
    """Authorize exactly the reviewed one-line demo delta from pinned objects."""
    identity = PRIdentity.model_validate(identity)
    reader = GitReader(repository)
    changes = reader.changes(identity.base_sha, identity.head_sha)
    if (identity.base_sha == identity.head_sha or len(changes) != 1
        or changes[0].path != SOURCE or changes[0].status != "modified"
        or changes[0].previous_path is not None):
        raise ValueError("demo delta rejected")
    contents = []
    for revision in (identity.base_sha, identity.head_sha):
        entry = reader.objects(revision)[SOURCE]
        if entry.mode != "100644":
            raise ValueError("demo source must be a regular non-executable file")
        contents.append(reader.blob(entry.object_id, max_bytes=16_384))
    before, after = contents
    if before.count(ORIGINAL) != 1 or after != before.replace(ORIGINAL, REPLACEMENT):
        raise ValueError("demo behavior change rejected")
    analysis = GitChangeAnalyzer(reader, base_sha=identity.base_sha, head_sha=identity.head_sha).inspect()
    if (analysis.decision.classification != "backend" or analysis.decision.action != "generate"
        or len(analysis.impacts) != 1 or TEST not in analysis.impacts[0].existing_tests):
        raise ValueError("demo analysis rejected")


def artifact_report(raw: bytes, identity: PRIdentity) -> PublicReport:
    """Validate correlation and project fixed diagnostics before publication.

    The file must come from trusted host orchestration. JSON validity is not
    authentication. Proposal/image invocation checks remain in the real verifier.
    """
    report = PublicReport.model_validate_json(raw)
    if (report.identity != identity or report.classification != "backend"
        or report.generated_paths not in {(), (TEST,)}
        or report.model_calls > 2 or report.repairs > 1):
        raise ValueError("demo report rejected")
    if report.usage is not None and (report.usage.accounted_cost_usd > 0.10
        or report.usage.estimated_reported_cost_usd > 0.10):
        raise ValueError("demo spending report rejected")
    evidence_identity = report.verification_identity
    if evidence_identity is not None and (evidence_identity.repository != identity.repository
        or evidence_identity.head_sha != identity.head_sha
        or evidence_identity.policy_digest != demo_config(budget_usd=0.10).execution_policy.digest
        or evidence_identity.artifact_refs):
        raise ValueError("demo evidence identity rejected")
    coverage = CoverageEvidence(state=report.coverage_state, metrics=report.coverage,
        reason=None if report.coverage_state == "available" else "coverage_unavailable")
    if report.verification_outcome is not None:
        VerificationEvidence(state=report.verification_state, outcome=report.verification_outcome,
            identity=evidence_identity, tests=report.tests,
            coverage=coverage,
            reason="demo_verification_observation")
    elif (report.verification_state == "available" or evidence_identity is not None
          or report.tests is not None or coverage.state == "available"):
        raise ValueError("demo evidence state inconsistent")
    if report.outcome == "passed" and (report.verification_state != "available"
        or report.verification_outcome != "passed" or report.generated_paths != (TEST,)
        or report.model_calls not in {1, 2} or report.usage is None
        or report.usage.unknown_cost_calls != 0 or not report.usage.input_tokens or not report.usage.output_tokens):
        raise ValueError("demo success lacks real generation/verification observations")
    if report.outcome == "skipped":
        raise ValueError("a skipped run is not this demonstration")
    reasons = set(get_args(ReasonCode)) | {"sandbox_unavailable", "provider_session_unavailable", "invalid_input"}
    return PublicReport(**{**report.model_dump(),
        "reason": report.reason if report.reason in reasons else "diagnostic_unavailable",
        "verification_reason": report.verification_reason if report.verification_reason in _VERIFICATION_REASONS
            else "diagnostic_unavailable"})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Trusted fixed-demo admission and evidence checks")
    parser.add_argument("action", choices=("check-pr", "check-candidate", "publish"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--number", type=int, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--image-file", type=Path)
    args = parser.parse_args(argv)
    try:
        identity = PRIdentity(repository=args.name, number=args.number, base_sha=args.base, head_sha=args.head)
        if identity.base_sha == identity.head_sha:
            raise ValueError("distinct revisions required")
        if args.action == "check-pr":
            validate_pr(identity, json.loads(read_bounded(args.metadata)))
        elif args.action == "check-candidate":
            validate_candidate(args.repository, identity)
            args.config.write_text(demo_config(budget_usd=0.10).model_dump_json(indent=2) + "\n")
        else:
            report = artifact_report(read_bounded(args.report, 65_536), identity)
            image = read_bounded(args.image_file, 128).decode().strip()
            if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
                raise ValueError("immutable image required")
            args.artifact.write_text(json.dumps({"image": image, "trusted_revision": identity.base_sha,
                "model": demo_config(budget_usd=0.10).model,
                "report": report.model_dump(mode="json")}, indent=2) + "\n")
            counts = report.tests
            print(f"Taskly live demonstration: {report.outcome}; head={identity.head_sha}; "
                  f"calls={report.model_calls}; repairs={report.repairs}; "
                  f"tests={counts.passed if counts else 'unavailable'} passed; "
                  f"accounted USD={report.usage.accounted_cost_usd if report.usage else 0}")
        return 0
    except Exception:
        print("Taskly demonstration checks failed; no unvalidated evidence published.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
