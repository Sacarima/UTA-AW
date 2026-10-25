"""Application unit tests only: never execute the materialized/proposed code."""

import hashlib
import os
from pathlib import Path
import stat

import pytest
from pydantic import ValidationError

from taskly_agent import application
from taskly_agent.analysis import GitChangeAnalyzer, eligible_test_paths
from taskly_agent.application import ApplicationLimits, ApplicationResult, AppliedPath, ProposalApplier, WorkspaceCleanupError
from taskly_agent.contracts import ContentLimits, PRIdentity, TestFileChange as FileChange, TestProposal as Proposal
from taskly_agent.git_changes import GitAnalysisError, GitObject, GitReader
from taskly_agent.policy import PathPolicyError, validate_proposal_targets

from test_analysis import commit, git, repository, write


def sha(data):
    return hashlib.sha256(data).hexdigest()


def proposal(*paths, content="# proposed, never executed\n", operation="create"):
    return Proposal(rationale="Focused regression coverage", changes=tuple(
        FileChange(path=path, operation=operation, content=content) for path in paths
    ))


def raw(*changes):
    return {"rationale": "Untrusted proposal", "changes": tuple(changes)}


@pytest.fixture
def make_applier(repository, tmp_path_factory):
    scratch = tmp_path_factory.mktemp("isolated-application")

    def make(files=None, *, limits=None, content_limits=None):
        base = git(repository, "rev-parse", "HEAD")
        for path, text in (files if files is not None else {"backend/app/service.py": "# source\n"}).items():
            write(repository, path, text)
        head = commit(repository)
        identity = PRIdentity(repository="Sacarima/UTA-AW", number=1, base_sha=base, head_sha=head)
        analyzer = GitChangeAnalyzer(GitReader(repository), base_sha=base, head_sha=head)
        return ProposalApplier(analyzer, identity=identity, limits=limits,
            content_limits=content_limits, temporary_parent=scratch)
    return make


def assert_failed(applier, proposed, reason=None, *, identity=None):
    with applier.apply(identity or applier.identity, proposed) as result:
        assert result.state == "failed"
        if reason is not None:
            assert result.reason == reason
        assert result.workspace is None and result.changes == ()
        assert result.identity == applier.identity
        assert list(applier._parent.iterdir()) == []  # disposal precedes failure delivery
        assert ApplicationResult.model_validate_json(result.model_dump_json()) == result
    return result


@pytest.mark.parametrize("source,test", [
    ("backend/app/service.py", "backend/tests/test_service.py"),
    ("frontend/src/Widget.tsx", "frontend/src/Widget.test.tsx"),
    ("frontend/src/client.ts", "frontend/src/client.test.ts"),
])
def test_creation_from_pinned_revision_and_scoped_cleanup(make_applier, source, test):
    applier = make_applier({source: "# pinned source\n"})
    proposed = proposal(test)
    with applier.apply(applier.identity, proposed) as result:
        assert result.state == "applied" and result.reason == "applied"
        workspace = Path(result.workspace)
        assert (workspace / source).read_bytes() == b"# pinned source\n"
        assert (workspace / test).read_bytes() == proposed.changes[0].content.encode()
        assert result.identity.head_sha == applier.analyzer.head_sha
        assert result.changes[0].original_sha256 is None
        assert result.changes[0].resulting_sha256 == sha(proposed.changes[0].content.encode())
        assert result.changes[0].operation == "create"
        assert not (workspace / ".git").exists()
        assert stat.S_IMODE(workspace.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE((workspace / test).stat().st_mode) == 0o600
        assert ApplicationResult.model_validate_json(result.model_dump_json()) == result
    assert not workspace.parent.exists()


def test_modification_records_hashes_and_preserves_original_repository(make_applier):
    applier = make_applier({"backend/app/service.py": "# source\n",
        "backend/tests/test_service.py": "# original tests\n"})
    repo = applier.analyzer.reader.repository
    before = {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    with applier.apply(applier.identity, proposal("backend/tests/test_service.py", operation="modify")) as result:
        assert result.state == "applied"
        assert result.changes[0].original_sha256 == sha(b"# original tests\n")
        assert result.changes[0].resulting_sha256 == sha(b"# proposed, never executed\n")
        assert (Path(result.workspace) / "backend/tests/test_service.py").stat().st_nlink == 1
    after = {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob("*") if path.is_file()}
    assert after == before  # includes the original .git index, refs and objects


def test_mixed_proposal_is_applied_as_one_workspace(make_applier):
    applier = make_applier({"backend/app/service.py": "# source\n", "frontend/src/Widget.tsx": "// source\n",
        "frontend/src/Widget.test.tsx": "// old test\n"})
    proposed = Proposal(rationale="Mixed coverage", changes=(
        FileChange(path="backend/tests/test_service.py", operation="create", content="# new test\n"),
        FileChange(path="frontend/src/Widget.test.tsx", operation="modify", content="// new test\n"),
    ))
    with applier.apply(applier.identity, proposed) as result:
        assert result.state == "applied" and len(result.changes) == 2
        assert [entry.operation for entry in result.changes] == ["create", "modify"]
        assert result.changes[1].original_sha256 == sha(b"// old test\n")
        assert all((Path(result.workspace) / change.path).read_text() == change.content for change in proposed.changes)


def test_dirty_and_hard_linked_working_tree_does_not_supply_source(make_applier, tmp_path_factory):
    applier = make_applier()
    repo = applier.analyzer.reader.repository
    source = repo / "backend/app/service.py"
    source.unlink()
    outside = tmp_path_factory.mktemp("outside") / "sentinel"
    outside.write_text("# dirty external file\n")
    os.link(outside, source)
    write(repo, "backend/tests/test_service.py", "# untracked test\n")
    with applier.apply(applier.identity, proposal("backend/tests/test_service.py")) as result:
        assert result.state == "applied"
        workspace = Path(result.workspace)
        assert (workspace / "backend/app/service.py").read_text() == "# source\n"
        assert (workspace / "backend/app/service.py").stat().st_nlink == 1
    assert outside.read_text() == "# dirty external file\n"
    assert (repo / "backend/tests/test_service.py").read_text() == "# untracked test\n"


def test_working_tree_symlink_is_not_followed(make_applier, tmp_path_factory):
    applier = make_applier()
    repo = applier.analyzer.reader.repository
    source = repo / "backend/app/service.py"
    source.unlink()
    source.symlink_to(tmp_path_factory.mktemp("outside") / "missing-secret")
    with applier.apply(applier.identity, proposal("backend/tests/test_service.py")) as result:
        assert result.state == "applied"
        assert (Path(result.workspace) / "backend/app/service.py").read_text() == "# source\n"
    assert source.is_symlink()


@pytest.mark.parametrize("path", ["backend/app/service.py", "backend/tests/conftest.py", ".github/workflows/test.yml",
    "backend/requirements.txt", "frontend/src/test/setup.ts", "../backend/tests/test_service.py",
    "/backend/tests/test_service.py", "backend/tests/../tests/test_service.py",
    "backend/tests/test_任务 file.py"])
def test_unsupported_and_unsafe_paths_are_revalidated_before_any_write(make_applier, path):
    applier = make_applier()
    # model_construct must never bypass application boundary revalidation.
    untrusted = Proposal.model_construct(rationale="bad", changes=(
        FileChange.model_construct(path=path, operation="create", content="bad"),))
    assert_failed(applier, untrusted, "invalid_input")


def test_allowlisted_but_unrelated_test_is_not_authorized(make_applier):
    applier = make_applier()
    assert_failed(applier, proposal("backend/tests/test_unrelated.py"), "unauthorized_path")


@pytest.mark.parametrize("existing,operation", [(True, "create"), (False, "modify")])
def test_wrong_create_modify_operation_is_rejected(make_applier, existing, operation):
    files = {"backend/app/service.py": "# source\n"}
    if existing:
        files["backend/tests/test_service.py"] = "# original\n"
    applier = make_applier(files)
    assert_failed(applier, proposal("backend/tests/test_service.py", operation=operation), "target_mismatch")


def test_missing_expected_original_in_inventory_is_rejected(make_applier, monkeypatch):
    applier = make_applier({"backend/app/service.py": "# source\n", "backend/tests/test_service.py": "# old\n"})
    real = applier.analyzer.reader.objects
    monkeypatch.setattr(applier.analyzer.reader, "objects", lambda head: {
        path: entry for path, entry in real(head).items() if path != "backend/tests/test_service.py"})
    assert_failed(applier, proposal("backend/tests/test_service.py", operation="modify"), "target_mismatch")


def test_creation_cannot_replace_a_pinned_directory(make_applier):
    applier = make_applier({"backend/app/service.py": "# source\n",
        "backend/tests/test_service.py/child.txt": "# existing directory\n"})
    assert_failed(applier, proposal("backend/tests/test_service.py"), "target_mismatch")


@pytest.mark.parametrize("paths", [
    ("backend/tests/test_service.py", "backend/tests/test_service.py"),
    ("backend/tests/test_service.py", "backend/tests/test_Service.py"),
])
def test_duplicate_paths_are_rejected(make_applier, paths):
    applier = make_applier()
    proposed = raw(*({"path": path, "operation": "create", "content": "# proposed"} for path in paths))
    assert_failed(applier, proposed, "invalid_input")


def test_overlapping_paths_are_rejected_by_shared_authorization(make_applier):
    applier = make_applier()
    paths = ("backend/tests/test_service.py", "backend/tests/test_service.py/test_child.py")
    proposed = proposal(*paths)
    with pytest.raises(PathPolicyError, match="overlapping"):
        validate_proposal_targets(proposed, {path: "create" for path in paths})
    assert_failed(applier, proposed, "unauthorized_path")


@pytest.mark.parametrize("field", ["repository", "number", "base_sha", "head_sha"])
def test_identity_mismatch_is_rejected(make_applier, field):
    applier = make_applier()
    values = applier.identity.model_dump()
    values[field] = "Other/repository" if field == "repository" else 2 if field == "number" else "a" * 40
    assert_failed(applier, proposal("backend/tests/test_service.py"), "identity_mismatch", identity=PRIdentity(**values))


def test_analyzer_cannot_drift_from_pinned_identity(make_applier):
    applier = make_applier()
    applier.analyzer.head_sha = "a" * 40
    assert_failed(applier, proposal("backend/tests/test_service.py"), "identity_mismatch")


def test_unicode_spaces_and_binary_files_materialize_without_normalization(make_applier):
    applier = make_applier({"backend/app/service.py": "# source\n", "docs/café 任务.txt": "unicode\n"})
    repo = applier.analyzer.reader.repository
    (repo / "binary.dat").write_bytes(b"\0\xff\x80")
    head = commit(repo)
    identity = PRIdentity(**{**applier.identity.model_dump(), "head_sha": head})
    analyzer = GitChangeAnalyzer(GitReader(repo), base_sha=identity.base_sha, head_sha=head)
    applier = ProposalApplier(analyzer, identity=identity, temporary_parent=applier._parent)
    with applier.apply(identity, proposal("backend/tests/test_service.py", content="# café 任务\n")) as result:
        assert result.state == "applied"
        assert (Path(result.workspace) / "docs/café 任务.txt").read_bytes() == b"unicode\n"
        assert (Path(result.workspace) / "binary.dat").read_bytes() == b"\0\xff\x80"


@pytest.mark.parametrize("target", ["/outside/secret", "../app/service.py"])
def test_pinned_symlinks_anywhere_are_rejected_without_following(make_applier, target):
    applier = make_applier()
    repo = applier.analyzer.reader.repository
    (repo / "link").symlink_to(target)
    head = commit(repo)
    identity = PRIdentity(**{**applier.identity.model_dump(), "head_sha": head})
    applier = ProposalApplier(GitChangeAnalyzer(GitReader(repo), base_sha=identity.base_sha, head_sha=head),
        identity=identity, temporary_parent=applier._parent)
    assert_failed(applier, proposal("backend/tests/test_service.py"), "unsafe_tree")


def test_submodule_inventory_is_rejected(make_applier, monkeypatch):
    applier = make_applier()
    real = applier.analyzer.reader.objects
    monkeypatch.setattr(applier.analyzer.reader, "objects", lambda head: {
        **real(head), "vendor/module": GitObject("160000", applier.identity.head_sha)})
    assert_failed(applier, proposal("backend/tests/test_service.py"), "unsafe_tree")


@pytest.mark.parametrize("paths", [
    ("docs/A.txt", "docs/a.txt"), ("Docs/a.txt", "docs/b.txt"),
    ("docs/café.txt", "docs/cafe\u0301.txt"), (".GiT/config",), ("docs/file. ",),
    ("docs/" + "x" * 256,), ("docs/" + "a/" * 64 + "file.txt",),
])
def test_ambiguous_snapshot_paths_and_git_metadata_are_rejected(make_applier, monkeypatch, paths):
    applier = make_applier()
    real = applier.analyzer.reader.objects
    original = real(applier.identity.head_sha)
    entry = next(iter(original.values()))
    monkeypatch.setattr(applier.analyzer.reader, "objects", lambda head: {**original, **{path: entry for path in paths}})
    assert_failed(applier, proposal("backend/tests/test_service.py"), "unsafe_tree")


@pytest.mark.parametrize("kind", ["symlink_file", "symlink_parent", "hardlink", "missing", "unexpected", "changed"])
def test_workspace_tampering_is_rejected_and_cleaned(make_applier, monkeypatch, tmp_path_factory, kind):
    applier = make_applier({"backend/app/service.py": "# source\n", "backend/tests/test_service.py": "# original\n"})
    sentinel = tmp_path_factory.mktemp("sentinel") / "external"
    sentinel.write_bytes(b"# original\n")
    real_audit = application._audit

    def tamper(root, expected):
        if kind in {"symlink_file", "hardlink", "missing", "changed"}:
            with application._parent(root, "backend/tests/test_service.py") as (parent, name):
                os.unlink(name, dir_fd=parent)
                if kind == "symlink_file":
                    os.symlink(sentinel, name, dir_fd=parent)
                elif kind == "hardlink":
                    os.link(sentinel, name, dst_dir_fd=parent)
            if kind == "changed":
                application._write_file(root, "backend/tests/test_service.py", b"# tampered\n", 0o600)
        elif kind == "symlink_parent":
            backend = os.open("backend", application._DIRECTORY, dir_fd=root)
            try:
                os.rename("tests", "old-tests", src_dir_fd=backend, dst_dir_fd=backend)
                os.symlink(sentinel.parent, "tests", dir_fd=backend)
            finally:
                os.close(backend)
        else:
            application._write_file(root, "unexpected.txt", b"# unexpected", 0o600)
        real_audit(root, expected)

    monkeypatch.setattr(application, "_audit", tamper)
    assert_failed(applier, proposal("backend/tests/test_service.py", operation="modify"))
    assert sentinel.read_bytes() == b"# original\n" and sentinel.stat().st_nlink == 1


def test_partial_proposal_write_is_never_accepted(make_applier, monkeypatch):
    applier = make_applier({"backend/app/a.py": "# a\n", "backend/app/b.py": "# b\n"})
    proposed = proposal("backend/tests/test_a.py", "backend/tests/test_b.py")
    real = application._write_file
    applied = []

    def interrupted(root, path, data, mode):
        if path == "backend/tests/test_b.py":
            real(root, path, data[:3], mode)
            raise OSError("sensitive simulated disk failure")
        real(root, path, data, mode)
        if path == "backend/tests/test_a.py":
            applied.append(path)

    monkeypatch.setattr(application, "_write_file", interrupted)
    result = assert_failed(applier, proposed, "io_error")
    assert applied == ["backend/tests/test_a.py"]
    assert "sensitive" not in result.model_dump_json()


def test_short_writes_are_completed(make_applier, monkeypatch):
    applier = make_applier()
    real_write = os.write
    monkeypatch.setattr(application.os, "write", lambda fd, data: real_write(fd, data[:2]))
    with applier.apply(applier.identity, proposal("backend/tests/test_service.py")) as result:
        assert result.state == "applied"


@pytest.mark.parametrize("content_limits,limits,reason", [
    (ContentLimits(max_proposed_files=1), ApplicationLimits(), "invalid_input"),
    (ContentLimits(max_test_content_bytes=3), ApplicationLimits(), "invalid_input"),
    (ContentLimits(), ApplicationLimits(max_total_write_bytes=3), "write_limit"),
    (ContentLimits(), ApplicationLimits(max_workspace_files=2), "workspace_limit"),
    (ContentLimits(), ApplicationLimits(max_workspace_file_bytes=3), "workspace_limit"),
    (ContentLimits(), ApplicationLimits(max_workspace_bytes=3), "workspace_limit"),
])
def test_limits_fail_closed(make_applier, content_limits, limits, reason):
    applier = make_applier({"backend/app/a.py": "a", "backend/app/b.py": "b"}, limits=limits, content_limits=content_limits)
    assert_failed(applier, proposal("backend/tests/test_a.py", "backend/tests/test_b.py", content="éé"), reason)


def test_exact_utf8_write_budget_and_workspace_budget(make_applier):
    applier = make_applier({"backend/app/service.py": "#"}, limits=ApplicationLimits(
        max_total_write_bytes=4, max_workspace_bytes=5), content_limits=ContentLimits(max_test_content_bytes=4))
    with applier.apply(applier.identity, proposal("backend/tests/test_service.py", content="éé")) as result:
        assert result.state == "applied"
        assert result.changes[0].resulting_sha256 == sha("éé".encode())


def test_oversized_pinned_blob_blocks_materialization(make_applier):
    applier = make_applier({"backend/app/service.py": "x" * 100}, limits=ApplicationLimits(max_workspace_file_bytes=10))
    assert_failed(applier, proposal("backend/tests/test_service.py", content="#"), "workspace_limit")


def test_no_repository_code_is_executed(make_applier):
    applier = make_applier({"backend/app/service.py": "raise RuntimeError('source must not run')\n",
        "conftest.py": "raise RuntimeError('configuration must not run')\n"})
    with applier.apply(applier.identity, proposal("backend/tests/test_service.py", content="raise RuntimeError('test must not run')\n")) as result:
        assert result.state == "applied"


def test_caller_exception_still_cleans_successful_workspace(make_applier):
    applier = make_applier()
    with pytest.raises(RuntimeError, match="caller"):
        with applier.apply(applier.identity, proposal("backend/tests/test_service.py")) as result:
            path = Path(result.workspace)
            raise RuntimeError("caller")
    assert not path.parent.exists()


def test_every_attempt_gets_a_fresh_workspace(make_applier):
    applier = make_applier()
    paths = []
    for _ in range(2):
        with applier.apply(applier.identity, proposal("backend/tests/test_service.py")) as result:
            assert result.state == "applied"
            paths.append(result.workspace)
    assert paths[0] != paths[1]
    assert list(applier._parent.iterdir()) == []


def test_git_failure_returns_fixed_reason_and_cleans_staging(make_applier, monkeypatch):
    applier = make_applier()

    def failure(*args, **kwargs):
        raise GitAnalysisError("sensitive Git diagnostic")

    monkeypatch.setattr(applier.analyzer.reader, "blob", failure)
    result = assert_failed(applier, proposal("backend/tests/test_service.py"), "git_error")
    assert "sensitive" not in result.model_dump_json()


def test_unexpected_failure_also_cleans_and_returns_fixed_reason(make_applier, monkeypatch):
    applier = make_applier()

    def failure(*args, **kwargs):
        raise RuntimeError("sensitive implementation details")

    monkeypatch.setattr(applier, "_populate", failure)
    result = assert_failed(applier, proposal("backend/tests/test_service.py"), "application_error")
    assert "sensitive" not in result.model_dump_json()


def test_keyboard_interrupt_during_preparation_cleans_workspace(make_applier, monkeypatch):
    applier = make_applier()

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(applier, "_populate", interrupt)
    with pytest.raises(KeyboardInterrupt):
        with applier.apply(applier.identity, proposal("backend/tests/test_service.py")):
            pytest.fail("interrupted workspace must not be accepted")
    assert list(applier._parent.iterdir()) == []


def test_original_repository_cannot_be_the_temporary_parent(make_applier):
    applier = make_applier()
    with pytest.raises(ValueError, match="outside"):
        ProposalApplier(applier.analyzer, identity=applier.identity, temporary_parent=applier.analyzer.reader.repository)


def test_missing_original_does_not_become_creation_permission(make_applier):
    applier = make_applier({"backend/app/service.py": "# source\n", "backend/tests/test_service.py": "# original\n"})
    assert eligible_test_paths(applier.analyzer.inspect().impacts, ()) == {}


def test_result_cannot_represent_partial_acceptance_or_verification(make_applier):
    applier = make_applier()
    change = AppliedPath(path="backend/tests/test_service.py", operation="create",
        original_sha256=None, resulting_sha256="a" * 64)
    with pytest.raises(ValidationError):
        ApplicationResult(state="failed", reason="io_error", identity=applier.identity, changes=(change,))
    with pytest.raises(ValidationError):
        ApplicationResult(state="applied", reason="applied", identity=applier.identity)
    with pytest.raises(ValidationError):
        AppliedPath(path=change.path, operation="modify", original_sha256=None, resulting_sha256="a" * 64)
    with pytest.raises(ValidationError):
        ApplicationResult.model_validate({"state": "failed", "reason": "io_error", "identity": applier.identity,
            "verification": {"outcome": "passed"}})


def test_workspace_is_published_only_after_both_complete_audits(make_applier, monkeypatch):
    applier = make_applier()
    real_audit = application._audit
    audits = []

    def audit(root, expected):
        temporary = next(applier._parent.iterdir())
        assert {path.name for path in temporary.iterdir()} == {"staging"}
        real_audit(root, expected)
        audits.append(tuple(expected))

    monkeypatch.setattr(application, "_audit", audit)
    with applier.apply(applier.identity, proposal("backend/tests/test_service.py")) as result:
        assert result.state == "applied" and len(audits) == 2
        assert {path.name for path in Path(result.workspace).parent.iterdir()} == {"workspace"}


@pytest.mark.parametrize("kind", ["create_collision", "symlink_parent", "hardlinked_original"])
def test_write_time_checks_reject_changes_after_initial_audit(make_applier, monkeypatch, tmp_path_factory, kind):
    files = {"backend/app/service.py": "# source\n"}
    operation = "modify" if kind == "hardlinked_original" else "create"
    if operation == "modify":
        files["backend/tests/test_service.py"] = "# original\n"
    applier = make_applier(files)
    sentinel = tmp_path_factory.mktemp("write-time") / "sentinel"
    sentinel.write_bytes(b"# original\n")
    real_audit = application._audit
    count = 0

    def audit(root, expected):
        nonlocal count
        real_audit(root, expected)
        count += 1
        if count != 1:
            return
        if kind == "create_collision":
            application._write_file(root, "backend/tests/test_service.py", b"# unexpected", 0o600)
        elif kind == "symlink_parent":
            with application._parent(root, "backend/tests") as (parent, name):
                os.symlink(sentinel.parent, name, dir_fd=parent)
        else:
            with application._parent(root, "backend/tests/test_service.py") as (parent, name):
                os.unlink(name, dir_fd=parent)
                os.link(sentinel, name, dst_dir_fd=parent)

    monkeypatch.setattr(application, "_audit", audit)
    assert_failed(applier, proposal("backend/tests/test_service.py", operation=operation))
    assert sentinel.read_bytes() == b"# original\n" and sentinel.stat().st_nlink == 1


def test_final_hash_audit_rejects_corrupted_proposal_write(make_applier, monkeypatch):
    applier = make_applier()
    real_write = application._write_file

    def corrupt(root, path, data, mode):
        if path == "backend/tests/test_service.py":
            data = b"# corrupted after preflight\n"
        real_write(root, path, data, mode)

    monkeypatch.setattr(application, "_write_file", corrupt)
    assert_failed(applier, proposal("backend/tests/test_service.py"), "target_mismatch")


def test_publication_failure_disposes_all_staged_changes(make_applier, monkeypatch):
    applier = make_applier()

    def failure(*args, **kwargs):
        raise OSError("sensitive publication details")

    monkeypatch.setattr(application.os, "rename", failure)
    result = assert_failed(applier, proposal("backend/tests/test_service.py"), "io_error")
    assert "sensitive" not in result.model_dump_json()


@pytest.mark.parametrize("successful_application", [True, False])
def test_cleanup_errors_are_explicit_and_sanitized(make_applier, monkeypatch, successful_application):
    applier = make_applier()
    real_cleanup = application.tempfile.TemporaryDirectory.cleanup

    def failed_cleanup(owner):
        real_cleanup(owner)
        raise OSError("sensitive cleanup details")

    monkeypatch.setattr(application.tempfile.TemporaryDirectory, "cleanup", failed_cleanup)
    if successful_application:
        with pytest.raises(WorkspaceCleanupError) as error:
            with applier.apply(applier.identity, proposal("backend/tests/test_service.py")) as result:
                assert result.state == "applied"
        assert str(error.value) == "workspace cleanup failed" and error.value.__context__ is None
    else:
        def failure(*args):
            raise OSError("sensitive write details")
        monkeypatch.setattr(applier, "_populate", failure)
        result = assert_failed(applier, proposal("backend/tests/test_service.py"), "cleanup_failed")
        assert "sensitive" not in result.model_dump_json()


def test_skip_analysis_does_not_authorize_application(make_applier):
    applier = make_applier({"README.md": "# docs\n"})
    assert_failed(applier, proposal("backend/tests/test_service.py"), "analysis_blocked")


def test_executable_mode_is_preserved_without_executing(make_applier):
    applier = make_applier({"backend/app/service.py": "# source\n", "backend/tests/test_service.py": "# original\n"})
    repo = applier.analyzer.reader.repository
    (repo / "backend/tests/test_service.py").chmod(0o755)
    head = commit(repo)
    identity = PRIdentity(**{**applier.identity.model_dump(), "head_sha": head})
    applier = ProposalApplier(GitChangeAnalyzer(GitReader(repo), base_sha=identity.base_sha, head_sha=head),
        identity=identity, temporary_parent=applier._parent)
    with applier.apply(identity, proposal("backend/tests/test_service.py", operation="modify")) as result:
        assert result.state == "applied"
        assert stat.S_IMODE((Path(result.workspace) / "backend/tests/test_service.py").stat().st_mode) == 0o700
