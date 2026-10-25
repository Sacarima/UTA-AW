from pathlib import Path
import os

import pytest

from taskly_agent.policy import (
    PathPolicyError,
    resolve_test_path,
    validate_repository_path,
    validate_test_path,
)


@pytest.mark.parametrize("path", [
    "backend/tests/test_repository.py",
    "backend/tests/test_.py",
    "backend/tests/features/todos/test_repository.py",
    "frontend/src/App.test.tsx",
    "frontend/src/lib/http.test.ts",
])
def test_allowed_test_paths(path):
    assert validate_test_path(path) == path


@pytest.mark.parametrize("path", [
    "backend/app/main.py", "frontend/src/App.tsx",
    "backend/tests/conftest.py", "backend/tests/mocks.py",
    "backend/tests/fixtures/test_data.py", "frontend/src/__fixtures__/data.test.ts",
    "frontend/src/test/setup.ts", "frontend/vite.config.ts", "pytest.ini",
    "backend/pyproject.toml", "backend/requirements.txt", "frontend/package.json",
    "frontend/package-lock.json", ".github/workflows/ci.yml",
    "backend/tests/test_new.py.bak", "frontend/src/a.test.js",
    "frontend/src/a.test.ts/extra", "other/backend/tests/test_a.py",
    "Backend/tests/test_a.py", "backend/tests2/test_a.py",
    "backend/tests/.hidden/test_a.py",
])
def test_protected_files_and_roots_are_rejected(path):
    with pytest.raises(PathPolicyError):
        validate_test_path(path)


@pytest.mark.parametrize("path", [
    "", "/backend/tests/test_a.py", "//backend/tests/test_a.py",
    "../backend/tests/test_a.py", "backend/tests/../test_a.py",
    "backend/tests/../../app/test_a.py", "./backend/tests/test_a.py",
    "backend//tests/test_a.py", "backend/tests/./test_a.py", "backend/tests/test_a.py/",
    "backend\\tests\\test_a.py", "C:/backend/tests/test_a.py",
    "C:backend/tests/test_a.py", "\\\\server\\tests\\test_a.py",
    "backend/tests/%2e%2e/test_a.py", "backend/tests/test_a.py\x00",
    "backend/tests/test_a.py\n", " backend/tests/test_a.py",
    "backend/tests/test_a.py ", "backend/tests/dir./test_a.py",
    "backend/tests/NUL/test_a.py", "backend/tests/COM1.txt/test_a.py",
    "backend/tests/test_а.py", "~/backend/tests/test_a.py", "backend/tests/test_*.py",
])
def test_traversal_absolute_and_ambiguous_forms_are_rejected(path):
    with pytest.raises(PathPolicyError):
        validate_test_path(path)


@pytest.mark.parametrize("value", [None, 12, Path("backend/tests/test_a.py")])
def test_paths_must_be_strings(value):
    with pytest.raises(PathPolicyError):
        validate_test_path(value)


def test_analysis_paths_can_include_hidden_repository_files():
    assert validate_repository_path(".github/workflows/ci.yml") == ".github/workflows/ci.yml"


def test_new_and_existing_test_paths_do_not_write_or_modify_files(tmp_path):
    parent = tmp_path / "backend/tests"
    parent.mkdir(parents=True)
    existing = parent / "test_existing.py"
    existing.write_text("original content\n")
    assert resolve_test_path(tmp_path, "backend/tests/test_existing.py") == existing
    new = resolve_test_path(tmp_path, "frontend/src/new/deep.test.ts")
    assert new == tmp_path / "frontend/src/new/deep.test.ts"
    assert not new.parent.exists()
    assert existing.read_text() == "original content\n"


@pytest.mark.parametrize("link_kind", ["root", "parent", "file", "broken", "internal", "loop"])
def test_symlink_components_fail_closed(tmp_path, link_kind):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if link_kind == "root":
        (repo / "backend").symlink_to(outside, target_is_directory=True)
    else:
        tests = repo / "backend/tests"
        tests.mkdir(parents=True)
        if link_kind == "parent":
            tests.rmdir()
            tests.symlink_to(outside, target_is_directory=True)
        else:
            target = outside / "target.py"
            if link_kind == "internal":
                target = tests / "test_target.py"
            if link_kind != "broken":
                target.write_text("original")
            link = tests / "test_link.py"
            link.symlink_to(link if link_kind == "loop" else target)
    # Lexically valid is not proof that filesystem access is safe.
    path = "backend/tests/test_link.py"
    assert validate_test_path(path) == path
    with pytest.raises(PathPolicyError):
        resolve_test_path(repo, path)


def test_hard_links_and_non_regular_targets_are_rejected(tmp_path):
    tests = tmp_path / "backend/tests"
    tests.mkdir(parents=True)
    source = tmp_path / "production.py"
    source.write_text("original")
    os.link(source, tests / "test_link.py")
    (tests / "test_directory.py").mkdir()
    os.mkfifo(tests / "test_fifo.py")
    for name in ("test_link.py", "test_directory.py", "test_fifo.py"):
        with pytest.raises(PathPolicyError):
            resolve_test_path(tmp_path, f"backend/tests/{name}")


def test_non_directory_parent_and_missing_root_are_rejected(tmp_path):
    (tmp_path / "backend").write_text("not a directory")
    for root in (tmp_path, tmp_path / "missing", tmp_path / "backend"):
        with pytest.raises(PathPolicyError):
            resolve_test_path(root, "backend/tests/test_a.py")


def test_inspection_errors_fail_closed(tmp_path, monkeypatch):
    original = Path.lstat

    def deny_test_path(path):
        if path.name == "backend":
            raise PermissionError("inspection denied")
        return original(path)

    monkeypatch.setattr(Path, "lstat", deny_test_path)
    with pytest.raises(PathPolicyError, match="cannot safely inspect"):
        resolve_test_path(tmp_path, "backend/tests/test_a.py")


@pytest.mark.parametrize("path", [
    "frontend/src/Task list.tsx", "backend/app/任务.py", "docs/Área de trabalho.md",
    "frontend/src/cafe\u0301.ts", "frontend/src/café.ts", "docs/name:details.md",
])
def test_metadata_paths_preserve_spaces_and_unicode_without_authorizing_writes(path):
    assert validate_repository_path(path) == path
    with pytest.raises(PathPolicyError):
        validate_test_path(path)


@pytest.mark.parametrize("path", [
    "backend/tests/test_任务.py", "backend/tests/test_task list.py",
    "frontend/src/Área/a.test.tsx", "frontend/src/Task list.test.ts",
    "backend/tests/dir./test_a.py", "backend/tests/NUL/test_a.py",
])
def test_metadata_acceptance_never_relaxes_write_policy(path):
    assert validate_repository_path(path) == path
    with pytest.raises(PathPolicyError):
        validate_test_path(path)


@pytest.mark.parametrize("path", [
    "", "/absolute/file", "../escape", "a/../escape", "a/./file", "a//file", "a/",
    "C:/file", "C:file", "a\\file", "a/line\nbreak", "a/null\x00", "a/\ud800",
])
def test_metadata_still_rejects_unsafe_path_forms(path):
    with pytest.raises(PathPolicyError):
        validate_repository_path(path)
