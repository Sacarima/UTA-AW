"""Run actual new workflow shell blocks against inert trusted command stubs."""

import json
from pathlib import Path
import re
import shlex
import subprocess

import pytest

from taskly_agent import pr_automation as automation
from taskly_agent.contracts import AgentDecision
from test_pr_automation import BASE, HEAD, FakeGitHub, context
from test_workflow_invocation import executable


WORKFLOW = Path(__file__).parents[2] / ".github/workflows/taskly-pr-agent.yml"
IMAGE = "sha256:" + "c" * 64
NAMES = {
    "prepare": ("analyze", "Resolve source run and current PR from GitHub"),
    "analyze": ("analyze", "Analyze real pinned changes without model credentials"),
    "admit": ("generate", "Recheck independent authorization and credential availability"),
    "generate": ("generate", "Invoke existing controller and real independent verifier"),
    "publish": ("publish", "Recheck freshness and publish a sanitized commit-bound PR comment"),
}


def job_lines(job):
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("  " + job + ":") + 1
    end = next((i for i in range(start, len(lines)) if re.match(r"^  [a-z]+:$", lines[i])), len(lines))
    return lines[start:end]


def step_lines(job, name):
    lines = job_lines(job)
    start = lines.index("      - name: " + name) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("      - ")), len(lines))
    return lines[start:end]


def command(job, name):
    lines = step_lines(job, name)
    return "\n".join(line.removeprefix("          ") for line in lines[lines.index("        run: |") + 1:])


def mapping(job, name):
    expressions = {"${{ github.repository }}": "Sacarima/UTA-AW", "${{ github.event.workflow_run.id }}": "10",
        "${{ github.sha }}": BASE, "${{ github.run_id }}": "20", "${{ vars.TASKLY_PR_AI_ENABLED }}": "true",
        "${{ vars.TASKLY_PR_AGENT_BASE_SHA }}": BASE, "${{ secrets.OPENAI_API_KEY }}": "inert-model-key",
        "${{ steps.prepare.outputs.head }}": HEAD, "${{ needs.analyze.outputs.head }}": HEAD,
        "${{ needs.analyze.outputs.classification }}": "frontend",
        "${{ needs.generate.result }}": "skipped", "${{ steps.admit.outputs.base }}": BASE,
        "${{ github.token }}": "inert-write-token" if job == "publish" else "inert-read-token"}
    lines = step_lines(job, name)
    if "        env:" not in lines:
        return {}
    result = {}
    for line in lines[lines.index("        env:") + 1:]:
        if not line.startswith("          "):
            break
        key, value = line.strip().split(": ", 1)
        result[key] = expressions[value]
    return result


@pytest.fixture
def runner(tmp_path):
    trusted = tmp_path / "checkouts 任务" / "trusted"
    trusted.mkdir(parents=True)
    (trusted.parent / "candidate").mkdir()
    temporary = tmp_path / "runner temp 任务"
    temporary.mkdir()
    log = temporary / "python.jsonl"
    for directory in ("pr-analysis", "pr-generation", "publish-analysis", "publish-generation"):
        (temporary / directory).mkdir()
    (temporary / "pr-generation/image.id").write_text(IMAGE)
    executable(temporary / "agent-venv/bin/python", f"""import json, os, sys
from pathlib import Path
with Path({str(log)!r}).open('a') as stream:
    stream.write(json.dumps({{'argv':sys.argv[1:],'env':dict(os.environ),'cwd':os.getcwd()}}) + '\\n')
""")
    return trusted, temporary, log


def run_command(runner, job, name, script=None):
    trusted, temporary, _ = runner
    assert "        working-directory: trusted" in step_lines(job, name)
    env = {"PATH": "/usr/bin:/bin", "RUNNER_TEMP": str(temporary), "GITHUB_OUTPUT": str(temporary / "outputs"),
        "GITHUB_TOKEN": "parent-token-must-not-leak", **mapping(job, name)}
    return subprocess.run(["/bin/bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", script or command(job, name)],
        cwd=trusted, env=env, capture_output=True, timeout=10)


@pytest.mark.parametrize("action", list(NAMES))
def test_actual_cli_arguments_environment_outputs_and_real_parser(runner, action, monkeypatch, capsys):
    job, name = NAMES[action]
    result = run_command(runner, job, name)
    assert result.returncode == 0, result.stderr.decode()
    trusted, temporary, log = runner
    call, = [json.loads(line) for line in log.read_text().splitlines()]
    directory = temporary / {"prepare": "pr-analysis", "analyze": "pr-analysis", "admit": "pr-generation",
        "generate": "pr-generation", "publish": "publish-analysis"}[action]
    expected = ["-I", "-m", "taskly_agent.pr_automation", action]
    if action in {"analyze", "generate"}: expected += ["--repository", "../candidate"]
    expected += ["--name", "Sacarima/UTA-AW", "--source-run", "10"]
    if action == "publish": expected += ["--workflow-run", "20"]
    if action in {"analyze", "admit", "generate"}: expected += ["--expected-head", HEAD]
    if action in {"admit", "generate"}: expected += ["--workflow-sha", BASE]
    if action == "admit": expected += ["--classification", "frontend"]
    if action == "generate": expected += ["--image-file", str(temporary / "pr-generation/image.id")]
    expected += ["--directory", str(directory)]
    if action == "publish":
        expected += ["--generated-report", str(temporary / "publish-generation/report.json"), "--generation-state", "skipped"]
    assert call["argv"] == expected and call["cwd"] == str(trusted)
    if job != "generate": assert "OPENAI_API_KEY" not in call["env"]
    if action == "generate":
        assert call["env"]["GITHUB_OUTPUT"] == str(temporary / "outputs")
        assert call["env"]["GH_TOKEN"] == "inert-read-token" and "GITHUB_TOKEN" not in call["env"]
    if action == "publish":
        assert call["env"]["GH_TOKEN"] == "inert-write-token" and "GITHUB_TOKEN" not in call["env"]
        assert "OPENAI_API_KEY" not in call["env"]
    for key in ("GH_TOKEN", "OPENAI_API_KEY", "TASKLY_PR_AI_ENABLED", "APPROVED_BASE", "GITHUB_OUTPUT"):
        if key in call["env"]: monkeypatch.setenv(key, call["env"][key])
        else: monkeypatch.delenv(key, raising=False)
    client = FakeGitHub()
    monkeypatch.setattr(automation, "GitHub", lambda *args: client)
    decision = AgentDecision(classification="backend", action="generate", rationale="Supported production changes detected.")
    monkeypatch.setattr(automation, "analyze", lambda repo, ctx: automation.unavailable(ctx, "ready", decision))
    async def fake_generation(*args, **kwargs):
        return automation.unavailable(context(), "generation_unavailable", decision)
    monkeypatch.setattr(automation, "generate", fake_generation)
    if action == "publish":
        (directory / "report.json").write_text(automation.unavailable(context(), "ready", decision).model_dump_json())
    assert automation.main(call["argv"][3:]) == (1 if action == "generate" else 0)
    if action in {"prepare", "analyze", "admit", "generate"}:
        assert (temporary / "outputs").exists()
    if action == "admit":
        report = automation.PRReport.model_validate_json((directory / "report.json").read_text())
        assert report.decision.classification == "frontend"
    if action == "publish": assert len(client.writes) == 1
    assert "inert-model-key" not in capsys.readouterr().out


def test_approved_image_build_and_iidfile_use_trusted_paths(runner):
    trusted, temporary, _ = runner
    job, name = "generate", "Provision existing sandbox from approved inputs only"
    log = temporary / "docker.json"
    stub = temporary / "docker-stub"
    executable(stub, f"""import json, os, sys
from pathlib import Path
Path({str(log)!r}).write_text(json.dumps({{'argv':sys.argv[1:],'cwd':os.getcwd(),'env':dict(os.environ)}}))
Path(sys.argv[sys.argv.index('--iidfile') + 1]).write_text({IMAGE!r})
""")
    script = command(job, name)
    assert script.count("/usr/bin/docker") == 1
    assert run_command(runner, job, name, script.replace("/usr/bin/docker", shlex.quote(str(stub)))).returncode == 0
    seen = json.loads(log.read_text())
    assert seen["argv"] == ["build", "--platform", "linux/amd64", "--build-arg", "TRUSTED_REVISION=" + BASE,
        "--iidfile", str(temporary / "pr-generation/image.id"), "--file", "agent/sandbox/Dockerfile", "."]
    assert seen["cwd"] == str(trusted) and "OPENAI_API_KEY" not in seen["env"]


def test_workflow_security_and_all_bash_blocks():
    text = WORKFLOW.read_text()
    assert "pull_request_target" not in text and "workflows: [Taskly isolated verification]" in text
    assert "types: [completed]" in text and "permissions: {}" in text and "cancel-in-progress: false" in text
    assert "github.run_attempt == 1" in text
    for job in ("analyze", "generate", "publish"):
        lines = job_lines(job)
        body = "\n".join(lines)
        if job == "publish":
            assert "pull-requests: write" in body and "OPENAI_API_KEY" not in body
            assert "path: candidate" not in body and "/usr/bin/docker" not in body
            assert "./agent[openai]" not in body
        else:
            assert "pull-requests: write" not in body
        if job == "analyze": assert "OPENAI_API_KEY" not in body and "environment:" not in body
        if job == "generate":
            assert "environment: taskly-pr-agent" in body and "vars.TASKLY_PR_AI_ENABLED == 'true'" in body
            assert "steps.execution.outputs.eligible == 'true'" in body
        assert "ref: ${{ github.sha }}" in body and "persist-credentials: false" in body
        for line in lines:
            if "uses: " in line:
                assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", line.split("uses: ")[1].split()[0])
        names = [line.split("- name: ")[1] for line in lines if line.startswith("      - name:")]
        for name in names:
            if "        run: |" not in step_lines(job, name): continue
            checked = subprocess.run(["/bin/bash", "-n"], input=command(job, name).encode(), capture_output=True, timeout=10)
            assert checked.returncode == 0, name
    # Downloads stay within this trusted workflow run, never the PR baseline run.
    assert "run-id:" not in text and "github-token:" not in text
    assert "pr-generation/patch/" in text and "if-no-files-found: error" in text
