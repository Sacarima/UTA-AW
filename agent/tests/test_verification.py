"""Canned reports ONLY: these tests do not execute Taskly or proposed test code."""

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import threading

import pytest

from taskly_agent.application import ApplicationResult, ProposalApplier
from taskly_agent.contracts import PRContext, VerificationIdentity
from taskly_agent.controller import ExecutionController
from taskly_agent.execution import ExecutionCapture, ExecutionLimits, execution_job
from taskly_agent.lifecycle import RunInput, proposal_digest
from taskly_agent.verification import VerifierAdapter
from taskly_agent.verification_reports import aggregate_reports, parse_capture

from test_analysis import repository
from test_application import make_applier, proposal
from test_adapters import make_system


def junit(passed=2, failed=0, skipped=0, errors=0):
    cases = '<testcase name="pass"/>' * passed + '<testcase><failure>secret diagnostic</failure></testcase>' * failed
    cases += '<testcase><skipped/></testcase>' * skipped + '<testcase><error>collection error</error></testcase>' * errors
    return (f'<testsuites><testsuite tests="{passed+failed+skipped+errors}" failures="{failed}" '
            f'skipped="{skipped}" errors="{errors}">{cases}</testsuite></testsuites>').encode()


def vitest(passed=2, failed=0, skipped=0, errors=0):
    assertions = [{"status": status} for status, count in (("passed", passed), ("failed", failed), ("skipped", skipped)) for _ in range(count)]
    bad = bool(failed or errors)
    return json.dumps({"success": not bad, "numTotalTests": passed+failed+skipped,
        "numPassedTests": passed, "numFailedTests": failed, "numPendingTests": skipped, "numTodoTests": 0,
        "numTotalTestSuites": 1, "numPassedTestSuites": int(not bad), "numFailedTestSuites": int(bad),
        "numPendingTestSuites": 0, "testResults": [{"status": "failed" if bad else "passed",
        "assertionResults": assertions, "message": "secret diagnostic" if bad else ""}]}).encode()


def coverage(suite):
    if suite == "backend":
        summary = dict(covered_lines=1, num_statements=2, covered_branches=1, num_branches=2)
        return json.dumps({"meta": {"branch_coverage": True}, "files": {"app/api.py": {"summary": summary}},
            "totals": summary}).encode()
    summary = {key: {"covered": 1, "total": 2, "skipped": 0, "pct": 50} for key in ("lines", "branches", "statements")}
    return json.dumps({"total": summary, "/workspace/frontend/src/a.ts": summary}).encode()


IDENTITY = VerificationIdentity(repository="Sacarima/UTA-AW", head_sha="a" * 40,
    proposal_digest="b" * 64, policy_digest="c" * 64)


def capture(suite="backend", *, passed=2, failed=0, skipped=0, errors=0, **updates):
    return ExecutionCapture(**{**dict(suite=suite, identity=IDENTITY, state="completed",
        exit_code=int(bool(failed or errors)), tests_report=(junit if suite == "backend" else vitest)(passed, failed, skipped, errors),
        coverage_report=coverage(suite)), **updates})


def parse(value):
    return parse_capture(value, ExecutionLimits())


@pytest.mark.parametrize("suite", ["backend", "frontend"])
def test_fixed_commands_preserve_project_configuration_and_no_host_environment(suite, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-copy")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-copy")
    job = execution_job(suite, IDENTITY)
    assert job.cwd == suite and job.identity == IDENTITY
    assert "must-not-copy" not in job.model_dump_json()
    assert dict(job.environment) == {"CI": "true", "HOME": "/tmp/empty-home",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    assert job.tests_artifact.startswith("/reports/")
    if suite == "backend":
        assert job.argv[:7] == ("/opt/venv/bin/python", "-I", "-m", "pytest", "-p", "pytest_cov.plugin", "tests")
        assert "--cov=app" in job.argv and "--junitxml=/reports/tests.xml" in job.argv
    else:
        assert job.argv[:3] == ("/usr/local/bin/node", "/opt/frontend/node_modules/vitest/vitest.mjs", "run")
        assert "--coverage" in job.argv and "--reporter=json" in job.argv
    assert all("install" not in arg and "shell" not in arg for arg in job.argv)
    with pytest.raises(ValueError):
        execution_job("backend; shell", IDENTITY)


@pytest.mark.parametrize("suite", ["backend", "frontend"])
@pytest.mark.parametrize("counts,outcome", [((2, 0, 1, 0), "passed"), ((1, 1, 0, 0), "failed"),
    ((0, 0, 0, 1), "failed"), ((0, 0, 2, 0), "incomplete")])
def test_structured_counts_and_coverage(suite, counts, outcome):
    result = parse(capture(suite, passed=counts[0], failed=counts[1], skipped=counts[2], errors=counts[3]))
    assert result.outcome == outcome
    assert tuple(result.tests.model_dump().values()) == counts
    assert result.coverage.state == "available"
    assert result.coverage.metrics.lines.covered == 1 and result.coverage.metrics.lines.total == 2
    assert "secret" not in result.model_dump_json()


def test_pytest_collection_exit_two_is_structured_failure():
    result = parse(capture(errors=1, passed=0, exit_code=2))
    assert result.outcome == "failed" and result.tests.errors == 1


@pytest.mark.parametrize("updates,reason", [
    ({"state": "timeout"}, "runner_timeout"), ({"state": "crashed"}, "runner_crashed"),
    ({"state": "unavailable"}, "runner_unavailable"), ({"exit_code": -9}, "runner_exit"),
    ({"exit_code": None}, "runner_exit"), ({"exit_code": 5}, "runner_exit"),
    ({"tests_report": None}, "test_report_missing"), ({"tests_report": b"not xml"}, "test_report_invalid"),
    ({"tests_report": b'<!DOCTYPE x [<!ENTITY x "bad">]><testsuites/>'}, "test_report_invalid"),
    ({"exit_code": 1}, "test_report_invalid"),
])
def test_missing_malformed_crashed_and_conflicting_tests_never_pass(updates, reason):
    result = parse(capture(**updates))
    assert result.outcome == "incomplete" and result.reason == reason and result.tests is None


@pytest.mark.parametrize("suite", ["backend", "frontend"])
@pytest.mark.parametrize("bad", [None, b"broken", b'{"total":{}}'])
def test_missing_or_malformed_coverage_is_explicit(suite, bad):
    result = parse(capture(suite, coverage_report=bad))
    assert result.outcome == "incomplete" and result.tests.passed == 2
    assert result.coverage.state == ("missing" if bad is None else "unavailable")


@pytest.mark.parametrize("suite", ["backend", "frontend"])
@pytest.mark.parametrize("case", ["mismatch", "negative", "bool", "covered_gt_total"])
def test_conflicting_coverage_counts_fail_closed(suite, case):
    data = json.loads(coverage(suite))
    target = data["totals"] if suite == "backend" else data["total"]["lines"]
    key = "covered_lines" if suite == "backend" else "covered"
    target[key] = {"mismatch": 0, "negative": -1, "bool": True, "covered_gt_total": 3}[case]
    result = parse(capture(suite, coverage_report=json.dumps(data).encode()))
    assert result.coverage.state == "unavailable" and result.outcome == "incomplete"


def test_duplicate_json_keys_and_vitest_totals_rejected():
    result = parse(capture("frontend", tests_report=b'{"success":true,"success":false}'))
    assert result.reason == "test_report_invalid"
    data = json.loads(vitest())
    data["numPassedTests"] = 100
    assert parse(capture("frontend", tests_report=json.dumps(data).encode())).reason == "test_report_invalid"
    data = json.loads(vitest())
    data["success"] = False
    assert parse(capture("frontend", tests_report=json.dumps(data).encode())).reason == "test_report_invalid"
    data = json.loads(vitest(passed=1, skipped=1))
    data["testResults"][0]["assertionResults"][1]["status"] = "pending"
    assert parse(capture("frontend", tests_report=json.dumps(data).encode())).outcome == "incomplete"


def test_xml_declared_counts_must_match_testcases():
    bad = junit().replace(b'tests="2"', b'tests="3"')
    assert parse(capture(tests_report=bad)).reason == "test_report_invalid"


def test_failed_tests_remain_failed_without_coverage():
    result = parse(capture(failed=1, coverage_report=None))
    assert result.outcome == "failed" and result.coverage.state == "missing"


@pytest.mark.parametrize("field", ["stdout", "stderr", "tests_report", "coverage_report"])
def test_output_is_bounded_and_never_echoed(field):
    limits = ExecutionLimits(max_output_bytes=8, max_report_bytes=4096)
    length = 9 if field in {"stdout", "stderr"} else 4097
    result = parse_capture(capture(**{field: b"x" * length}), limits)
    assert result.reason == "output_limit" and result.outcome == "incomplete"


def test_mixed_aggregation_requires_both_suites_and_complete_coverage():
    backend, frontend = parse(capture()), parse(capture("frontend"))
    summary = aggregate_reports((backend, frontend), ("backend", "frontend"))
    assert summary.outcome == "passed" and summary.tests.passed == 4
    assert summary.coverage.metrics.lines.covered == 2 and summary.coverage.metrics.lines.total == 4
    for reports in ((backend,), (backend, backend)):
        summary = aggregate_reports(reports, ("backend", "frontend"))
        assert summary.outcome == "incomplete" and summary.tests is None
    missing = parse(capture("frontend", coverage_report=None))
    summary = aggregate_reports((backend, missing), ("backend", "frontend"))
    assert summary.outcome == "incomplete" and summary.coverage.state == "unavailable"
    failed = parse(capture("frontend", failed=1))
    assert aggregate_reports((backend, failed), ("backend", "frontend")).outcome == "failed"


class SimulatedRunner:
    mode = "simulation"

    def __init__(self):
        self.jobs = []
        self.workspace = None
        self.error = None
        self.overrides = {}
        self.wait = None

    async def execute(self, workspace, job, limits):
        assert workspace.exists()
        self.workspace = workspace
        self.jobs.append(job)
        if self.wait is not None:
            self.wait.set()
            try:
                await asyncio.Event().wait()
            finally:
                assert workspace.exists()  # lifetime includes runner cancellation cleanup
        if self.error:
            raise self.error
        return capture(job.suite, identity=job.identity, **self.overrides)


def request_for(applier):
    return RunInput(pr=PRContext(identity=applier.identity, changed_files=()))


def verify(applier, runner=None, proposed=None):
    from taskly_agent.lifecycle import ExecutionPolicy
    policy = ExecutionPolicy(content_limits=applier.content_limits)
    return asyncio.run(VerifierAdapter(applier, runner).verify(request_for(applier),
        proposed or proposal("backend/tests/test_service.py"), policy))


def test_default_real_execution_is_unavailable_without_applying_or_executing(make_applier, monkeypatch):
    applier = make_applier()
    monkeypatch.setattr(applier, "apply", lambda *args: pytest.fail("unavailable sandbox must not prepare execution"))
    evidence = verify(applier)
    assert evidence.state == "unavailable" and evidence.outcome == "blocked"
    assert evidence.reason == "sandbox_unavailable" and evidence.tests is None
    assert evidence.identity.head_sha == applier.identity.head_sha
    assert evidence.identity.proposal_digest == proposal_digest(proposal("backend/tests/test_service.py"))


@pytest.mark.parametrize("mode", ["host", "live", "docker", "secure"])
def test_unvalidated_sandbox_modes_are_rejected(make_applier, mode):
    applier = make_applier()
    runner = SimulatedRunner()
    runner.mode = mode
    evidence = verify(applier, runner)
    assert evidence.reason == "unsafe_sandbox" and evidence.outcome == "blocked" and runner.jobs == []


@pytest.mark.parametrize("files,target,suites", [
    ({"backend/app/service.py": "# code\n"}, "backend/tests/test_service.py", ("backend",)),
    ({"frontend/src/Widget.tsx": "// code\n"}, "frontend/src/Widget.test.tsx", ("frontend",)),
    ({"backend/app/service.py": "# code\n", "frontend/src/Widget.tsx": "// code\n"},
        "backend/tests/test_service.py", ("backend", "frontend")),
])
def test_simulation_uses_analysis_stacks_and_scoped_pinned_workspace(make_applier, files, target, suites):
    applier = make_applier(files)
    runner = SimulatedRunner()
    evidence = verify(applier, runner, proposal(target))
    assert tuple(job.suite for job in runner.jobs) == suites
    assert not runner.workspace.parent.exists()
    assert evidence.state == "unavailable" and evidence.outcome == "incomplete" and evidence.tests is None
    assert evidence.reason == "simulation_only:aggregate_passed"
    assert all(job.identity == evidence.identity for job in runner.jobs)
    assert list(applier._parent.iterdir()) == []


@pytest.mark.parametrize("failure", ["exception", "timeout", "malformed", "identity"])
def test_simulated_runner_failures_clean_workspace_and_cannot_pass(make_applier, failure):
    applier = make_applier()
    runner = SimulatedRunner()
    if failure == "exception":
        runner.error = RuntimeError("secret diagnostics")
    elif failure == "timeout":
        runner.error = TimeoutError("secret timeout")
    elif failure == "malformed":
        runner.overrides = {"tests_report": b"malformed secret report"}
    else:
        async def mismatch(workspace, job, limits):
            runner.workspace = workspace
            return capture(identity=IDENTITY)
        runner.execute = mismatch
    evidence = verify(applier, runner)
    assert evidence.state == "unavailable" and evidence.outcome != "passed"
    assert not runner.workspace.parent.exists() and "secret" not in evidence.model_dump_json()


def test_task_cancellation_keeps_workspace_until_runner_stops_then_cleans(make_applier):
    applier = make_applier()
    from taskly_agent.lifecycle import ExecutionPolicy

    async def run():
        runner = SimulatedRunner()
        runner.wait = asyncio.Event()
        verifier = VerifierAdapter(applier, runner)
        task = asyncio.create_task(verifier.verify(request_for(applier), proposal("backend/tests/test_service.py"), ExecutionPolicy()))
        await asyncio.wait_for(runner.wait.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not runner.workspace.parent.exists()
    asyncio.run(run())


def test_cancellation_during_threaded_preparation_reaps_then_cleans(make_applier, monkeypatch):
    applier = make_applier()
    from taskly_agent.lifecycle import ExecutionPolicy
    started, release = threading.Event(), threading.Event()
    original = applier._populate

    def slow(root, plan):
        started.set()
        assert release.wait(10)
        original(root, plan)
    monkeypatch.setattr(applier, "_populate", slow)

    async def run():
        runner = SimulatedRunner()
        task = asyncio.create_task(VerifierAdapter(applier, runner).verify(request_for(applier),
            proposal("backend/tests/test_service.py"), ExecutionPolicy()))
        assert await asyncio.to_thread(started.wait, 10)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runner.jobs == [] and list(applier._parent.iterdir()) == []
    asyncio.run(run())


def test_application_digest_mismatch_never_reaches_runner(make_applier, monkeypatch):
    applier = make_applier()
    original = applier.apply

    @contextmanager
    def forged(identity, proposed, **kwargs):
        with original(identity, proposed, **kwargs) as result:
            yield ApplicationResult.model_validate({**result.model_dump(), "proposal_digest": "0" * 64})
    monkeypatch.setattr(applier, "apply", forged)
    runner = SimulatedRunner()
    evidence = verify(applier, runner)
    assert evidence.reason == "application_identity_mismatch" and runner.jobs == []
    assert list(applier._parent.iterdir()) == []


def test_full_controller_fake_provider_and_independent_simulation_cannot_claim_real_pass(make_system, tmp_path_factory):
    system = make_system()
    applier = ProposalApplier(system.analyzer, identity=system.request.pr.identity,
        temporary_parent=tmp_path_factory.mktemp("verification"))
    runner = SimulatedRunner()
    controller = ExecutionController(analyzer=system.analyzer, generator=system.generator,
        reviewer=system.reviewer, verifier=VerifierAdapter(applier, runner))
    report = asyncio.run(controller.run_configured(system.request, system.config))
    assert report.result.outcome == "incomplete" and report.reason_code == "unavailable_evidence"
    assert report.model_calls == 1 and report.repair_attempts == 0 and report.result.review is None
    evidence = report.result.verification
    assert evidence.state == "unavailable" and evidence.reason.startswith("simulation_only:")
    assert evidence.identity.proposal_digest == proposal_digest(report.proposal)
    assert evidence.identity.policy_digest == system.config.execution_policy.digest
    assert not runner.workspace.parent.exists()


def test_actual_runner_wall_timeout_cleans_workspace(make_applier):
    applier = make_applier()
    from taskly_agent.lifecycle import ExecutionPolicy

    async def run():
        runner = SimulatedRunner()
        runner.wait = asyncio.Event()
        evidence = await VerifierAdapter(applier, runner, limits=ExecutionLimits(wall_seconds=1)).verify(
            request_for(applier), proposal("backend/tests/test_service.py"), ExecutionPolicy())
        assert evidence.reason == "runner_timeout" and evidence.state == "unavailable"
        assert not runner.workspace.parent.exists()
    asyncio.run(run())


def test_controller_cancellation_during_verification_cleans_before_report(make_system, tmp_path_factory):
    system = make_system()
    applier = ProposalApplier(system.analyzer, identity=system.request.pr.identity,
        temporary_parent=tmp_path_factory.mktemp("verification-cancellation"))

    async def run():
        runner = SimulatedRunner()
        runner.wait = asyncio.Event()
        controller = ExecutionController(analyzer=system.analyzer, generator=system.generator,
            verifier=VerifierAdapter(applier, runner))
        task = asyncio.create_task(controller.run_configured(system.request, system.config))
        await asyncio.wait_for(runner.wait.wait(), 10)
        task.cancel()
        report = await task
        assert report.result.outcome == "incomplete" and report.reason_code == "cancelled"
        assert report.result.verification is None and report.model_calls == 1
        assert not runner.workspace.parent.exists()
    asyncio.run(run())


def test_head_mismatch_blocks_before_workspace_or_runner(make_applier):
    from taskly_agent.contracts import PRIdentity
    from taskly_agent.lifecycle import ExecutionPolicy

    applier = make_applier()
    runner = SimulatedRunner()
    wrong = PRIdentity(**{**applier.identity.model_dump(), "head_sha": "a" * 40})
    evidence = asyncio.run(VerifierAdapter(applier, runner).verify(
        RunInput(pr=PRContext(identity=wrong, changed_files=())), proposal("backend/tests/test_service.py"), ExecutionPolicy()))
    assert evidence.reason == "identity_mismatch" and evidence.outcome == "blocked"
    assert runner.jobs == [] and list(applier._parent.iterdir()) == []


def test_application_rejection_and_cleanup_failure_never_run_or_pass(make_applier, monkeypatch):
    from taskly_agent.application import WorkspaceCleanupError

    applier = make_applier()
    runner = SimulatedRunner()
    evidence = verify(applier, runner, proposal("backend/tests/test_unrelated.py"))
    assert evidence.reason == "application_failed" and runner.jobs == []
    original = applier.apply

    @contextmanager
    def cleanup_failure(identity, proposed, **kwargs):
        with original(identity, proposed, **kwargs) as result:
            yield result
        raise WorkspaceCleanupError("fixed cleanup failure")

    monkeypatch.setattr(applier, "apply", cleanup_failure)
    evidence = verify(applier, runner)
    assert evidence.reason == "cleanup_failed" and evidence.state == "unavailable"
    assert not runner.workspace.parent.exists()


def test_preparation_cancellation_stops_between_git_object_reads(make_applier, monkeypatch):
    applier = make_applier({"backend/app/service.py": "# code\n", "docs/a.txt": "# more\n"})
    cancellation = threading.Event()
    original = applier.analyzer.reader.blob
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(args[0])
        data = original(*args, **kwargs)
        cancellation.set()
        return data

    monkeypatch.setattr(applier.analyzer.reader, "blob", interrupted)
    with applier.apply(applier.identity, proposal("backend/tests/test_service.py"), cancellation=cancellation) as result:
        assert result.state == "failed" and result.reason == "cancelled"
        assert len(calls) == 1 and list(applier._parent.iterdir()) == []
