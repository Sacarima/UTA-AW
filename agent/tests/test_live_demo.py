"""Admission and publication only: real Git data, no candidate execution/API."""

import json
from pathlib import Path

import pytest

from taskly_agent import live_demo
from taskly_agent.contracts import PRIdentity
from taskly_agent.end_to_end import PublicReport
from taskly_agent.openai_transport import demo_config

from test_analysis import commit, git, repository, write


ROOT = Path(__file__).parents[2]


@pytest.fixture
def candidate(repository):
    # The suite must also run from the demo branch, whose working source already
    # contains the proposed change. Construct an unsorted baseline as test data.
    original = (ROOT / live_demo.SOURCE).read_bytes().replace(live_demo.REPLACEMENT, live_demo.ORIGINAL)
    assert original.count(live_demo.ORIGINAL) == 1
    write(repository, live_demo.SOURCE, original.decode())
    write(repository, live_demo.TEST, "# existing convention-matched tests\n")
    base = commit(repository)
    write(repository, live_demo.SOURCE, original.replace(live_demo.ORIGINAL, live_demo.REPLACEMENT).decode())
    head = commit(repository)
    identity = PRIdentity(repository="Sacarima/UTA-AW", number=42, base_sha=base, head_sha=head)
    return repository, identity, original


def metadata(identity):
    return {"number": identity.number, "state": "open", "base": {"sha": identity.base_sha,
        "ref": "main", "repo": {"full_name": identity.repository}},
        "head": {"sha": identity.head_sha, "repo": {"full_name": identity.repository}}}


def report_data(identity):
    return {"identity": identity.model_dump(mode="json"), "classification": "backend", "outcome": "passed",
        "reason": "completed", "generated_paths": [live_demo.TEST], "verification_state": "available",
        "verification_outcome": "passed", "verification_reason": "aggregate_passed",
        "verification_identity": {"repository": identity.repository, "head_sha": identity.head_sha,
            "proposal_digest": "d" * 64, "policy_digest": demo_config(budget_usd=0.10).execution_policy.digest},
        "tests": {"passed": 2, "failed": 0, "skipped": 0, "errors": 0}, "coverage_state": "available",
        "coverage": {"lines": {"covered": 1, "total": 2}, "statements": None, "branches": None},
        "model_calls": 1, "repairs": 0, "usage": {"input_tokens": 100, "output_tokens": 40,
            "estimated_reported_cost_usd": 0.000255, "accounted_cost_usd": 0.000255, "unknown_cost_calls": 0}}


def test_real_pinned_demo_diff_and_patch_without_applying_to_original(candidate):
    repository, identity, original = candidate
    live_demo.validate_candidate(repository, identity)
    patch = (ROOT / "agent/demo/deterministic-validation.patch").read_text().splitlines()
    assert [line for line in patch if line.startswith("diff --git ")] == [
        f"diff --git a/{live_demo.SOURCE} b/{live_demo.SOURCE}"]
    assert len([line for line in patch if line.startswith("@@ ")]) == 1
    assert [line for line in patch if line.startswith("-") and not line.startswith("---")] == [
        "-" + live_demo.ORIGINAL.decode().rstrip("\n")]
    assert [line for line in patch if line.startswith("+") and not line.startswith("+++")] == [
        "+" + live_demo.REPLACEMENT.decode().rstrip("\n")]
    # Check the supplied patch on a disposable working copy only, without writes
    # by Git apply. A dirty tree must not influence pinned-object admission.
    write(repository, live_demo.SOURCE, original.decode())
    git(repository, "apply", "--check", str(ROOT / "agent/demo/deterministic-validation.patch"))
    write(repository, live_demo.SOURCE, "raise RuntimeError('workspace must not execute')\n")
    live_demo.validate_candidate(repository, identity)


@pytest.mark.parametrize("change", ["extra-source", "dependency", "test", "wrong-edit", "mode", "removed", "symlink"])
def test_rejects_any_other_candidate_delta(candidate, change):
    repository, identity, _ = candidate
    path = repository / live_demo.SOURCE
    if change in {"extra-source", "dependency", "test"}:
        target = {"extra-source": "frontend/src/Other.ts", "dependency": "backend/requirements.txt",
                  "test": live_demo.TEST}[change]
        write(repository, target, "# different contents\n")
    elif change == "wrong-edit":
        path.write_text(path.read_text() + "# unreviewed addition\n")
    elif change == "mode":
        path.chmod(0o755)
    elif change == "removed":
        path.unlink()
    else:
        path.unlink()
        path.symlink_to("/etc/passwd")
    head = commit(repository)
    identity = PRIdentity(**{**identity.model_dump(), "head_sha": head})
    with pytest.raises(ValueError):
        live_demo.validate_candidate(repository, identity)


def test_identical_commits_cannot_be_a_demo(candidate):
    repository, identity, _ = candidate
    identity = PRIdentity(**{**identity.model_dump(), "head_sha": identity.base_sha})
    with pytest.raises(ValueError):
        live_demo.validate_candidate(repository, identity)
    with pytest.raises(ValueError):
        live_demo.validate_pr(identity, metadata(identity))


@pytest.mark.parametrize("field", ["number", "state", "base-sha", "head-sha", "fork", "base-branch"])
def test_actual_pr_metadata_must_match_every_identity(candidate, field):
    _, identity, _ = candidate
    data = metadata(identity)
    live_demo.validate_pr(identity, data)
    if field == "number":
        data["number"] = "42"
    elif field == "state":
        data["state"] = "closed"
    elif field in {"base-sha", "head-sha"}:
        data[field.split("-")[0]]["sha"] = "f" * 40
    elif field == "fork":
        data["head"]["repo"]["full_name"] = "Someone/fork"
    else:
        data["base"]["ref"] = "unreviewed"
    with pytest.raises(ValueError):
        live_demo.validate_pr(identity, data)


def test_success_artifact_preserves_numeric_evidence_and_suppresses_unknown_diagnostics(candidate):
    _, identity, _ = candidate
    data = report_data(identity)
    data.update(reason="private prompt or source", verification_reason="private raw diagnostics")
    result = live_demo.artifact_report(json.dumps(data).encode(), identity)
    assert result.tests.passed == 2 and result.coverage.lines.total == 2
    assert result.usage.accounted_cost_usd == 0.000255
    assert "private" not in result.model_dump_json()


@pytest.mark.parametrize("field", ["head", "policy", "artifact-url", "paths", "calls", "repairs", "cost",
    "unknown-usage", "missing-usage", "missing-identity", "simulation", "tests", "coverage", "extra", "skipped"])
def test_rejects_inconsistent_or_unsafe_success_reports(candidate, field):
    _, identity, _ = candidate
    data = report_data(identity)
    if field == "head":
        data["identity"]["head_sha"] = "f" * 40
    elif field == "policy":
        data["verification_identity"]["policy_digest"] = "f" * 64
    elif field == "artifact-url":
        data["verification_identity"]["artifact_refs"] = ["private/untrusted"]
    elif field == "paths":
        data["generated_paths"] = ["backend/app/main.py"]
    elif field == "calls":
        data["model_calls"] = 3
    elif field == "repairs":
        data["repairs"] = 2
    elif field == "cost":
        data["usage"]["accounted_cost_usd"] = 0.11
    elif field == "unknown-usage":
        data["usage"]["unknown_cost_calls"] = 1
    elif field == "missing-usage":
        data["usage"] = None
    elif field == "missing-identity":
        data["verification_identity"] = None
    elif field == "simulation":
        data["verification_state"] = "unavailable"
    elif field == "tests":
        data["tests"]["failed"] = 1
    elif field == "coverage":
        data["coverage"] = None
    elif field == "extra":
        data["raw_source"] = "private"
    else:
        data["outcome"] = "skipped"
    with pytest.raises(ValueError):
        live_demo.artifact_report(json.dumps(data).encode(), identity)


def test_cli_admission_fixed_config_and_safe_artifact(candidate, tmp_path, capsys):
    repository, identity, _ = candidate
    common = ["--name", identity.repository, "--number", str(identity.number),
        "--base", identity.base_sha, "--head", identity.head_sha]
    pr = tmp_path / "pr.json"
    pr.write_text(json.dumps(metadata(identity)))
    assert live_demo.main(["check-pr", *common, "--metadata", str(pr)]) == 0
    config = tmp_path / "config.json"
    assert live_demo.main(["check-candidate", *common, "--repository", str(repository), "--config", str(config)]) == 0
    assert config.read_text().strip() == demo_config(budget_usd=0.10).model_dump_json(indent=2)
    raw, artifact, image = [tmp_path / name for name in ("raw.json", "evidence.json", "image.id")]
    raw.write_text(json.dumps(report_data(identity)))
    image.write_text("sha256:" + "c" * 64 + "\n")
    assert live_demo.main(["publish", *common, "--report", str(raw), "--artifact", str(artifact), "--image-file", str(image)]) == 0
    saved = json.loads(artifact.read_text())
    assert saved["trusted_revision"] == identity.base_sha
    assert PublicReport.model_validate_json(json.dumps(saved["report"])).outcome == "passed"
    assert "passed" in capsys.readouterr().out
    artifact.unlink()
    raw.write_text('{"private invalid input": "secret"}')
    assert live_demo.main(["publish", *common, "--report", str(raw), "--artifact", str(artifact), "--image-file", str(image)]) == 1
    assert not artifact.exists() and "secret" not in capsys.readouterr().out


def test_report_read_limit_is_explicit(tmp_path):
    path = tmp_path / "oversized"
    path.write_bytes(b"x" * 101)
    with pytest.raises(ValueError):
        live_demo.read_bounded(path, 100)
