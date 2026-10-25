"""Execute reviewed workflow shell blocks against inert command stubs.

No Docker, package installation, Git mutation or Taskly code execution occurs.
The small extractor deliberately handles this workflow's literal run blocks
only; YAML syntax is validated separately, without adding a YAML dependency.
"""

import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tomllib

import pytest

from taskly_agent import ci
from taskly_agent.docker_runner import DockerSandbox
from test_verification import capture, parse
from taskly_agent.verification_reports import aggregate_reports


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/taskly-verification.yml"
PREFLIGHT = "Validate trusted checkout inputs"
INSTALL = "Install and test reviewed agent code"
BUILD = "Provision image from reviewed manifests only"
VERIFY = "Validate sandbox and run real Taskly suites"
BASE = "a" * 40
HEAD = "b" * 40
IMAGE = "sha256:" + "c" * 64
EVENT = {
    "${{ github.event.pull_request.base.sha }}": BASE,
    "${{ github.event.pull_request.head.sha }}": HEAD,
    "${{ github.event.pull_request.number }}": "37",
    "${{ github.repository }}": "Sacarima/UTA-AW",
}


def step(name):
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("      - name: " + name) + 1
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("      - ")), len(lines))
    block = lines[start:end]
    command = block.index("        run: |")
    working = next(line.partition(": ")[2] for line in block if line.startswith("        working-directory: "))
    environment = {}
    if "        env:" in block:
        for line in block[block.index("        env:") + 1:command]:
            key, separator, value = line.strip().partition(": ")
            assert separator and line.startswith("          "), "unsupported environment mapping"
            environment[key] = EVENT[value]  # Unknown/swapped expressions fail the invocation assertions.
    return working, environment, "\n".join(line.removeprefix("          ") for line in block[command + 1:])


def executable(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!" + sys.executable + "\n" + body)
    path.chmod(0o700)


@pytest.fixture
def checkout(tmp_path):
    trusted = tmp_path / "checkouts" / "trusted"
    candidate = trusted.parent / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "sentinel.txt").write_text("candidate data only")
    shutil.copytree(ROOT / "agent/src", trusted / "agent/src",
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    shutil.copytree(ROOT / "agent/sandbox", trusted / "agent/sandbox", ignore=shutil.ignore_patterns("__pycache__"))
    for relative in ("agent/pyproject.toml", "backend/requirements.txt", "frontend/package.json", "frontend/package-lock.json"):
        target = trusted / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    temporary = tmp_path / "runner temp 任务"
    temporary.mkdir()
    summary = temporary / "job summary.md"
    docker_log, python_log = tmp_path / "docker.json", tmp_path / "python.jsonl"
    bin_path = tmp_path / "stub-bin"
    executable(bin_path / "docker", f"""import json, os, sys
from pathlib import Path
args = sys.argv[1:]
Path({str(docker_log)!r}).write_text(json.dumps({{"argv": args, "cwd": os.getcwd()}}))
assert args[0] == 'build'
image_file = Path(args[args.index('--iidfile') + 1])
image_file.write_text({IMAGE!r} + '\\n')
""")
    python_stub = f"""import json, os, sys
from pathlib import Path
with Path({str(python_log)!r}).open('a') as log:
    log.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "environment": dict(os.environ)}}) + '\\n')
"""
    executable(bin_path / "python", python_stub)
    executable(trusted / ".ci-venv/bin/python", python_stub)
    return dict(trusted=trusted, candidate=candidate, temporary=temporary, summary=summary,
        docker_log=docker_log, python_log=python_log, bin_path=bin_path)


def run_step(checkout, name, *, unset=()):
    working, mappings, command = step(name)
    assert working == "trusted", "PR data must never become the working directory"
    environment = {
        "PATH": str(checkout["bin_path"]) + ":/usr/bin:/bin",
        "RUNNER_TEMP": str(checkout["temporary"]),
        "GITHUB_STEP_SUMMARY": str(checkout["summary"]),
        "OPENAI_API_KEY": "test-only-not-a-credential",
        "GITHUB_TOKEN": "test-only-not-a-token",
        **mappings,
    }
    for key in unset:
        environment.pop(key, None)
    return subprocess.run(["/bin/bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
        cwd=checkout["trusted"], env=environment, capture_output=True, timeout=10)


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_build_expands_base_sha_and_writes_actual_runner_temp_image_file(checkout):
    result = run_step(checkout, BUILD)
    assert result.returncode == 0, result.stderr.decode()
    observed = json.loads(checkout["docker_log"].read_text())
    assert observed["cwd"] == str(checkout["trusted"])
    assert observed["argv"] == ["build", "--platform", "linux/amd64", "--build-arg", "TRUSTED_REVISION=" + BASE,
        "--iidfile", str(checkout["temporary"] / "taskly-image.id"), "--file", "agent/sandbox/Dockerfile", "."]
    assert (checkout["temporary"] / "taskly-image.id").read_text().strip() == IMAGE


def test_ci_expands_every_argument_and_real_cli_reads_image_and_writes_summary(checkout, monkeypatch):
    assert run_step(checkout, BUILD).returncode == 0
    result = run_step(checkout, VERIFY)
    assert result.returncode == 0, result.stderr.decode()
    observed = records(checkout["python_log"])
    assert len(observed) == 1
    invocation = observed[0]
    assert invocation["cwd"] == str(checkout["trusted"])
    assert invocation["argv"] == ["-I", "-m", "taskly_agent.ci", "--repository", "../candidate",
        "--name", "Sacarima/UTA-AW", "--number", "37", "--base", BASE, "--head", HEAD,
        "--trusted-revision", BASE, "--image-file", str(checkout["temporary"] / "taskly-image.id"),
        "--summary", str(checkout["summary"])]
    # Runtime-added locale metadata is platform-specific; event/credential vars
    # must still be absent after env -i. GitHub's Linux case has no CF metadata.
    allowed_environment = {"PATH", "HOME", "LC_CTYPE"}
    if sys.platform == "darwin":
        allowed_environment.add("__CF_USER_TEXT_ENCODING")
    assert set(invocation["environment"]) <= allowed_environment
    assert invocation["environment"]["HOME"] == str(checkout["temporary"] / "taskly-empty-home")
    calls = []
    async def fake_baseline(repository, identity, image, trusted_revision):
        assert repository == Path("../candidate")
        assert identity.repository == "Sacarima/UTA-AW" and identity.number == 37
        assert identity.base_sha == BASE and identity.head_sha == HEAD
        runtime = DockerSandbox(image, trusted_revision)  # constructor only; no Docker execution
        assert runtime.image == IMAGE and runtime.trusted_revision == BASE
        calls.append(identity)
        reports = tuple(parse(capture(suite)) for suite in ("backend", "frontend"))
        return reports, aggregate_reports(reports, ("backend", "frontend"))
    monkeypatch.setattr(ci, "baseline", fake_baseline)
    assert ci.main(invocation["argv"][3:]) == 0  # real argparse + image-file read + summary write
    assert len(calls) == 1
    summary = checkout["summary"].read_text()
    assert BASE in summary and HEAD in summary and IMAGE in summary
    assert (checkout["candidate"] / "sentinel.txt").read_text() == "candidate data only"
    assert list(checkout["candidate"].iterdir()) == [checkout["candidate"] / "sentinel.txt"]


def test_trusted_checkout_has_image_package_and_skill_inputs(checkout):
    assert run_step(checkout, PREFLIGHT).returncode == 0
    dockerfile = (checkout["trusted"] / "agent/sandbox/Dockerfile").read_text()
    for line in dockerfile.splitlines():
        if not line.startswith("COPY "):
            continue
        words = shlex.split(line)
        if words and words[0] == "COPY" and not words[1].startswith("--from="):
            assert all((checkout["trusted"] / path).exists() for path in words[1:-1])
    assert "ARG TRUSTED_REVISION" in dockerfile
    assert "taskly.trusted_revision=$TRUSTED_REVISION" in dockerfile
    package = tomllib.loads((checkout["trusted"] / "agent/pyproject.toml").read_text())
    assert "bundled_skills/*/SKILL.md" in package["tool"]["setuptools"]["package-data"]["taskly_agent"]
    assert len(list((checkout["trusted"] / "agent/src/taskly_agent/bundled_skills").glob("*/SKILL.md"))) == 4
    assert WORKFLOW.read_text().index("- name: " + PREFLIGHT) < WORKFLOW.read_text().index("- name: " + INSTALL)
    assert run_step(checkout, INSTALL).returncode == 0
    observed = records(checkout["python_log"])
    assert [(item["cwd"], item["argv"]) for item in observed] == [
        (str(checkout["trusted"]), ["-m", "venv", ".ci-venv"]),
        (str(checkout["trusted"]), ["-m", "pip", "install", "./agent[test]"]),
        (str(checkout["trusted"] / "agent"), ["-m", "pytest"]),
    ]


@pytest.mark.parametrize("relative", ["agent/src/taskly_agent/ci.py", "agent/sandbox/Dockerfile",
    "backend/requirements.txt", "agent/src/taskly_agent/bundled_skills/python-unit-tests/SKILL.md"])
def test_missing_trusted_input_blocks_before_installation_or_build(checkout, relative):
    (checkout["trusted"] / relative).unlink()
    result = run_step(checkout, PREFLIGHT)
    assert result.returncode != 0 and b"Required trusted CI input missing" in result.stderr
    assert not checkout["docker_log"].exists() and not checkout["python_log"].exists()


@pytest.mark.parametrize("name,key", [(BUILD, "BASE_SHA"), (BUILD, "RUNNER_TEMP"),
    (VERIFY, "HEAD_SHA"), (VERIFY, "PR_NUMBER"), (VERIFY, "REPOSITORY"), (VERIFY, "GITHUB_STEP_SUMMARY")])
def test_missing_event_or_path_variable_fails_before_command_invocation(checkout, name, key):
    result = run_step(checkout, name, unset=(key,))
    assert result.returncode != 0
    assert not checkout["docker_log"].exists() and not checkout["python_log"].exists()


def test_missing_image_id_file_blocks_ci_before_python(checkout):
    assert run_step(checkout, VERIFY).returncode != 0
    assert not checkout["python_log"].exists()
