"""Automatic PR integration over deterministic Git/HTTP/Docker fakes only."""

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from taskly_agent import pr_automation as automation
from taskly_agent.contracts import PRIdentity
from taskly_agent.github_pr import GitHub, GitHubError, PullRequestRun, resolve_run

from test_analysis import git, repository
from test_end_to_end import pipeline
from test_docker_runner import IMAGE
from test_openai_transport import isolated_process
from test_live_demo import report_data


pytestmark = pytest.mark.usefixtures("isolated_process")
BASE, HEAD = "a" * 40, "b" * 40


def context(identity=None, **changes):
    identity = identity or PRIdentity(repository="Sacarima/UTA-AW", number=2, base_sha=BASE, head_sha=HEAD)
    return PullRequestRun(identity=identity, source_run_id=10, **{**dict(fork=False, trusted_author=True, draft=False,
        baseline_passed=True, first_attempt=True, source_bound=True), **changes})


class FakeGitHub:
    repository = "Sacarima/UTA-AW"

    def __init__(self, identity=None):
        identity = identity or context().identity
        self.metadata = {"number": identity.number, "state": "open", "draft": False, "author_association": "OWNER",
            "base": {"sha": identity.base_sha, "ref": "main", "repo": {"full_name": self.repository}},
            "head": {"sha": identity.head_sha, "repo": {"full_name": self.repository}}}
        self.source = {"id": 10, "event": "pull_request", "status": "completed", "name": "Taskly isolated verification",
            "path": ".github/workflows/taskly-verification.yml", "repository": {"full_name": self.repository},
            "head_sha": identity.head_sha, "pull_requests": [copy.deepcopy(self.metadata)], "run_attempt": 1, "conclusion": "success"}
        self.writes, self.history = [], []
        self.freshness_reads = 0
        self.change_on_read = None

    def run(self, number):
        assert number == 10
        return self.source

    def pr(self, number):
        assert number == self.metadata["number"]
        self.freshness_reads += 1
        if self.freshness_reads == self.change_on_read:
            self.metadata["head"]["sha"] = "f" * 40
        return self.metadata

    def comments(self, number):
        return self.history

    def comment(self, number, body):
        self.writes.append((number, body))
        return {"id": 1}


def test_source_run_resolution_and_fork_policy_are_commit_bound():
    client = FakeGitHub()
    assert resolve_run(client, 10) == context()
    client.metadata["head"]["repo"]["full_name"] = "Someone/fork"
    result = resolve_run(client, 10)
    assert result.fork and result.source_bound
    client.source["pull_requests"] = []
    with pytest.raises(GitHubError, match="GitHub operation unavailable"):
        resolve_run(client, 10)


def test_synthetic_merge_sha_is_not_confused_with_pinned_pr_head():
    client = FakeGitHub()
    client.source["head_sha"] = "c" * 40
    client.metadata["merge_commit_sha"] = "c" * 40
    assert resolve_run(client, 10).identity.head_sha == HEAD
    client.metadata["merge_commit_sha"] = "d" * 40
    with pytest.raises(GitHubError):
        resolve_run(client, 10)


@pytest.mark.parametrize("bad", ["stale-head", "stale-base", "wrong-workflow", "wrong-event", "closed", "ambiguous"])
def test_unrelated_or_stale_source_run_is_rejected(bad):
    client = FakeGitHub()
    if bad == "stale-head":
        client.metadata["head"]["sha"] = "f" * 40
    elif bad == "stale-base":
        client.metadata["base"]["sha"] = "f" * 40
    elif bad == "wrong-workflow":
        client.source["path"] = ".github/workflows/attacker.yml"
    elif bad == "wrong-event":
        client.source["event"] = "push"
    elif bad == "closed":
        client.metadata["state"] = "closed"
    else:
        client.source["pull_requests"] *= 2
    with pytest.raises((ValueError, GitHubError)):
        resolve_run(client, 10)


def test_authorized_generation_reuses_real_adapters_controller_and_retains_patch(pipeline, tmp_path_factory):
    report = asyncio.run(automation.generate(pipeline.repo, context(pipeline.identity), image=IMAGE,
        workflow_sha=pipeline.identity.base_sha, approved_base=pipeline.identity.base_sha, enabled=True,
        credential_present=True, patch_directory=tmp_path_factory.mktemp("review") / "patch"))
    assert report.result.outcome == "passed" and report.patch_available
    assert report.result.verification_state == "available" and report.result.model_calls == 1
    assert report.result.tests.passed == 2 and report.result.coverage.lines.total == 2
    assert len(pipeline.http.requests) == 1
    # Two private applications: verification then export, never the checkout.
    assert len(pipeline.workspaces) == 2 and all(not item.exists() for item in pipeline.workspaces)
    assert (pipeline.repo / "backend/tests/test_service.py").read_text() == "# pinned original\n"


@pytest.mark.parametrize("case,reason", [("fork", "fork"), ("author", "untrusted_author"), ("draft", "draft"),
    ("baseline", "baseline_failed"), ("repeat", "repeat_attempt"), ("binding", "source_unbound"),
    ("disabled", "ai_disabled"), ("approval", "approval_mismatch"), ("key", "missing_credential")])
def test_unauthorized_generation_never_reaches_provider(pipeline, tmp_path_factory, case, reason):
    changes = {"fork": {"fork": True}, "author": {"trusted_author": False}, "draft": {"draft": True},
        "baseline": {"baseline_passed": False}, "repeat": {"first_attempt": False}, "binding": {"source_bound": False}}
    report = asyncio.run(automation.generate(pipeline.repo, context(pipeline.identity, **changes.get(case, {})),
        image=IMAGE, workflow_sha=pipeline.identity.base_sha,
        approved_base="f" * 40 if case == "approval" else pipeline.identity.base_sha,
        enabled=case != "disabled", credential_present=case != "key", patch_directory=tmp_path_factory.mktemp("blocked") / "patch"))
    assert report.reason == reason and report.result.outcome == "blocked"
    assert report.result.model_calls == 0 and not report.patch_available
    assert not pipeline.http.requests and not pipeline.daemon.calls


def test_failed_verification_is_visible_without_patch_or_success(pipeline, tmp_path_factory):
    pipeline.daemon.capture_overrides = {"failed": 1}
    destination = tmp_path_factory.mktemp("failure") / "patch"
    report = asyncio.run(automation.generate(pipeline.repo, context(pipeline.identity), image=IMAGE,
        workflow_sha=pipeline.identity.base_sha, approved_base=pipeline.identity.base_sha, enabled=True,
        credential_present=True, patch_directory=destination))
    assert report.result.outcome == "failed" and report.result.verification_outcome == "failed"
    assert report.result.model_calls == 2 and report.result.repairs == 1
    assert not report.patch_available and not destination.exists()
    client = FakeGitHub(pipeline.identity)
    assert automation.publish(client, report, 10, 20) == "published"
    assert "Independent verification: available/failed" in client.writes[0][1]
    assert "Workflow outcome: failed" in client.writes[0][1]


def test_sanitized_publication_and_duplicate_prevention(pipeline):
    report = automation.analyze(pipeline.repo, context(pipeline.identity))
    report = automation.unavailable(report.context, "ai_disabled", report.decision)
    client = FakeGitHub(pipeline.identity)
    assert automation.publish(client, report, 10, 20) == "published"
    number, body = client.writes[0]
    assert number == pipeline.identity.number and pipeline.identity.head_sha in body and pipeline.identity.base_sha in body
    assert "AI success" in body and "Tests: unavailable" in body and "Coverage: unavailable" in body
    assert "def value" not in body and "pinned original" not in body
    client.history = [{"user": {"login": "github-actions[bot]", "type": "Bot"}, "body": body}]
    assert automation.publish(client, report, 10, 20) == "already_reported" and len(client.writes) == 1


@pytest.mark.parametrize("read", [1, 2])
def test_stale_commits_never_publish(pipeline, read):
    report = automation.analyze(pipeline.repo, context(pipeline.identity))
    client = FakeGitHub(pipeline.identity)
    client.change_on_read = read
    with pytest.raises(GitHubError):
        automation.publish(client, report, 10, 20)
    assert not client.writes


def test_fake_marker_from_contributor_cannot_suppress_bot_report(pipeline):
    report = automation.analyze(pipeline.repo, context(pipeline.identity))
    client = FakeGitHub(pipeline.identity)
    client.history = [{"user": {"login": "attacker", "type": "User"}, "body": automation.render(report, 20)}]
    assert automation.publish(client, report, 10, 20) == "published"


def test_cli_missing_secret_and_generation_report_failure_are_explicit(pipeline, tmp_path, monkeypatch, capsys):
    client = FakeGitHub(pipeline.identity)
    monkeypatch.setattr(automation, "GitHub", lambda *args: client)
    monkeypatch.setenv("TASKLY_PR_AI_ENABLED", "true")
    monkeypatch.setenv("APPROVED_BASE", pipeline.identity.base_sha)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = ["--name", client.repository, "--source-run", "10", "--directory", str(tmp_path),
        "--expected-head", pipeline.identity.head_sha, "--workflow-sha", pipeline.identity.base_sha]
    assert automation.main(["admit", *args]) == 1
    assert automation.PRReport.model_validate_json((tmp_path / "report.json").read_text()).reason == "missing_credential"
    assert not pipeline.http.requests
    assert automation.main(["analyze", *args, "--repository", str(pipeline.repo)]) == 0
    assert automation.main(["publish", *args, "--workflow-run", "20", "--generation-state", "failure"]) == 0
    assert "generation_report_unavailable" in client.writes[0][1]
    assert "Usage/cost: unavailable" in client.writes[0][1] or "usage/cost: unavailable" in client.writes[0][1]
    assert "private-key" not in capsys.readouterr().out


def test_github_transport_is_fixed_bounded_non_redirecting_and_does_not_leak_credentials():
    client = GitHub("Sacarima/UTA-AW", "inert-test-token")
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'{"id":10}'
    class Opener:
        def open(self, request, timeout):
            requests.append((request, timeout))
            return Response()
    client._opener = Opener()
    assert client.run(10) == {"id": 10}
    request, timeout = requests[0]
    assert request.full_url == "https://api.github.com/repos/Sacarima/UTA-AW/actions/runs/10" and timeout == 15
    assert request.get_header("Authorization") == "Bearer inert-test-token"
    with pytest.raises(GitHubError): client.run(-1)
    class Broken:
        def open(self, *args, **kwargs): raise RuntimeError("inert-test-token private-body")
    client._opener = Broken()
    with pytest.raises(GitHubError) as exc:
        client.run(10)
    assert "inert-test-token" not in str(exc.value) and "private" not in str(exc.value)


@pytest.mark.parametrize("missing", ["generation", "verification", "passed-verification", "patch"])
def test_skipped_generation_or_missing_failed_evidence_can_never_be_success(missing):
    ctx = context()
    data = report_data(ctx.identity)
    if missing == "generation":
        data["model_calls"] = 0
    elif missing == "verification":
        data.update(verification_state="unavailable", verification_outcome=None, verification_identity=None,
            tests=None, coverage_state="unavailable", coverage=None)
    elif missing == "passed-verification":
        data["verification_outcome"] = "failed"
        data["tests"]["failed"] = 1
    decision = automation.AgentDecision(classification="backend", action="generate", rationale="Supported changes")
    with pytest.raises(ValueError):
        automation.PRReport(context=ctx, decision=decision, reason="completed",
            result=automation.PublicReport.model_validate_json(json.dumps(data)), patch_available=missing != "patch")


def test_duplicate_head_is_stopped_before_any_model_call(pipeline, tmp_path, monkeypatch):
    client = FakeGitHub(pipeline.identity)
    client.history = [{"user": {"login": "github-actions[bot]", "type": "Bot"},
                       "body": f"<!-- taskly-agent:{pipeline.identity.head_sha} -->\nAlready reported"}]
    monkeypatch.setattr(automation, "GitHub", lambda *args: client)
    monkeypatch.setenv("TASKLY_PR_AI_ENABLED", "true")
    monkeypatch.setenv("APPROVED_BASE", pipeline.identity.base_sha)
    args = ["--name", client.repository, "--source-run", "10", "--directory", str(tmp_path),
        "--expected-head", pipeline.identity.head_sha, "--workflow-sha", pipeline.identity.base_sha]
    assert automation.main(["admit", *args]) == 0
    assert automation.PRReport.model_validate_json((tmp_path / "report.json").read_text()).reason == "repeat_attempt"
    assert not pipeline.http.requests and not pipeline.daemon.calls
