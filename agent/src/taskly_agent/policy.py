"""Read-only validation of untrusted repository paths.

Analysis paths preserve spaces and Unicode as opaque relative Git metadata.
They never authorize filesystem access. Writable paths additionally require
portable ASCII segments and the narrow test allowlist. Neither validator
decodes, normalizes, expands, or consults the filesystem.

Filesystem checks additionally reject every symlink component (even links
within the repository), special files, hard-linked files, and non-directory
parents. The repository root must come from the trusted caller.

These checks are snapshots, not a file-writing sandbox: a concurrent rename
can invalidate them. A future writer must enforce containment at write time
with safe directory handles/no-follow operations and isolated execution.
No function here writes files or authorizes execution of their contents.
"""

from pathlib import Path
from collections.abc import Mapping
import re
import stat
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import TestProposal


class PathPolicyError(ValueError):
    """An untrusted path violates the repository/test-file policy."""


_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+", re.ASCII)
_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


def validate_repository_path(path: str) -> str:
    """Validate read-only metadata, not permission to read or write a file.

    Preserve spelling (including Unicode normalization and spaces). For safe
    handling across platforms, reject backslashes, drive prefixes, control
    characters and surrogate code points even though Git can store some of them.
    """
    if not isinstance(path, str) or not path:
        raise PathPolicyError("path must be a non-empty string")
    if (
        "\\" in path
        or re.match(r"^[A-Za-z]:", path)
        or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise PathPolicyError("metadata path must be an unambiguous relative Git path")
    return path


def validate_test_path(path: str) -> str:
    """Allow writable test filenames, without accessing the filesystem.

    Metadata acceptance does not grant write permission. Fixtures/configuration
    are protected through a narrow allowlist; contents are not inspected here.
    """
    validate_repository_path(path)
    for part in path.split("/"):
        if (
            _SEGMENT.fullmatch(part) is None
            or part in {".", ".."}
            or part.endswith(".")
            or part.split(".", 1)[0].lower() in _RESERVED
        ):
            raise PathPolicyError("path must be a canonical relative POSIX path")
    parts = path.split("/")
    name = parts[-1]
    if any(
        part.startswith(".")
        or part.casefold() in {"fixtures", "__fixtures__", "mocks", "__mocks__", "__snapshots__"}
        for part in parts[:-1]
    ):
        raise PathPolicyError("fixture, mock, and hidden directories are protected")
    backend = (
        len(parts) >= 3
        and parts[:2] == ["backend", "tests"]
        and re.fullmatch(r"test_[A-Za-z0-9_.-]*\.py", name) is not None
    )
    frontend = (
        len(parts) >= 3
        and parts[:2] == ["frontend", "src"]
        and re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.test\.tsx?", name)
        is not None
    )
    if not (backend or frontend):
        raise PathPolicyError("only backend and frontend test-file paths are allowed")
    return path


def validate_proposal_targets(proposal: "TestProposal", eligible: Mapping[str, str]) -> None:
    """Authorize exact analysis-selected paths/operations, never broad roots alone."""
    paths = []
    for change in proposal.changes:
        validate_test_path(change.path)
        path = change.path.casefold()
        if any(path == other or path.startswith(other + "/") or other.startswith(path + "/") for other in paths):
            raise PathPolicyError("duplicate or overlapping proposal paths")
        paths.append(path)
    if any(eligible.get(change.path) != change.operation for change in proposal.changes):
        raise PathPolicyError("proposal is outside deterministic test scope")


def resolve_test_path(repository_root: Path, path: str) -> Path:
    """Check existing components and return a contained new or existing path.

    Missing descendants are allowed so new tests/directories can be proposed.
    Permission and other inspection errors fail closed as PathPolicyError.
    """
    validate_test_path(path)
    try:
        root = repository_root.resolve(strict=True)
        if not root.is_dir():
            raise PathPolicyError("repository root must be an existing directory")
        parts = path.split("/")
        candidate = root
        for index, part in enumerate(parts):
            candidate = candidate / part
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise PathPolicyError("symlink components are forbidden")
            if index < len(parts) - 1:
                if not stat.S_ISDIR(info.st_mode):
                    raise PathPolicyError("parent components must be directories")
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PathPolicyError("existing targets must be regular files with one hard link")
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            raise PathPolicyError("test path escapes the repository root")
        return resolved
    except (OSError, RuntimeError) as exc:
        raise PathPolicyError("cannot safely inspect the proposed test path") from exc
