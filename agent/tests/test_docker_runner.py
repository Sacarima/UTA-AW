"""Deterministic Docker control-plane tests; no daemon or PR code execution."""
import asyncio
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

from taskly_agent import docker_runner
from taskly_agent.application import ProposalApplier
from taskly_agent.docker_runner import DockerSandbox, SandboxError, create_arguments, transfer, validate_container
from taskly_agent.execution import ExecutionCapture, ExecutionLimits, execution_job
from taskly_agent.verification import VerifierAdapter
from taskly_agent.lifecycle import ExecutionPolicy

from test_analysis import repository
from test_application import make_applier, proposal
from test_verification import IDENTITY, capture, request_for
from test_adapters import make_system, proposal as generated_proposal


IMAGE = "sha256:" + "a" * 64
REVISION = "b" * 40


def container(limits):
    arguments = create_arguments("name", IMAGE, limits)
    return {"Image": IMAGE, "Config": {"User": "10001:10001", "Cmd": [],
        "Entrypoint": ["/opt/venv/bin/python", "-I", "/opt/taskly/launch.py"]}, "Mounts": [],
        "State": {"Running": False, "Pid": 0, "OOMKilled": False, "ExitCode": 0},
        "HostConfig": {"NetworkMode": "none", "ReadonlyRootfs": True, "Privileged": False,
        "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"], "CgroupnsMode": "private",
        "PidMode": "", "IpcMode": "private", "UTSMode": "", "PidsLimit": limits.max_processes,
        "Memory": limits.memory_bytes, "MemorySwap": limits.memory_bytes, "NanoCpus": 1_000_000_000,
        "LogConfig": {"Type": "none"}, "Ulimits": [{"Name": "cpu", "Hard": limits.cpu_seconds, "Soft": limits.cpu_seconds}],
        "Tmpfs": dict(argument.split("=", 1)[1].split(":", 1) for argument in arguments if argument.startswith("--tmpfs="))}}


class Daemon:
    def __init__(self, limits):
        self.limits = limits
        self.calls = []
        self.wait = None
        self.start_failure = None
        self.smoke_bad = False
        self.capture_overrides = {}

    async def call(self, *arguments, data=b"", **kwargs):
        self.calls.append(arguments)
        if arguments[0] == "info":
            return 0, json.dumps({"OSType": "linux", "CgroupVersion": "2",
                "SecurityOptions": ["name=seccomp,profile=builtin"]}).encode()
        if arguments[:2] == ("image", "inspect"):
            config = container(self.limits)["Config"]
            config["Labels"] = {"taskly.trusted_revision": REVISION, "taskly.profile": "v1"}
            return 0, json.dumps([{"Id": IMAGE, "Config": config}]).encode()
        if arguments[0] == "create":
            return 0, ("c" * 64).encode()
        if arguments[0] == "inspect":
            return 0, json.dumps([container(self.limits)]).encode()
        if arguments[0] == "start":
            header = json.loads(data.split(b"\n", 1)[0])
            if header["smoke"]:
                return 0, json.dumps({"smoke": "bad" if self.smoke_bad else "ok"}).encode()
            if self.wait is not None:
                self.wait.set()
                await asyncio.Event().wait()
            if self.start_failure:
                raise self.start_failure
            value = capture(header["job"]["suite"], **self.capture_overrides)
            return 0, json.dumps({"state": value.state, "exit_code": value.exit_code,
                "tests_report": base64.b64encode(value.tests_report).decode() if value.tests_report is not None else None,
                "coverage_report": base64.b64encode(value.coverage_report).decode() if value.coverage_report is not None else None}).encode()
        if arguments[0] in {"rm", "ps"}:
            return 0, b""
        raise AssertionError("unexpected Docker command")


@pytest.fixture
def runtime(monkeypatch):
    limits = ExecutionLimits()
    daemon = Daemon(limits)
    runner = DockerSandbox(IMAGE, REVISION)
    monkeypatch.setattr(docker_runner.platform, "system", lambda: "Linux")
    monkeypatch.setattr(runner, "_call", daemon.call)
    return runner, daemon, limits


@pytest.mark.parametrize("key,bad", [("NetworkMode", "host"), ("ReadonlyRootfs", False), ("Privileged", True),
    ("CapDrop", []), ("SecurityOpt", ["seccomp=unconfined"]), ("CgroupnsMode", "host"),
    ("PidMode", "host"), ("IpcMode", "host"), ("Memory", 0), ("MemorySwap", -1),
    ("PidsLimit", 0), ("NanoCpus", 0), ("Binds", ["/:/host"]), ("Devices", ["/dev/mem"]),
    ("CapAdd", ["SYS_ADMIN"]), ("PortBindings", {"80/tcp": []}), ("VolumesFrom", ["host"]), ("Tmpfs", {})])
def test_enforcement_rejects_weakened_container_configuration(key, bad):
    limits = ExecutionLimits()
    value = container(limits)
    value["HostConfig"][key] = bad
    with pytest.raises(SandboxError):
        validate_container(value, IMAGE, limits)


def test_fixed_flags_and_no_host_mounts_or_environment():
    arguments = create_arguments("name", IMAGE, ExecutionLimits())
    assert arguments[-1] == IMAGE
    assert "--network=none" in arguments and "--read-only" in arguments
    assert not any(argument.startswith(("--volume", "--mount", "--env", "--privileged")) for argument in arguments)
    validate_container(container(ExecutionLimits()), IMAGE, ExecutionLimits())


def test_image_and_user_mismatch_rejected():
    for key, value in (("Image", "wrong"), ("Mounts", [{"Type": "bind", "Destination": "/host"}])):
        info = container(ExecutionLimits())
        info[key] = value
        with pytest.raises(SandboxError):
            validate_container(info, IMAGE, ExecutionLimits())
    with pytest.raises(SandboxError):
        DockerSandbox("python:latest", REVISION)


def test_unsupported_host_rejected(runtime, monkeypatch):
    runner, daemon, limits = runtime
    monkeypatch.setattr(docker_runner.platform, "system", lambda: "Darwin")
    with pytest.raises(SandboxError):
        asyncio.run(runner.validate(limits, IDENTITY))
    assert daemon.calls == []


def test_runtime_probe_required_and_failed_probe_cannot_enable_execution(runtime, tmp_path):
    runner, daemon, limits = runtime

    async def run():
        with pytest.raises(SandboxError):
            await runner.execute(tmp_path, execution_job("backend", IDENTITY), limits)
        daemon.smoke_bad = True
        with pytest.raises(SandboxError):
            await runner.validate(limits, IDENTITY)
        assert not runner._validated
    asyncio.run(run())
    assert daemon.calls[-2][0] == "rm" and daemon.calls[-1][0] == "ps"


def test_receipts_are_single_use_identity_bound_and_after_cleanup(runtime, tmp_path):
    runner, daemon, limits = runtime
    (tmp_path / "source file 任务.py").write_text("# data\n")

    async def run():
        await runner.validate(limits, IDENTITY)
        job = execution_job("backend", IDENTITY)
        value = await runner.execute(tmp_path, job, limits)
        assert daemon.calls[-2][0] == "rm" and daemon.calls[-1][0] == "ps"
        assert runner.consume_receipt(ExecutionCapture.model_validate(value), job)
        assert not runner.consume_receipt(value, job)
        value = await runner.execute(tmp_path, job, limits)
        modified = ExecutionCapture(**{**value.model_dump(), "tests_report": b"forged"})
        assert not runner.consume_receipt(modified, job)
        assert not runner.consume_receipt(value, job)
    asyncio.run(run())


@pytest.mark.parametrize("error", [TimeoutError("sensitive"), RuntimeError("sensitive")])
def test_execution_failure_removes_container_before_return(runtime, tmp_path, error):
    runner, daemon, limits = runtime

    async def run():
        await runner.validate(limits, IDENTITY)
        daemon.start_failure = error
        with pytest.raises(type(error)):
            await runner.execute(tmp_path, execution_job("backend", IDENTITY), limits)
        assert daemon.calls[-2][0] == "rm" and daemon.calls[-1][0] == "ps" and runner._receipts == {}
    asyncio.run(run())


def test_async_cancellation_reaps_entire_container_before_propagating(runtime, tmp_path):
    runner, daemon, limits = runtime

    async def run():
        await runner.validate(limits, IDENTITY)
        daemon.wait = asyncio.Event()
        task = asyncio.create_task(runner.execute(tmp_path, execution_job("frontend", IDENTITY), limits))
        await daemon.wait.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert daemon.calls[-2][0] == "rm" and daemon.calls[-1][0] == "ps" and runner._receipts == {}
    asyncio.run(run())


@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_transfer_rejects_links(tmp_path, link):
    source = tmp_path / "source"
    source.write_text("data")
    target = tmp_path / "target"
    if link == "symlink":
        target.symlink_to(source)
    else:
        import os
        os.link(source, target)
    with pytest.raises(SandboxError):
        transfer(tmp_path, execution_job("backend", IDENTITY), ExecutionLimits())


def test_transfer_preserves_unicode_bytes_and_omits_metadata(tmp_path):
    path = "source file 任务.py"
    (tmp_path / path).write_bytes(b"# exact\n")
    value = transfer(tmp_path, execution_job("backend", IDENTITY), ExecutionLimits())
    header, archive = value.split(b"\n", 1)
    assert json.loads(header)["job"]["identity"]["head_sha"] == IDENTITY.head_sha
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        assert source.getnames() == [path]
        assert source.extractfile(path).read() == b"# exact\n"


def test_trusted_launcher_rejects_archive_traversal_and_report_links(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    path = Path(__file__).parents[1] / "sandbox/launch.py"
    spec = importlib.util.spec_from_file_location("trusted_launcher_test", path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        entry = tarfile.TarInfo("../outside")
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"x"))
    data.seek(0)
    with pytest.raises(ValueError):
        launcher.unpack(data, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("overrides,outcome", [({}, "passed"), ({"failed": 1}, "failed"),
    ({"coverage_report": None}, "incomplete")])
def test_real_adapter_boundary_with_fake_daemon_uses_actual_parser_and_applier(runtime, make_applier, overrides, outcome):
    runner, daemon, limits = runtime
    daemon.capture_overrides = overrides
    applier = make_applier()
    evidence = asyncio.run(VerifierAdapter(applier, runner).verify(request_for(applier),
        proposal("backend/tests/test_service.py"), ExecutionPolicy()))
    assert evidence.state == "available" and evidence.outcome == outcome
    assert evidence.identity.head_sha == applier.identity.head_sha
    assert evidence.identity.policy_digest == ExecutionPolicy().digest
    assert list(applier._parent.iterdir()) == []


def test_workflow_security_properties_and_image_pins():
    root = Path(__file__).parents[2]
    workflow = (root / ".github/workflows/taskly-verification.yml").read_text()
    assert "pull_request:" in workflow and "contents: read" in workflow
    assert "pull_request_target" not in workflow and "secrets." not in workflow and "write" not in workflow
    assert workflow.count("persist-credentials: false") == 2
    assert "taskly_agent.ci" in workflow and "env -i" in workflow
    assert "working-directory: candidate" not in workflow and "run: npm" not in workflow
    import re
    assert all(re.fullmatch(r"[0-9a-f]{40}", pin) for pin in re.findall(r"uses: [^@]+@([^\s]+)", workflow))
    dockerfile = (root / "agent/sandbox/Dockerfile").read_text()
    assert len(re.findall(r"FROM [^\n]+@sha256:[0-9a-f]{64}", dockerfile)) == 2
    assert "npm ci --ignore-scripts" in dockerfile and "USER 10001:10001" in dockerfile


@pytest.mark.parametrize("failure", ["output_limit", "timeout", "cancellation"])
def test_cli_is_sanitized_bounded_and_reaped_without_real_processes(monkeypatch, failure):
    kills = []
    class Stream:
        async def read(self, size):
            if failure == "output_limit":
                return b"secret oversized output"
            await asyncio.Event().wait()
    class Input:
        def write(self, data):
            pass
        async def drain(self):
            pass
        def close(self):
            pass
    class Process:
        pid = 424242
        returncode = None
        stdout = Stream()
        stderr = Stream()
        stdin = Input()
        async def wait(self):
            return -9
    async def spawn(*arguments, **kwargs):
        assert arguments == ("/usr/bin/docker", "info")
        assert kwargs["start_new_session"] and "OPENAI_API_KEY" not in kwargs["env"]
        assert kwargs["env"]["DOCKER_HOST"] == "unix:///var/run/docker.sock"
        return Process()
    monkeypatch.setattr(docker_runner.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(docker_runner.os, "killpg", lambda pid, sig: kills.append(pid))
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    async def run():
        runner = DockerSandbox(IMAGE, REVISION)
        if failure == "cancellation":
            task = asyncio.create_task(runner._call("info"))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(SandboxError if failure == "output_limit" else TimeoutError) as error:
                await runner._call("info", maximum=8, timeout=0.01)
            assert "secret" not in str(error.value)
    asyncio.run(run())
    assert kills == [424242]


def test_failed_cleanup_cannot_issue_receipt(runtime, tmp_path, monkeypatch):
    runner, daemon, limits = runtime
    async def run():
        await runner.validate(limits, IDENTITY)
        original = daemon.call
        async def failure(*args, **kwargs):
            if args[0] == "ps":
                return 0, b"still-running-container"
            return await original(*args, **kwargs)
        monkeypatch.setattr(runner, "_call", failure)
        with pytest.raises(SandboxError):
            await runner.execute(tmp_path, execution_job("backend", IDENTITY), limits)
        assert not runner._validated and runner._receipts == {}
    asyncio.run(run())


def test_changed_limits_require_new_probe(runtime, tmp_path):
    runner, daemon, limits = runtime
    async def run():
        await runner.validate(limits, IDENTITY)
        changed = ExecutionLimits(memory_bytes=limits.memory_bytes * 2)
        with pytest.raises(SandboxError):
            await runner.execute(tmp_path, execution_job("backend", IDENTITY), changed)
    asyncio.run(run())


def test_baseline_runs_both_stacks_without_fake_proposal(runtime, make_applier, monkeypatch):
    from taskly_agent import ci
    from taskly_agent.execution import BaselineIdentity
    runner, daemon, _ = runtime
    daemon.limits = ExecutionLimits(memory_bytes=1_073_741_824)
    applier = make_applier({"README.md": "# docs-only PR\n"})
    monkeypatch.setattr(ci, "DockerSandbox", lambda image, trusted: runner)
    reports, summary = asyncio.run(ci.baseline(applier.analyzer.reader.repository, applier.identity, IMAGE, REVISION))
    assert tuple(report.suite for report in reports) == ("backend", "frontend")
    assert summary.outcome == "passed" and summary.tests.passed == 4
    # Public receipt schema cannot confuse a baseline and a TestProposal.
    baseline = BaselineIdentity(repository=IDENTITY.repository, head_sha=IDENTITY.head_sha,
        policy_digest=IDENTITY.policy_digest, baseline_digest="d" * 64)
    assert "proposal_digest" not in baseline.model_dump()


def test_controller_proposal_path_uses_authenticated_capture_only(runtime, make_system, tmp_path_factory):
    from taskly_agent.controller import ExecutionController
    runner, daemon, limits = runtime
    system = make_system()
    system.fake.outputs = [generated_proposal().model_dump_json(), '{"findings":[]}']
    applier = ProposalApplier(
        system.analyzer, identity=system.request.pr.identity, temporary_parent=tmp_path_factory.mktemp("live-boundary"))
    controller = ExecutionController(analyzer=system.analyzer, generator=system.generator, reviewer=system.reviewer,
        verifier=VerifierAdapter(applier, runner))
    report = asyncio.run(controller.run_configured(system.request, system.config))
    assert report.result.outcome == "passed" and report.result.verification.state == "available"
    assert report.model_calls == 2 and report.result.review.findings == ()
    assert runner._receipts == {} and list(applier._parent.iterdir()) == []


@pytest.mark.parametrize("kind", ["valid", "symlink_file", "symlink_parent", "hardlink", "oversized", "fifo"])
def test_report_capture_rejects_unsafe_paths_and_types(tmp_path, monkeypatch, kind):
    import os
    monkeypatch.setattr(sys, "path", list(sys.path))
    source = Path(__file__).parents[1] / "sandbox/launch.py"
    spec = importlib.util.spec_from_file_location("trusted_report_test", source)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    reports = tmp_path / "reports"
    reports.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"{}")
    target = reports / "tests.json"
    artifact = "/reports/tests.json"
    if kind == "symlink_file":
        target.symlink_to(outside)
    elif kind == "symlink_parent":
        (reports / "coverage").symlink_to(tmp_path, target_is_directory=True)
        artifact = "/reports/coverage/outside"
    elif kind == "hardlink":
        os.link(outside, target)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_bytes(b"oversized" if kind == "oversized" else b"{}")
    real_open = os.open
    def redirect(path, flags, *args, **kwargs):
        return real_open(reports if path == "/reports" else path, flags, *args, **kwargs)
    monkeypatch.setattr(launcher.os, "open", redirect)
    if kind == "valid":
        assert base64.b64decode(launcher.report(artifact, 4)) == b"{}"
    else:
        with pytest.raises((OSError, ValueError)):
            launcher.report(artifact, 4)
    assert outside.read_bytes() == b"{}"


@pytest.mark.parametrize("case", ["matching", "changed", "missing"])
def test_candidate_dependencies_must_match_trusted_provisioning(tmp_path, monkeypatch, case):
    monkeypatch.setattr(sys, "path", list(sys.path))
    path = Path(__file__).parents[1] / "sandbox/launch.py"
    spec = importlib.util.spec_from_file_location("trusted_dependencies_test", path)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    workspace, provisioned = tmp_path / "workspace", tmp_path / "provisioned"
    pairs = (("backend/requirements.txt", "backend-requirements.txt"),
        ("frontend/package.json", "frontend/package.json"), ("frontend/package-lock.json", "frontend/package-lock.json"))
    for source, trusted in pairs:
        for root, relative in ((workspace, source), (provisioned, trusted)):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"pinned")
    target = workspace / "frontend/package-lock.json"
    if case == "changed":
        target.write_bytes(b"untrusted different graph")
    elif case == "missing":
        target.unlink()
    if case == "matching":
        launcher.check_dependencies(workspace, provisioned)
    else:
        with pytest.raises((ValueError, OSError)):
            launcher.check_dependencies(workspace, provisioned)
