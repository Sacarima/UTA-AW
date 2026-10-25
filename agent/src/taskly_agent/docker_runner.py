"""Linux-only Docker transport. No bind mounts, shell, credentials or host tests.

Runtime receipts bind fresh capture bytes to this in-memory invocation, image,
job and enforced limits; they are not cryptographic attestations, and cannot
prove that adversarial Python/JS did not forge its own reports. Never publish
raw report files or treat them as privileged instructions. Containers share a
kernel; this boundary assumes a trusted, patched hosted VM/daemon and image.
"""

import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import signal
import stat
import tarfile
import secrets

from .contracts import CommitSHA
from .execution import ExecutionCapture, ExecutionJob, ExecutionLimits, execution_job
from .verification import _drain


class SandboxError(RuntimeError):
    def __init__(self):
        super().__init__("sandbox unavailable or invalid")


def transfer(workspace: Path, job: ExecutionJob, limits: ExecutionLimits) -> bytes:
    """Recheck a private applied tree; never archive Git metadata, links or specials."""
    from .application import _layout, _read_file, _DIRECTORY
    entries = []
    total = 0
    root = os.open(workspace, _DIRECTORY)
    try:
        def walk(fd, prefix=""):
            nonlocal total
            for name in sorted(os.listdir(fd)):
                path = prefix + name
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, _DIRECTORY, dir_fd=fd)
                    try:
                        walk(child, path + "/")
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 2_000_000:
                    total += info.st_size
                    if total > 32_000_000 or len(entries) >= 2000:
                        raise SandboxError()
                    entries.append((path, _read_file(root, path, info.st_size)))
                else:
                    raise SandboxError()
        walk(root)
    finally:
        os.close(root)
    _layout(path for path, _ in entries)
    archive = io.BytesIO()
    header = json.dumps({"job": job.model_dump(mode="json"), "limits": limits.model_dump(), "smoke": False}, separators=(",", ":")).encode() + b"\n"
    archive.write(header)
    with tarfile.open(fileobj=archive, mode="w", format=tarfile.PAX_FORMAT) as output:
        for path, data in entries:
            member = tarfile.TarInfo(path)
            member.size, member.mode, member.uid, member.gid = len(data), 0o600, 10001, 10001
            output.addfile(member, io.BytesIO(data))
    return archive.getvalue()


def create_arguments(name: str, image: str, limits: ExecutionLimits) -> tuple[str, ...]:
    return ("create", "--name", name, "--init", "--interactive", "--network=none", "--user=10001:10001",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--cgroupns=private", "--ipc=private",
        "--pids-limit=" + str(limits.max_processes), "--memory=" + str(limits.memory_bytes),
        "--memory-swap=" + str(limits.memory_bytes), "--cpus=1", "--ulimit=cpu=" + str(limits.cpu_seconds) + ":" + str(limits.cpu_seconds),
        "--ulimit=nofile=256:256", "--log-driver=none",
        "--tmpfs=/workspace:rw,noexec,nosuid,nodev,size=67108864,uid=10001,gid=10001,mode=700",
        "--tmpfs=/reports:rw,noexec,nosuid,nodev,size=16777216,uid=10001,gid=10001,mode=700",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=67108864,uid=10001,gid=10001,mode=700", image)


def validate_container(info: dict, image: str, limits: ExecutionLimits) -> None:
    host, config = info["HostConfig"], info["Config"]
    if not (info["Image"] == image and config["User"] == "10001:10001" and host["NetworkMode"] == "none"
        and host["ReadonlyRootfs"] and not host["Privileged"] and host["CapDrop"] == ["ALL"]
        and "no-new-privileges" in host["SecurityOpt"] and host["CgroupnsMode"] == "private"
        and host["PidMode"] == "" and host["IpcMode"] == "private" and host["UTSMode"] == ""
        and host["PidsLimit"] == limits.max_processes and host["Memory"] == limits.memory_bytes
        and host["MemorySwap"] == limits.memory_bytes and host["NanoCpus"] == 1_000_000_000
        and host["LogConfig"]["Type"] == "none" and not host.get("Binds") and not host.get("Devices")
        and not host.get("CapAdd") and not host.get("PortBindings") and not host.get("VolumesFrom")
        and config["Entrypoint"] == ["/opt/venv/bin/python", "-I", "/opt/taskly/launch.py"]
        and not config.get("Cmd") and all("unconfined" not in option for option in host["SecurityOpt"])):
        raise SandboxError()
    expected = dict(arg.split("=", 1)[1].split(":", 1) for arg in create_arguments("unused", image, limits) if arg.startswith("--tmpfs="))
    if host["Tmpfs"] != expected or any(mount["Type"] != "tmpfs" or mount["Destination"] not in expected for mount in info.get("Mounts", [])):
        raise SandboxError()
    cpu = [limit for limit in host["Ulimits"] if limit["Name"] == "cpu"]
    if len(cpu) != 1 or cpu[0]["Hard"] != limits.cpu_seconds or cpu[0]["Soft"] != limits.cpu_seconds:
        raise SandboxError()


def _fingerprint(capture: ExecutionCapture) -> bytes:
    values = [capture.identity.model_dump_json().encode(), capture.suite.encode(), capture.state.encode(),
              str(capture.exit_code).encode(), (capture.execution_id or "").encode()]
    values.extend(hashlib.sha256(value).digest() if value is not None else b"missing"
                  for value in (capture.tests_report, capture.coverage_report, capture.stdout, capture.stderr))
    return hashlib.sha256(b"\0".join(values)).digest()


class DockerSandbox:
    mode = "docker"

    def __init__(self, image: str, trusted_revision: str):
        from pydantic import TypeAdapter
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None:
            raise SandboxError()
        self.trusted_revision = TypeAdapter(CommitSHA).validate_python(trusted_revision, strict=True)
        self.image = image
        self._receipts = {}
        self._validated = False
        self._validated_limits = None

    async def _call(self, *args, data=b"", maximum=8_388_608, timeout=15):
        process = await asyncio.create_subprocess_exec("/usr/bin/docker", *args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "DOCKER_CONFIG": "/nonexistent",
                 "DOCKER_HOST": "unix:///var/run/docker.sock", "LC_ALL": "C"}, start_new_session=True)
        size = 0

        async def read(stream):
            nonlocal size
            chunks = []
            while chunk := await stream.read(65536):
                size += len(chunk)
                if size > maximum:
                    raise SandboxError()
                chunks.append(chunk)
            return b"".join(chunks)

        async def send():
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        tasks = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr)), asyncio.create_task(send())]
        try:
            output, _, _ = await asyncio.wait_for(asyncio.gather(*tasks), timeout)
            await asyncio.wait_for(process.wait(), timeout)
            return process.returncode, output
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for task in tasks:
                task.cancel()
            await _drain(asyncio.create_task(process.wait()))
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _json(self, *args):
        code, output = await self._call(*args, maximum=1_048_576)
        if code != 0:
            raise SandboxError()
        return json.loads(output)

    async def _preflight(self):
        if platform.system() != "Linux":
            raise SandboxError()
        info = await self._json("info", "--format", "{{json .}}")
        if info["OSType"] != "linux" or info["CgroupVersion"] != "2" or not any(
            "name=seccomp,profile=builtin" in option for option in info["SecurityOptions"]
        ):
            raise SandboxError()
        image = (await self._json("image", "inspect", self.image))[0]
        config = image["Config"]
        if image["Id"] != self.image or config["User"] != "10001:10001" or config.get("Volumes") or (
            config["Entrypoint"] != ["/opt/venv/bin/python", "-I", "/opt/taskly/launch.py"]
            or config.get("Cmd") or config["Labels"].get("taskly.trusted_revision") != self.trusted_revision
            or config["Labels"].get("taskly.profile") != "v1"
        ):
            raise SandboxError()

    async def _cleanup(self, name):
        await self._call("rm", "--force", name, maximum=65536)
        code, output = await self._call("ps", "--all", "--filter", "name=^/" + name + "$", "--format", "{{.ID}}", maximum=65536)
        if code != 0 or output.strip():
            raise SandboxError()

    async def _run(self, job, limits, data):
        name = "taskly-" + secrets.token_hex(16)
        cancelled = False
        try:
            code, output = await self._call(*create_arguments(name, self.image, limits), maximum=65536)
            if code or re.fullmatch(rb"[0-9a-f]{64}\n?", output) is None:
                raise SandboxError()
            container = (await self._json("inspect", name))[0]
            validate_container(container, self.image, limits)
            code, output = await self._call("start", "--attach", "--interactive", name, data=data,
                maximum=2 * limits.max_report_bytes * 2 + 131072, timeout=limits.wall_seconds)
            state = (await self._json("inspect", name))[0]["State"]
            if code or state["Running"] or state["Pid"] != 0 or state["OOMKilled"] or state["ExitCode"] != 0:
                raise SandboxError()
            return json.loads(output)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            try:
                _, interrupted = await _drain(asyncio.create_task(self._cleanup(name)))
                if interrupted:
                    raise asyncio.CancelledError()
            except Exception:
                self._validated = False
                if cancelled:
                    raise asyncio.CancelledError() from None
                raise SandboxError() from None

    async def validate(self, limits: ExecutionLimits, identity):
        """Run a trusted harmless probe under the SAME enforced restrictions."""
        self._validated = False
        self._validated_limits = None
        await self._preflight()
        job = execution_job("backend", identity)
        data = json.dumps({"job": job.model_dump(mode="json"), "limits": limits.model_dump(), "smoke": True}).encode() + b"\n"
        if await self._run(job, limits, data) != {"smoke": "ok"}:
            raise SandboxError()
        self._validated = True
        self._validated_limits = limits

    async def execute(self, workspace, job, limits):
        if not self._validated or self._validated_limits != limits or job != execution_job(job.suite, job.identity):
            raise SandboxError()
        await self._preflight()
        copying = asyncio.create_task(asyncio.to_thread(transfer, workspace, job, limits))
        try:
            data = await asyncio.shield(copying)
        except asyncio.CancelledError:
            try:
                await _drain(copying)
            finally:
                raise asyncio.CancelledError() from None
        raw = await self._run(job, limits, data)
        capture = ExecutionCapture(suite=job.suite, identity=job.identity, state=raw["state"], exit_code=raw["exit_code"],
            tests_report=base64.b64decode(raw["tests_report"], validate=True) if raw["tests_report"] is not None else None,
            coverage_report=base64.b64decode(raw["coverage_report"], validate=True) if raw["coverage_report"] is not None else None,
            execution_id=secrets.token_hex(16))
        if any(value is not None and len(value) > limits.max_report_bytes for value in (capture.tests_report, capture.coverage_report)):
            raise SandboxError()
        self._receipts[capture.execution_id] = (_fingerprint(capture), job)
        return capture

    def consume_receipt(self, capture, job):
        receipt = self._receipts.pop(capture.execution_id, None)
        return receipt == (_fingerprint(capture), job)
