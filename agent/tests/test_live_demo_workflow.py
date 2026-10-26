"""Exercise real workflow shell blocks with inert commands, never Docker/API."""

import json
from pathlib import Path
import re
import shlex
import subprocess

import pytest

from taskly_agent import end_to_end, live_demo
from taskly_agent.contracts import PRIdentity
from taskly_agent.openai_transport import demo_config
from test_live_demo import metadata, report_data
from test_workflow_invocation import executable


WORKFLOW = Path(__file__).parents[2] / ".github/workflows/taskly-live-demo.yml"
BASE, HEAD, IMAGE = "a" * 40, "b" * 40, "sha256:" + "c" * 64
GATE = "Gate approved workflow and commit identities"
INSTALL = "Install reviewed orchestration only"
PR = "Validate actual GitHub PR identity"
ADMIT = "Admit exact demonstration delta and fixed spending configuration"
BUILD = "Build immutable sandbox from approved base only"
PAID = "Run existing agent with explicit paid authorization"
PUBLISH = "Validate and sanitize evidence artifact"
CONTEXT = {
    "${{ inputs.base_sha }}": BASE, "${{ inputs.head_sha }}": HEAD,
    "${{ inputs.pr_number }}": "37", "${{ github.repository }}": "Sacarima/UTA-AW",
    "${{ vars.TASKLY_DEMO_BASE_SHA }}": BASE, "${{ github.sha }}": BASE,
    "${{ github.token }}": "inert-github-token", "${{ secrets.OPENAI_API_KEY }}": "inert-key-not-a-credential",
}


def step_lines(name):
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("      - name: " + name) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("      - name:")), len(lines))
    return lines[start:end]


def block(name):
    lines = step_lines(name)
    run = lines.index("        run: |")
    return "\n".join(line.removeprefix("          ") for line in lines[run + 1:])


def environment_mapping(lines, indentation):
    """Read this workflow's simple scalar env maps, not a general YAML parser."""
    marker = " " * indentation + "env:"
    if marker not in lines:
        return {}
    values = {}
    for line in lines[lines.index(marker) + 1:]:
        if not line.startswith(" " * (indentation + 2)):
            break
        if line.strip().startswith("#"):
            continue
        key, separator, value = line.strip().partition(": ")
        assert separator and key not in values
        values[key] = CONTEXT[value] if value.startswith("${{") else value
    return values


@pytest.fixture
def hosted(tmp_path):
    trusted = tmp_path / "workspace 任务" / "trusted"
    trusted.mkdir(parents=True)
    (trusted.parent / "candidate").mkdir()
    temporary = tmp_path / "runner temp 任务"
    (temporary / "taskly-demo").mkdir(parents=True)
    python_log = temporary / "python.jsonl"
    identity = PRIdentity(repository="Sacarima/UTA-AW", number=37, base_sha=BASE, head_sha=HEAD)
    executable(temporary / "demo-venv/bin/python", f"""import json, os, sys
from pathlib import Path
with Path({str(python_log)!r}).open('a') as log:
    log.write(json.dumps({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}}) + '\\n')
if sys.argv[3] == 'taskly_agent.end_to_end':
    print({json.dumps(report_data(identity))!r})
    print('private diagnostic must not enter public logs', file=sys.stderr)
""")
    (temporary / "taskly-demo/image.id").write_text(IMAGE + "\n")
    (temporary / "taskly-demo/config.json").write_text(demo_config(budget_usd=0.10).model_dump_json())
    environment = {"PATH": "/usr/bin:/bin", "RUNNER_TEMP": str(temporary), "GITHUB_STEP_SUMMARY": str(temporary / "summary.md"),
        "GITHUB_TOKEN": "inert-token", "GH_TOKEN": "poisoned-parent-gh-token"}
    return trusted, temporary, python_log, environment


def execute(hosted, name, *, overrides=None, script=None):
    trusted, _, _, environment = hosted
    lines = step_lines(name)
    working = [line.split(": ", 1)[1] for line in lines if line.startswith("        working-directory:")]
    assert working in ([], ["trusted"]), "Only reviewed host working directories may execute"
    mappings = {**environment_mapping(WORKFLOW.read_text().splitlines(), 4),
                **environment_mapping(lines, 8)}
    return subprocess.run(["/bin/bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", script or block(name)],
        cwd=trusted if working else trusted.parent,
        env={**environment, **mappings, **(overrides or {})}, capture_output=True, timeout=10)


def records(hosted):
    return [json.loads(line) for line in hosted[2].read_text().splitlines()]


def test_actual_paid_command_expansion_environment_redirection_and_parser(hosted, monkeypatch, capsys):
    result = execute(hosted, PAID)
    assert result.returncode == 0 and result.stdout == result.stderr == b""
    trusted, temporary, _, _ = hosted
    call, = records(hosted)
    assert call["cwd"] == str(trusted)
    args = ["-I", "-m", "taskly_agent.end_to_end", "--repository", "../candidate",
        "--name", "Sacarima/UTA-AW", "--number", "37", "--base", BASE, "--head", HEAD,
        "--trusted-revision", BASE, "--image", IMAGE, "--config", str(temporary / "taskly-demo/config.json"), "--allow-paid"]
    assert call["argv"] == args
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "REPOSITORY", "HEAD_SHA"} & set(call["env"])
    assert call["env"]["OPENAI_API_KEY"] == "inert-key-not-a-credential"
    assert call["env"]["HOME"] == str(temporary)
    assert json.loads((temporary / "taskly-demo/report.raw.json").read_text())["identity"]["head_sha"] == HEAD
    assert "private diagnostic" in (temporary / "taskly-demo/summary.raw.txt").read_text()
    calls = []

    async def fake_run(repository, identity, image, trusted_revision, config, *, allow_paid):
        assert repository == Path("../candidate") and identity.base_sha == BASE and identity.head_sha == HEAD
        assert image == IMAGE and trusted_revision == BASE and config == demo_config(budget_usd=0.10) and allow_paid
        calls.append(identity)
        return end_to_end.PublicReport.model_validate_json(json.dumps(report_data(identity)))

    monkeypatch.setattr(end_to_end, "run", fake_run)
    assert end_to_end.main(call["argv"][3:]) == 0
    assert len(calls) == 1 and "private diagnostic" not in capsys.readouterr().out


def test_exact_admission_and_publication_commands_expand_paths_without_credentials(hosted, monkeypatch, capsys):
    assert execute(hosted, ADMIT).returncode == 0
    assert execute(hosted, PUBLISH).returncode == 0
    trusted, temporary, _, _ = hosted
    admit, publish = records(hosted)
    common = ["--name", "Sacarima/UTA-AW", "--number", "37", "--base", BASE, "--head", HEAD]
    assert admit["argv"] == ["-I", "-m", "taskly_agent.live_demo", "check-candidate", "--repository", "../candidate",
        *common, "--config", str(temporary / "taskly-demo/config.json")]
    assert publish["argv"] == ["-I", "-m", "taskly_agent.live_demo", "publish", *common,
        "--report", str(temporary / "taskly-demo/report.raw.json"), "--image-file", str(temporary / "taskly-demo/image.id"),
        "--artifact", str(temporary / "taskly-demo/evidence.json")]
    assert admit["cwd"] == publish["cwd"] == str(trusted) and "OPENAI_API_KEY" not in publish["env"]
    assert "GITHUB_TOKEN" not in publish["env"] and "GH_TOKEN" not in publish["env"]
    admitted = []
    monkeypatch.setattr(live_demo, "validate_candidate", lambda repository, identity: admitted.append((repository, identity)))
    # Real argparse/config writing, with pinned Git admission already tested in
    # test_live_demo. Publication itself is exercised without a fake publisher.
    assert live_demo.main(admit["argv"][3:]) == 0
    assert admitted[0][0] == Path("../candidate") and admitted[0][1].head_sha == HEAD
    data = report_data(admitted[0][1])
    data.update(reason="private source must be suppressed", verification_reason="private diagnostics")
    (temporary / "taskly-demo/report.raw.json").write_text(json.dumps(data))
    assert live_demo.main(publish["argv"][3:]) == 0
    artifact = (temporary / "taskly-demo/evidence.json").read_text()
    assert json.loads(artifact)["report"]["verification_outcome"] == "passed"
    assert "private" not in artifact + capsys.readouterr().out


def test_pr_endpoint_redirections_token_mapping_and_real_parser(hosted, capsys):
    _, temporary, _, _ = hosted
    bin_path = temporary / "stub-bin"
    log = temporary / "gh.json"
    identity = PRIdentity(repository="Sacarima/UTA-AW", number=37, base_sha=BASE, head_sha=HEAD)
    executable(bin_path / "gh", f"""import json, os, sys
from pathlib import Path
Path({str(log)!r}).write_text(json.dumps({{'args':sys.argv[1:],'token':os.environ.get('GH_TOKEN'),'host':os.environ.get('GH_HOST')}}))
print({json.dumps(metadata(identity))!r})
print('private API diagnostics', file=sys.stderr)
""")
    executable(bin_path / "timeout", f"""import os, sys
assert sys.argv[1:3] == ['20s', 'gh']
os.execv({str(bin_path / 'gh')!r}, ['gh', *sys.argv[3:]])
""")
    result = execute(hosted, PR, overrides={"PATH": str(bin_path) + ":/usr/bin:/bin"})
    assert result.returncode == 0 and result.stdout == result.stderr == b""
    assert json.loads(log.read_text()) == {"args": ["api", "--hostname", "github.com", "repos/Sacarima/UTA-AW/pulls/37"],
        "token": "inert-github-token", "host": "github.com"}
    call, = records(hosted)
    assert call["cwd"] == str(hosted[0])
    assert call["argv"] == ["-I", "-m", "taskly_agent.live_demo", "check-pr", "--name", identity.repository,
        "--number", "37", "--base", BASE, "--head", HEAD, "--metadata", str(temporary / "taskly-demo/pr.json")]
    assert live_demo.main(call["argv"][3:]) == 0
    assert "private" not in capsys.readouterr().out


def test_install_block_uses_only_trusted_package_and_real_temporary_paths(hosted):
    trusted, temporary, _, _ = hosted
    fresh = temporary / "fresh runner temp 任务"
    fresh.mkdir()
    bin_path = temporary / "install-stubs"
    log = temporary / "install.jsonl"
    executable(bin_path / "python", f"""import json, os, shutil, sys
from pathlib import Path
with Path({str(log)!r}).open('a') as stream:
    stream.write(json.dumps({{'args':sys.argv[1:],'cwd':os.getcwd()}}) + '\\n')
if sys.argv[1:3] == ['-m', 'venv']:
    target = Path(sys.argv[3]) / 'bin/python'
    target.parent.mkdir(parents=True)
    shutil.copyfile(sys.argv[0], target)
    target.chmod(0o700)
else:
    assert sys.argv[1:] == ['-m', 'pip', 'install', './agent[openai]']
""")
    result = execute(hosted, INSTALL, overrides={"PATH": str(bin_path) + ":/usr/bin:/bin", "RUNNER_TEMP": str(fresh)})
    assert result.returncode == 0 and result.stdout == result.stderr == b""
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        {"args": ["-m", "venv", str(fresh / "demo-venv")], "cwd": str(trusted)},
        {"args": ["-m", "pip", "install", "./agent[openai]"], "cwd": str(trusted)}]
    assert (fresh / "taskly-demo").stat().st_mode & 0o777 == 0o700
    assert not list((trusted.parent / "candidate").iterdir())


def test_workflow_environment_mappings_are_not_supplied_independently_by_fixtures():
    assert environment_mapping(WORKFLOW.read_text().splitlines(), 4) == {
        "BASE_SHA": BASE, "HEAD_SHA": HEAD, "PR_NUMBER": "37", "REPOSITORY": "Sacarima/UTA-AW"}
    assert environment_mapping(step_lines(GATE), 8) == {"APPROVED_BASE": BASE, "WORKFLOW_SHA": BASE}
    assert environment_mapping(step_lines(PR), 8) == {"GH_TOKEN": "inert-github-token", "GH_HOST": "github.com"}
    assert environment_mapping(step_lines(PAID), 8) == {"OPENAI_API_KEY": "inert-key-not-a-credential"}
    for name in (INSTALL, ADMIT, BUILD, PUBLISH):
        assert environment_mapping(step_lines(name), 8) == {}


@pytest.mark.parametrize("flag", ["--name", "--number", "--base", "--head", "--image", "--config"])
def test_missing_required_flags_in_an_export_are_rejected_by_real_parser(hosted, flag):
    script = block(PAID)
    value = {"--name": '"$REPOSITORY"', "--number": '"$PR_NUMBER"', "--base": '"$BASE_SHA"',
        "--head": '"$HEAD_SHA"', "--image": '"$IMAGE"', "--config": '"$RUNNER_TEMP/taskly-demo/config.json"'}[flag]
    needle = flag + " " + value
    assert script.count(needle) == 1
    assert execute(hosted, PAID, script=script.replace(needle, "")).returncode == 0
    # The inert process intentionally accepts arbitrary argv, unlike argparse.
    # A zero shell exit alone would miss this defect; the real parser must fail.
    call, = records(hosted)
    with pytest.raises(SystemExit) as exc:
        end_to_end.main(call["argv"][3:])
    assert exc.value.code == 2


def test_literal_base_and_broken_parentheses_cannot_masquerade_as_valid_invocations(hosted, monkeypatch, capsys):
    script = block(PAID)
    broken = script.replace('IMAGE=$(cat "$RUNNER_TEMP/taskly-demo/image.id")', 'IMAGE=cat "$RUNNER_TEMP/taskly-demo/image.id")')
    assert broken != script
    result = subprocess.run(["/bin/bash", "-n"], input=broken.encode(), capture_output=True, timeout=10)
    assert result.returncode != 0
    assert execute(hosted, PAID, script=script.replace('--base "$BASE_SHA"', '--base BASE_SHA')).returncode == 0
    call, = records(hosted)
    async def forbidden_run(*args, **kwargs):
        pytest.fail("Invalid identity must never reach the paid pipeline")
    monkeypatch.setattr(end_to_end, "run", forbidden_run)
    assert end_to_end.main(call["argv"][3:]) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "blocked"


@pytest.mark.parametrize("condition", ["absent", "unavailable", "failed", "invalid"])
def test_publication_cannot_turn_missing_or_failed_docker_verification_into_success(hosted, capsys, condition):
    assert execute(hosted, PUBLISH).returncode == 0
    call, = records(hosted)
    _, temporary, _, _ = hosted
    identity = PRIdentity(repository="Sacarima/UTA-AW", number=37, base_sha=BASE, head_sha=HEAD)
    data = report_data(identity)
    if condition in {"absent", "unavailable"}:
        data.update(verification_state="unavailable", verification_outcome=None if condition == "absent" else "blocked",
            tests=None, coverage_state="unavailable", coverage=None)
        if condition == "absent":
            data["verification_identity"] = None
    elif condition == "failed":
        data["verification_outcome"] = "failed"
        data["tests"]["failed"] = 1
    path = temporary / "taskly-demo/report.raw.json"
    path.write_text("invalid private diagnostics" if condition == "invalid" else json.dumps(data))
    assert live_demo.main(call["argv"][3:]) == 1
    assert not (temporary / "taskly-demo/evidence.json").exists()
    assert "private" not in capsys.readouterr().out


def test_build_expands_trusted_base_and_immutable_image_file(hosted):
    trusted, temporary, _, _ = hosted
    log = temporary / "build.json"
    stub = temporary / "stub-docker"
    executable(stub, f"""import json, os, sys
from pathlib import Path
Path({str(log)!r}).write_text(json.dumps({{'args':sys.argv[1:],'cwd':os.getcwd()}}))
Path(sys.argv[sys.argv.index('--iidfile') + 1]).write_text({IMAGE!r})
""")
    script = block(BUILD)
    assert script.count("/usr/bin/docker") == 1
    result = execute(hosted, BUILD, script=script.replace("/usr/bin/docker", shlex.quote(str(stub))))
    assert result.returncode == 0
    assert json.loads(log.read_text()) == {"cwd": str(trusted), "args": ["build", "--platform", "linux/amd64",
        "--build-arg", "TRUSTED_REVISION=" + BASE, "--iidfile", str(temporary / "taskly-demo/image.id"),
        "--file", "agent/sandbox/Dockerfile", "."]}


def gate_diagnostics(result):
    diagnostics = {}
    for line in result.stdout.decode().splitlines():
        assert re.fullmatch(r'identity_gate\.[A-Za-z0-9_]+=(true|false)', line)
        key, value = line.removeprefix('identity_gate.').split('=')
        diagnostics[key] = value == 'true'
    return diagnostics


@pytest.mark.parametrize("bad,predicate", [
    ({"APPROVED_BASE": ""}, "APPROVED_BASE_present"),
    ({"APPROVED_BASE": HEAD}, "approval_matches_base"),
    ({"APPROVED_BASE": "private-approval-value"}, "approval_matches_base"),
    ({"WORKFLOW_SHA": HEAD}, "workflow_matches_base"),
    ({"WORKFLOW_SHA": ""}, "WORKFLOW_SHA_present"),
    ({"WORKFLOW_SHA": "private-workflow-value"}, "workflow_matches_base"),
    ({"HEAD_SHA": BASE}, "base_head_distinct"),
    ({"BASE_SHA": ""}, "BASE_SHA_present"),
    ({"BASE_SHA": "main"}, "base_sha_format_valid"),
    ({"HEAD_SHA": "$(touch never)"}, "head_sha_format_valid"),
    ({"PR_NUMBER": "1; echo injected"}, "pr_number_format_valid"),
])
def test_approval_gate_rejects_bad_identities_before_checkout_or_installation(hosted, bad, predicate):
    result = execute(hosted, GATE, overrides=bad)
    assert result.returncode != 0
    output = (result.stdout + result.stderr).decode()
    assert gate_diagnostics(result)[predicate] is False and BASE not in output and HEAD not in output
    assert "private-" not in output and "$(touch never)" not in output and "echo injected" not in output
    assert not hosted[2].exists() and not (hosted[0] / "never").exists()


def test_missing_environment_configuration_context_does_not_fall_back_to_input_or_process_env(hosted, monkeypatch):
    # Environment-only values arrive after the environment is declared. Model
    # an absent vars value honestly instead of always supplying BASE in tests.
    monkeypatch.setitem(CONTEXT, "${{ vars.TASKLY_DEMO_BASE_SHA }}", "")
    result = execute(hosted, GATE, overrides={"TASKLY_DEMO_BASE_SHA": BASE})
    assert result.returncode != 0 and gate_diagnostics(result)["APPROVED_BASE_present"] is False
    assert BASE.encode() not in result.stderr and not hosted[2].exists()


def test_workflow_commit_context_is_not_replaced_with_submitted_base(hosted, monkeypatch):
    monkeypatch.setitem(CONTEXT, "${{ github.sha }}", HEAD)
    result = execute(hosted, GATE)
    assert result.returncode != 0 and gate_diagnostics(result)["workflow_matches_base"] is False
    assert BASE.encode() not in result.stderr and HEAD.encode() not in result.stderr


def test_runtime_only_configuration_is_not_a_documented_vars_replacement(hosted, monkeypatch):
    monkeypatch.setitem(CONTEXT, "${{ vars.TASKLY_DEMO_BASE_SHA }}", HEAD)
    result = execute(hosted, GATE, overrides={"TASKLY_DEMO_BASE_SHA": BASE})
    assert result.returncode != 0 and gate_diagnostics(result)["approval_matches_base"] is False
    assert not hosted[2].exists()


def test_original_expression_passes_known_pinned_commits_and_reported_pr_number(hosted):
    base = "cbc3d57489aad5b01e6f1fe218d1f7cc2d2620fe"
    head = "036d546d52be2789c0f8cb4b5e2eeb9e38eda981"
    result = execute(hosted, GATE, overrides={"BASE_SHA": base, "HEAD_SHA": head, "APPROVED_BASE": base,
        "WORKFLOW_SHA": base, "PR_NUMBER": "2", "OPENAI_API_KEY": "private-key-must-not-appear"})
    assert result.returncode == 0
    diagnostics = gate_diagnostics(result)
    for predicate in ("base_sha_format_valid", "head_sha_format_valid", "base_head_distinct",
                      "approval_matches_base", "workflow_matches_base", "pr_number_format_valid"):
        assert diagnostics[predicate] is True
    output = (result.stdout + result.stderr).decode()
    assert "private-key" not in output and base not in output and head not in output


@pytest.mark.parametrize("field,pattern", [("HEAD_SHA", "head_sha_format_valid"), ("PR_NUMBER", "pr_number_format_valid")])
@pytest.mark.parametrize("formatting", ["leading-space", "trailing-space", "newline", "crlf", "double-quotes", "single-quotes", "zero-width"])
def test_hidden_formatting_identifies_exact_predicate_without_normalizing_input(hosted, field, pattern, formatting):
    original = HEAD if field == "HEAD_SHA" else "2"
    value = {"leading-space": " " + original, "trailing-space": original + " ", "newline": original + "\n",
        "crlf": original + "\r\n", "double-quotes": '"' + original + '"', "single-quotes": "'" + original + "'",
        "zero-width": original + "\u200b"}[formatting]
    result = execute(hosted, GATE, overrides={field: value})
    assert result.returncode != 0
    diagnostics = gate_diagnostics(result)
    assert diagnostics[pattern] is False
    assert diagnostics["approval_matches_base"] and diagnostics["workflow_matches_base"] and diagnostics["base_head_distinct"]
    if formatting in {"leading-space", "trailing-space", "newline", "crlf"}:
        assert diagnostics[field + "_has_outer_whitespace"] is True
    elif formatting in {"double-quotes", "single-quotes"}:
        assert diagnostics[field + "_contains_quote"] is True
    else:
        assert diagnostics[field + "_ascii_only"] is False
    assert HEAD not in result.stdout.decode() and BASE not in result.stdout.decode()


def test_valid_gate_and_manual_only_workflow_security_contract(hosted):
    assert execute(hosted, GATE).returncode == 0
    text = WORKFLOW.read_text()
    assert text.split("'on':\n", 1)[1].split("permissions:\n", 1)[0].startswith("  workflow_dispatch:\n")
    assert "pull_request_target" not in text and "\n  pull_request:" not in text and "\n  push:" not in text
    assert "default: false" in text and "environment: taskly-live-demo" in text
    guard = next(line.strip() for line in text.splitlines() if line.startswith("    if:"))
    assert guard == ("if: github.event_name == 'workflow_dispatch' && github.repository == 'Sacarima/UTA-AW' "
        "&& github.ref == 'refs/heads/main' && inputs.allow_paid && github.run_attempt == 1")
    assert text.split("permissions:\n", 1)[1].split("concurrency:\n", 1)[0] == "  contents: read\n  pull-requests: read\n"
    assert "cancel-in-progress: false" in text and "continue-on-error" not in text
    assert text.count("${{ secrets.OPENAI_API_KEY }}") == 1
    assert text.count("persist-credentials: false") == 2
    assert text.count("ref: ${{ inputs.base_sha }}") == text.count("ref: ${{ inputs.head_sha }}") == 1
    assert text.index("- name: " + GATE) < text.index("uses: actions/checkout")
    assert text.index("- name: Validate actual GitHub PR identity") < text.index("- name: Checkout pinned candidate")
    assert text.index("- name: " + ADMIT) < text.index("- name: " + BUILD) < text.index("- name: " + PAID)
    for line in text.splitlines():
        if "uses: " in line:
            assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", line.split("uses: ")[1].split()[0])
    upload = text.split("      - name: Upload sanitized evidence only\n", 1)[1]
    assert "steps.evidence.outcome == 'success'" in upload
    assert "path: ${{ runner.temp }}/taskly-demo/evidence.json\n" in upload
    assert "report.raw" not in upload and "summary.raw" not in upload and "retention-days: 3" in upload


def test_all_workflow_run_blocks_have_valid_bash_syntax():
    names = [line.split("- name: ", 1)[1] for line in WORKFLOW.read_text().splitlines() if line.startswith("      - name:")]
    for name in names:
        if "        run: |" not in step_lines(name):
            assert any(line.startswith("        uses:") for line in step_lines(name))
            continue
        command = block(name)
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-n"], input=command.encode(),
            capture_output=True, timeout=10, env={"PATH": "/usr/bin:/bin"})
        assert result.returncode == 0, name
