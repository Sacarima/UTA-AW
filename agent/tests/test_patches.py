"""Verified proposal export as review data; no candidate code execution."""

import asyncio
from contextlib import contextmanager
import hashlib
import json

import pytest

from taskly_agent.application import ProposalApplier
from taskly_agent.contracts import RunResult
from taskly_agent.controller import ExecutionController
from taskly_agent.lifecycle import ExecutionReport
from taskly_agent.patches import export_verified_patch
from taskly_agent import patches
from taskly_agent.git_changes import GitReader

from test_adapters import FakeVerifier, make_system, proposal
from test_analysis import git, repository


@pytest.mark.parametrize("operation", ["modify", "create"])
@pytest.mark.parametrize("content", [
    "def test_value():\n    assert 1 + 1 == 2\n",
    "def test_value():\r\n    assert 1 + 1 == 2\r\n",
    "def test_value():\r\n    assert 1 + 1 == 2",
    "def test_value():\n    assert 'olá 世界' == 'olá 世界'\n",
    "def test_value():\n    assert 1 + 1 == 2",
])
def test_patch_applies_only_to_expected_tests_and_preserves_bound_hashes(make_system, tmp_path_factory, operation, content):
    files = {"backend/app/service.py": "def value():\n    return 2\n"}
    if operation == "modify": files["backend/tests/test_service.py"] = "# original without newline"
    system = make_system(files=files)
    candidate = proposal(operation=operation, content=content)
    system.fake.outputs = [candidate.model_dump_json()]
    controller = ExecutionController(analyzer=system.analyzer, generator=system.generator, verifier=FakeVerifier())
    execution = asyncio.run(controller.run(system.request, system.config.execution_policy))
    applier = ProposalApplier(system.analyzer, identity=system.request.pr.identity,
        content_limits=system.config.execution_policy.content_limits)
    output = tmp_path_factory.mktemp("patch-review") / "patch"
    before = git(system.repo, "status", "--porcelain")
    export_verified_patch(applier, execution, system.config.execution_policy, output)
    patch = (output / "tests.patch").read_bytes()
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["identity"]["head_sha"] == system.request.pr.identity.head_sha
    assert manifest["verification_identity"] == execution.result.verification.identity.model_dump(mode="json")
    assert manifest["patch_sha256"] == hashlib.sha256(patch).hexdigest()
    assert [item["path"] for item in manifest["changes"]] == ["backend/tests/test_service.py"]
    git(system.repo, "apply", "--check", str(output / "tests.patch"))
    # Independently consume the actual exported artifact, not just --check.
    with applier.baseline(system.request.pr.identity) as workspace:
        GitReader(workspace)._run("apply", "--whitespace=nowarn", "--", str(output / "tests.patch"))
        actual = (workspace / candidate.changes[0].path).read_bytes()
        assert actual == content.encode("utf-8")
        assert hashlib.sha256(actual).hexdigest() == manifest["changes"][0]["resulting_sha256"]
    assert git(system.repo, "status", "--porcelain") == before
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "tests.patch").stat().st_mode & 0o777 == 0o600


def _export_case(make_system, tmp_path_factory, *, content="def test_value():\n    assert 1 + 1 == 2\n", original=None):
    files = {"backend/app/service.py": "def value():\n    return 2\n"}
    if original is not None:
        files["backend/tests/test_service.py"] = original
    system = make_system(files=files)
    system.fake.outputs = [proposal(operation="modify" if original is not None else "create", content=content).model_dump_json()]
    controller = ExecutionController(analyzer=system.analyzer, generator=system.generator, verifier=FakeVerifier())
    execution = asyncio.run(controller.run(system.request, system.config.execution_policy))
    assert execution.result.outcome == "passed"
    applier = ProposalApplier(system.analyzer, identity=system.request.pr.identity)
    output = tmp_path_factory.mktemp("round-trip-review") / "patch"
    return system, execution, applier, output


@pytest.mark.parametrize("separator", ["\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("in_original", [False, True])
def test_unsupported_line_separators_never_export(make_system, tmp_path_factory, separator, in_original):
    unusual = "# first" + separator + "# second\n"
    kwargs = {"original": unusual} if in_original else {"content": unusual}
    system, execution, applier, output = _export_case(make_system, tmp_path_factory, **kwargs)
    before = git(system.repo, "status", "--porcelain")
    with pytest.raises(ValueError, match="unsupported test line separator"):
        export_verified_patch(applier, execution, system.config.execution_policy, output)
    assert not output.exists()
    assert git(system.repo, "status", "--porcelain") == before


@pytest.mark.parametrize("tampering", ["content", "malformed", "production"])
def test_tampered_patch_rejected_and_workspace_disposed(make_system, tmp_path_factory, monkeypatch, tampering):
    system, execution, applier, output = _export_case(make_system, tmp_path_factory)
    original_diff = patches.difflib.unified_diff

    def corrupt(*args, **kwargs):
        if tampering == "malformed":
            yield "invalid patch\n"
            return
        for line in original_diff(*args, **kwargs):
            yield line.replace("== 2", "== 3") if tampering == "content" else line
        if tampering == "production":
            yield ("diff --git a/backend/app/service.py b/backend/app/service.py\n"
                   "--- a/backend/app/service.py\n+++ b/backend/app/service.py\n"
                   "@@ -1,2 +1,2 @@\n def value():\n-    return 2\n+    return 3\n")

    workspaces = []
    baseline = applier.baseline

    @contextmanager
    def tracked(identity):
        with baseline(identity) as workspace:
            workspaces.append(workspace)
            yield workspace

    monkeypatch.setattr(patches.difflib, "unified_diff", corrupt)
    monkeypatch.setattr(applier, "baseline", tracked)
    before = git(system.repo, "status", "--porcelain")
    with pytest.raises(ValueError, match="patch round-trip rejected"):
        export_verified_patch(applier, execution, system.config.execution_policy, output)
    assert workspaces and all(not workspace.parent.exists() for workspace in workspaces)
    assert not output.exists()
    assert git(system.repo, "status", "--porcelain") == before
    assert (system.repo / "backend/app/service.py").read_bytes() == b"def value():\n    return 2\n"


@pytest.mark.parametrize("field", ["original_sha256", "resulting_sha256"])
def test_inconsistent_application_hash_rejected(make_system, tmp_path_factory, monkeypatch, field):
    system, execution, applier, output = _export_case(make_system, tmp_path_factory, original="# original\r\n")
    apply = applier.apply

    @contextmanager
    def inconsistent(*args, **kwargs):
        with apply(*args, **kwargs) as applied:
            change = applied.changes[0].model_copy(update={field: "f" * 64})
            yield applied.model_copy(update={"changes": (change,)})

    monkeypatch.setattr(applier, "apply", inconsistent)
    with pytest.raises(ValueError, match="proposal hash mismatch"):
        export_verified_patch(applier, execution, system.config.execution_policy, output)
    assert not output.exists()


def test_failed_run_never_exports_a_patch(make_system, tmp_path_factory):
    system = make_system()
    controller = ExecutionController(analyzer=system.analyzer, generator=system.generator, verifier=FakeVerifier(("failed", "failed")))
    execution = asyncio.run(controller.run(system.request, system.config.execution_policy))
    applier = ProposalApplier(system.analyzer, identity=system.request.pr.identity)
    output = tmp_path_factory.mktemp("no-patch") / "patch"
    with pytest.raises(ValueError):
        export_verified_patch(applier, execution, system.config.execution_policy, output)
    assert not output.exists()


@pytest.mark.parametrize("field", ["head_sha", "proposal_digest", "policy_digest"])
def test_stale_evidence_never_exports_a_patch(make_system, tmp_path_factory, field):
    system = make_system()
    controller = ExecutionController(analyzer=system.analyzer, generator=system.generator, verifier=FakeVerifier())
    execution = asyncio.run(controller.run(system.request, system.config.execution_policy))
    data = execution.model_dump()
    data["result"]["verification"]["identity"][field] = "f" * (40 if field == "head_sha" else 64)
    stale = ExecutionReport.model_validate(data)
    applier = ProposalApplier(system.analyzer, identity=system.request.pr.identity)
    output = tmp_path_factory.mktemp("stale-patch") / "patch"
    with pytest.raises(ValueError):
        export_verified_patch(applier, stale, system.config.execution_policy, output)
    assert not output.exists()
