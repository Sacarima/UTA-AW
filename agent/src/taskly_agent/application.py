"""Transactional proposal application to disposable, private POSIX workspaces.

Usage: ``with applier.apply(identity, proposal) as result: ...``. An applied
workspace exists only for that scope. No repository code is executed here.
Application is not verification. No checkout, Git index, worktree registration,
filters, archive extraction or links are used. Git blobs supply exact bytes;
only the executable bit is preserved, with owner-only filesystem permissions.

The whole tree is staging: no path is returned until application and a complete
manifest/hash audit succeed. Failure disposes the entire tree before yielding a
failed result. Publication is one directory rename, not a Git commit. This is
logical atomicity, not crash durability. A process crash may leave a private temp
directory; an untrusted process with the same OS identity is outside this boundary.
Future test execution requires a separate sandbox. Never execute from this scope.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Annotated, Literal
import unicodedata

from pydantic import AfterValidator, Field, ValidationError, model_validator

from .analysis import GitChangeAnalyzer, eligible_test_paths
from .contracts import ContentLimits, Contract, PRIdentity, SHA256Digest, TestProposal
from .git_changes import GitAnalysisError, GitObject
from .lifecycle import proposal_digest
from .policy import PathPolicyError, validate_proposal_targets, validate_repository_path, validate_test_path


class ApplicationLimits(Contract):
    max_total_write_bytes: Annotated[int, Field(gt=0)] = 262_144
    max_workspace_files: Annotated[int, Field(gt=0)] = 2000
    max_workspace_file_bytes: Annotated[int, Field(gt=0)] = 2_000_000
    max_workspace_bytes: Annotated[int, Field(gt=0)] = 32_000_000


class AppliedPath(Contract):
    path: Annotated[str, AfterValidator(validate_test_path)]
    operation: Literal["create", "modify"]
    original_sha256: SHA256Digest | None
    resulting_sha256: SHA256Digest

    @model_validator(mode="after")
    def original_matches_operation(self):
        if (self.operation == "modify") != (self.original_sha256 is not None):
            raise ValueError("only modifications require an original hash")
        return self


ApplicationReason = Literal[
    "applied", "invalid_input", "identity_mismatch", "analysis_blocked",
    "unauthorized_path", "target_mismatch", "unsafe_tree", "write_limit",
    "workspace_limit", "git_error", "io_error", "application_error", "cleanup_failed",
]


class ApplicationResult(Contract):
    state: Literal["applied", "failed"]
    reason: ApplicationReason
    identity: PRIdentity
    proposal_digest: SHA256Digest | None = None
    workspace: str | None = None
    changes: tuple[AppliedPath, ...] = ()

    @model_validator(mode="after")
    def complete_or_absent(self):
        if self.state == "applied":
            if self.reason != "applied" or self.workspace is None or not self.changes or self.proposal_digest is None:
                raise ValueError("application requires a complete workspace receipt")
        elif self.reason == "applied" or self.workspace is not None or self.changes:
            raise ValueError("failed application cannot expose accepted partial changes")
        return self


class WorkspaceCleanupError(RuntimeError):
    """Fixed diagnostic for cleanup failure when leaving a successful scope."""


class _Rejected(Exception):
    def __init__(self, reason: ApplicationReason):
        self.reason = reason


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _layout(paths) -> set[str]:
    """Reject file/parent conflicts, metadata names and filesystem aliases."""
    seen, directories = {}, set()
    for path in paths:
        validate_repository_path(path)
        parts = path.split("/")
        # Bound representability and traversal/cleanup depth before mkdir/open.
        if len(path.encode("utf-8")) > 4096 or len(parts) > 64 or any(
            len(unicodedata.normalize("NFD", part).encode("utf-8")) > 255 for part in parts
        ):
            raise _Rejected("unsafe_tree")
        for index, part in enumerate(parts):
            if part.rstrip(" .").casefold() == ".git" or part.endswith((".", " ")):
                raise _Rejected("unsafe_tree")
            prefix = "/".join(parts[:index + 1])
            key = unicodedata.normalize("NFD", prefix).casefold()
            entry = (prefix, index == len(parts) - 1)
            if key in seen and seen[key] != entry:
                raise _Rejected("unsafe_tree")
            seen[key] = entry
            if not entry[1]:
                directories.add(prefix)
    return directories


_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def _parent(root: int, path: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
    """Walk only via directory handles; never resolve a repository path."""
    current = os.dup(root)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(part, _DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
        yield current, parts[-1]
    finally:
        os.close(current)


def _write_file(root: int, path: str, data: bytes, mode: int) -> None:
    with _parent(root, path, create=True) as (parent, name):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _Rejected("unsafe_tree")
            remaining = memoryview(data)
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short write")
                remaining = remaining[written:]
            os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)


def _read_file(root: int, path: str, size: int) -> bytes:
    with _parent(root, path) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _Rejected("unsafe_tree")
            if info.st_size != size:
                raise _Rejected("target_mismatch")
            data = bytearray()
            while len(data) <= size:
                chunk = os.read(fd, min(65_536, size + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) != size:
                raise _Rejected("target_mismatch")
            return bytes(data)
        finally:
            os.close(fd)


def _audit(root: int, expected: dict[str, tuple[str, int]]) -> None:
    """Check all names, hashes and link types, including untouched files."""
    found, directories = set(), set()

    def walk(directory: int, prefix: str):
        for name in os.listdir(directory):
            path = prefix + name
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                directories.add(path)
                child = os.open(name, _DIRECTORY, dir_fd=directory)
                try:
                    walk(child, path + "/")
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if path not in expected or _hash(_read_file(root, path, expected[path][1])) != expected[path][0]:
                    raise _Rejected("target_mismatch")
                found.add(path)
            else:
                raise _Rejected("unsafe_tree")

    walk(root, "")
    if found != set(expected) or directories != _layout(expected):
        raise _Rejected("target_mismatch")


@dataclass(frozen=True)
class _Plan:
    proposal: TestProposal
    inventory: dict[str, GitObject]
    originals: dict[str, bytes]
    changes: tuple[AppliedPath, ...]


class ProposalApplier:
    def __init__(self, analyzer: GitChangeAnalyzer, *, identity: PRIdentity,
                 content_limits: ContentLimits | None = None, limits: ApplicationLimits | None = None,
                 temporary_parent: Path | None = None):
        self.analyzer = analyzer
        self.identity = PRIdentity.model_validate(identity)
        if (self.identity.base_sha, self.identity.head_sha) != (analyzer.base_sha, analyzer.head_sha):
            raise ValueError("trusted identity must match the analyzer commit range")
        self.content_limits = ContentLimits.model_validate(ContentLimits() if content_limits is None else content_limits)
        self.limits = ApplicationLimits.model_validate(ApplicationLimits() if limits is None else limits)
        self._parent = (Path(tempfile.gettempdir()) if temporary_parent is None else temporary_parent).resolve(strict=True)
        if not self._parent.is_dir() or self._parent.is_relative_to(analyzer.reader.repository):
            raise ValueError("temporary parent must be outside the original repository")
        if os.name != "posix" or not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("safe application requires POSIX directory handles and hardened cleanup")

    def _validate(self, identity: PRIdentity, proposal: TestProposal) -> _Plan:
        identity = PRIdentity.model_validate(identity)
        if identity != self.identity or (self.analyzer.base_sha, self.analyzer.head_sha) != (
            self.identity.base_sha, self.identity.head_sha,
        ):
            raise _Rejected("identity_mismatch")
        proposal = TestProposal.model_validate(proposal, context={"content_limits": self.content_limits})
        if sum(len(change.content.encode("utf-8")) for change in proposal.changes) > self.limits.max_total_write_bytes:
            raise _Rejected("write_limit")
        if any(len(change.content.encode("utf-8")) > self.limits.max_workspace_file_bytes for change in proposal.changes):
            raise _Rejected("workspace_limit")
        analysis = self.analyzer.inspect()
        if analysis.decision.action != "generate":
            raise _Rejected("analysis_blocked")
        inventory = self.analyzer.reader.objects(self.identity.head_sha)
        if len(inventory) > self.limits.max_workspace_files:
            raise _Rejected("workspace_limit")
        if any(entry.mode not in {"100644", "100755"} for entry in inventory.values()):
            raise _Rejected("unsafe_tree")
        directories = _layout(inventory)
        for impact in analysis.impacts:
            if impact.recommendation != "skip_removed" and impact.change.path not in inventory:
                raise _Rejected("target_mismatch")
        for change in proposal.changes:
            exists = change.path in inventory
            if exists != (change.operation == "modify") or change.path in directories:
                raise _Rejected("target_mismatch")
        validate_proposal_targets(proposal, eligible_test_paths(analysis.impacts, inventory.keys()))
        final_paths = set(inventory) | {change.path for change in proposal.changes}
        _layout(final_paths)
        if len(final_paths) > self.limits.max_workspace_files:
            raise _Rejected("workspace_limit")
        originals, changes = {}, []
        for change in proposal.changes:
            original = None
            if change.operation == "modify":
                original = self.analyzer.reader.blob(inventory[change.path].object_id,
                    max_bytes=self.limits.max_workspace_file_bytes)
                originals[change.path] = original
            changes.append(AppliedPath(path=change.path, operation=change.operation,
                original_sha256=_hash(original) if original is not None else None,
                resulting_sha256=_hash(change.content.encode("utf-8"))))
        return _Plan(proposal, inventory, originals, tuple(changes))

    def _populate(self, root: int, plan: _Plan) -> None:
        os.mkdir("staging", 0o700, dir_fd=root)
        tree = os.open("staging", _DIRECTORY, dir_fd=root)
        try:
            expected, total = {}, 0
            for path, entry in plan.inventory.items():
                data = plan.originals.get(path)
                if data is None:
                    data = self.analyzer.reader.blob(entry.object_id, max_bytes=self.limits.max_workspace_file_bytes)
                total += len(data)
                if total > self.limits.max_workspace_bytes:
                    raise _Rejected("workspace_limit")
                _write_file(tree, path, data, 0o700 if entry.mode == "100755" else 0o600)
                expected[path] = (_hash(data), len(data))
            _audit(tree, expected)
            resulting_total = total + sum(len(change.content.encode("utf-8")) - len(plan.originals.get(change.path, b""))
                                         for change in plan.proposal.changes)
            if resulting_total > self.limits.max_workspace_bytes:
                raise _Rejected("workspace_limit")
            for change in plan.proposal.changes:
                data = change.content.encode("utf-8")
                if change.operation == "modify":
                    original = plan.originals[change.path]
                    if _read_file(tree, change.path, len(original)) != original:
                        raise _Rejected("target_mismatch")
                    with _parent(tree, change.path) as (parent, name):
                        os.unlink(name, dir_fd=parent)
                mode = 0o700 if plan.inventory.get(change.path, GitObject("100644", "")).mode == "100755" else 0o600
                _write_file(tree, change.path, data, mode)
                expected[change.path] = (_hash(data), len(data))
            _audit(tree, expected)
            if set(os.listdir(root)) != {"staging"}:
                raise _Rejected("target_mismatch")
            os.rename("staging", "workspace", src_dir_fd=root, dst_dir_fd=root)
        finally:
            os.close(tree)

    @contextmanager
    def apply(self, identity: PRIdentity, proposal: TestProposal) -> Iterator[ApplicationResult]:
        """Yield a receipt and temporary workspace; cleanup also follows caller errors.

        identity is checked against the constructor's trusted repository/PR/range,
        never against an untrusted origin URL. No existing workspace is accepted.
        Failure receipts have no paths/hashes for partially written proposals.
        """
        owner, digest = None, None
        reason = None
        try:
            plan = self._validate(identity, proposal)
            digest = proposal_digest(plan.proposal)
            owner = tempfile.TemporaryDirectory(prefix="taskly-application-", dir=self._parent)
            root = os.open(owner.name, _DIRECTORY)
            try:
                self._populate(root, plan)
            finally:
                os.close(root)
            result = ApplicationResult(state="applied", reason="applied", identity=self.identity,
                proposal_digest=digest, workspace=str(Path(owner.name) / "workspace"), changes=plan.changes)
        except _Rejected as error:
            reason = error.reason
        except (ValidationError, UnicodeError):
            reason = "invalid_input"
        except PathPolicyError:
            reason = "unauthorized_path"
        except GitAnalysisError as error:
            reason = "workspace_limit" if str(error) == "blob_limit" else "git_error"
        except OSError:
            reason = "io_error"
        except Exception:
            reason = "application_error"
        except BaseException:
            if owner is not None:
                owner.cleanup()
            raise
        if reason is not None:
            if owner is not None:
                try:
                    owner.cleanup()
                except OSError:
                    reason = "cleanup_failed"
                owner = None
            result = ApplicationResult(state="failed", reason=reason, identity=self.identity, proposal_digest=digest)
        try:
            yield result
        finally:
            if owner is not None:
                failed = False
                try:
                    owner.cleanup()
                except OSError:
                    failed = True
                if failed:
                    raise WorkspaceCleanupError("workspace cleanup failed")
