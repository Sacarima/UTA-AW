"""Trusted image entrypoint. Never import this module from the PR checkout.

Only stdin's bounded header selects a fixed plan; archive members are plain
files. Reports are sandbox observations, not proof against malicious in-process
test/report forgery. No diagnostics or PR strings are emitted to Actions logs.
"""
import base64
import json
import os
from pathlib import Path
import resource
import selectors
import signal
import socket
import stat
import subprocess
import sys
import tarfile

sys.path.insert(0, "/opt/agent")
from taskly_agent.execution import ExecutionJob, ExecutionLimits, execution_job
from taskly_agent.policy import validate_repository_path


def unpack(stream, destination=Path("/workspace")):
    count = total = 0
    root = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with tarfile.open(fileobj=stream, mode="r|") as archive:
            for member in archive:
                validate_repository_path(member.name)
                parts = member.name.split("/")
                count += 1
                total += member.size
                if not member.isfile() or count > 2000 or total > 32_000_000 or not 0 <= member.size <= 2_000_000:
                    raise ValueError("archive_limit")
                if len(parts) > 64 or any(part.casefold() == ".git" for part in parts):
                    raise ValueError("archive_path")
                parent = os.dup(root)
                try:
                    for part in parts[:-1]:
                        try:
                            os.mkdir(part, 0o700, dir_fd=parent)
                        except FileExistsError:
                            pass
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                        os.close(parent)
                        parent = child
                    fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    with os.fdopen(fd, "wb") as output:
                        source = archive.extractfile(member)
                        remaining = member.size
                        while remaining:
                            data = source.read(min(65536, remaining))
                            if not data:
                                raise ValueError("short_archive")
                            output.write(data)
                            remaining -= len(data)
                finally:
                    os.close(parent)
    finally:
        os.close(root)


def report(path, maximum):
    # No symlink parent or final target can redirect capture into dependencies,
    # /proc, or source files. Report directory is separate from source transfer.
    parts = Path(path).relative_to("/reports").parts
    parent = os.open("/reports", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
                raise ValueError("unsafe_report")
            with os.fdopen(os.dup(fd), "rb") as source:
                data = source.read(maximum + 1)
            if len(data) > maximum:
                raise ValueError("report_limit")
            return base64.b64encode(data).decode("ascii")
        finally:
            os.close(fd)
    except FileNotFoundError:
        return None
    finally:
        os.close(parent)


def smoke(limits):
    assert os.getuid() == 10001
    status = Path("/proc/self/status").read_text()
    assert "NoNewPrivs:\t1" in status and "Seccomp:\t2" in status
    assert "CapEff:\t0000000000000000" in status
    assert Path("/sys/fs/cgroup/pids.max").read_text().strip() == str(limits.max_processes)
    assert int(Path("/sys/fs/cgroup/memory.max").read_text()) == limits.memory_bytes
    quota, period = map(int, Path("/sys/fs/cgroup/cpu.max").read_text().split())
    assert 0 < quota <= period
    assert resource.getrlimit(resource.RLIMIT_CPU)[1] == limits.cpu_seconds
    try:
        Path("/opt/probe").write_text("forbidden")
    except OSError:
        pass
    else:
        raise ValueError("writable_root")
    assert not Path("/var/run/docker.sock").exists() and not Path("/github").exists()
    assert not any(key in os.environ for key in ("OPENAI_API_KEY", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN"))
    assert len(socket.if_nameindex()) == 1 and socket.if_nameindex()[0][1] == "lo"
    for path in ("/workspace", "/reports", "/tmp"):
        probe = Path(path) / "probe"
        probe.write_text("ok")
        probe.unlink()


def check_dependencies(workspace=Path("/workspace"), provisioned=Path("/opt")):
    """Do not verify a different PR dependency graph using base dependencies."""
    for relative, trusted in (
        ("backend/requirements.txt", "backend-requirements.txt"),
        ("frontend/package.json", "frontend/package.json"),
        ("frontend/package-lock.json", "frontend/package-lock.json"),
    ):
        if (workspace / relative).read_bytes() != (provisioned / trusted).read_bytes():
            raise ValueError("unsupported_dependency_change")


def main():
    header = sys.stdin.buffer.readline(16385)
    if len(header) > 16384:
        raise ValueError("header_limit")
    header = json.loads(header)
    job = ExecutionJob.model_validate_json(json.dumps(header["job"]))
    limits = ExecutionLimits.model_validate_json(json.dumps(header["limits"]))
    if job != execution_job(job.suite, job.identity):
        raise ValueError("non_allowlisted_plan")
    if header.get("smoke") is True:
        smoke(limits)
        print('{"smoke":"ok"}')
        return
    unpack(sys.stdin.buffer)
    check_dependencies()
    if job.suite == "frontend":
        modules = Path("/workspace/frontend/node_modules")
        modules.mkdir()  # reject a PR-supplied node_modules tree
        for package in Path("/opt/frontend/node_modules").iterdir():
            (modules / package.name).symlink_to(package, target_is_directory=package.is_dir())
    environment = dict(job.environment)
    environment["PATH"] = "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"
    child = subprocess.Popen(job.argv, cwd=Path("/workspace") / job.cwd, env=environment,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    size = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            selector.register(child.stderr, selectors.EVENT_READ)
            while selector.get_map():
                for key, _ in selector.select(1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    size += len(chunk)
                    if size > limits.max_output_bytes:
                        raise ValueError("output_limit")
        code = child.wait()
    finally:
        # Kill descendants even when their immediate parent exits successfully.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
    payload = {"state": "completed", "exit_code": code,
        "tests_report": report(job.tests_artifact, limits.max_report_bytes),
        "coverage_report": report(job.coverage_artifact, limits.max_report_bytes)}
    print(json.dumps(payload, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"state":"crashed","exit_code":null,"tests_report":null,"coverage_report":null}')
        sys.exit(1)
