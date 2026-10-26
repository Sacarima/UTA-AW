"""Frontend composition over real Git and fake Responses/Docker; no live execution."""

import asyncio
import hashlib
import json

import pytest

from taskly_agent import docker_runner, pr_automation as automation
from taskly_agent.contracts import PRIdentity

from test_analysis import git, repository, write
from test_docker_runner import IMAGE
from test_end_to_end import pipeline
from test_openai_transport import api_response, isolated_process
from test_pr_automation import FakeGitHub, context


pytestmark = [pytest.mark.usefixtures("isolated_process"),
    pytest.mark.parametrize("pipeline", ["frontend", "frontend-create", "frontend-ts"], indirect=True)]


def generate(pipeline, directory, **overrides):
    options = dict(image=IMAGE, workflow_sha=pipeline.identity.base_sha,
        approved_base=pipeline.identity.base_sha, enabled=True, credential_present=True,
        patch_directory=directory)
    options.update(overrides)
    return asyncio.run(automation.generate(pipeline.repo, context(pipeline.identity), **options))


def test_frontend_generation_verification_patch_and_publication(pipeline, tmp_path_factory):
    write(pipeline.repo, pipeline.source_path, "// dirty workspace must not reach model\n")
    before = git(pipeline.repo, "status", "--porcelain")
    destination = tmp_path_factory.mktemp("frontend-review") / "patch"
    report = generate(pipeline, destination)
    assert report.reason == "completed" and report.result.outcome == "passed" and report.patch_available
    assert report.result.classification == "frontend"
    assert report.result.generated_paths == (pipeline.test_path,)
    assert report.result.verification_state == "available" and report.result.verification_outcome == "passed"
    assert report.result.tests.passed == 2
    assert report.result.coverage.statements.covered == report.result.coverage.branches.covered == report.result.coverage.lines.covered == 1
    assert report.result.model_calls == 1 and report.result.repairs == 0
    assert 0 < report.result.usage.accounted_cost_usd <= 0.10
    assert report.result.verification_identity.head_sha == pipeline.identity.head_sha
    assert [start["job"]["suite"] for start in pipeline.starts] == ["frontend"]
    assert pipeline.starts[0]["job"]["argv"][1] == "/opt/frontend/node_modules/vitest/vitest.mjs"
    assert "--coverage" in pipeline.starts[0]["job"]["argv"]
    wire = json.loads(pipeline.http.requests[0].content)
    envelope = json.loads(wire["input"][1]["content"][0]["text"])
    trusted = json.loads(wire["input"][0]["content"][0]["text"])
    skills = [skill["name"] for skill in trusted["skills"]]
    assert "react-unit-tests" in skills and "python-unit-tests" not in skills
    assert ("update-existing-tests" in skills) == (not pipeline.create)
    assert envelope["untrusted_reference"]["repository"]["identity"]["head_sha"] == pipeline.identity.head_sha
    assert "dirty workspace" not in json.dumps(envelope)
    assert envelope["untrusted_reference"]["eligible_test_paths"][pipeline.test_path] == ("create" if pipeline.create else "modify")
    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["changes"][0]["resulting_sha256"] == hashlib.sha256(pipeline.content.encode()).hexdigest()
    client = FakeGitHub(pipeline.identity)
    assert automation.publish(client, report, 10, 20) == "published"
    assert "Classification: frontend" in client.writes[0][1]
    assert "Independent verification: available/passed" in client.writes[0][1]
    assert pipeline.test_path in client.writes[0][1] and pipeline.content not in client.writes[0][1]
    assert git(pipeline.repo, "status", "--porcelain") == before
    assert all(not workspace.exists() for workspace in pipeline.workspaces)
    assert (pipeline.repo / pipeline.test_path).exists() == (not pipeline.create)


@pytest.mark.parametrize("bad", ["json", "production", "unrelated", "wrong-operation", "other-stack"])
def test_frontend_invalid_proposals_are_charged_but_never_applied(pipeline, bad):
    paths = {"production": pipeline.source_path, "unrelated": "frontend/src/Unrelated.test.tsx",
        "other-stack": "backend/tests/test_service.py"}
    if bad == "json":
        output = "not JSON"
    else:
        operation = "modify" if pipeline.create else "create"
        output = json.dumps({"rationale": "test", "changes": [{"path": paths.get(bad, pipeline.test_path),
            "operation": operation, "content": pipeline.content}]})
    pipeline.http.raw = api_response(output)
    report = pipeline.run()
    assert report.outcome == "failed" and report.reason == "invalid_component_output"
    assert report.model_calls == 1 and report.usage.accounted_cost_usd > 0
    assert not pipeline.starts and not pipeline.workspaces


def test_frontend_failures_use_one_bounded_repair_and_publish_no_patch(pipeline, tmp_path_factory):
    pipeline.daemon.capture_overrides = {"failed": 1}
    destination = tmp_path_factory.mktemp("frontend-failed") / "patch"
    report = generate(pipeline, destination)
    assert report.result.outcome == "failed" and report.result.verification_outcome == "failed"
    assert report.result.model_calls == 2 and report.result.repairs == 1
    assert len(pipeline.starts) == len(pipeline.http.requests) == 2
    assert not report.patch_available and not destination.exists()
    feedback = json.loads(json.loads(pipeline.http.requests[1].content)["input"][1]["content"][0]["text"])
    assert feedback["untrusted_reference"]["verification_feedback"]["identity"]["head_sha"] == pipeline.identity.head_sha
    client = FakeGitHub(pipeline.identity)
    automation.publish(client, report, 10, 20)
    assert "Independent verification: available/failed" in client.writes[0][1]


@pytest.mark.parametrize("unavailable", ["timeout", "missing-reports", "simulation"])
def test_frontend_unavailable_verification_never_exports_or_repairs(pipeline, tmp_path_factory, monkeypatch, unavailable):
    if unavailable == "timeout":
        pipeline.daemon.start_failure = TimeoutError("private diagnostics")
    elif unavailable == "missing-reports":
        pipeline.daemon.capture_overrides = {"tests_report": None, "coverage_report": None}
    else:
        monkeypatch.setattr(docker_runner.DockerSandbox, "mode", "simulation")
    destination = tmp_path_factory.mktemp("frontend-unavailable") / "patch"
    report = generate(pipeline, destination)
    assert report.result.outcome != "passed" and report.result.verification_state != "available"
    assert report.result.repairs == 0 and not report.patch_available and not destination.exists()
    assert len(pipeline.http.requests) == (0 if unavailable == "simulation" else 1)


def test_frontend_stale_capture_identity_cannot_pass(pipeline, monkeypatch):
    execute = docker_runner.DockerSandbox.execute

    async def stale(self, *args):
        capture = await execute(self, *args)
        identity = type(capture.identity)(**{**capture.identity.model_dump(), "head_sha": "f" * 40})
        return type(capture)(**{**capture.model_dump(), "identity": identity})

    monkeypatch.setattr(docker_runner.DockerSandbox, "execute", stale)
    report = pipeline.run()
    assert report.verification_reason == "capture_identity_mismatch" and report.outcome != "passed"
    assert report.verification_state == "unavailable" and report.repairs == 0


@pytest.mark.parametrize("case,reason", [("approval", "approval_mismatch"), ("credential", "missing_credential")])
def test_frontend_authorization_is_not_weakened(pipeline, tmp_path_factory, case, reason):
    destination = tmp_path_factory.mktemp("frontend-blocked") / "patch"
    report = generate(pipeline, destination, **({"approved_base": "f" * 40} if case == "approval" else {"credential_present": False}))
    assert report.reason == reason and report.result.outcome == "blocked"
    assert not pipeline.http.requests and not pipeline.daemon.calls and not destination.exists()


def test_frontend_missing_head_blocks_before_provider(pipeline):
    identity = PRIdentity(**{**pipeline.identity.model_dump(), "head_sha": "f" * 40})
    report = pipeline.run(selected_identity=identity)
    assert report.outcome == "blocked" and not pipeline.http.requests


def test_frontend_report_rejects_cross_stack_paths(pipeline):
    report = automation.analyze(pipeline.repo, context(pipeline.identity))
    data = report.model_dump()
    data["result"]["generated_paths"] = ("backend/tests/test_service.py",)
    with pytest.raises(ValueError, match="generated test path"):
        automation.PRReport.model_validate(data)
