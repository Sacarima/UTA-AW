"""Sandbox execution seam. There is deliberately NO live/host implementation.

Local inspection: Docker CLI exists but its daemon is unavailable. macOS
sandbox-exec can launch a harmless command outside the outer sandbox, but no
profile with all required resource/isolation guarantees or pinned dependencies
has been validated. Existing Dockerfiles/Compose are deployment infrastructure,
not verification sandboxes; no CI sandbox configuration exists.

A future live executor must copy the disposable workspace into a hardened
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

from .contracts import Contract, VerificationIdentity


Suite = Literal["backend", "frontend"]


class ExecutionLimits(Contract):
    wall_seconds: Annotated[int, Field(gt=0)] = 60
    cpu_seconds: Annotated[int, Field(gt=0)] = 30
    memory_bytes: Annotated[int, Field(gt=0)] = 536_870_912
    max_processes: Annotated[int, Field(gt=0)] = 64
    max_output_bytes: Annotated[int, Field(gt=0)] = 65_536
    max_report_bytes: Annotated[int, Field(gt=0)] = 2_097_152


class ExecutionJob(Contract):
    suite: Suite
    identity: VerificationIdentity
    argv: tuple[str, ...]
    cwd: Literal["backend", "frontend"]
    environment: tuple[tuple[str, str], ...]
    tests_artifact: str
    coverage_artifact: str


def execution_job(suite: Suite, identity: VerificationIdentity) -> ExecutionJob:
    """Fixed executable locations are INSIDE a future trusted image, never host PATH.

    Preserve project pytest/Vitest config; bypass package scripts. /reports must
    be empty and private per suite/run. Dependency resolution/provisioning is a
    future executor responsibility, not an install step in untrusted execution.
    """
    if suite == "backend":
        argv = ("/opt/venv/bin/python", "-m", "pytest", "tests", "--junitxml=/reports/tests.xml",
                "--cov=app", "--cov-report=json:/reports/coverage.json")
        tests, coverage = "/reports/tests.xml", "/reports/coverage.json"
    elif suite == "frontend":
        argv = ("/usr/local/bin/node", "/opt/frontend/node_modules/vitest/vitest.mjs", "run", "--coverage",
                "--reporter=json", "--outputFile=/reports/tests.json",
                "--coverage.reporter=json-summary", "--coverage.reportsDirectory=/reports/coverage")
        tests, coverage = "/reports/tests.json", "/reports/coverage/coverage-summary.json"
    else:
        raise ValueError("unsupported suite")
    return ExecutionJob(suite=suite, identity=identity, argv=argv, cwd=suite,
        environment=(("CI", "true"), ("HOME", "/tmp/empty-home"), ("PYTHONDONTWRITEBYTECODE", "1"),
                     ("PYTHONHASHSEED", "0")), tests_artifact=tests, coverage_artifact=coverage)


class ExecutionCapture(Contract):
    suite: Suite
    identity: VerificationIdentity
    state: Literal["completed", "timeout", "crashed", "unavailable"]
    exit_code: int | None = None
    tests_report: bytes | None = None
    coverage_report: bytes | None = None
    stdout: bytes = b""
    stderr: bytes = b""


class SandboxRunner(Protocol):
    """Trusted code injection, never a PR/model-controlled capability declaration.

    Only non-live modes exist. A simulator must not execute workspace code.
    Tests inject canned captures; verifier evidence always remains unavailable.
    """

    mode: Literal["unavailable", "simulation"]

    async def execute(self, workspace: Path, job: ExecutionJob, limits: ExecutionLimits) -> ExecutionCapture: ...


class UnavailableSandbox:
    mode: Literal["unavailable"] = "unavailable"

    async def execute(self, workspace: Path, job: ExecutionJob, limits: ExecutionLimits) -> ExecutionCapture:
        return ExecutionCapture(suite=job.suite, identity=job.identity, state="unavailable")
