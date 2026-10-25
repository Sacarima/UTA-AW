import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from taskly_agent.analysis import GitChangeAnalyzer
from taskly_agent.contracts import ChangedFile, PRContext, PRIdentity
from taskly_agent.controller import ExecutionController
from taskly_agent.git_changes import (
    GIT_EXECUTABLE, GitAnalysisError, GitReader, parse_name_status, parse_tree,
)
from taskly_agent.lifecycle import ExecutionPolicy, RunInput


def git(repo, *args):
    result = subprocess.run(
        [GIT_EXECUTABLE, "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
         "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
        cwd=repo, env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": os.devnull,
                       "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"},
        check=True, capture_output=True, timeout=10,
    )
    return result.stdout.decode().strip()


def write(repo, path, content="value = 1\n"):
    file = repo / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content)


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "--quiet")
    git(tmp_path, "commit", "--allow-empty", "--quiet", "-m", "base")
    return tmp_path


def commit(repo):
    git(repo, "add", "--all")
    git(repo, "commit", "--quiet", "-m", "fixture")
    return git(repo, "rev-parse", "HEAD")


def analyzer(repo, base):
    return GitChangeAnalyzer(GitReader(repo), base_sha=base, head_sha=git(repo, "rev-parse", "HEAD"))


@pytest.mark.parametrize("source,classification,suggested", [
    ("frontend/src/features/todos/api/new.ts", "frontend", "frontend/src/features/todos/api/new.test.ts"),
    ("frontend/src/components/New.tsx", "frontend", "frontend/src/components/New.test.tsx"),
    ("backend/app/features/todos/new.py", "backend", "backend/tests/features/todos/test_new.py"),
    ("backend/app/core/config.py", "backend", "backend/tests/core/test_config.py"),
])
def test_added_sources_become_convention_targets(repository, source, classification, suggested):
    base = git(repository, "rev-parse", "HEAD")
    write(repository, source)
    commit(repository)
    report = analyzer(repository, base).inspect()
    assert report.decision.classification == classification
    assert report.decision.action == "generate"
    assert report.decision.selected_skills == ()
    impact, = report.impacts
    assert impact.change.status == "added"
    assert impact.recommendation == "add_tests"
    assert impact.suggested_test == suggested
    assert impact.existing_tests == ()


@pytest.mark.parametrize("source,test", [
    ("backend/app/features/todos/router.py", "backend/tests/features/todos/test_router.py"),
    ("frontend/src/components/Item.tsx", "frontend/src/components/Item.test.tsx"),
    ("frontend/src/lib/http.ts", "frontend/src/lib/__tests__/http.test.ts"),
])
def test_modified_sources_locate_existing_tests_without_claiming_coverage(repository, source, test):
    write(repository, source)
    write(repository, test)
    base = commit(repository)
    write(repository, source, "value = 2\n")
    commit(repository)
    report = analyzer(repository, base).inspect()
    assert report.impacts[0].existing_tests == (test,)
    assert report.impacts[0].recommendation == "review_tests"
    assert report.impacts[0].change.status == "modified"
    assert "review/update" in report.decision.rationale
    assert "do not establish coverage" in report.decision.rationale


def test_mixed_changes_and_working_tree_files_do_not_affect_head_inventory(repository):
    base = git(repository, "rev-parse", "HEAD")
    write(repository, "backend/app/service.py")
    write(repository, "frontend/src/Service.ts")
    commit(repository)
    write(repository, "backend/tests/test_service.py")  # uncommitted, not evidence
    report = analyzer(repository, base).inspect()
    assert report.decision.classification == "mixed"
    assert report.decision.action == "generate"
    assert len(report.impacts) == 2
    assert all(not impact.existing_tests for impact in report.impacts)


@pytest.mark.parametrize("path", [
    "README.md", "docs/architecture.md", "backend/tests/test_service.py",
    "frontend/src/Service.test.ts", "frontend/src/Service.spec.tsx", "frontend/src/test/setup.ts",
    "frontend/vite.config.ts", "frontend/package.json", "backend/pyproject.toml",
    "frontend/src/types.ts", "frontend/src/Button.stories.tsx", "frontend/src/env.d.ts",
    "frontend/src/__generated__/client.ts", "backend/app/generated/client.py",
    "agent/src/taskly_agent/provider.py", ".github/workflows/test.yml",
])
def test_non_production_changes_skip_generation(repository, path):
    base = git(repository, "rev-parse", "HEAD")
    write(repository, path)
    commit(repository)
    report = analyzer(repository, base).inspect()
    assert report.decision.classification == "irrelevant"
    assert report.decision.action == "skip"
    assert report.impacts == ()


def test_removed_source_is_cleanup_not_new_generation(repository):
    write(repository, "backend/app/service.py")
    write(repository, "backend/tests/test_service.py")
    base = commit(repository)
    (repository / "backend/app/service.py").unlink()
    commit(repository)
    report = analyzer(repository, base).inspect()
    assert report.changes[0].status == "removed"
    assert report.decision.classification == "backend"
    assert report.decision.action == "skip"
    assert report.impacts[0].recommendation == "skip_removed"
    assert report.impacts[0].suggested_test is None
    assert report.impacts[0].existing_tests == ("backend/tests/test_service.py",)
    assert "cleanup" in report.decision.rationale


def test_rename_preserves_old_name_test_references(repository):
    write(repository, "backend/app/old.py")
    write(repository, "backend/tests/test_old.py")
    base = commit(repository)
    (repository / "backend/app/old.py").rename(repository / "backend/app/new.py")
    commit(repository)
    report = analyzer(repository, base).inspect()
    change, = report.changes
    assert change.status == "renamed" and change.previous_path == "backend/app/old.py"
    assert report.impacts[0].existing_tests == ("backend/tests/test_old.py",)
    assert report.impacts[0].suggested_test == "backend/tests/test_new.py"
    assert "old-name test imports" in report.decision.rationale


def test_move_out_of_source_root_does_not_target_documentation(repository):
    write(repository, "backend/app/old.py")
    base = commit(repository)
    (repository / "docs").mkdir()
    (repository / "backend/app/old.py").rename(repository / "docs/example.md")
    commit(repository)
    report = analyzer(repository, base).inspect()
    assert report.decision.classification == "backend" and report.decision.action == "skip"
    assert report.impacts[0].suggested_test is None


@pytest.mark.parametrize("path", ["frontend/src/Área de trabalho.tsx", "backend/app/任务 file.py",
                                 "frontend/src/$(touch INJECTION).ts"])
def test_spaces_unicode_and_shell_like_names_remain_opaque_metadata(repository, path):
    base = git(repository, "rev-parse", "HEAD")
    write(repository, path)
    commit(repository)
    report = analyzer(repository, base).inspect()
    assert report.changes[0].path == path
    assert report.decision.action == "generate"
    assert report.impacts[0].suggested_test is None
    assert not (repository / "INJECTION").exists()


@pytest.mark.parametrize("sha", ["HEAD", "main", "a" * 39, "a" * 41, "--output=/tmp/x", "a" * 40 + "\n", 1])
def test_invalid_sha_is_rejected_before_git(repository, monkeypatch, sha):
    reader = GitReader(repository)
    monkeypatch.setattr(reader, "_run", lambda *args: pytest.fail("must not invoke Git"))
    with pytest.raises(GitAnalysisError, match="invalid_commit"):
        reader.changes(sha, "a" * 40)


@pytest.mark.parametrize("output", [
    b"A\0path", b"A\0", b"R100\0old\0", b"R101\0old\0new\0", b"Q\0file\0",
    b"A\0../escape\0", b"M\0/absolute\0", b"A\0bad\npath\0", b"A\0\xff\0",
    b"A\0same\0M\0same\0", b"R100\0same\0same\0", b"\0",
])
def test_malformed_or_unsupported_diff_output_is_rejected(output):
    with pytest.raises(GitAnalysisError):
        parse_name_status(output)


def test_nul_parser_handles_all_supported_statuses():
    changes = parse_name_status(b"A\0space name\0M\0unicode-\xc3\xa9\0D\0old\0R099\0before\0after\0")
    assert [change.status for change in changes] == ["added", "modified", "removed", "renamed"]
    assert changes[-1].previous_path == "before"
    assert parse_name_status(b"") == ()


@pytest.mark.parametrize("output", [
    b"missing metadata\0", b"100644 blob bad\tfile\0", b"999999 blob " + b"a" * 40 + b"\tfile\0",
    b"100644 tree " + b"a" * 40 + b"\tfile\0", b"100644 blob " + b"a" * 40 + b"\tfile",
    b"100644 blob " + b"a" * 40 + b"\t../file\0",
])
def test_malformed_tree_is_rejected(output):
    with pytest.raises(GitAnalysisError):
        parse_tree(output)


def test_non_commit_object_and_missing_commit_block_analysis(repository):
    write(repository, "README.md")
    base = commit(repository)
    blob = git(repository, "rev-parse", "HEAD:README.md")
    for head in (blob, "0" * 40):
        report = GitChangeAnalyzer(GitReader(repository), base_sha=base, head_sha=head).inspect()
        assert report.decision.action == "block"


@pytest.mark.parametrize("mode", ["timeout", "output_limit", "stderr_limit", "command_error"])
def test_subprocess_deadlines_output_caps_and_errors(repository, monkeypatch, mode):
    real_popen = subprocess.Popen
    scripts = {
        "timeout": "import time; time.sleep(10)",
        "output_limit": "import sys; sys.stdout.write('x'*10000)",
        "stderr_limit": "import sys; sys.stderr.write('x'*10000)",
        "command_error": "import sys; sys.stderr.write('secret'); sys.exit(1)",
    }

    def fake_git(command, **kwargs):
        assert command[0] == GIT_EXECUTABLE and kwargs["shell"] is False
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        assert "GIT_EXTERNAL_DIFF" not in kwargs["env"]
        return real_popen([sys.executable, "-c", scripts[mode]], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_git)
    reader = GitReader(repository, timeout_seconds=0.1 if mode == "timeout" else 5, max_output_bytes=100)
    with pytest.raises(GitAnalysisError) as caught:
        reader._run("cat-file", "-t", "a" * 40)
    assert str(caught.value) == ("output_limit" if mode == "stderr_limit" else mode)
    assert "secret" not in str(caught.value)


def test_git_error_blocks_without_exposing_stderr(tmp_path):
    report = GitChangeAnalyzer(GitReader(tmp_path), base_sha="a" * 40, head_sha="b" * 40).inspect()
    assert report.decision.action == "block"
    assert "command_error" in report.decision.rationale


def test_pr_metadata_cannot_override_actual_git_changes(repository):
    base = git(repository, "rev-parse", "HEAD")
    write(repository, "backend/app/service.py")
    head = commit(repository)
    instance = analyzer(repository, base)
    request = RunInput(pr=PRContext(identity=PRIdentity(repository="Sacarima/UTA-AW", number=1,
        base_sha=base, head_sha=head), changed_files=(ChangedFile(path="README.md", status="modified"),)))
    decision = asyncio.run(instance.analyze(request, ExecutionPolicy()))
    assert decision.action == "generate" and decision.classification == "backend"
    mismatch = request.model_copy(update={"pr": PRContext(identity=PRIdentity(
        repository="Sacarima/UTA-AW", number=1, base_sha=head, head_sha=head), changed_files=())})
    assert asyncio.run(instance.analyze(mismatch, ExecutionPolicy())).action == "block"


def test_non_regular_production_source_blocks_without_following_link(repository):
    base = git(repository, "rev-parse", "HEAD")
    folder = repository / "backend/app"
    folder.mkdir(parents=True)
    (folder / "link.py").symlink_to("/definitely/not/a/source/file")
    commit(repository)
    report = analyzer(repository, base).inspect()
    assert report.decision.action == "block"
    assert "non_regular_source" in report.decision.rationale


def test_non_regular_test_is_not_a_relevant_or_suggested_test(repository):
    base = git(repository, "rev-parse", "HEAD")
    write(repository, "backend/app/service.py")
    folder = repository / "backend/tests"
    folder.mkdir(parents=True)
    (folder / "test_service.py").symlink_to("/definitely/not/a/test")
    commit(repository)
    impact, = analyzer(repository, base).inspect().impacts
    assert impact.existing_tests == () and impact.suggested_test is None


def test_cross_stack_rename_is_mixed(repository):
    write(repository, "backend/app/old.py")
    base = commit(repository)
    (repository / "frontend/src").mkdir(parents=True)
    (repository / "backend/app/old.py").rename(repository / "frontend/src/new.ts")
    commit(repository)
    report = analyzer(repository, base).inspect()
    assert report.decision.classification == "mixed"
    assert report.decision.action == "generate"
    assert report.changes[0].status == "renamed"


def test_empty_commit_range_skips(repository):
    base = git(repository, "rev-parse", "HEAD")
    report = analyzer(repository, base).inspect()
    assert report.changes == () and report.decision.action == "skip"


@pytest.mark.parametrize("kind", ["generate", "skip", "block"])
def test_controller_respects_real_analyzer_decision(repository, kind):
    base = git(repository, "rev-parse", "HEAD")
    write(repository, "backend/app/service.py" if kind == "generate" else "docs/readme.md")
    head = commit(repository)
    if kind == "block":
        head = "0" * 40
    instance = GitChangeAnalyzer(GitReader(repository), base_sha=base, head_sha=head)

    class Components:
        calls = 0

        async def generate(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("fake generation reached")

        async def verify(self, *args):
            pytest.fail("no generated proposal in this test")

    components = Components()
    request = RunInput(pr=PRContext(identity=PRIdentity(repository="Sacarima/UTA-AW", number=1,
        base_sha=base, head_sha=head), changed_files=()))
    report = asyncio.run(ExecutionController(analyzer=instance, generator=components,
        verifier=components).run(request, ExecutionPolicy()))
    assert components.calls == (1 if kind == "generate" else 0)
    assert report.result.outcome == {"generate": "failed", "skip": "skipped", "block": "blocked"}[kind]
    assert report.reason_code == {"generate": "component_failure", "skip": "analysis_skipped", "block": "analysis_blocked"}[kind]
