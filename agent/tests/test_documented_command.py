"""Execute only the documented CLI invocation, using an inert Python stub.

No provisioning, Git, Docker, credentials, network, or model calls occur.
The expanded arguments then exercise the real CLI parser with a fake pipeline.
"""

import json
from pathlib import Path
import re
import subprocess
import sys
import textwrap

import pytest

from taskly_agent import end_to_end
from taskly_agent.openai_transport import demo_config


DOCUMENT = Path(__file__).parents[1] / "END_TO_END.md"
BASE = "a" * 40
HEAD = "b" * 40
IMAGE = "sha256:" + "c" * 64


def documented_invocation():
    blocks = re.findall(r"```bash\n(.*?)\n\s*```", DOCUMENT.read_text(), re.DOTALL)
    block, = [textwrap.dedent(block) for block in blocks if "-m taskly_agent.end_to_end" in block]
    # Extract just env/Python/redirections; never run the credential prompt,
    # provisioning commands, or anything else from the demonstration procedure.
    return block[block.index("env -i "):].split("\nRUN_STATUS=", 1)[0]


@pytest.mark.parametrize("paid", [False, True], ids=["without-paid-opt-in", "authorized-command"])
def test_documented_shell_expansion_and_actual_argparse_contract(tmp_path, monkeypatch, capsys, paid):
    demo = tmp_path / "demo directory 任务"
    candidate = tmp_path / "candidate repository 任务"
    candidate.mkdir()
    python = demo / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!" + sys.executable + "\n" + textwrap.dedent("""\
        import json, os, sys
        print(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}))
        print("inert summary", file=sys.stderr)
    """))
    python.chmod(0o700)
    (demo / "image.id").write_text(IMAGE + "\n")
    config = demo_config(budget_usd=0.10)
    (demo / "config.json").write_text(config.model_dump_json())
    command = documented_invocation()
    if not paid:
        assert command.count("--allow-paid") == 1
        command = command.replace("--allow-paid", "")
    result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-euo", "pipefail", "-c", command],
        cwd=demo, env={"PATH": "/usr/bin:/bin", "DEMO_DIR": str(demo), "CANDIDATE": str(candidate),
            "BASE": BASE, "HEAD": HEAD, "PR_NUMBER": "37", "OPENAI_API_KEY": "inert-test-placeholder"},
        capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout == result.stderr == b""  # Both streams must be redirected.
    report_path = demo / "report.json"
    summary_path = demo / "summary.txt"
    observed = json.loads(report_path.read_text())
    assert summary_path.read_text() == "inert summary\n"
    expected = ["-I", "-m", "taskly_agent.end_to_end", "--repository", str(candidate),
        "--name", "Sacarima/UTA-AW", "--number", "37", "--base", BASE, "--head", HEAD,
        "--trusted-revision", BASE, "--image", IMAGE, "--config", str(demo / "config.json")]
    assert observed == {"argv": expected + (["--allow-paid"] if paid else []), "cwd": str(demo)}
    assert 'Review `"$DEMO_DIR/report.json"`' in DOCUMENT.read_text()

    calls = []

    async def fake_run(repository, identity, image, trusted_revision, configuration, *, allow_paid):
        assert repository == candidate
        assert identity.repository == "Sacarima/UTA-AW" and identity.number == 37
        assert identity.base_sha == BASE and identity.head_sha == HEAD
        assert trusted_revision == BASE and image == IMAGE
        assert configuration == config and allow_paid is paid
        calls.append(identity)
        return end_to_end.PublicReport(identity=identity, classification="backend", outcome="blocked",
                                       reason="inert_documentation_test")

    monkeypatch.setattr(end_to_end, "run", fake_run)
    assert end_to_end.main(observed["argv"][3:]) == 1
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "inert_documentation_test"


def test_all_documented_bash_examples_have_valid_syntax():
    blocks = re.findall(r"```bash\n(.*?)\n\s*```", DOCUMENT.read_text(), re.DOTALL)
    assert len(blocks) == 2
    for block in blocks:
        result = subprocess.run(["/bin/bash", "--noprofile", "--norc", "-n"],
            input=textwrap.dedent(block).encode(), capture_output=True, timeout=10,
            env={"PATH": "/usr/bin:/bin"})
        assert result.returncode == 0, result.stderr.decode()
