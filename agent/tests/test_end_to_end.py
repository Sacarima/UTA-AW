"""Full composition over real Git objects, mocked SDK HTTP and Docker daemon.

These tests execute no candidate code and are NOT real Docker verification.
"""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
import json
from pathlib import Path

import pytest

from taskly_agent import docker_runner, end_to_end, openai_transport
from taskly_agent.config import AgentConfig
from taskly_agent.contracts import PRIdentity, RunLimits
from taskly_agent.execution import ExecutionLimits
from taskly_agent.lifecycle import ExecutionPolicy, accounted_cost
from taskly_agent.openai_transport import demo_config, openai_provider

from test_analysis import commit, git, repository, write
from test_adapters import proposal
from test_docker_runner import Daemon, IMAGE
from test_openai_transport import HTTP, api_response, isolated_process


pytestmark = pytest.mark.usefixtures("isolated_process")


@pytest.fixture
def pipeline(repository, monkeypatch, request):
    stack = getattr(request, "param", "backend")
    frontend = stack.startswith("frontend")
    create = stack == "frontend-create"
    source_path = "frontend/src/Widget.tsx" if frontend else "backend/app/service.py"
    test_path = "frontend/src/Widget.test.tsx" if frontend else "backend/tests/test_service.py"
    source = ('export function Widget() { return <button>Save</button> }\n' if frontend
              else "def value():\n    return 2\n")
    content = ("import { render, screen } from '@testing-library/react'\n"
               "import { expect, it } from 'vitest'\n"
               "import { Widget } from './Widget'\n"
               "it('shows the save action', () => {\n"
               "  render(<Widget />)\n"
               "  expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()\n"
               "})\n" if frontend else proposal().changes[0].content)
    if stack == "frontend-ts":
        source_path, test_path = "frontend/src/double.ts", "frontend/src/double.test.ts"
        source = "export const double = (value: number) => value * 2\n"
        content = ("import { expect, it } from 'vitest'\nimport { double } from './double'\n"
                   "it('doubles its input', () => { expect(double(3)).toBe(6) })\n")
    base = git(repository, "rev-parse", "HEAD")
    write(repository, source_path, source)
    if not create:
        write(repository, test_path, "// pinned original\n" if frontend else "# pinned original\n")
    head = commit(repository)
    identity = PRIdentity(repository="Sacarima/UTA-AW", number=7, base_sha=base, head_sha=head)
    config = demo_config()
    daemon = Daemon(ExecutionLimits(memory_bytes=1_073_741_824))
    # The shared daemon fixture validates labels against its module constant.
    monkeypatch.setattr("test_docker_runner.REVISION", base)
    monkeypatch.setattr(docker_runner.platform, "system", lambda: "Linux")
    starts, workspaces = [], []

    async def docker_call(self, *args, **kwargs):
        if args[0] == "start":
            header = json.loads(kwargs["data"].split(b"\n", 1)[0])
            if not header["smoke"]:
                starts.append(header)
        return await daemon.call(*args, **kwargs)

    monkeypatch.setattr(docker_runner.DockerSandbox, "_call", docker_call)
    original_apply = end_to_end.ProposalApplier.apply

    @asynccontextmanager
    async def session(selected_config):
        async with openai_provider(selected_config, http_transport=http.transport()) as provider:
            yield provider

    # Still use the complete existing SDK, provider, request validation and ledger.
    http = HTTP(api_response(proposal(test_path, "create" if create else "modify", content).model_dump_json()))
    monkeypatch.setattr(end_to_end, "openai_provider", session)
    from contextlib import contextmanager

    @contextmanager
    def apply(self, *args, **kwargs):
        with original_apply(self, *args, **kwargs) as applied:
            if applied.workspace:
                workspaces.append(Path(applied.workspace))
                assert (Path(applied.workspace) / test_path).read_text() == content
            yield applied

    monkeypatch.setattr(end_to_end.ProposalApplier, "apply", apply)

    class Pipeline:
        def run(self, *, selected_config=config, selected_identity=identity, image=IMAGE,
                trusted=base, allow_paid=True, patch_directory=None):
            return asyncio.run(end_to_end.run(repository, selected_identity, image, trusted,
                                             selected_config, allow_paid=allow_paid, patch_directory=patch_directory))

    value = Pipeline()
    value.repo, value.identity, value.config = repository, identity, config
    value.http, value.daemon, value.starts, value.workspaces = http, daemon, starts, workspaces
    value.test_path, value.source_path, value.content = test_path, source_path, content
    value.create = create
    return value


def test_complete_pipeline_uses_pinned_context_and_cleans_applied_workspace(pipeline):
    write(pipeline.repo, "backend/app/service.py", "# dirty source must not be sent\n")
    before = git(pipeline.repo, "status", "--porcelain")
    report = pipeline.run()
    assert report.outcome == "passed", report
    assert report.classification == "backend" and report.identity == pipeline.identity
    assert report.generated_paths == ("backend/tests/test_service.py",)
    assert report.verification_state == "available" and report.verification_outcome == "passed"
    assert report.tests.passed == 2 and report.coverage.lines.covered == 1
    assert report.verification_identity.head_sha == pipeline.identity.head_sha
    assert report.verification_identity.policy_digest == pipeline.config.execution_policy.digest
    assert report.model_calls == 1 and report.repairs == 0
    assert report.usage.accounted_cost_usd == report.usage.estimated_reported_cost_usd == 0.000255
    assert report.usage.unknown_cost_calls == 0 and report.usage.input_tokens == 100
    assert accounted_cost(openai_transport._PROCESS_STORE.snapshot()) == Decimal("0.000255")
    wire = pipeline.http.requests[0].content.decode()
    assert "dirty source" not in wire and "return 2" in wire
    assert pipeline.workspaces and all(not path.exists() for path in pipeline.workspaces)
    assert git(pipeline.repo, "status", "--porcelain") == before
    assert (pipeline.repo / "backend/tests/test_service.py").read_text() == "# pinned original\n"
    public = report.model_dump_json()
    for private in ("Cover the changed behavior", "def test_value", "unit-test-credential", "secret diagnostic", "req_mock"):
        assert private not in public
    assert len(pipeline.starts) == 1
    assert pipeline.starts[0]["job"]["suite"] == "backend"
    assert "OPENAI_API_KEY" not in json.dumps(pipeline.starts)


def test_only_genuine_failed_verification_triggers_one_repair(pipeline):
    pipeline.daemon.capture_overrides = {"failed": 1}
    report = pipeline.run()
    assert report.outcome == "failed" and report.reason == "repair_limit"
    assert report.model_calls == 2 and report.repairs == 1
    assert len(pipeline.http.requests) == len(pipeline.starts) == 2
    repaired = json.loads(pipeline.http.requests[1].content)["input"][1]["content"][0]["text"]
    reference = json.loads(repaired)["untrusted_reference"]
    assert reference["verification_feedback"]["outcome"] == "failed"
    assert reference["verification_feedback"]["identity"]["head_sha"] == pipeline.identity.head_sha


def test_successful_repair_is_independently_verified(pipeline, monkeypatch):
    pipeline.daemon.capture_overrides = {"failed": 1}
    original = pipeline.http.handle

    async def handle(request):
        if pipeline.http.requests:
            pipeline.daemon.capture_overrides = {}
        return await original(request)

    monkeypatch.setattr(pipeline.http, "handle", handle)
    report = pipeline.run()
    assert report.outcome == "passed" and report.verification_outcome == "passed"
    assert report.model_calls == 2 and report.repairs == 1 and len(pipeline.starts) == 2
    assert report.usage.accounted_cost_usd == 0.00051


def test_one_call_configuration_prevents_repair_invocation(pipeline):
    data = pipeline.config.model_dump()
    data["execution_policy"]["limits"]["max_model_calls"] = 1
    data["execution_policy"]["limits"]["max_repair_attempts"] = 0
    pipeline.daemon.capture_overrides = {"failed": 1}
    report = pipeline.run(selected_config=AgentConfig.model_validate(data))
    assert report.reason == "verification_failed" and report.outcome == "failed"
    assert len(pipeline.http.requests) == 1 and report.repairs == 0


@pytest.mark.parametrize("budget,reason,calls", [(0.001, "budget_exhausted", 0), (0.05, "budget_exhausted", 1)])
def test_run_cost_limit_stops_additional_calls(pipeline, budget, reason, calls):
    config = AgentConfig(**{**pipeline.config.model_dump(), "execution_policy": ExecutionPolicy(
        **{**pipeline.config.execution_policy.model_dump(), "limits": RunLimits(
            **{**pipeline.config.execution_policy.limits.model_dump(), "estimated_budget_usd": budget})})})
    pipeline.daemon.capture_overrides = {"failed": 1}
    report = pipeline.run(selected_config=config)
    assert report.outcome == "blocked" and report.reason == reason
    assert len(pipeline.http.requests) == calls


@pytest.mark.parametrize("field,value", [("image", "python:latest"), ("trusted", "a" * 40)])
def test_invalid_pinned_image_or_revision_stops_before_network(pipeline, field, value):
    report = pipeline.run(**{field: value})
    assert report.outcome == "blocked" and report.reason == "invalid_input"
    assert not pipeline.http.requests and not pipeline.daemon.calls


def test_missing_commit_objects_fail_closed(pipeline):
    identity = PRIdentity(**{**pipeline.identity.model_dump(), "head_sha": "a" * 40})
    report = pipeline.run(selected_identity=identity)
    assert report.reason == "analysis_blocked" and report.outcome == "blocked"
    assert not pipeline.http.requests


def test_mixed_guard_precedes_provider(pipeline):
    base = pipeline.identity.base_sha
    write(pipeline.repo, "frontend/src/Widget.ts", "export const value = 1\n")
    head = commit(pipeline.repo)
    identity = PRIdentity(**{**pipeline.identity.model_dump(), "base_sha": base, "head_sha": head})
    report = pipeline.run(selected_identity=identity, trusted=base)
    assert report.classification == "mixed" and report.reason == "unsupported_stack"
    assert not pipeline.http.requests and not pipeline.daemon.calls


def test_docs_only_skip_and_explicit_paid_opt_in(pipeline):
    assert pipeline.run(allow_paid=False).reason == "paid_execution_not_authorized"
    base = pipeline.identity.head_sha
    write(pipeline.repo, "README.md", "documentation\n")
    head = commit(pipeline.repo)
    identity = PRIdentity(**{**pipeline.identity.model_dump(), "base_sha": base, "head_sha": head})
    report = pipeline.run(selected_identity=identity, trusted=base)
    assert report.outcome == "skipped" and report.reason == "analysis_skipped"
    assert not pipeline.http.requests and not pipeline.daemon.calls


def test_unavailable_docker_blocks_before_opening_provider(pipeline, monkeypatch):
    monkeypatch.setattr(docker_runner.platform, "system", lambda: "Darwin")
    report = pipeline.run()
    assert report.outcome == "blocked" and report.reason == "sandbox_unavailable"
    assert not pipeline.http.requests and not pipeline.daemon.calls


@pytest.mark.parametrize("output", ["not JSON", json.dumps({"rationale": "invalid", "changes": [
    {"path": "backend/app/service.py", "operation": "modify", "content": "invalid"}]}),
    proposal("backend/tests/test_unrelated.py", "create").model_dump_json(), proposal(operation="create").model_dump_json()])
def test_malformed_and_unauthorized_proposals_are_charged_without_application(pipeline, output):
    pipeline.http.raw = api_response(output)
    report = pipeline.run()
    assert report.outcome == "failed" and report.reason == "invalid_component_output"
    assert report.model_calls == 1 and report.usage.accounted_cost_usd == 0.000255
    assert not pipeline.starts and not pipeline.workspaces


def test_unknown_usage_retains_reservation_without_verification_or_repair(pipeline):
    pipeline.http.raw = api_response(proposal().model_dump_json(), usage=False)
    report = pipeline.run()
    assert report.reason == "unknown_cost" and report.outcome == "incomplete"
    assert report.usage.unknown_cost_calls == 1 and report.usage.accounted_cost_usd == 0.05
    assert report.usage.input_tokens is None and report.repairs == 0
    assert not pipeline.starts


def test_unavailable_verification_is_not_repaired(pipeline):
    pipeline.daemon.start_failure = TimeoutError("private diagnostics")
    report = pipeline.run()
    assert report.outcome == "incomplete" and report.verification_state == "unavailable"
    assert report.verification_reason == "runner_timeout" and report.repairs == 0
    assert len(pipeline.http.requests) == 1


def test_simulator_cannot_become_real_verification(pipeline, monkeypatch):
    monkeypatch.setattr(docker_runner.DockerSandbox, "mode", "simulation")
    report = pipeline.run()
    assert report.outcome != "passed" and report.verification_state == "unavailable"
    assert report.tests is None and report.coverage is None and report.repairs == 0
    assert not pipeline.http.requests


def test_cumulative_budget_cannot_fund_provider_reservation(pipeline):
    config = AgentConfig(**{**pipeline.config.model_dump(), "cumulative_budget_usd": 0.001})
    report = pipeline.run(selected_config=config)
    assert report.reason == "budget_exhausted" and report.outcome == "blocked"
    assert not pipeline.http.requests and openai_transport._PROCESS_STORE.snapshot() == ()


def test_rejected_docker_receipt_never_passes_or_triggers_repair(pipeline, monkeypatch):
    monkeypatch.setattr(docker_runner.DockerSandbox, "consume_receipt", lambda *args: False)
    report = pipeline.run()
    assert report.outcome == "incomplete" and report.verification_state == "unavailable"
    assert report.verification_reason == "execution_not_authenticated"
    assert len(pipeline.http.requests) == 1 and report.repairs == 0


def test_mismatched_capture_head_never_produces_evidence(pipeline, monkeypatch):
    original = docker_runner.DockerSandbox.execute

    async def execute(self, *args):
        capture = await original(self, *args)
        identity = type(capture.identity)(**{**capture.identity.model_dump(), "head_sha": "f" * 40})
        return type(capture)(**{**capture.model_dump(), "identity": identity})

    monkeypatch.setattr(docker_runner.DockerSandbox, "execute", execute)
    report = pipeline.run()
    assert report.outcome != "passed" and report.verification_state == "unavailable"
    assert report.verification_reason == "capture_identity_mismatch" and report.repairs == 0


def test_provider_close_failure_preserves_independent_evidence_and_cost(pipeline, monkeypatch):
    original = end_to_end.openai_provider

    @asynccontextmanager
    async def broken_session(config):
        async with original(config) as provider:
            yield provider
        raise RuntimeError("private closing detail")

    monkeypatch.setattr(end_to_end, "openai_provider", broken_session)
    report = pipeline.run()
    assert report.outcome == "incomplete" and report.reason == "provider_session_unavailable"
    assert report.verification_outcome == "passed" and report.tests.passed == 2
    assert report.model_calls == 1 and report.usage.accounted_cost_usd > 0
    assert "private closing detail" not in report.model_dump_json()


@pytest.mark.parametrize("option", ["review", "calls", "fake"])
def test_incompatible_demo_configuration_is_rejected(pipeline, option):
    data = pipeline.config.model_dump()
    if option == "review":
        data["review_enabled"] = True
    elif option == "calls":
        data["execution_policy"]["limits"]["max_model_calls"] = 3
    else:
        data["mode"] = "fake"
    report = pipeline.run(selected_config=AgentConfig.model_validate(data))
    assert report.outcome == "blocked" and report.reason == "invalid_input"
    assert not pipeline.http.requests


def test_cli_produces_json_and_safe_human_summary(pipeline, tmp_path, capsys):
    path = tmp_path / "trusted.json"
    path.write_text(pipeline.config.model_dump_json())
    identity = pipeline.identity
    code = end_to_end.main(["--repository", str(pipeline.repo), "--name", identity.repository,
        "--number", str(identity.number), "--base", identity.base_sha, "--head", identity.head_sha,
        "--trusted-revision", identity.base_sha, "--image", IMAGE, "--config", str(path), "--allow-paid"])
    out = capsys.readouterr()
    report = json.loads(out.out)
    assert code == 0 and report["outcome"] == "passed"
    assert "verification=available/passed" in out.err and "2 passed" in out.err
    assert "def test_value" not in out.out + out.err


def test_invalid_cli_config_is_sanitized(tmp_path, capsys):
    path = tmp_path / "trusted.json"
    path.write_text('{"api_key":"private-key-must-not-appear"}')
    code = end_to_end.main(["--repository", str(tmp_path), "--name", "Sacarima/UTA-AW", "--number", "1",
        "--base", "a" * 40, "--head", "b" * 40, "--trusted-revision", "a" * 40,
        "--image", IMAGE, "--config", str(path)])
    out = capsys.readouterr()
    assert code == 1 and json.loads(out.out)["outcome"] == "blocked"
    assert "private-key" not in out.out + out.err
