"""Fixed sandbox execution plans. No host test execution is permitted.

Local inspection: Docker CLI exists but its daemon is unavailable. macOS
sandbox-exec can launch a harmless command outside the outer sandbox, but no
profile with all required resource/isolation guarantees or pinned dependencies
has been validated locally. M3A adds a separately provisioned Linux Docker
executor with a mandatory same-profile probe; deployment Compose is never used.

The Docker executor copies the disposable workspace into a hardened
sandbox, never mount host paths/sockets, supply only the environment below,
use separately provisioned digest-pinned dependencies, disable networking and
enforce filesystem, CPU, memory, process, time and streaming output limits.
It must terminate/reap all children before returning OR propagating cancellation,
and capture fresh reports through trusted instrumentation. PR code can forge
report files: parsing is not provenance. Merely changing mode to 'live' is
intentionally unsupported. No command in this module is executed.
"""

from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import Field

from .contracts import CommitSHA, Contract, RepositoryName, SHA256Digest, VerificationIdentity


Suite = Literal["backend", "frontend"]


class ExecutionLimits(Contract):
    wall_seconds: Annotated[int, Field(gt=0)] = 60
    cpu_seconds: Annotated[int, Field(gt=0)] = 30
    memory_bytes: Annotated[int, Field(gt=0)] = 536_870_912
    max_processes: Annotated[int, Field(gt=0)] = 64
    max_output_bytes: Annotated[int, Field(gt=0)] = 65_536
    max_report_bytes: Annotated[int, Field(gt=0)] = 2_097_152


class BaselineIdentity(Contract):
    """Baseline execution has no generated proposal; do not invent one."""
    repository: RepositoryName
    head_sha: CommitSHA
    policy_digest: SHA256Digest
    baseline_digest: SHA256Digest


class ExecutionJob(Contract):
    suite: Suite
    identity: VerificationIdentity | BaselineIdentity
    argv: tuple[str, ...]
    cwd: Literal["backend", "frontend"]
    environment: tuple[tuple[str, str], ...]
    tests_artifact: str
    coverage_artifact: str


def execution_job(suite: Suite, identity: VerificationIdentity | BaselineIdentity) -> ExecutionJob:
    """Fixed executable locations are INSIDE the trusted image, never host PATH.

    Preserve project pytest/Vitest config; bypass package scripts. /reports must
    be empty and private per suite/run. Dependency resolution/provisioning is a
    future executor responsibility, not an install step in untrusted execution.
    """
    if suite == "backend":
        argv = ("/opt/venv/bin/python", "-I", "-m", "pytest", "-p", "pytest_cov.plugin", "tests", "--junitxml=/reports/tests.xml",
                "--cov=app", "--cov-report=json:/reports/coverage.json")
        tests, coverage = "/reports/tests.xml", "/reports/coverage.json"
    elif suite == "frontend":
        argv = ("/usr/local/bin/node", "/opt/frontend/node_modules/vitest/vitest.mjs", "run", "--coverage", "--maxWorkers=1",
                "--reporter=json", "--outputFile=/reports/tests.json",
                "--coverage.reporter=json-summary", "--coverage.reportsDirectory=/reports/coverage")
        tests, coverage = "/reports/tests.json", "/reports/coverage/coverage-summary.json"
    else:
        raise ValueError("unsupported suite")
    return ExecutionJob(suite=suite, identity=identity, argv=argv, cwd=suite,
        environment=(("CI", "true"), ("HOME", "/tmp/empty-home"), ("PYTHONDONTWRITEBYTECODE", "1"),
                     ("PYTHONHASHSEED", "0"), ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")), tests_artifact=tests, coverage_artifact=coverage)


class ExecutionCapture(Contract):
    suite: Suite
    identity: VerificationIdentity | BaselineIdentity
    state: Literal["completed", "timeout", "crashed", "unavailable"]
    exit_code: int | None = None
    tests_report: bytes | None = None
    coverage_report: bytes | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    execution_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None


class SandboxRunner(Protocol):
    """Trusted code injection, never a PR/model-controlled capability declaration.

    Simulators must not execute workspace code or produce available evidence.
    Only the reviewed DockerSandbox concrete type and its single-use receipts
    can enable real verification; an arbitrary mode declaration is insufficient.
    """

    mode: Literal["unavailable", "simulation", "docker"]

    async def execute(self, workspace: Path, job: ExecutionJob, limits: ExecutionLimits) -> ExecutionCapture: ...


class UnavailableSandbox:
    mode: Literal["unavailable"] = "unavailable"

    async def execute(self, workspace: Path, job: ExecutionJob, limits: ExecutionLimits) -> ExecutionCapture:
        return ExecutionCapture(suite=job.suite, identity=job.identity, state="unavailable")
